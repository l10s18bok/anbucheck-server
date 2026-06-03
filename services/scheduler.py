"""APScheduler 기반 스케줄러

- 매 1분: heartbeat 미수신 경고 체크
- 매일 00:00 KST: 당일 보호자 알림 자정 일괄 삭제
- 매일 00:00 KST: 구독 만료 체크
- 매일 03:00 KST: 보호자 미연결 대상자 정리
- 매일 04:00 KST: 30일 초과 heartbeat_logs 삭제
"""
from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR

import asyncpg

from database import (
    try_advisory_lock,
    LOCK_HEARTBEAT_CHECK,
    LOCK_CLEANUP_NOTI,
    LOCK_SUB_EXPIRE,
    LOCK_CLEANUP_SUBJECTS,
    LOCK_CLEANUP_LOGS,
)
from i18n.messages import get_message
from services.alert_service import get_guardian_settings, should_send, should_push
from services.heartbeat_service import _save_notification_event, _get_active_guardians, _get_invite_code, _push_to_guardians

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


def _singleton(lock_key: int):
    """잡을 advisory lock으로 감싸 멀티 인스턴스에서 단 하나만 실행하게 한다.

    락을 못 잡으면(다른 인스턴스가 실행 중) 즉시 양보한다. 락 보유 인스턴스가
    죽어도 다음 fire 때 다른 인스턴스가 락을 잡아 자가 치유된다 (SPOF 없음).
    잡 본문은 DB 트랜잭션으로 감싸지 않고 기존처럼 짧은 statement를 유지한다
    — push 네트워크 호출 동안 tx/락을 길게 들고 있지 않기 위함.
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper():
            async with try_advisory_lock(lock_key) as acquired:
                if not acquired:
                    logger.debug("[%s] 다른 인스턴스가 실행 중 — 스킵", fn.__name__)
                    return
                await fn()
        return wrapper
    return deco


# ─────────────────────────────────────────────────────────────
# 2. Heartbeat 미수신 경고 체크 (매 1분)
# ─────────────────────────────────────────────────────────────

async def job_heartbeat_check() -> None:
    # 미수신 판정은 기기 로컬 타임존(devices.timezone) 기준으로 수행한다 —
    # "오늘 자정"과 "예약시각 +2h"를 모두 기기 로컬 시각으로 계산한 뒤 UTC instant로
    # 환산해 now()와 비교한다. (과거 KST 벽시계 하드코딩은 비-KST 사용자에게
    # 예약+2h를 (offset-9)시간 어긋나게 발화시켰다 — 자정 정리/걸음수 통계 등
    # 다른 잡과 동일하게 devices.timezone을 존중하도록 정합화.)
    #
    # 안정성: zone 문자열을 그대로 `AT TIME ZONE`에 넣으면 불량 값 1건이 매분 전체
    # 쿼리를 throw시켜 미수신 체크를 마비시킬 수 있다. pg_timezone_names에 LEFT JOIN해
    # 유효하지 않은 zone은 'Asia/Seoul'로 폴백시켜 항상 유효 zone만 사용한다.
    from database import get_pool
    async with get_pool().acquire() as db:
        missed = await db.fetch(
            """SELECT u.id AS user_id, d.device_id, d.last_seen,
                      d.battery_level,
                      d.suspicious_count, d.platform,
                      d.heartbeat_hour, d.heartbeat_minute,
                      d.fcm_token, d.locale
               FROM users u
               JOIN devices d ON u.id = d.user_id
               LEFT JOIN pg_timezone_names z ON z.name = d.timezone
               CROSS JOIN LATERAL (SELECT COALESCE(z.name, 'Asia/Seoul') AS tz) zz
               WHERE u.invite_code IS NOT NULL
                 AND date_trunc('minute', now()) = date_trunc('minute',
                       (date_trunc('day', now() AT TIME ZONE zz.tz)
                          + make_interval(mins => d.heartbeat_hour * 60 + d.heartbeat_minute + 120)
                       ) AT TIME ZONE zz.tz)
                 AND d.last_seen < (date_trunc('day', now() AT TIME ZONE zz.tz) AT TIME ZONE zz.tz)""",
        )

        for row in missed:
            await _process_missed_heartbeat(db, dict(row))


async def _process_missed_heartbeat(db: asyncpg.Connection, row: dict) -> None:
    user_id = row["user_id"]
    battery_level = row["battery_level"] or 0

    # 0. 대상자 본인 안부유도 푸시 (Android 한정, 구독·보호자 유무와 무관).
    #    iOS는 클라의 정시 로컬알림(gs_deadman)이 PRIMARY 트리거이므로 서버 푸시 제외.
    #    아래 보호자 gate(_get_active_guardians) **앞**에 두어, 보호자가 없거나 전원
    #    구독 만료여도 대상자에겐 "앱을 열어 안부를 보내달라"는 푸시가 전달되게 한다.
    #    스케줄러가 미수신일마다 하루 1회 발화하므로, 사용자가 무시하면 다음 날 같은
    #    시각에 재발송되어 기존 일일 로컬 안전망 알림의 "무시 시 반복"을 그대로 대체한다.
    if row.get("platform") == "android" and row.get("fcm_token"):
        from services.push_service import push_subject_safety_net
        await push_subject_safety_net(row["fcm_token"], row.get("locale") or "ko_KR")

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
                "info",
                get_message("ko_KR", "push_battery_dead_title"),
                get_message("ko_KR", "push_battery_dead_body", battery_level=battery_level),
                message_key="battery_dead",
                message_params={"battery_level": battery_level},
            )
            await _push_to_guardians(
                db, guardians, "info",
                lambda token, locale: push_battery_dead(token, user_id, battery_level, invite_code=invite_code, locale=locale),
            )
        return

    # 2. 누적 미수신 등급 판정
    has_urgent  = await has_active_alert(db, user_id, "urgent")
    has_warning = await has_active_alert(db, user_id, "warning")
    has_caution = await has_active_alert(db, user_id, "caution")

    if has_urgent:
        await _escalate_urgent_if_needed(db, user_id, last_seen_dt, guardians, invite_code)

    elif has_warning:
        await create_alert(db, user_id, "urgent", last_seen_dt, days_inactive=3)
        await _save_notification_event(
            db, user_id, invite_code,
            "urgent",
            get_message("ko_KR", "push_urgent_title"),
            get_message("ko_KR", "push_urgent_body", days=3),
            message_key="urgent",
            message_params={"days": 3},
        )
        await _push_to_guardians(
            db, guardians, "urgent",
            lambda token, locale: push_urgent(token, user_id, days=3, invite_code=invite_code, locale=locale),
        )

    elif has_caution:
        await create_alert(db, user_id, "warning", last_seen_dt, days_inactive=2)
        await _save_notification_event(
            db, user_id, invite_code,
            "warning",
            get_message("ko_KR", "push_warning_title"),
            get_message("ko_KR", "push_warning_body"),
            message_key="warning",
        )
        await _push_to_guardians(
            db, guardians, "warning",
            lambda token, locale: push_warning(token, user_id, invite_code=invite_code, locale=locale),
        )

    else:
        await create_alert(db, user_id, "caution", last_seen_dt)
        await _save_notification_event(
            db, user_id, invite_code,
            "caution",
            get_message("ko_KR", "push_caution_title"),
            get_message("ko_KR", "push_caution_missing_body"),
            message_key="caution_missing",
        )
        await _push_to_guardians(
            db, guardians, "caution",
            lambda token, locale: push_caution(token, user_id, invite_code=invite_code, locale=locale),
        )


async def _escalate_urgent_if_needed(
    db: asyncpg.Connection,
    user_id: int,
    last_seen_dt: datetime,
    guardians: list,
    invite_code: str | None = None,
) -> None:
    """긴급 등급 기존 경고 업데이트 + push_count < 5이면 푸시 발송"""
    from services.push_service import push_urgent_secondary
    row = await db.fetchrow(
        """UPDATE alerts SET days_inactive = days_inactive + 1, push_count = push_count + 1
           WHERE subject_user_id = $1 AND alert_level = 'urgent' AND status = 'active'
           RETURNING days_inactive, push_count""",
        user_id,
    )
    days = row["days_inactive"] if row else 0
    push_count = row["push_count"] if row else 0

    await _save_notification_event(
        db, user_id, invite_code,
        "urgent",
        get_message("ko_KR", "push_urgent_title"),
        get_message("ko_KR", "push_urgent_body", days=days),
        message_key="urgent",
        message_params={"days": days},
    )
    # 푸시 발송은 최대 5회까지만
    if push_count <= 5:
        await _push_to_guardians(
            db, guardians, "urgent",
            lambda token, locale, d=days: push_urgent_secondary(token, user_id, days=d, invite_code=invite_code, locale=locale),
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
            """SELECT u.id AS user_id, d.fcm_token, d.locale
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
                locale = row.get("locale") or "ko_KR"
                await push_subscription_expired(row["fcm_token"], locale=locale)

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
               WHERE u.invite_code IS NOT NULL
                 AND u.role = 'subject'
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
        # rate_limits 윈도우는 60초짜리라 1시간만 지나도 무의미 — 테이블 무한 증식 방지.
        await db.execute(
            "DELETE FROM rate_limits WHERE window_start < NOW() - INTERVAL '1 hour'"
        )
    logger.info("30일 초과 heartbeat_logs + 만료 rate_limits 삭제 완료")


# ─────────────────────────────────────────────────────────────
# 스케줄러 등록
# ─────────────────────────────────────────────────────────────

def _on_job_error(event) -> None:
    """잡 실행 중 예외 발생 시 Discord로 통보.

    APScheduler 리스너가 이벤트 루프 스레드에서 호출된다는 보장이 없으므로,
    asyncio.create_task 대신 동기 진입점(notify_error_sync)으로 보낸다 — 전송은
    내부에서 자체적으로 처리되고 절대 예외를 던지지 않는다.
    event.traceback은 사전 포맷된 스택 문자열이라 그대로 넘긴다(예외 객체의
    __traceback__은 리스너까지 살아오지 않는 경우가 있음).
    """
    from services.notify import notify_error_sync
    notify_error_sync(
        f"스케줄러 잡 '{event.job_id}'",
        exc=event.exception,
        tb_text=event.traceback,
    )


def setup_scheduler() -> AsyncIOScheduler:
    # 잡 실행 예외를 단일 리스너로 포착해 Discord 알림으로 보낸다(매 분 도는 미수신
    # 체크가 조용히 죽는 것을 방지). 5개 잡 전부 이 리스너 하나로 커버된다.
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # 각 잡을 advisory lock으로 감싼다 — 멀티 인스턴스에서 같은 시각에 여러 스케줄러가
    # fire해도 락을 잡은 하나만 실제로 실행되어 중복 push/중복 정리를 차단한다.
    scheduler.add_job(_singleton(LOCK_HEARTBEAT_CHECK)(job_heartbeat_check), CronTrigger(second=0), id="heartbeat_check", replace_existing=True)
    logger.info("스케줄러 등록: Heartbeat 미수신 체크 — 매 분 정각 실행")
    scheduler.add_job(_singleton(LOCK_CLEANUP_NOTI)(job_cleanup_notifications), CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"), id="cleanup_noti", replace_existing=True)
    logger.info("스케줄러 등록: 알림 자정 정리 — 매일 00:00 KST")
    scheduler.add_job(_singleton(LOCK_SUB_EXPIRE)(job_subscription_expire_check), CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"), id="sub_expire", replace_existing=True)
    logger.info("스케줄러 등록: 구독 만료 체크 — 매일 00:00 KST")
    scheduler.add_job(_singleton(LOCK_CLEANUP_SUBJECTS)(job_cleanup_orphan_subjects), CronTrigger(hour=3, minute=0, timezone="Asia/Seoul"), id="cleanup_subjects", replace_existing=True)
    logger.info("스케줄러 등록: 보호자 미연결 대상자 정리 — 매일 03:00 KST")
    scheduler.add_job(_singleton(LOCK_CLEANUP_LOGS)(job_cleanup_old_logs), CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"), id="cleanup_logs", replace_existing=True)
    logger.info("스케줄러 등록: heartbeat_logs 30일 초과 삭제 — 매일 04:00 KST")
    return scheduler
