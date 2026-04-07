from datetime import datetime, timezone, timedelta
import asyncio
import json
import logging

import asyncpg
from zoneinfo import ZoneInfo

from i18n.messages import get_message
from services import alert_service, push_service
from services.alert_service import get_guardian_settings, should_send, should_push


logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


async def _save_notification_event(
    db: asyncpg.Connection,
    subject_user_id: int,
    invite_code: str | None,
    alert_level: str,
    title: str,
    body: str,
    message_key: str | None = None,
    message_params: dict | None = None,
) -> None:
    """notification_events 테이블에 대상자 기준 1건 저장"""
    params_json = json.dumps(message_params, ensure_ascii=False) if message_params else None
    await db.execute(
        """INSERT INTO notification_events
           (subject_user_id, invite_code, alert_level, title, body, message_key, message_params)
           VALUES ($1, $2, $3, $4, $5, $6, $7)""",
        subject_user_id, invite_code, alert_level, title, body, message_key, params_json,
    )


async def _get_active_guardians(db: asyncpg.Connection, subject_user_id: int) -> list:
    """구독 활성 보호자 목록 조회 (fcm_token + locale 포함)"""
    return await db.fetch(
        """SELECT g.guardian_user_id, d.fcm_token, d.locale
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1
             AND s.plan != 'expired'
             AND s.expires_at > NOW()
             AND d.fcm_token IS NOT NULL
             AND d.fcm_token != ''""",
        subject_user_id,
    )


async def _get_invite_code(db: asyncpg.Connection, user_id: int) -> str | None:
    """대상자 invite_code 조회"""
    row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    return row["invite_code"] if row else None


async def _push_to_guardians(
    db: asyncpg.Connection,
    guardians: list,
    level: str,
    push_fn,
) -> None:
    """보호자별 settings 확인 후 Push 전송 (DB 저장 없음)
    push_fn은 (fcm_token, locale) → coroutine 형태"""
    coros = []
    for g in guardians:
        settings = await get_guardian_settings(db, g["guardian_user_id"])
        if not should_send(settings, level):
            continue
        if should_push(settings, level):
            locale = g.get("locale") or "ko_KR"
            coros.append(push_fn(g["fcm_token"], locale))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def process_heartbeat(db: asyncpg.Connection, user_id: int, payload: dict) -> dict:
    device_id = payload["device_id"]

    # 기기 정보 조회
    device = await db.fetchrow(
        "SELECT id, suspicious_count, heartbeat_hour, heartbeat_minute, last_seen, last_steps, timezone FROM devices WHERE user_id = $1 AND device_id = $2",
        user_id, device_id,
    )

    if device is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기기를 찾을 수 없습니다")

    now_dt = datetime.now(timezone.utc)
    suspicious    = payload["suspicious"]
    battery_level = payload.get("battery_level")
    manual        = payload.get("manual", False)

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
    if not suspicious:
        if manual:
            # 수동 안부 확인 알림을 먼저 저장 후 경고 해소 (알림 순서 보장)
            await _send_manual_report_to_guardians(db, user_id)
            await alert_service.resolve_active_alerts(db, user_id, include_emergency=True)
        else:
            await alert_service.resolve_active_alerts(db, user_id, include_emergency=True)
            await _send_auto_report_to_guardians(db, user_id)

        # 활동 감지 알림 — steps_delta > 0일 때만
        if steps_delta is not None and steps_delta > 0:
            await _save_steps_info_notification(
                db, user_id, steps_delta,
                prev_seen=device["last_seen"],
                now_dt=now_dt,
                tz_name=device["timezone"],
            )
    else:
        await alert_service.downgrade_alerts_on_suspicious(db, user_id)
        # PRD 4.6: suspicious=true 시 보호자에게 주의/경고 알림 발송
        invite_code = await _get_invite_code(db, user_id)
        guardians = await _get_active_guardians(db, user_id)
        if new_suspicious_count == 1:
            # 1회 → 주의(caution) 등급: 폰 사용 흔적 없음
            await alert_service.create_alert(db, user_id, "caution", now_dt)
            await _save_notification_event(
                db, user_id, invite_code,
                "caution",
                get_message("ko_KR", "push_caution_title"),
                get_message("ko_KR", "push_caution_suspicious_body"),
                message_key="caution_suspicious",
            )
            await _push_to_guardians(
                db, guardians, "caution",
                lambda token, locale: push_service.push_caution(token, user_id, invite_code=invite_code, reason="suspicious", locale=locale),
            )
        elif new_suspicious_count == 2:
            # 2회 → 경고(warning) 등급
            await alert_service.create_alert(db, user_id, "warning", now_dt)
            await _save_notification_event(
                db, user_id, invite_code,
                "warning",
                get_message("ko_KR", "push_warning_title"),
                get_message("ko_KR", "push_warning_body"),
                message_key="warning",
            )
            await _push_to_guardians(
                db, guardians, "warning",
                lambda token, locale: push_service.push_warning(token, user_id, invite_code=invite_code, locale=locale),
            )
        elif new_suspicious_count >= 3:
            # 3회 이상 → 긴급(urgent) 등급
            days = new_suspicious_count
            await alert_service.create_alert(db, user_id, "urgent", now_dt, days_inactive=days)
            await _save_notification_event(
                db, user_id, invite_code,
                "urgent",
                get_message("ko_KR", "push_urgent_title"),
                get_message("ko_KR", "push_urgent_body", days=days),
                message_key="urgent",
                message_params={"days": days},
            )
            await _push_to_guardians(
                db, guardians, "urgent",
                lambda token, locale, d=days: push_service.push_urgent(token, user_id, days=d, invite_code=invite_code, locale=locale),
            )

    # 배터리 < 20% → 보호자 정보 알림
    if battery_level is not None and battery_level < 20:
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


async def _save_steps_info_notification(
    db: asyncpg.Connection,
    user_id: int,
    steps_delta: int,
    prev_seen: datetime,
    now_dt: datetime,
    tz_name: str,
) -> None:
    """활동 감지 알림 — 이벤트 1건 저장 (Push 없음)"""
    invite_code = await _get_invite_code(db, user_id)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    prev_local = prev_seen.astimezone(tz)
    now_local = now_dt.astimezone(tz)
    # 한국어 기본 title/body (하위 호환)
    from_time = f"{prev_local.month}/{prev_local.day} {_format_ampm(prev_local)}"
    to_time = f"{now_local.month}/{now_local.day} {_format_ampm(now_local)}"
    body = get_message("ko_KR", "noti_steps_body", from_time=from_time, to_time=to_time, steps=f"{steps_delta:,}")
    await _save_notification_event(
        db, user_id, invite_code,
        "health",
        get_message("ko_KR", "noti_steps_title"),
        body,
        message_key="steps",
        message_params={
            "from_time": from_time,
            "to_time": to_time,
            "steps": f"{steps_delta:,}",
        },
    )


def _format_ampm(dt: datetime) -> str:
    """datetime → '오전 09:30' / '오후 02:15' 형식"""
    hour = dt.hour
    period = "오전" if hour < 12 else "오후"
    h12 = hour if hour <= 12 else hour - 12
    if h12 == 0:
        h12 = 12
    return f"{period} {h12:02d}:{dt.minute:02d}"


async def _send_battery_low_to_guardians(db: asyncpg.Connection, user_id: int) -> None:
    """배터리 부족 알림 — 이벤트 1건 저장 + 보호자별 Push 전송"""
    invite_code = await _get_invite_code(db, user_id)
    guardians = await _get_active_guardians(db, user_id)

    await _save_notification_event(
        db, user_id, invite_code,
        "info",
        get_message("ko_KR", "push_battery_low_title"),
        get_message("ko_KR", "push_battery_low_body"),
        message_key="battery_low",
    )
    await _push_to_guardians(
        db, guardians, "info",
        lambda token, locale: push_service.push_battery_low(token, user_id, invite_code=invite_code, locale=locale),
    )


async def _send_auto_report_to_guardians(db: asyncpg.Connection, user_id: int) -> None:
    """정상 상태 자동 안부 확인 — 이벤트 1건 저장 + 보호자별 Push 전송"""
    invite_code = await _get_invite_code(db, user_id)
    guardians = await _get_active_guardians(db, user_id)

    await _save_notification_event(
        db, user_id, invite_code,
        "info",
        get_message("ko_KR", "push_auto_report_title"),
        get_message("ko_KR", "push_auto_report_body"),
        message_key="auto_report",
    )
    await _push_to_guardians(
        db, guardians, "info",
        lambda token, locale: push_service.push_auto_report(token, user_id, invite_code=invite_code, locale=locale),
    )


async def _send_manual_report_to_guardians(db: asyncpg.Connection, user_id: int) -> None:
    """평상시 수동 안부 보고 — 이벤트 1건 저장 + 보호자별 Push 전송"""
    invite_code = await _get_invite_code(db, user_id)
    guardians = await _get_active_guardians(db, user_id)

    await _save_notification_event(
        db, user_id, invite_code,
        "info",
        get_message("ko_KR", "push_manual_report_title"),
        get_message("ko_KR", "push_manual_report_body"),
        message_key="manual_report",
    )
    await _push_to_guardians(
        db, guardians, "info",
        lambda token, locale: push_service.push_manual_report(token, user_id, invite_code=invite_code, locale=locale),
    )
