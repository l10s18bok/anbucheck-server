"""APScheduler 기반 스케줄러

- 매 1분: heartbeat 미수신 경고 체크
- 매일 00:00 KST: 당일 보호자 알림 자정 일괄 삭제
- 매일 00:00 KST: 구독 만료 체크
- 매일 03:00 KST: 보호자 미연결 대상자 정리
- 매일 04:00 KST: 30일 초과 heartbeat_logs 삭제
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import asyncpg

from services.alert_service import get_guardian_settings, should_send, should_push
from services.heartbeat_service import _save_notification_event, _get_active_guardians, _get_invite_code, _push_to_guardians

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


# ─────────────────────────────────────────────────────────────
# 2. Heartbeat 미수신 경고 체크 (매 1분)
# ─────────────────────────────────────────────────────────────

async def job_heartbeat_check() -> None:
    now_kst = datetime.now(KST)
    current_minutes = now_kst.hour * 60 + now_kst.minute

    today_kst_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc_start = today_kst_start.astimezone(timezone.utc)

    from database import get_pool
    async with get_pool().acquire() as db:
        missed = await db.fetch(
            """SELECT u.id AS user_id, d.device_id, d.last_seen,
                      d.battery_level,
                      d.suspicious_count, d.platform,
                      d.heartbeat_hour, d.heartbeat_minute
               FROM users u
               JOIN devices d ON u.id = d.user_id
               WHERE u.role = 'subject'
                 AND (d.heartbeat_hour * 60 + d.heartbeat_minute + 120) = $1
                 AND d.last_seen < $2""",
            current_minutes, today_utc_start,
        )

        for row in missed:
            await _process_missed_heartbeat(db, dict(row))


async def _process_missed_heartbeat(db: asyncpg.Connection, row: dict) -> None:
    user_id = row["user_id"]
    battery_level = row["battery_level"] or 0

    guardians = await _get_active_guardians(db, user_id)
    if not guardians:
        return

    last_seen_dt = row["last_seen"]
    from services.alert_service import create_alert, has_active_alert
    from services.push_service import (
        push_battery_dead, push_caution, push_warning, push_urgent
    )

    invite_code = await _get_invite_code(db, user_id)

    # 1. 배터리 < 20% → 정보 등급 1회 발송 후 종료
    if battery_level < 20:
        if not await has_active_alert(db, user_id, "info"):
            await create_alert(db, user_id, "info", last_seen_dt)
            await _save_notification_event(
                db, user_id, invite_code,
                "info", "🔋 배터리 방전 추정",
                "대상자의 폰이 배터리 방전으로 꺼진 것 같습니다. 충전 후 자동으로 정상 복귀됩니다.",
            )
            await _push_to_guardians(
                db, guardians, "info",
                lambda token: push_battery_dead(token, user_id, battery_level, invite_code=invite_code),
            )
        return

    # 2. 누적 미수신 등급 판정
    has_urgent  = await has_active_alert(db, user_id, "urgent")
    has_warning = await has_active_alert(db, user_id, "warning")
    has_caution = await has_active_alert(db, user_id, "caution")

    if has_urgent:
        await _escalate_urgent_if_needed(db, user_id, last_seen_dt, guardians, invite_code)

    elif has_warning:
        await create_alert(db, user_id, "urgent", last_seen_dt)
        await _save_notification_event(
            db, user_id, invite_code,
            "urgent", "🚨 긴급: 대상자 확인 필요",
            "안부 확인이 없으며 마지막 확인 시 폰 사용 흔적도 없었습니다. 즉시 확인이 필요합니다.",
        )
        await _push_to_guardians(
            db, guardians, "urgent",
            lambda token: push_urgent(token, user_id, invite_code=invite_code),
        )

    elif has_caution:
        await create_alert(db, user_id, "warning", last_seen_dt, days_inactive=2)
        await _save_notification_event(
            db, user_id, invite_code,
            "warning", "⚠ 안부 확인",
            "대상자의 오늘 안부 확인이 없습니다. 통신 불가 상태일 수 있습니다.",
        )
        await _push_to_guardians(
            db, guardians, "warning",
            lambda token: push_warning(token, user_id, invite_code=invite_code),
        )

    else:
        await create_alert(db, user_id, "caution", last_seen_dt)
        await _save_notification_event(
            db, user_id, invite_code,
            "caution", "⚠ 안부 확인 필요",
            "오늘 대상자의 안부 확인이 아직 없습니다. 직접 안부를 확인해 보시기 바랍니다.",
        )
        await _push_to_guardians(
            db, guardians, "caution",
            lambda token: push_caution(token, user_id, invite_code=invite_code),
        )


async def _escalate_urgent_if_needed(
    db: asyncpg.Connection,
    user_id: int,
    last_seen_dt: datetime,
    guardians: list,
    invite_code: str | None = None,
) -> None:
    """긴급 등급 기존 경고 업데이트 + 2차 보호자 발송"""
    from services.push_service import push_urgent_secondary
    await db.execute(
        """UPDATE alerts SET days_inactive = days_inactive + 1
           WHERE subject_user_id = $1 AND alert_level = 'urgent' AND status = 'active'""",
        user_id,
    )
    await _save_notification_event(
        db, user_id, invite_code,
        "urgent", "🚨 긴급: 대상자 확인 필요",
        "안부 확인이 없으며 마지막 확인 시 폰 사용 흔적도 없었습니다. 즉시 확인이 필요합니다.",
    )
    await _push_to_guardians(
        db, guardians, "urgent",
        lambda token: push_urgent_secondary(token, user_id, invite_code=invite_code),
    )


# ─────────────────────────────────────────────────────────────
# 3. 당일 알림 자정 일괄 삭제 (매일 00:00 KST)
# ─────────────────────────────────────────────────────────────

async def job_cleanup_notifications() -> None:
    """전날 알림 일괄 삭제 — notification_events는 대상자 기준이므로
    대상자 기기 timezone 기준 자정 이전 알림 삭제."""
    from database import get_pool
    async with get_pool().acquire() as db:
        # 대상자별 timezone 조회
        rows = await db.fetch(
            """SELECT DISTINCT ne.subject_user_id,
                      COALESCE(d.timezone, 'Asia/Seoul') AS tz
               FROM notification_events ne
               LEFT JOIN devices d ON d.user_id = ne.subject_user_id"""
        )
        total_deleted = 0
        for row in rows:
            try:
                tz = ZoneInfo(row["tz"])
            except (ZoneInfoNotFoundError, Exception):
                tz = ZoneInfo("Asia/Seoul")
            midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_utc = midnight_local.astimezone(timezone.utc)
            result = await db.execute(
                "DELETE FROM notification_events WHERE subject_user_id = $1 AND created_at < $2",
                row["subject_user_id"], midnight_utc,
            )
            count = int(result.split()[-1]) if result else 0
            total_deleted += count
    logger.info(f"[자정 알림 정리] 삭제 완료 — {total_deleted}건")


# ─────────────────────────────────────────────────────────────
# 4. 구독 만료 체크 (매일 00:00 KST)
# ─────────────────────────────────────────────────────────────

async def job_subscription_expire_check() -> None:
    from database import get_pool
    async with get_pool().acquire() as db:
        expired = await db.fetch(
            """SELECT u.id AS user_id, d.fcm_token
               FROM users u
               JOIN subscriptions s ON u.id = s.user_id
               LEFT JOIN devices d ON d.user_id = u.id
               WHERE u.role = 'guardian'
                 AND s.plan IN ('free_trial', 'yearly')
                 AND s.expires_at < NOW()""",
        )

        from services.push_service import push_subscription_expired

        for row in expired:
            await db.execute(
                "UPDATE subscriptions SET plan = 'expired', updated_at = NOW() WHERE user_id = $1",
                row["user_id"],
            )
            if row["fcm_token"]:
                await push_subscription_expired(row["fcm_token"])

        if expired:
            logger.info(f"구독 만료 처리: {len(expired)}명")


# ─────────────────────────────────────────────────────────────
# 5. 보호자 미연결 대상자 정리 (매일 03:00 KST)
# ─────────────────────────────────────────────────────────────

async def job_cleanup_orphan_subjects() -> None:
    from database import get_pool
    async with get_pool().acquire() as db:
        subjects_rows = await db.fetch(
            """SELECT u.id FROM users u
               WHERE u.role = 'subject'
                 AND u.created_at < NOW() - INTERVAL '30 days'
                 AND NOT EXISTS (
                   SELECT 1 FROM guardians g WHERE g.subject_user_id = u.id
                 )""",
        )
        subjects = [row["id"] for row in subjects_rows]

        for user_id in subjects:
            device_rows = await db.fetch(
                "SELECT device_id FROM devices WHERE user_id = $1", user_id
            )
            for dev in device_rows:
                await db.execute(
                    "DELETE FROM heartbeat_logs WHERE device_id = $1", dev["device_id"]
                )

            await db.execute("DELETE FROM alerts WHERE subject_user_id = $1", user_id)
            await db.execute("DELETE FROM notification_events WHERE subject_user_id = $1", user_id)
            await db.execute("DELETE FROM devices WHERE user_id = $1", user_id)
            await db.execute("DELETE FROM users WHERE id = $1", user_id)

        if subjects:
            logger.info(f"보호자 미연결 대상자 정리: {len(subjects)}명")


# ─────────────────────────────────────────────────────────────
# 6. heartbeat_logs 30일 초과 삭제 (매일 04:00 KST)
# ─────────────────────────────────────────────────────────────

async def job_cleanup_old_logs() -> None:
    from database import get_pool
    async with get_pool().acquire() as db:
        await db.execute(
            "DELETE FROM heartbeat_logs WHERE server_ts < NOW() - INTERVAL '30 days'"
        )
    logger.info("30일 초과 heartbeat_logs 삭제 완료")


# ─────────────────────────────────────────────────────────────
# 스케줄러 등록
# ─────────────────────────────────────────────────────────────

def setup_scheduler() -> AsyncIOScheduler:
    scheduler.add_job(job_heartbeat_check, CronTrigger(second=0), id="heartbeat_check", replace_existing=True)
    logger.info("스케줄러 등록: Heartbeat 미수신 체크 — 매 분 정각 실행")
    scheduler.add_job(job_cleanup_notifications, CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"), id="cleanup_noti", replace_existing=True)
    logger.info("스케줄러 등록: 알림 자정 정리 — 매일 00:00 KST")
    scheduler.add_job(job_subscription_expire_check, CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"), id="sub_expire", replace_existing=True)
    logger.info("스케줄러 등록: 구독 만료 체크 — 매일 00:00 KST")
    scheduler.add_job(job_cleanup_orphan_subjects, CronTrigger(hour=3, minute=0, timezone="Asia/Seoul"), id="cleanup_subjects", replace_existing=True)
    logger.info("스케줄러 등록: 보호자 미연결 대상자 정리 — 매일 03:00 KST")
    scheduler.add_job(job_cleanup_old_logs, CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"), id="cleanup_logs", replace_existing=True)
    logger.info("스케줄러 등록: heartbeat_logs 30일 초과 삭제 — 매일 04:00 KST")
    return scheduler
