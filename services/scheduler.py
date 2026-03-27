"""APScheduler 기반 스케줄러

- 매 1분: Heartbeat 트리거 Silent Push 발송 (iOS/Android 공통)
- 매 1분: heartbeat 미수신 경고 체크
- 매일 00:00 KST: 구독 만료 체크
- 매일 03:00 KST: 보호자 미연결 대상자 정리
- 매일 04:00 KST: 30일 초과 heartbeat_logs 삭제
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import asyncpg

from services.alert_service import get_guardian_settings, is_in_dnd, should_send, use_sound

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


def _can_send(settings: dict, level: str) -> bool:
    """스케줄러용 발송 가능 여부 — DND 시간대에는 발송 안 함 (urgent 제외)"""
    return should_send(settings, level) and use_sound(settings, level)


# ─────────────────────────────────────────────────────────────
# 1. Heartbeat 트리거 Silent Push 발송 (매 1분, iOS/Android 공통)
# ─────────────────────────────────────────────────────────────

async def job_heartbeat_trigger() -> None:
    now_kst = datetime.now(KST)
    current_hour = now_kst.hour
    current_minute = now_kst.minute

    from database import get_pool
    async with get_pool().acquire() as db:
        devices = await db.fetch(
            """SELECT d.fcm_token, d.device_id, d.platform
               FROM devices d
               JOIN users u ON d.user_id = u.id
               WHERE u.role = 'subject'
                 AND d.heartbeat_hour = $1
                 AND d.heartbeat_minute = $2
                 AND d.fcm_token IS NOT NULL""",
            current_hour, current_minute,
        )

        if not devices:
            logger.debug(f"Heartbeat 트리거 해당 기기 없음 (KST {current_hour:02d}:{current_minute:02d})")
            return

        from services.push_service import push_heartbeat_trigger
        for dev in devices:
            token_invalid = await push_heartbeat_trigger(dev["fcm_token"], dev["platform"])
            if token_invalid:
                await db.execute(
                    "UPDATE devices SET fcm_token = NULL WHERE device_id = $1",
                    dev["device_id"],
                )
        logger.info(f"Heartbeat 트리거 발송: {len(devices)}대 (KST {current_hour:02d}:{current_minute:02d})")


# ─────────────────────────────────────────────────────────────
# 2. Heartbeat 미수신 경고 체크 (매 1분)
# ─────────────────────────────────────────────────────────────

async def job_heartbeat_check() -> None:
    now_kst = datetime.now(KST)
    current_minutes = now_kst.hour * 60 + now_kst.minute

    # heartbeat 시각 + 120분 = 현재인 기기 중 오늘 미수신
    # last_seen < 오늘 자정(KST → UTC 기준)
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

    # 구독 활성 보호자 확인
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

    if not guardians:
        return

    last_seen_dt = row["last_seen"]  # TIMESTAMPTZ → datetime
    from services.alert_service import create_alert, has_active_alert
    from services.push_service import (
        push_battery_dead, push_caution, push_warning, push_urgent
    )

    # 대상자 invite_code 조회
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    invite_code = invite_row["invite_code"] if invite_row else None

    # 1. 배터리 ≤ 10% → 정보 등급 1회 발송 후 종료 (이후 상향 없음)
    if battery_level <= 10:
        if not await has_active_alert(db, user_id, "info"):
            await create_alert(db, user_id, "info", last_seen_dt)
            for g in guardians:
                settings = await get_guardian_settings(db, g["guardian_user_id"])
                if _can_send(settings, "info"):
                    await push_battery_dead(g["fcm_token"], user_id, battery_level, invite_code=invite_code)
        return

    # 2. 누적 미수신 등급 판정
    #    기존 활성 경고 상태로 횟수 결정:
    #    없음 → 1회(주의) / 주의 있음 → 2회(경고) / 경고 있음 → 3회이상(긴급) / 긴급 있음 → 긴급 반복
    has_urgent  = await has_active_alert(db, user_id, "urgent")
    has_warning = await has_active_alert(db, user_id, "warning")
    has_caution = await has_active_alert(db, user_id, "caution")

    if has_urgent:
        # 긴급 지속 — days_inactive 증가 + 반복 발송
        await _escalate_urgent_if_needed(db, user_id, last_seen_dt, guardians, invite_code)

    elif has_warning:
        # 경고 3회 이상 → 긴급 상향
        await create_alert(db, user_id, "urgent", last_seen_dt)
        for g in guardians:
            settings = await get_guardian_settings(db, g["guardian_user_id"])
            if _can_send(settings, "urgent"):
                await push_urgent(g["fcm_token"], user_id, invite_code=invite_code)

    elif has_caution:
        # 2회 미수신 → 경고
        await create_alert(db, user_id, "warning", last_seen_dt, days_inactive=2)
        for g in guardians:
            settings = await get_guardian_settings(db, g["guardian_user_id"])
            if _can_send(settings, "warning"):
                await push_warning(g["fcm_token"], user_id, invite_code=invite_code)

    else:
        # 1회 미수신 → 주의
        await create_alert(db, user_id, "caution", last_seen_dt)
        for g in guardians:
            settings = await get_guardian_settings(db, g["guardian_user_id"])
            if _can_send(settings, "caution"):
                await push_caution(g["fcm_token"], user_id, invite_code=invite_code)


async def _escalate_urgent_if_needed(
    db: asyncpg.Connection,
    user_id: int,
    last_seen_dt: datetime,
    guardians: list,
    invite_code: str | None = None,
) -> None:
    """긴급 등급 기존 경고 업데이트 + 2차 보호자 발송"""
    from services.push_service import push_urgent_secondary
    # days_inactive 증가
    await db.execute(
        """UPDATE alerts SET days_inactive = days_inactive + 1
           WHERE subject_user_id = $1 AND alert_level = 'urgent' AND status = 'active'""",
        user_id,
    )
    # 모든 보호자에게 2차 발송 (긴급은 DND 무관)
    for g in guardians:
        settings = await get_guardian_settings(db, g["guardian_user_id"])
        if _can_send(settings, "urgent"):
            await push_urgent_secondary(g["fcm_token"], user_id, invite_code=invite_code)


# ─────────────────────────────────────────────────────────────
# 3. 구독 만료 체크 (매일 00:00 KST)
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
# 4. 보호자 미연결 대상자 정리 (매일 03:00 KST)
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
            await db.execute("DELETE FROM devices WHERE user_id = $1", user_id)
            await db.execute("DELETE FROM users WHERE id = $1", user_id)

        if subjects:
            logger.info(f"보호자 미연결 대상자 정리: {len(subjects)}명")


# ─────────────────────────────────────────────────────────────
# 5. heartbeat_logs 30일 초과 삭제 (매일 04:00 KST)
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
    scheduler.add_job(job_heartbeat_trigger, CronTrigger(second=0), id="heartbeat_trigger", replace_existing=True)
    scheduler.add_job(job_heartbeat_check, CronTrigger(second=0), id="heartbeat_check", replace_existing=True)
    scheduler.add_job(job_subscription_expire_check, CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"), id="sub_expire", replace_existing=True)
    scheduler.add_job(job_cleanup_orphan_subjects, CronTrigger(hour=3, minute=0, timezone="Asia/Seoul"), id="cleanup_subjects", replace_existing=True)
    scheduler.add_job(job_cleanup_old_logs, CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"), id="cleanup_logs", replace_existing=True)
    return scheduler
