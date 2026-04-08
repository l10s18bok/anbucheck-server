import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
from typing import Optional

import asyncpg

from services import push_service

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

_LEVEL_KEY_MAP = {
    "urgent":  "urgent_enabled",
    "warning": "warning_enabled",
    "caution": "caution_enabled",
    "info":    "info_enabled",
}


async def get_guardian_settings(db: asyncpg.Connection, guardian_user_id: int) -> dict:
    """보호자 알림 설정 조회 — 없으면 기본값(모두 ON) 반환.
    guardian_timezone: 보호자 기기 timezone (IANA 문자열, 기본 'Asia/Seoul')."""
    row = await db.fetchrow(
        """SELECT gns.*, COALESCE(d.timezone, 'Asia/Seoul') AS guardian_timezone
           FROM guardian_notification_settings gns
           LEFT JOIN devices d ON d.user_id = gns.guardian_user_id
           WHERE gns.guardian_user_id = $1""",
        guardian_user_id,
    )
    if row is None:
        tz_row = await db.fetchrow(
            "SELECT COALESCE(timezone, 'Asia/Seoul') AS tz FROM devices WHERE user_id = $1",
            guardian_user_id,
        )
        return {
            "all_enabled": True, "urgent_enabled": True, "warning_enabled": True,
            "caution_enabled": True, "info_enabled": True,
            "dnd_enabled": False, "dnd_start": None, "dnd_end": None,
            "guardian_timezone": tz_row["tz"] if tz_row else "Asia/Seoul",
        }
    return dict(row)


def is_in_dnd(settings: dict) -> bool:
    """현재 보호자 로컬 시각이 방해금지 시간대인지 확인.
    settings['guardian_timezone']: IANA timezone 문자열 (기본 'Asia/Seoul')."""
    if not settings["dnd_enabled"]:
        return False
    dnd_start = settings.get("dnd_start")
    dnd_end   = settings.get("dnd_end")
    if not dnd_start or not dnd_end:
        return False

    try:
        tz = ZoneInfo(settings.get("guardian_timezone") or "Asia/Seoul")
    except (ZoneInfoNotFoundError, Exception):
        tz = ZoneInfo("Asia/Seoul")

    now_local   = datetime.now(tz)
    now_minutes = now_local.hour * 60 + now_local.minute
    start_h, start_m = map(int, dnd_start.split(":"))
    end_h,   end_m   = map(int, dnd_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes   = end_h   * 60 + end_m

    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes <= end_minutes
    # 자정을 넘기는 경우 (예: 22:00 ~ 07:00)
    return now_minutes >= start_minutes or now_minutes <= end_minutes


def should_send(settings: dict, level: str) -> bool:
    """알림 자체를 보낼지 여부 (DND와 무관한 ON/OFF 설정만 확인)
    긴급(urgent) 알림은 스위치 OFF여도 항상 발송"""
    if level == "urgent":
        return True
    if not settings["all_enabled"]:
        return False
    key = _LEVEL_KEY_MAP.get(level)
    if key and not settings[key]:
        return False
    return True


def should_push(settings: dict, level: str) -> bool:
    """Push 발송 여부 — DND 시간대면 Push 미발송 (urgent는 DND 무관 항상 발송)"""
    if level == "urgent":
        return True
    return not is_in_dnd(settings)


async def get_active_alerts(db: asyncpg.Connection, guardian_user_id: int, subject_user_id: int | None = None) -> list[dict]:
    """보호자에게 연결된 대상자의 활성 경고 목록 조회"""
    query = """
        SELECT a.id, a.subject_user_id, u.invite_code, a.status,
               a.days_inactive, a.last_seen_at, a.created_at
        FROM alerts a
        JOIN users u ON a.subject_user_id = u.id
        JOIN guardians g ON g.subject_user_id = a.subject_user_id
        WHERE g.guardian_user_id = $1
          AND a.status = 'active'
    """
    params: list = [guardian_user_id]
    if subject_user_id is not None:
        query += " AND a.subject_user_id = $2"
        params.append(subject_user_id)
    query += " ORDER BY a.created_at DESC"

    rows = await db.fetch(query, *params)

    return [
        {
            **dict(row),
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]


async def clear_alert(db: asyncpg.Connection, alert_id: int, guardian_user_id: int) -> None:
    # 권한 확인
    row = await db.fetchrow(
        """SELECT a.id FROM alerts a
           JOIN guardians g ON g.subject_user_id = a.subject_user_id
           WHERE a.id = $1 AND g.guardian_user_id = $2""",
        alert_id, guardian_user_id,
    )

    if row is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="경고를 찾을 수 없습니다")

    await db.execute("DELETE FROM alerts WHERE id = $1", alert_id)


async def clear_all_alerts(
    db: asyncpg.Connection, subject_user_id: int, guardian_user_id: int
) -> dict:
    # 권한 확인
    row = await db.fetchrow(
        "SELECT id FROM guardians WHERE subject_user_id = $1 AND guardian_user_id = $2",
        subject_user_id, guardian_user_id,
    )

    if row is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 없습니다")

    active_alerts = await db.fetch(
        "SELECT id, alert_level FROM alerts WHERE subject_user_id = $1 AND status = 'active'",
        subject_user_id,
    )

    if not active_alerts:
        cleared_levels: list[str] = []
        cleared_count = 0
    else:
        cleared_levels = list({row["alert_level"] for row in active_alerts})
        cleared_count = len(active_alerts)

    await db.execute(
        "DELETE FROM alerts WHERE subject_user_id = $1 AND status = 'active'",
        subject_user_id,
    )
    # suspicious_count 리셋
    await db.execute(
        "UPDATE devices SET suspicious_count = 0 WHERE user_id = $1",
        subject_user_id,
    )

    # 경고가 있었을 때만 다른 보호자에게 알림 발송
    if cleared_count > 0:
        from services.heartbeat_service import _save_notification_event, _get_active_guardians, _get_invite_code, _push_to_guardians

        invite_code = await _get_invite_code(db, subject_user_id)

        # notification_events 저장
        await _save_notification_event(
            db, subject_user_id, invite_code,
            "info", "✅ 안부 확인 완료",
            "보호자 중 한 명이 대상자의 안전을 직접 확인했습니다.",
            message_key="cleared_by_guardian",
        )

        # 요청 보호자를 제외한 다른 보호자에게 Push 발송
        guardians = await _get_active_guardians(db, subject_user_id)
        other_guardians = [g for g in guardians if g["guardian_user_id"] != guardian_user_id]
        if other_guardians:
            await _push_to_guardians(
                db, other_guardians, "info",
                lambda token, locale: push_service.push_alert_cleared(
                    token, subject_user_id, invite_code=invite_code, locale=locale,
                ),
            )

    now_kst = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    return {
        "cleared_count": cleared_count,
        "cleared_levels": cleared_levels,
        "cleared_by": guardian_user_id,
        "cleared_at": now_kst,
        "adaptive_cycle_reset": True,
        "message": "모든 경고가 클리어되었습니다. 적응형 주기가 정상(매일 고정 시각)으로 복원됩니다.",
    }


async def resolve_active_alerts(db: asyncpg.Connection, subject_user_id: int, include_emergency: bool = False) -> list[str]:
    """heartbeat 수신 시 활성 경고 해소 처리, 보호자 Push 발송 (DND 적용)
    include_emergency=True: 수동 heartbeat — 긴급 도움 요청 알림도 함께 해소
    include_emergency=False: 자동 heartbeat — 긴급 도움 요청 알림은 보호자 수동 클리어만 가능"""
    from services.heartbeat_service import _save_notification_event, _get_active_guardians, _get_invite_code, _push_to_guardians

    if include_emergency:
        active = await db.fetch(
            "SELECT a.id, a.alert_level FROM alerts a WHERE a.subject_user_id = $1 AND a.status = 'active'",
            subject_user_id,
        )
    else:
        active = await db.fetch(
            "SELECT a.id, a.alert_level FROM alerts a WHERE a.subject_user_id = $1 AND a.status = 'active' AND (a.note IS NULL OR a.note != 'emergency_request')",
            subject_user_id,
        )

    if not active:
        return []

    alert_ids = [row["id"] for row in active]
    await db.execute(
        "DELETE FROM alerts WHERE id = ANY($1::int[])",
        alert_ids,
    )

    resolved_levels = [row["alert_level"] for row in active]

    # 정상 복귀 알림은 caution/warning/urgent 해소 시에만 발송
    # info(배터리)만 해소될 경우 정상 복귀 알림 없음
    if not bool(set(resolved_levels) & {"caution", "warning", "urgent"}):
        return resolved_levels

    invite_code = await _get_invite_code(db, subject_user_id)
    guardians = await _get_active_guardians(db, subject_user_id)

    await _save_notification_event(
        db, subject_user_id, invite_code,
        "info", "✅ 정상", "보호 대상자의 안부가 정상적으로 확인되었습니다.",
        message_key="resolved",
    )
    await _push_to_guardians(
        db, guardians, "info",
        lambda token, locale: push_service.push_resolved(token, subject_user_id, invite_code=invite_code, locale=locale),
    )

    return resolved_levels


async def downgrade_alerts_on_suspicious(db: asyncpg.Connection, subject_user_id: int) -> None:
    """suspicious=true heartbeat 수신 시 warning/urgent 활성 경고를 caution으로 하향.
    정상 복귀 알림은 발송하지 않음 — 사람이 직접 폰을 사용한 증거가 없으므로."""
    await db.execute(
        """UPDATE alerts SET alert_level = 'caution'
           WHERE subject_user_id = $1 AND status = 'active'
             AND alert_level IN ('warning', 'urgent')""",
        subject_user_id,
    )
    logger.info(f"subject_user_id={subject_user_id}: suspicious heartbeat → warning/urgent 주의 등급 하향 (정상 복귀 알림 없음)")


async def has_active_alert(db: asyncpg.Connection, user_id: int, level: str) -> bool:
    """특정 등급의 활성 경고가 존재하는지 확인"""
    row = await db.fetchrow(
        "SELECT id FROM alerts WHERE subject_user_id = $1 AND alert_level = $2 AND status = 'active'",
        user_id, level,
    )
    return row is not None


async def create_alert(
    db: asyncpg.Connection,
    subject_user_id: int,
    alert_level: str,
    last_seen_at: datetime,
    days_inactive: int = 1,
) -> int:
    alert_id = await db.fetchval(
        """INSERT INTO alerts (subject_user_id, alert_level, status, days_inactive, last_seen_at)
           VALUES ($1, $2, 'active', $3, $4) RETURNING id""",
        subject_user_id, alert_level, days_inactive, last_seen_at,
    )
    return alert_id


async def send_alert_to_guardians(
    db: asyncpg.Connection,
    subject_user_id: int,
    alert_level: str,
    battery_level: int | None = None,
) -> None:
    """보호자들에게 경고 Push 발송 (구독 활성 보호자만)"""
    guardians = await db.fetch(
        """SELECT d.fcm_token, g.guardian_user_id
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

    # 대상자 invite_code 조회
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", subject_user_id)
    invite_code = invite_row["invite_code"] if invite_row else None

    def _make_push_coro(token: str):
        if alert_level == "info_battery_low":
            return push_service.push_battery_low(token, subject_user_id, invite_code=invite_code)
        if alert_level == "info_battery_dead":
            return push_service.push_battery_dead(token, subject_user_id, battery_level or 0, invite_code=invite_code)
        if alert_level == "caution":
            return push_service.push_caution(token, subject_user_id, invite_code=invite_code)
        if alert_level == "warning":
            return push_service.push_warning(token, subject_user_id, invite_code=invite_code)
        if alert_level == "urgent":
            return push_service.push_urgent(token, subject_user_id, invite_code=invite_code)
        return None

    coros = [c for g in guardians if (c := _make_push_coro(g["fcm_token"])) is not None]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)
