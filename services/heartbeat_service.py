from datetime import datetime, timezone, timedelta
import logging

import asyncpg

from services import alert_service, push_service
from services.alert_service import get_guardian_settings, should_send, use_sound

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


async def process_heartbeat(db: asyncpg.Connection, user_id: int, payload: dict) -> dict:
    device_id = payload["device_id"]

    # 기기 정보 조회
    device = await db.fetchrow(
        "SELECT id, suspicious_count, heartbeat_hour, heartbeat_minute, last_seen FROM devices WHERE user_id = $1 AND device_id = $2",
        user_id, device_id,
    )

    if device is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기기를 찾을 수 없습니다")

    now_dt = datetime.now(timezone.utc)
    suspicious    = payload["suspicious"]
    battery_level = payload.get("battery_level")
    manual        = payload.get("manual", False)

    # 오늘(KST) 이미 heartbeat를 보낸 경우 suspicious 판정 무시
    # → 하루 첫 번째 heartbeat에서만 센서값 비교
    if suspicious and not manual:
        last_seen = device["last_seen"]
        if last_seen is not None:
            last_seen_date = last_seen.astimezone(KST).date()
            today_kst = datetime.now(KST).date()
            if last_seen_date == today_kst:
                suspicious = False

    # devices 테이블 갱신
    new_suspicious_count = device["suspicious_count"] + 1 if suspicious else 0
    await db.execute(
        """UPDATE devices SET
            last_seen = $1,
            accel_x = $2, accel_y = $3, accel_z = $4,
            gyro_x = $5, gyro_y = $6, gyro_z = $7,
            battery_level = $8,
            suspicious_count = $9,
            updated_at = $10
           WHERE user_id = $11 AND device_id = $12""",
        now_dt,
        payload.get("accel_x"), payload.get("accel_y"), payload.get("accel_z"),
        payload.get("gyro_x"), payload.get("gyro_y"), payload.get("gyro_z"),
        battery_level,
        new_suspicious_count,
        now_dt,
        user_id, device_id,
    )

    # heartbeat_logs 기록
    await db.execute(
        """INSERT INTO heartbeat_logs
           (device_id, accel_x, accel_y, accel_z,
            gyro_x, gyro_y, gyro_z, suspicious, battery_level,
            client_ts, server_ts)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
        device_id,
        payload.get("accel_x"), payload.get("accel_y"), payload.get("accel_z"),
        payload.get("gyro_x"), payload.get("gyro_y"), payload.get("gyro_z"),
        int(suspicious),
        battery_level,
        payload["timestamp"],
        now_dt,
    )

    # 활성 경고 해소 — suspicious=false일 때만 "정상 복귀" 알림 발송
    # suspicious=true면 폰 신호만 온 것이므로 warning/urgent → caution 하향만 처리
    if not suspicious:
        resolved = await alert_service.resolve_active_alerts(db, user_id)
        if not resolved:
            # 경고 해소 없음 → 수동/자동 안부 알림 발송
            if manual:
                await _send_manual_report_to_guardians(db, user_id)
            else:
                await _send_auto_report_to_guardians(db, user_id)
    else:
        await alert_service.downgrade_alerts_on_suspicious(db, user_id)

    # suspicious 판정 처리
    await _handle_suspicious(db, user_id, device_id, suspicious, new_suspicious_count, now_dt)

    # 배터리 ≤ 10% → 보호자 정보 알림 (1회만 발송, DND 적용)
    if battery_level is not None and battery_level <= 10:
        if not await alert_service.has_active_alert(db, user_id, "info"):
            await alert_service.create_alert(db, user_id, "info", now_dt)
            await _send_battery_low_to_guardians(db, user_id)

    heartbeat_hour = device["heartbeat_hour"]
    heartbeat_minute = device["heartbeat_minute"]
    now_kst = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    return {
        "status": "ok",
        "server_time": now_kst,
        "heartbeat_hour": heartbeat_hour,
        "heartbeat_minute": heartbeat_minute,
    }


async def _handle_suspicious(
    db: asyncpg.Connection,
    user_id: int,
    device_id: str,
    suspicious: bool,
    suspicious_count: int,
    now_dt: datetime,
) -> None:
    if not suspicious:
        return

    # suspicious=true: 대상자에게 wellbeing_check만 발송
    # 보호자 경고는 job_heartbeat_check(미수신 판정)에서만 처리
    await _send_wellbeing_check_if_enabled(db, user_id, device_id)


async def _send_wellbeing_check_if_enabled(
    db: asyncpg.Connection, user_id: int, device_id: str
) -> None:
    """보호자가 안부 확인 알림 ON인 경우 대상자에게 Push 발송
    (현재 구현: 항상 발송. 향후 보호자 설정 테이블 추가 시 조건 추가 가능)"""
    row = await db.fetchrow(
        "SELECT fcm_token FROM devices WHERE user_id = $1 AND device_id = $2",
        user_id, device_id,
    )
    if row and row["fcm_token"]:
        await push_service.push_wellbeing_check(row["fcm_token"])


async def _send_battery_low_to_guardians(db: asyncpg.Connection, user_id: int) -> None:
    """배터리 부족 알림 — 구독 활성 보호자에게 발송 (DND 적용)"""
    guardians = await db.fetch(
        """SELECT g.guardian_user_id, d.fcm_token
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1
             AND s.plan != 'expired'
             AND s.expires_at > NOW()
             AND d.fcm_token IS NOT NULL""",
        user_id,
    )

    # 대상자 invite_code 조회
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    invite_code = invite_row["invite_code"] if invite_row else None

    for guardian in guardians:
        settings = await get_guardian_settings(db, guardian["guardian_user_id"])
        if not should_send(settings, "info"):
            continue
        sound = "default" if use_sound(settings, "info") else None
        await push_service.push_battery_low(guardian["fcm_token"], user_id, sound=sound, invite_code=invite_code)


async def _send_auto_report_to_guardians(db: asyncpg.Connection, user_id: int) -> None:
    """정상 상태 자동 안부 확인 — 구독 활성 보호자에게 알림 발송 (DND 적용)"""
    guardians = await db.fetch(
        """SELECT g.guardian_user_id, d.fcm_token
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1
             AND s.plan != 'expired'
             AND s.expires_at > NOW()
             AND d.fcm_token IS NOT NULL""",
        user_id,
    )

    # 대상자 invite_code 조회
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    invite_code = invite_row["invite_code"] if invite_row else None

    for guardian in guardians:
        settings = await get_guardian_settings(db, guardian["guardian_user_id"])
        if not should_send(settings, "info"):
            continue
        sound = "default" if use_sound(settings, "info") else None
        await push_service.push_auto_report(guardian["fcm_token"], user_id, sound=sound, invite_code=invite_code)


async def _send_manual_report_to_guardians(db: asyncpg.Connection, user_id: int) -> None:
    """평상시 수동 안부 보고 — 구독 활성 보호자에게 알림 발송 (DND 적용)"""
    guardians = await db.fetch(
        """SELECT g.guardian_user_id, d.fcm_token
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1
             AND s.plan != 'expired'
             AND s.expires_at > NOW()
             AND d.fcm_token IS NOT NULL""",
        user_id,
    )

    # 대상자 invite_code 조회
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    invite_code = invite_row["invite_code"] if invite_row else None

    for guardian in guardians:
        settings = await get_guardian_settings(db, guardian["guardian_user_id"])
        if not should_send(settings, "info"):
            continue
        sound = "default" if use_sound(settings, "info") else None
        await push_service.push_manual_report(guardian["fcm_token"], user_id, sound=sound, invite_code=invite_code)
