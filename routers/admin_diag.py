"""진단 전용 라우터 — iOS heartbeat 트리거 푸시를 즉시 발사한다.

정규 스케줄러 잡(`job_ios_heartbeat_trigger`)은 "예약시각 정각 + 오늘 미수신"에서만
발화하므로 **하루에 한 번만** 시험할 수 있다. iOS 확장을 고치고 확인하는 주기가 하루가
되면 진단이 불가능하다. 이 엔드포인트는 시각·미수신 조건을 무시하고 같은 푸시를 쏜다.

⚠️ X-Admin-Key로 보호된다(app_version과 동일한 ADMIN_SECRET_KEY, fail-closed).
"""
import logging
import secrets

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from config import ADMIN_SECRET_KEY
from database import get_db

router = APIRouter(prefix="/api/v1", tags=["admin_diag"])
logger = logging.getLogger(__name__)


def _verify_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    # ADMIN_SECRET_KEY 미설정("")이면 fail-closed(전부 거부). 비교는 상수시간.
    if not ADMIN_SECRET_KEY or not secrets.compare_digest(x_admin_key, ADMIN_SECRET_KEY):
        raise HTTPException(status_code=403, detail="권한이 없습니다")


@router.post("/admin/ios-heartbeat-trigger")
async def ios_heartbeat_trigger(
    collapse: bool = Query(True, description="apns-collapse-id 부착 여부 — 전달 문제 A/B 진단용"),
    dry_run: bool = Query(False, description="발송 없이 대상 상태만 조회 — 예약시각·플래그 확인용"),
    device_id: str | None = Query(None, description="특정 기기만. 생략 시 **정규 잡과 동일 조건**(확장 탑재 + G+S)의 iOS 기기"),
    _: None = Depends(_verify_admin),
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await db.fetch(
        """SELECT d.device_id, d.fcm_token, d.locale, d.heartbeat_hour, d.heartbeat_minute,
                  d.last_seen, d.supports_push_heartbeat, u.invite_code IS NOT NULL AS is_gs,
                  -- 정규 잡의 발사 조건이 계산하는 시각 3개를 그대로 노출한다.
                  -- 산술(타임존 환산·오프셋)이 맞는지 로그를 뒤지지 않고 확인하기 위함.
                  ARRAY(
                    SELECT (date_trunc('day', now() AT TIME ZONE zz.tz)
                             + make_interval(mins => d.heartbeat_hour * 60
                                                    + d.heartbeat_minute + o)
                           ) AT TIME ZONE zz.tz
                    FROM unnest(ARRAY[0, 5, 10]) AS o
                  ) AS trigger_times,
                  -- 지금 이 순간의 오프셋(분). 잡이 발사하려면 이 값이 0/5/10이어야 한다.
                  EXTRACT(EPOCH FROM (
                    date_trunc('minute', now())
                    - (date_trunc('day', now() AT TIME ZONE zz.tz)
                         + make_interval(mins => d.heartbeat_hour * 60 + d.heartbeat_minute)
                      ) AT TIME ZONE zz.tz
                  )) / 60 AS offset_now,
                  (d.last_seen < (date_trunc('day', now() AT TIME ZONE zz.tz) AT TIME ZONE zz.tz))
                    AS missing_today
           FROM users u
           JOIN devices d ON u.id = d.user_id
           WHERE d.platform = 'ios'
             AND d.fcm_token IS NOT NULL
             -- ⚠️ **정규 잡과 동일한 대상 조건을 반드시 포함한다.**
             -- 2026-08-23 사고: 이 두 줄이 없어 "모든 iOS 기기"가 대상이 되었고,
             -- 확장이 없는 **실사용자 22명**에게 일요일 아침 6시 40분에 불필요한
             -- "안부 확인이 필요합니다" 알림이 발송됐다. 진단 도구가 운영 사용자에게
             -- 닿을 수 있는 형태로 존재해서는 안 된다.
             AND d.supports_push_heartbeat = true
             AND u.invite_code IS NOT NULL
             AND ($1::text IS NULL OR d.device_id = $1)""",
        device_id,
    )

    from services.push_service import push_heartbeat_trigger

    out = []
    for r in rows:
        # dry_run이면 상태만 본다 — 예약시각/플래그를 확인하려고 실제 푸시를 쏘는 것은
        # 사용자에게 불필요한 알림을 만드는 일이다.
        ok = None if dry_run else await push_heartbeat_trigger(
            r["fcm_token"], r["locale"] or "ko_KR", collapse=collapse
        )
        # 오늘 그 기기의 heartbeat 기록 — 어떤 키로 몇 시에 들어왔는지.
        # ⚠️ last_seen만 봐서는 "정시 안부"와 "회복 전송(recovery_)"이 구분되지 않는다.
        # 서버는 회복 전송을 당일 안부로 치지 않는데(is_todays_report=False) last_seen은
        # 갱신되므로, 진단할 때 둘을 반드시 분리해서 봐야 한다.
        logs = await db.fetch(
            """SELECT scheduled_key, server_ts
               FROM heartbeat_logs
               WHERE device_id = $1
                 AND server_ts >= now() - interval '36 hours'
               ORDER BY server_ts DESC LIMIT 10""",
            r["device_id"],
        )
        out.append({
            "trigger_times": [t.isoformat() for t in r["trigger_times"]],
            "offset_now_min": float(r["offset_now"]),
            "missing_today": r["missing_today"],
            "recent_logs": [
                {"key": lg["scheduled_key"], "at": lg["server_ts"].isoformat()} for lg in logs
            ],
            "device_id": r["device_id"][:8] + "...",
            "fcm_token": (r["fcm_token"] or "")[:10] + "...",
            "schedule": f'{r["heartbeat_hour"]:02d}:{r["heartbeat_minute"]:02d}',
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "supports_push_heartbeat": r["supports_push_heartbeat"],
            "is_gs": r["is_gs"],
            "sent": ok,
        })
    if dry_run:
        logger.info("[진단] iOS 트리거 대상 조회(dry-run) — %d건", len(out))
    else:
        logger.info("[진단] iOS 트리거 즉시 발사 — %d건 (collapse=%s)", len(out), collapse)
    return {"dry_run": dry_run, "collapse": collapse, "count": len(out), "targets": out}
