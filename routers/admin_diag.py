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
    push_type: str = Query(
        "heartbeat",
        description=(
            "heartbeat = 트리거 푸시(기본) / manual_report = 피기백 검증용 보호자 알림. "
            "피기백(확장이 트리거 아닌 푸시에 안부를 얹어 보내는 것)은 대상자가 실제로 "
            "보고해야 재현되는데, 그러려면 안드로이드 테스트폰을 깨워야 해서 그쪽 "
            "standby 버킷 관측이 오염된다. 그래서 같은 종류의 푸시를 서버에서 직접 쏜다."
        ),
    ),
    _: None = Depends(_verify_admin),
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await db.fetch(
        """SELECT d.device_id, d.fcm_token, d.locale, d.heartbeat_hour, d.heartbeat_minute,
                  d.last_seen, d.supports_push_heartbeat, u.invite_code IS NOT NULL AS is_gs,
                  -- 정규 잡의 발사 조건이 계산하는 값을 그대로 노출한다.
                  -- zz.tz는 아래 CROSS JOIN LATERAL에서 정의된다(기기 타임존, 불량 값은
                  -- 'Asia/Seoul' 폴백). 표현식만 옮겨 오고 조인을 빠뜨리면 죽는다.
                  -- ⚠️ 정규 잡은 **정각 1회만** 발사한다(재시도 없음). 한때 +5/+10분
                  -- 재시도가 있어 여기에 세 값을 노출했는데, 그 잔재를 보고 "재시도가
                  -- 아직 살아 있다"고 오독하는 일이 실제로 있었다. 하나만 남긴다.
                  (date_trunc('day', now() AT TIME ZONE zz.tz)
                     + make_interval(mins => d.heartbeat_hour * 60 + d.heartbeat_minute)
                  ) AT TIME ZONE zz.tz AS fire_time,
                  (d.last_seen < (date_trunc('day', now() AT TIME ZONE zz.tz) AT TIME ZONE zz.tz))
                    AS missing_today
           FROM users u
           JOIN devices d ON u.id = d.user_id
           -- ⚠️ 타임존 표현식(zz.tz)을 쓰려면 이 두 줄이 **반드시** 함께 있어야 한다.
           -- 2026-08-26: 스케줄러 쿼리에서 표현식만 복사해 오고 이 조인을 빠뜨려
           -- "missing FROM-clause entry for table zz"로 두 번 연속 500을 냈다.
           LEFT JOIN pg_timezone_names z ON z.name = d.timezone
           CROSS JOIN LATERAL (SELECT COALESCE(z.name, 'Asia/Seoul') AS tz) zz
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

    from services.push_service import push_heartbeat_trigger, push_manual_report

    out = []
    for r in rows:
        # dry_run이면 상태만 본다 — 예약시각/플래그를 확인하려고 실제 푸시를 쏘는 것은
        # 사용자에게 불필요한 알림을 만드는 일이다.
        if dry_run:
            ok = None
        elif push_type == "manual_report":
            # 피기백 검증 전용. 확장의 허용목록에 든 타입이라, 오늘 안부가 아직
            # 나가지 않았으면 확장이 이 푸시에 안부를 얹어 보내야 한다.
            # (본문은 원본 그대로 배달되는 것이 정상 — 훼손되면 피기백 규칙 위반이다.)
            ok = await push_manual_report(
                r["fcm_token"], 0, locale=r["locale"] or "ko_KR"
            )
        else:
            ok = await push_heartbeat_trigger(
                r["fcm_token"], r["locale"] or "ko_KR", collapse=collapse
            )
        # 오늘 그 기기의 heartbeat 기록 — 어떤 키로 몇 시에 들어왔는지.
        # ⚠️ last_seen만 봐서는 "정시 안부"와 "회복 전송(recovery_)"이 구분되지 않는다.
        # 서버는 회복 전송을 당일 안부로 치지 않는데(is_todays_report=False) last_seen은
        # 갱신되므로, 진단할 때 둘을 반드시 분리해서 봐야 한다.
        # steps_(걸음수 스냅샷)는 안부와 무관한 사용자 조작이라 이 목록에서 제외한다 —
        # 섞이면 "오늘 안부가 도착했는가" 판독이 오염된다.
        logs = await db.fetch(
            """SELECT scheduled_key, server_ts
               FROM heartbeat_logs
               WHERE device_id = $1
                 AND server_ts >= now() - interval '36 hours'
                 AND (scheduled_key IS NULL OR scheduled_key NOT LIKE 'steps%')
               ORDER BY server_ts DESC LIMIT 10""",
            r["device_id"],
        )
        out.append({
            "fire_time": r["fire_time"].isoformat(),
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
        # ⚠️ 어떤 종류를 쐈는지 반드시 남긴다. 로그가 항상 "트리거"로 찍히던 탓에
        # manual_report를 발사하고도 트리거로 오독하는 일이 있었다.
        logger.info(
            "[진단] iOS %s 즉시 발사 — %d건 (collapse=%s)",
            "manual_report" if push_type == "manual_report" else "트리거",
            len(out), collapse,
        )
    return {"dry_run": dry_run, "collapse": collapse, "count": len(out), "targets": out}
