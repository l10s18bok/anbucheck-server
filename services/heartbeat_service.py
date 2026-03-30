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
        "SELECT id, suspicious_count, heartbeat_hour, heartbeat_minute, last_seen, last_steps FROM devices WHERE user_id = $1 AND device_id = $2",
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
    steps_delta = payload.get("steps_delta")
    await db.execute(
        """UPDATE devices SET
            last_seen = $1,
            steps_delta = $2,
            battery_level = $3,
            suspicious_count = $4,
            updated_at = $5
           WHERE user_id = $6 AND device_id = $7""",
        now_dt,
        steps_delta,
        battery_level,
        new_suspicious_count,
        now_dt,
        user_id, device_id,
    )

    # heartbeat_logs 기록
    await db.execute(
        """INSERT INTO heartbeat_logs
           (device_id, steps_delta, suspicious, battery_level, client_ts, server_ts)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        device_id,
        steps_delta,
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

        # 걸음수 정보 알림 — steps_delta 있을 때만 (자동 heartbeat만, 당일 1회)
        if not manual and steps_delta is not None:
            today_kst_start = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
            today_utc_start = today_kst_start.astimezone(timezone.utc)
            already_sent = await db.fetchval(
                """SELECT 1 FROM guardian_notifications
                   WHERE subject_user_id = $1
                     AND title = '👟 오늘 걸음수 정보'
                     AND created_at >= $2
                   LIMIT 1""",
                user_id, today_utc_start,
            )
            if not already_sent:
                await _save_steps_info_notification(db, user_id, steps_delta)
    else:
        await alert_service.downgrade_alerts_on_suspicious(db, user_id)

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



async def _save_guardian_notification(
    db: asyncpg.Connection,
    guardian_user_id: int,
    subject_user_id: int,
    invite_code: str | None,
    alert_level: str,
    title: str,
    body: str,
    is_push_sent: bool,
) -> None:
    """guardian_notifications 테이블에 알림 저장"""
    await db.execute(
        """INSERT INTO guardian_notifications
           (guardian_user_id, subject_user_id, invite_code, alert_level, title, body, is_push_sent)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        guardian_user_id, subject_user_id, invite_code, alert_level, title, body, is_push_sent,
    )


async def _save_steps_info_notification(
    db: asyncpg.Connection,
    user_id: int,
    today_steps: int,
) -> None:
    """오늘 걸음수 정보 알림 DB 저장 (Push 발송 없음)
    - steps == 0: "건강을 위해 가벼운 산책이 필요해보입니다.(걸음수: 0보)"
    - steps > 0:  "오늘은 N보를 걸으셨습니다."
    """
    if today_steps == 0:
        body = "건강을 위해 가벼운 산책이 필요해보입니다.(걸음수: 0보)"
    else:
        body = f"오늘은 {today_steps:,}보를 걸으셨습니다."
    title = "👟 오늘 걸음수 정보"

    # 대상자 invite_code 조회
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    invite_code = invite_row["invite_code"] if invite_row else None

    # 연결된 구독 활성 보호자에게 DB 저장
    guardians = await db.fetch(
        """SELECT g.guardian_user_id
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1
             AND s.plan != 'expired'
             AND s.expires_at > NOW()""",
        user_id,
    )

    for guardian in guardians:
        await _save_guardian_notification(
            db, guardian["guardian_user_id"], user_id, invite_code,
            "health", title, body, is_push_sent=False,
        )



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
        await _save_guardian_notification(
            db, guardian["guardian_user_id"], user_id, invite_code,
            "info", "🔋 대상자 폰 배터리 부족",
            "폰 배터리가 10% 이하입니다. 충전이 필요할 수 있습니다.",
            is_push_sent=True,
        )


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
        await _save_guardian_notification(
            db, guardian["guardian_user_id"], user_id, invite_code,
            "info", "✅ 오늘 안부 확인 완료",
            "대상자의 오늘 안부 확인이 정상 수신되었습니다.",
            is_push_sent=True,
        )


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
        await _save_guardian_notification(
            db, guardian["guardian_user_id"], user_id, invite_code,
            "info", "✅ 수동 안부 확인",
            "대상자께서 직접 안부 확인을 보냈습니다.",
            is_push_sent=True,
        )
