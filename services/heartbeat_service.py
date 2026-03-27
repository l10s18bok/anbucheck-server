from datetime import datetime, timezone, timedelta
import logging

import aiosqlite

from services import alert_service, push_service
from services.alert_service import get_guardian_settings, should_send, use_sound

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


async def process_heartbeat(db: aiosqlite.Connection, user_id: int, payload: dict) -> dict:
    device_id = payload["device_id"]

    # 기기 정보 조회
    async with db.execute(
        "SELECT id, suspicious_count, heartbeat_hour, heartbeat_minute FROM devices WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    ) as cur:
        device = await cur.fetchone()

    if device is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기기를 찾을 수 없습니다")

    now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    suspicious    = payload["suspicious"]
    battery_level = payload.get("battery_level")
    manual        = payload.get("manual", False)

    # devices 테이블 갱신
    new_suspicious_count = device["suspicious_count"] + 1 if suspicious else 0
    await db.execute(
        """UPDATE devices SET
            last_seen = ?,
            accel_x = ?, accel_y = ?, accel_z = ?,
            gyro_x = ?, gyro_y = ?, gyro_z = ?,
            battery_level = ?,
            suspicious_count = ?,
            updated_at = ?
           WHERE user_id = ? AND device_id = ?""",
        (
            now_str,
            payload.get("accel_x"), payload.get("accel_y"), payload.get("accel_z"),
            payload.get("gyro_x"), payload.get("gyro_y"), payload.get("gyro_z"),
            battery_level,
            new_suspicious_count,
            now_str,
            user_id, device_id,
        ),
    )

    # heartbeat_logs 기록
    await db.execute(
        """INSERT INTO heartbeat_logs
           (device_id, accel_x, accel_y, accel_z,
            gyro_x, gyro_y, gyro_z, suspicious, battery_level,
            client_ts, server_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            device_id,
            payload.get("accel_x"), payload.get("accel_y"), payload.get("accel_z"),
            payload.get("gyro_x"), payload.get("gyro_y"), payload.get("gyro_z"),
            int(suspicious),
            battery_level,
            payload["timestamp"],
            now_str,
        ),
    )
    await db.commit()

    # 활성 경고 해소 — suspicious=false일 때만 "정상 복귀" 알림 발송
    # suspicious=true면 폰 신호만 온 것이므로 warning/urgent → caution 하향만 처리
    if not suspicious:
        resolved = await alert_service.resolve_active_alerts(db, user_id)
        # 수동 보고이고 해소할 경고가 없는 평상시 → 보호자에게 수동 안부 확인 알림 발송
        if manual and not resolved:
            await _send_manual_report_to_guardians(db, user_id)
    else:
        await alert_service.downgrade_alerts_on_suspicious(db, user_id)

    # suspicious 판정 처리
    await _handle_suspicious(db, user_id, device_id, suspicious, new_suspicious_count)

    # 배터리 ≤ 10% → 보호자 정보 알림 (1회만 발송)
    if battery_level is not None and battery_level <= 10:
        if not await alert_service.has_active_alert(db, user_id, "info"):
            await alert_service.create_alert(db, user_id, "info", now_str)
            await alert_service.send_alert_to_guardians(db, user_id, "info_battery_low")

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
    db: aiosqlite.Connection,
    user_id: int,
    device_id: str,
    suspicious: bool,
    suspicious_count: int,
) -> None:
    if not suspicious:
        return

    last_seen_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

    if suspicious_count == 1:
        # 주의 등급 발생 (중복 생성 방지)
        if not await alert_service.has_active_alert(db, user_id, "caution"):
            await alert_service.create_alert(db, user_id, "caution", last_seen_str)
            await alert_service.send_alert_to_guardians(db, user_id, "caution")
        return

    if suspicious_count >= 2:
        # 경고 등급 발생 (warning/urgent 중복 생성 방지)
        if not await alert_service.has_active_alert(db, user_id, "warning") and \
           not await alert_service.has_active_alert(db, user_id, "urgent"):
            await alert_service.create_alert(db, user_id, "warning", last_seen_str)
            await alert_service.send_alert_to_guardians(db, user_id, "warning")

        # 보호자 "안부 확인 알림" 설정 확인 후 대상자에게 Push
        await _send_wellbeing_check_if_enabled(db, user_id, device_id)


async def _send_wellbeing_check_if_enabled(
    db: aiosqlite.Connection, user_id: int, device_id: str
) -> None:
    """보호자가 안부 확인 알림 ON인 경우 대상자에게 Push 발송
    (현재 구현: 항상 발송. 향후 보호자 설정 테이블 추가 시 조건 추가 가능)"""
    async with db.execute(
        "SELECT fcm_token FROM devices WHERE user_id = ? AND device_id = ?",
        (user_id, device_id),
    ) as cur:
        row = await cur.fetchone()
    if row and row["fcm_token"]:
        await push_service.push_wellbeing_check(row["fcm_token"])


async def _send_manual_report_to_guardians(db: aiosqlite.Connection, user_id: int) -> None:
    """평상시 수동 안부 보고 — 구독 활성 보호자에게 알림 발송 (DND 적용)"""
    async with db.execute(
        """SELECT g.guardian_user_id, d.fcm_token
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = ?
             AND s.plan != 'expired'
             AND s.expires_at > datetime('now')
             AND d.fcm_token IS NOT NULL""",
        (user_id,),
    ) as cur:
        guardians = await cur.fetchall()

    for guardian in guardians:
        settings = await get_guardian_settings(db, guardian["guardian_user_id"])
        if not should_send(settings, "info"):
            continue
        sound = "default" if use_sound(settings, "info") else None
        await push_service.push_manual_report(guardian["fcm_token"], user_id, sound=sound)
