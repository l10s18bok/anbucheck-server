from datetime import datetime, timezone, timedelta
import logging

import aiosqlite

from services import push_service

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


async def get_active_alerts(db: aiosqlite.Connection, guardian_user_id: int, subject_user_id: int | None = None) -> list[dict]:
    """보호자에게 연결된 대상자의 활성 경고 목록 조회"""
    query = """
        SELECT a.id, a.subject_user_id, u.invite_code, a.status,
               a.days_inactive, a.last_seen_at, a.created_at
        FROM alerts a
        JOIN users u ON a.subject_user_id = u.id
        JOIN guardians g ON g.subject_user_id = a.subject_user_id
        WHERE g.guardian_user_id = ?
          AND a.status = 'active'
    """
    params: list = [guardian_user_id]
    if subject_user_id is not None:
        query += " AND a.subject_user_id = ?"
        params.append(subject_user_id)
    query += " ORDER BY a.created_at DESC"

    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()

    return [dict(row) for row in rows]


async def clear_alert(db: aiosqlite.Connection, alert_id: int, guardian_user_id: int) -> None:
    # 권한 확인
    async with db.execute(
        """SELECT a.id FROM alerts a
           JOIN guardians g ON g.subject_user_id = a.subject_user_id
           WHERE a.id = ? AND g.guardian_user_id = ?""",
        (alert_id, guardian_user_id),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="경고를 찾을 수 없습니다")

    await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    await db.commit()


async def clear_all_alerts(
    db: aiosqlite.Connection, subject_user_id: int, guardian_user_id: int
) -> dict:
    # 권한 확인
    async with db.execute(
        "SELECT id FROM guardians WHERE subject_user_id = ? AND guardian_user_id = ?",
        (subject_user_id, guardian_user_id),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="권한이 없습니다")

    async with db.execute(
        "SELECT id, alert_level FROM alerts WHERE subject_user_id = ? AND status = 'active'",
        (subject_user_id,),
    ) as cur:
        active_alerts = await cur.fetchall()

    if not active_alerts:
        cleared_levels: list[str] = []
        cleared_count = 0
    else:
        cleared_levels = list({row["alert_level"] for row in active_alerts})
        cleared_count = len(active_alerts)

    await db.execute(
        "DELETE FROM alerts WHERE subject_user_id = ? AND status = 'active'",
        (subject_user_id,),
    )
    # suspicious_count 리셋
    await db.execute(
        "UPDATE devices SET suspicious_count = 0 WHERE user_id = ?",
        (subject_user_id,),
    )
    await db.commit()

    now_kst = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    return {
        "cleared_count": cleared_count,
        "cleared_levels": cleared_levels,
        "cleared_by": guardian_user_id,
        "cleared_at": now_kst,
        "adaptive_cycle_reset": True,
        "message": "모든 경고가 클리어되었습니다. 적응형 주기가 정상(매일 고정 시각)으로 복원됩니다.",
    }


async def resolve_active_alerts(db: aiosqlite.Connection, subject_user_id: int) -> list[str]:
    """heartbeat 수신 시 활성 경고 해소 처리, 보호자 Push 발송"""
    async with db.execute(
        """SELECT a.id, a.alert_level FROM alerts a
           WHERE a.subject_user_id = ? AND a.status = 'active'""",
        (subject_user_id,),
    ) as cur:
        active = await cur.fetchall()

    if not active:
        return []

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    await db.execute(
        "DELETE FROM alerts WHERE subject_user_id = ? AND status = 'active'",
        (subject_user_id,),
    )
    await db.commit()

    resolved_levels = [row["alert_level"] for row in active]

    # 정상 복귀 알림은 caution/warning/urgent 해소 시에만 발송
    # info(배터리)만 해소될 경우 정상 복귀 알림 없음
    should_notify = bool(set(resolved_levels) & {"caution", "warning", "urgent"})

    if should_notify:
        async with db.execute(
            """SELECT d.fcm_token FROM guardians g
               JOIN devices d ON d.user_id = g.guardian_user_id
               WHERE g.subject_user_id = ? AND d.fcm_token IS NOT NULL""",
            (subject_user_id,),
        ) as cur:
            tokens = [row["fcm_token"] for row in await cur.fetchall()]

        for token in tokens:
            await push_service.push_resolved(token, subject_user_id)

    return resolved_levels


async def downgrade_alerts_on_suspicious(db: aiosqlite.Connection, subject_user_id: int) -> None:
    """suspicious=true heartbeat 수신 시 warning/urgent 활성 경고를 caution으로 하향.
    정상 복귀 알림은 발송하지 않음 — 사람이 직접 폰을 사용한 증거가 없으므로."""
    await db.execute(
        """UPDATE alerts SET alert_level = 'caution'
           WHERE subject_user_id = ? AND status = 'active'
             AND alert_level IN ('warning', 'urgent')""",
        (subject_user_id,),
    )
    await db.commit()
    logger.info(f"subject_user_id={subject_user_id}: suspicious heartbeat → warning/urgent 주의 등급 하향 (정상 복귀 알림 없음)")


async def has_active_alert(db: aiosqlite.Connection, user_id: int, level: str) -> bool:
    """특정 등급의 활성 경고가 존재하는지 확인"""
    async with db.execute(
        "SELECT id FROM alerts WHERE subject_user_id = ? AND alert_level = ? AND status = 'active'",
        (user_id, level),
    ) as cur:
        return await cur.fetchone() is not None


async def create_alert(
    db: aiosqlite.Connection,
    subject_user_id: int,
    alert_level: str,
    last_seen_at: str,
    days_inactive: int = 1,
) -> int:
    async with db.execute(
        """INSERT INTO alerts (subject_user_id, alert_level, status, days_inactive, last_seen_at)
           VALUES (?, ?, 'active', ?, ?)""",
        (subject_user_id, alert_level, days_inactive, last_seen_at),
    ) as cur:
        alert_id = cur.lastrowid
    await db.commit()
    return alert_id


async def send_alert_to_guardians(
    db: aiosqlite.Connection,
    subject_user_id: int,
    alert_level: str,
    battery_level: int | None = None,
) -> None:
    """보호자들에게 경고 Push 발송 (구독 활성 보호자만)"""
    async with db.execute(
        """SELECT d.fcm_token, g.guardian_user_id
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = ?
             AND s.plan != 'expired'
             AND s.expires_at > datetime('now')
             AND d.fcm_token IS NOT NULL""",
        (subject_user_id,),
    ) as cur:
        guardians = await cur.fetchall()

    for guardian in guardians:
        token = guardian["fcm_token"]
        if alert_level == "info_battery_low":
            await push_service.push_battery_low(token, subject_user_id)
        elif alert_level == "info_battery_dead":
            await push_service.push_battery_dead(token, subject_user_id, battery_level or 0)
        elif alert_level == "caution":
            await push_service.push_caution(token, subject_user_id)
        elif alert_level == "warning":
            await push_service.push_warning(token, subject_user_id)
        elif alert_level == "urgent":
            await push_service.push_urgent(token, subject_user_id)
