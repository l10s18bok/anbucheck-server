from datetime import datetime, timezone, timedelta
import asyncio
import json
import logging

import asyncpg
from zoneinfo import ZoneInfo

from i18n.messages import get_message
from services import alert_service, push_service
from services.alert_service import get_guardian_settings, should_send, should_push
from services.heartbeat_keys import is_backfill, is_recovery_key


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
    location_lat: float | None = None,
    location_lng: float | None = None,
    location_accuracy: float | None = None,
    location_captured_at: datetime | None = None,
) -> None:
    """notification_events 테이블에 대상자 기준 1건 저장.
    location_* 필드는 긴급 요청 시에만 값이 들어가며, 그 외 알림에서는 모두 NULL."""
    params_json = json.dumps(message_params, ensure_ascii=False) if message_params else None
    await db.execute(
        """INSERT INTO notification_events
           (subject_user_id, invite_code, alert_level, title, body, message_key, message_params,
            location_lat, location_lng, location_accuracy, location_captured_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
        subject_user_id, invite_code, alert_level, title, body, message_key, params_json,
        location_lat, location_lng, location_accuracy, location_captured_at,
    )


async def _get_active_guardians(db: asyncpg.Connection, subject_user_id: int) -> list:
    """구독 활성 보호자 목록 조회 (fcm_token + locale + alias 포함)

    alias는 보호자마다 다르다(같은 대상자를 A는 "삼촌", B는 "아버지"로 부름).
    따라서 push_fn 람다는 대상자 단위로 한 번만 만들어지므로 alias를 클로저에
    묶을 수 없고, _push_to_guardians가 보호자 루프 안에서 넘겨야 한다.
    """
    return await db.fetch(
        """SELECT DISTINCT ON (g.guardian_user_id)
                  g.guardian_user_id, g.alias, d.fcm_token, d.locale
           FROM guardians g
           JOIN subscriptions s ON s.user_id = g.guardian_user_id
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1
             AND s.plan != 'expired'
             AND s.expires_at > NOW()
             AND d.fcm_token IS NOT NULL
             AND d.fcm_token != ''
           ORDER BY g.guardian_user_id, d.updated_at DESC""",
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
    push_fn은 (fcm_token, locale, alias) → coroutine 형태

    alias(보호자가 대상자에게 붙인 별칭)를 seam으로 넘기는 이유: push_fn 람다는
    호출부에서 대상자 단위로 한 번만 만들어지는데 alias는 보호자마다 다르므로,
    람다 클로저에 묶을 수 없고 이 루프 안에서 건네야 한다.
    """
    coros = []
    for g in guardians:
        settings = await get_guardian_settings(db, g["guardian_user_id"])
        if not should_send(settings, level):
            continue
        if should_push(settings, level):
            locale = g.get("locale") or "ko_KR"
            coros.append(push_fn(g["fcm_token"], locale, g.get("alias")))
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
    scheduled_key = payload.get("scheduled_key")

    # HTTP 재전송 중복 차단 — 자동 heartbeat에 한해 (device_id, scheduled_key) dedup.
    # 클라가 응답 패킷 유실로 retry하면 서버는 같은 요청을 2번 받는다.
    # 첫 요청이 이미 heartbeat_logs에 INSERT된 상태라면 auto_report Push / steps /
    # 경고 해소 등 부수효과를 다시 실행하지 않고 200 OK만 반환해 보호자 알림 중복을 차단.
    # manual=true는 사용자 의도적 액션이므로 스킵 대상에서 제외 (클라 lastManualReportDate가 하루 1회 가드).
    if not manual and scheduled_key:
        is_duplicate = await db.fetchval(
            "SELECT 1 FROM heartbeat_logs WHERE device_id = $1 AND scheduled_key = $2 LIMIT 1",
            device_id, scheduled_key,
        )
        if is_duplicate:
            logger.info(
                f"[heartbeat dedup] skip duplicate device_id={device_id} scheduled_key={scheduled_key}"
            )
            now_kst = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            return {
                "status": "ok",
                "server_time": now_kst,
                "heartbeat_hour": device["heartbeat_hour"],
                "heartbeat_minute": device["heartbeat_minute"],
            }

    steps_delta = payload.get("steps_delta")

    # ── 이 heartbeat가 "오늘의 안부 확인"인가, "지난 기록 보정"인가 ──
    #
    # 도착 시각과 기록이 원래 속한 날짜는 다를 수 있다. 통신 장애로 n일 heartbeat가
    # 클라 보류 큐에 남았다가 n+1일에 뒤늦게 전송되면, 걸음수는 n일 것인데 도착은 n+1일이다.
    # 이걸 오늘 것으로 취급하면 (1) "오늘 안부 확인 완료" Push가 잘못 나가고,
    # (2) 뒤이어 도착하는 진짜 오늘 heartbeat가 is_first_today=False에 걸려
    #     "오늘 N보" 알림을 잃으며, (3) 지난 날의 suspicious로 오늘 경고가 새로 생긴다.
    #
    # 판정 규칙: scheduled_key가 "<도착일>_HH:MM"인 경우에만 오늘의 안부 확인이다.
    #   · "<지난날>_HH:MM"      → 지난 기록 보정 (is_backfill)
    #   · "recovery_<오늘>"     → 예약시각 이전 살아있음 신호. 걸음수를 싣지 않으며
    #                             당일 안부 확인으로 치지 않는다
    #   · None (수동 보고)      → 사용자가 직접 누른 것이므로 항상 당일 취급
    try:
        device_tz = ZoneInfo(device["timezone"] or "Asia/Seoul")
    except Exception:
        device_tz = ZoneInfo("Asia/Seoul")
    arrival_date = now_dt.astimezone(device_tz).date()
    # 판정 규칙과 그 경계(엄격히 과거일 때만)는 heartbeat_keys.is_backfill 참조.
    backfill = is_backfill(scheduled_key, arrival_date)
    # 당일 안부 확인으로 카운트할 기록인가 (auto_report / 오늘 N보 알림 대상)
    is_todays_report = not backfill and not is_recovery_key(scheduled_key)

    # ── devices 테이블 갱신 ──────────────────────────────────────
    #
    # ⚠️ **last_seen은 "오늘의 안부가 확인된 시각"이다 — 당일 안부 확인 기록만 전진시킨다.**
    #
    # 지난 기록 보정(backfill)과 살아있음 신호(recovery)는 `is_todays_report=False`이므로
    # 이 값을 밀지 않는다. 밀면 스케줄러의 두 잡이 그 기기를 **그날 통째로 건너뛴다** —
    # 둘 다 `last_seen < 오늘 로컬 자정`으로 대상을 거르기 때문이다(services/scheduler.py):
    #   · job_ios_heartbeat_trigger(예약시각 정각) — iOS는 이것이 유일한 자동 전송 트리거라
    #     발사되지 않으면 그날 안부 경로가 통째로 사라진다(사용자가 앱을 다시 열지 않는 한).
    #   · job_heartbeat_check(예약시각 +2h) — 실제로 미수신인 날의 보호자 경고가 조용히 사라진다.
    #
    # 2026-09-09 운영 데이터(30일)로 확인한 실제 피해:
    #   · 마스킹 기록 19건 / 13일. **전부 그날 예약시각 이전에 도착했다(예외 0건)** —
    #     recovery는 `예약시각 −15분` 이전에만 발동하고 백필은 아침 큐 플러시로 나가기
    #     때문이다. 즉 이 기록이 생기면 사실상 항상 마스킹이 성립한다.
    #   · iOS 3대(09-01 ×2, 08-31)는 그날 트리거가 실제로 미발사됐다.
    #   · 안드로이드 1대는 09-02·09-03 이틀 연속 미수신 경고가 소실됐다(09-04에 09-03분이
    #     백필로 도착 = 그날 정시 전송이 없었다는 증거).
    #
    # 같은 규칙을 routers/device.py의 [내 걸음수] 엔드포인트가 이미 따르고 있다 —
    # "걸음수를 확인한 것"과 "오늘 안부가 확인된 것"은 다른 사실이라 last_seen을 갱신하지 않는다.
    #
    # ⚠️ **steps_delta도 같은 조건으로 묶는다.** recovery는 걸음수를 싣지 않으므로(null),
    # 묶지 않으면 마지막으로 알던 값을 NULL로 덮는다. 지금은 이 컬럼을 읽는 곳이 없어
    # 무해하지만(걸음수 차트는 heartbeat_logs에서 온다) 되살아나기 쉬운 함정이다. 백필
    # 분기는 원래부터 이걸 피하고 있었고 — "과거 값이 대시보드 카드에 현재 걸음수로
    # 표시되면 오정보" — recovery만 예외였던 것이 실수였다.
    #
    # 반면 battery_level은 **어느 경우에도 갱신한다.** 이 필드의 계약은 "마지막으로 수신한
    # heartbeat의 배터리"이고(미수신 스케줄러가 `battery_level < 20` → '배터리 방전 추정'으로
    # 분기할 때 읽는 값, services/scheduler.py), 지난 기록도 엄연히 수신한 heartbeat다. 저장을
    # 생략하면 그보다 **더 오래된** 값이 남아 계약이 더 어긋난다. "지금 배터리가 부족하다"고
    # 주장하는 Push를 생략하는 것과, 마지막으로 아는 값을 보관하는 것은 별개다.
    # ⚠️ 이 필드는 표시 전용이 아니라 스케줄러의 경고 등급 분기 입력이다 — 바꾸기 전에
    #    scheduler.py의 battery 분기를 함께 볼 것.
    #
    # suspicious_count는 **당일 안부 확인일 때만 에스컬레이션 입력**으로 쓰고, 그 외에는
    # 활동을 증명할 때만 리셋한다. 아래에서 resolve_active_alerts로 활성 경고를 지우면서
    # 카운터만 남겨두면, 경고는 사라졌는데 다음 suspicious 한 번에 곧장 상위 등급으로 튀는
    # 불일치가 생긴다.
    #
    # ⚠️ **살아있음 신호(recovery)도 카운터를 리셋한다** (2026-09-12 확정).
    # 회복 전송은 기기가 켜졌다고 아무 때나 나가지 않는다 — `걸음수>0 || 잠금해제 ||
    # 오늘앱실행` 중 하나가 참일 때만 나가며, 이는 정상 전송이 `suspicious=false`가
    # 되는 조건과 **같은 세 신호**다. 사람 흔적의 양이 정상 전송과 동등하고 빠진 것은
    # "예약시각에"뿐이므로, 아래 resolve_active_alerts와 **함께** 리셋한다.
    #
    # ⚠️ 이 둘은 반드시 같이 움직인다. 한쪽만 게이팅하면 "경고는 지워졌는데 카운터는
    # 2"가 되어 다음 suspicious 한 번에 곧장 상위 등급으로 튄다.
    #
    # 대가(알고 받아들인다): 회복 전송이 도착한 날 **그날 정시 전송까지 실패하면** 그
    # 날짜의 등급이 한 칸 오르지 않는다(주의 → 주의). 영구 상한이 아니라 그만큼 지연일
    # 뿐이다 — 기기가 완전히 죽으면 회복 전송도 멈춰 사다리가 정상적으로 오른다. 정시
    # 전송이 성공한 날은 어차피 그쪽이 해소하므로 결과가 같다.
    # ⚠️ 2026-09-10에 이 자리에 "회복 전송이 사다리를 **매일** 리셋한다"고 적고 게이팅을
    #    넣었던 것은 과장이었다. 미수신 사다리는 `alerts` **행**으로 오르므로(scheduler의
    #    has_active_alert), 정시 전송이 성공한 날에는 그 전송이 어차피 경고를 지운다.
    #    같은 근거로 다시 게이팅하지 말 것.
    #
    # ⚠️ 지난 기록 보정(backfill)도 같다. 그 날 기기가 실제로 살아 있었다는 사후
    # 증거이므로 카운터를 리셋하고 아래에서 경고도 해소한다.
    if is_todays_report:
        new_suspicious_count = device["suspicious_count"] + 1 if suspicious else 0
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
    elif suspicious:
        # 지난 기록이 활동을 증명하지 못했다 — 카운터는 건드리지 않는다.
        # (회복 전송은 suspicious=false라 아래 else로 떨어져 카운터를 리셋한다.)
        await db.execute(
            """UPDATE devices SET battery_level = $1, updated_at = $2
               WHERE user_id = $3 AND device_id = $4""",
            battery_level, now_dt, user_id, device_id,
        )
    else:
        await db.execute(
            """UPDATE devices SET battery_level = $1, suspicious_count = 0, updated_at = $2
               WHERE user_id = $3 AND device_id = $4""",
            battery_level, now_dt, user_id, device_id,
        )

    # 당일 첫 heartbeat 여부 판정 — heartbeat_logs INSERT 전에 조회해야 정확하다.
    # 기기 로컬 타임존 기준 자정 이후 수신 이력 유무로 판단.
    # 이 플래그는 auto_report / steps 알림 중복 생성을 차단하는 데 쓴다.
    # heartbeat_logs INSERT는 매번 수행(이력·차트용), 알림 생성만 당일 첫 수신에 한정.
    #
    # **"오늘의 안부 확인"에 해당하는 행만 센다.** 지난 기록 보정("<지난날>_HH:MM")이나
    # 살아있음 신호("recovery_<날짜>")가 먼저 도착했다는 이유로 뒤이어 오는 진짜 오늘
    # heartbeat가 "첫 수신 아님"으로 걸리면 "오늘 N보" 알림이 통째로 사라진다.
    # 비교는 **파이썬의 is_backfill과 정확히 같은 기준**이어야 한다 — `= 오늘`이 아니라
    # `>= 오늘`이다. 파이썬은 `키 날짜 < 도착일`일 때만 지난 기록으로 보므로, 시계 오차로
    # 날짜가 앞선 행은 "오늘의 기록"으로 처리된다. SQL만 `=`로 두면 그 행이 카운트되지 않아
    # 같은 날 "오늘 N보"가 두 번 발송된다(방금 클라 쪽에서 고친 것과 똑같은 종류의 불일치).
    # 날짜 문자열은 YYYY-MM-DD라 사전순 비교가 날짜순과 일치한다.
    # recovery_는 접두사가 날짜보다 사전순으로 뒤라 `>=`를 통과하므로 명시적으로 제외한다.
    # steps_(걸음수 스냅샷)도 같은 이유로 제외한다 — left('steps_2026-08-28',10)='steps_2026'이고
    # 's' > '2'라 `>=` 비교를 통과해 버린다. 제외하지 않으면 사용자가 [내 걸음수]를 한 번만
    # 눌러도 그날 auto_report와 "오늘 N보" 알림이 통째로 사라진다.
    # 수동 보고는 key가 NULL이며 기존과 동일하게 당일 기록으로 카운트한다.
    is_first_today = await db.fetchval(
        """WITH safe AS (
               SELECT COALESCE(z.name, 'Asia/Seoul') AS tz
               FROM (SELECT $2::text AS inp) t
               LEFT JOIN pg_timezone_names z ON z.name = t.inp
           )
           SELECT NOT EXISTS (
               SELECT 1 FROM heartbeat_logs, safe
               WHERE device_id = $1
                 AND server_ts >= (
                     (now() AT TIME ZONE safe.tz)::date
                 )::timestamp AT TIME ZONE safe.tz
                 AND (
                     scheduled_key IS NULL
                     OR (
                         scheduled_key NOT LIKE 'recovery%'
                         AND scheduled_key NOT LIKE 'steps%'
                         AND left(scheduled_key, 10) >= to_char((now() AT TIME ZONE safe.tz)::date, 'YYYY-MM-DD')
                     )
                 )
           )""",
        device_id,
        device["timezone"],
    )

    # heartbeat_logs 기록.
    # scheduled_key가 있는 자동 heartbeat는 UNIQUE 제약 충돌 시(극단적 동시 INSERT)
    # ON CONFLICT DO NOTHING으로 조용히 스킵 — 이미 SELECT dedup에서 거른 뒤라서
    # 실제로 도달할 가능성은 낮지만 정합성 보장용 defense-in-depth.
    await db.execute(
        """INSERT INTO heartbeat_logs
           (device_id, steps_delta, suspicious, battery_level, client_ts, server_ts, scheduled_key)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT DO NOTHING""",
        device_id,
        steps_delta,
        int(suspicious),
        battery_level,
        payload["timestamp"],
        now_dt,
        scheduled_key,
    )

    # 지난 기록 보정 — 이력 적재와 생존 확인까지만 하고 끝낸다.
    #
    # 아래 알림들은 모두 "오늘의 상태"를 전제하므로 지난 날짜 기록으로 발송하면 오정보가 된다:
    #   · auto_report("오늘 안부 확인 완료") — 오늘 확인된 게 아니다
    #   · steps("오늘 N보") — 어제 걸음수가 오늘 수치로 나간다
    #   · suspicious 에스컬레이션 — 그 날의 미수신은 서버 스케줄러가 이미 경고했다.
    #     지금 또 만들면 같은 날에 대해 두 번 경고하는 셈이 된다
    #   · 배터리 부족 Push — "지금 배터리가 부족하다"는 주장이라 지난 시점 잔량으로 보내면
    #     오정보다. (값 자체는 위에서 devices에 보관 — 저장과 알림은 별개)
    # 반면 **활성 경고 해소는 수행한다** — 늦게라도 도착했다는 것은 그 날 기기가 살아
    # 있었다는 증거이고, 보호자의 걱정을 푸는 것이 이 신호의 핵심 가치다
    # (위에서 suspicious_count도 함께 리셋해 경고/카운터 상태를 일치시킨다).
    if backfill:
        if not suspicious:
            # 긴급 도움 요청(SOS)은 대상자의 의도적 액션이므로 지난 기록으로 지우지 않는다.
            await alert_service.resolve_active_alerts(db, user_id, include_emergency=False)
        logger.info(
            f"[heartbeat backfill] device_id={device_id} key={scheduled_key} "
            f"arrival={arrival_date} — 이력 적재 + 경고 해소만 수행"
        )
        return {
            "status": "ok",
            "server_time": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            "heartbeat_hour": device["heartbeat_hour"],
            "heartbeat_minute": device["heartbeat_minute"],
        }

    # 활성 경고 해소 — suspicious=false일 때만 "정상 복귀" 알림 발송
    if not suspicious:
        if manual:
            # 수동 안부 확인은 사용자 의도적 액션이므로 당일 첫 수신 여부와 무관하게 매번 알림
            await _send_manual_report_to_guardians(db, user_id)
            await alert_service.resolve_active_alerts(db, user_id, include_emergency=True)
        else:
            # ⚠️ **회복 전송도 활성 경고를 해소한다** (2026-09-12 확정).
            #
            # 근거 1 — 증거의 양이 정상 전송과 같다. 이 분기는 `suspicious=false`일
            # 때만 도달하고, 회복 전송이 나가는 조건(`걸음수>0 || 잠금해제 ||
            # 오늘앱실행`)은 정상 전송이 suspicious=false가 되는 조건과 같은 세
            # 신호다. 빠진 것은 "예약시각에"뿐이다.
            #
            # 근거 2 — 제품 요구. 죽어 있던 기기가 살아났다는 사실은 **한시라도 빨리**
            # 보호자에게 닿아야 한다. 보호자가 헛되이 전화하거나 찾아가는 것을 막는
            # 것이 이 신호의 존재 이유다. 경고만 남기고 알리지 않으면 보호자는 아무것도
            # 받지 못한 채 대시보드 카드만 긴급으로 남는다 — 매일 "안부 확인 완료"
            # 알림은 정상으로 가는데 카드는 긴급인 불일치가 그대로 굳는다.
            #
            # ⚠️ `include_emergency=True`는 의도적이다. 대상자가 누른 SOS도 함께
            # 해소한다 — 이 분기가 사람 흔적을 요구하므로 "쓰러진 사람 옆에서 폰이
            # 혼자 SOS를 지우는" 경로는 애초에 없고(그 경우 suspicious=true),
            # SOS 푸시는 이미 즉시·무조건 전 보호자에게 나갔으며 알림 목록에도 그날
            # 내내 남는다. 여기서 지우는 것은 사건이 아니라 카드 등급이다. 그리고
            # 이 경로를 막으면 아무도 [건강 확인 완료]를 누르지 않을 때 카드가
            # **영구히 긴급**으로 박힌다(SOS 행을 지우는 다른 경로가 없다).
            #
            # ⚠️ 2026-09-10에 이것을 is_todays_report로 게이팅했다가 09-12에 되돌렸다.
            #    당시 근거였던 "사다리가 매일 리셋된다"는 과장이었다 — 실제로는 그날
            #    정시 전송까지 실패한 날에만 한 칸 지연된다(위 카운터 주석 참조).
            #    다시 게이팅하지 말 것.
            resolved_levels = await alert_service.resolve_active_alerts(db, user_id, include_emergency=True)
            # caution/warning/urgent가 해소되면 alert_service가 이미 "정상 복귀"(alert_resolved)
            # Push를 발송한다. 그 위에 auto_report까지 보내면 "정상 복귀" + "오늘 안부 확인 완료"
            # 두 알림이 같은 초에 도착해 보호자 화면이 지저분해진다.
            # → 실제 경고가 해소된 경우는 resolved 쪽을 우선하고 auto_report는 생략.
            serious_resolved = bool(set(resolved_levels) & {"caution", "warning", "urgent"})
            # is_todays_report=False는 회복 전송("recovery_<오늘>") — 예약시각 이전에
            # 보내는 살아있음 신호다. 경고 해소는 하되 "오늘 안부 확인 완료"는 보내지
            # 않는다. 보내면 몇 시간 뒤 정시 전송에서 같은 알림이 한 번 더 나간다.
            if not serious_resolved and is_todays_report:
                await _send_auto_report_to_guardians(db, user_id, steps_delta)

        # 활동 감지 알림 — 자동 heartbeat + 당일 첫 수신 + steps_delta > 0일 때만.
        # 수동 보고는 "수동 안부 확인" 알림이 이미 있으므로 steps 알림 중복 생략.
        # 하루 여러 번 전송 시 동일 걸음수가 여러 번 뜨는 UX 문제를 차단한다.
        if not manual and is_todays_report and is_first_today and steps_delta is not None and steps_delta > 0:
            await _save_steps_info_notification(db, user_id, steps_delta)
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
                lambda token, locale, alias: push_service.push_caution(token, user_id, invite_code=invite_code, reason="suspicious", locale=locale, alias=alias),
            )
        elif new_suspicious_count == 2:
            # 2회 → 경고(warning) 등급 (suspicious 전용 문구)
            await alert_service.create_alert(db, user_id, "warning", now_dt)
            await _save_notification_event(
                db, user_id, invite_code,
                "warning",
                get_message("ko_KR", "push_warning_title"),
                get_message("ko_KR", "push_warning_suspicious_body"),
                message_key="warning_suspicious",
            )
            await _push_to_guardians(
                db, guardians, "warning",
                lambda token, locale, alias: push_service.push_warning(token, user_id, invite_code=invite_code, reason="suspicious", locale=locale, alias=alias),
            )
        elif new_suspicious_count >= 3:
            # 3회 이상 → 긴급(urgent) 등급 (suspicious 전용 문구)
            days = new_suspicious_count
            await alert_service.create_alert(db, user_id, "urgent", now_dt, days_inactive=days)
            await _save_notification_event(
                db, user_id, invite_code,
                "urgent",
                get_message("ko_KR", "push_urgent_title"),
                get_message("ko_KR", "push_urgent_suspicious_body", days=days),
                message_key="urgent_suspicious",
                message_params={"days": days},
            )
            await _push_to_guardians(
                db, guardians, "urgent",
                lambda token, locale, alias, d=days: push_service.push_urgent(token, user_id, days=d, invite_code=invite_code, reason="suspicious", locale=locale, alias=alias),
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
) -> None:
    """활동 감지 알림 — 이벤트 1건 저장 (Push 없음).
    클라이언트가 자정~heartbeat 시각 누적 걸음수를 전송하므로 시간 범위는 표시하지 않는다.
    """
    invite_code = await _get_invite_code(db, user_id)
    steps_str = f"{steps_delta:,}"
    body = get_message("ko_KR", "noti_steps_body", steps=steps_str)
    await _save_notification_event(
        db, user_id, invite_code,
        "health",
        get_message("ko_KR", "noti_steps_title"),
        body,
        message_key="steps",
        message_params={"steps": steps_str},
    )


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
        lambda token, locale, alias: push_service.push_battery_low(token, user_id, invite_code=invite_code, locale=locale, alias=alias),
    )


async def _send_auto_report_to_guardians(
    db: asyncpg.Connection,
    user_id: int,
    steps_delta: int | None = None,
) -> None:
    """정상 상태 자동 안부 확인 — 이벤트 1건 저장 + 보호자별 Push 전송

    steps_delta는 **Push 본문에만** 쓴다 — 걸음수가 있으면 "오늘 N보를 걸으셨습니다"로
    나가 보호자가 안심할 근거를 더 구체적으로 받는다. 가드(`> 0`)는 push_auto_report
    안에 있고, 아래 _save_steps_info_notification 호출부의 조건과 문자 그대로 같아야
    한다(상세는 push_service.push_auto_report docstring).

    ⚠️ notification_events 저장 본문은 그대로 둔다 — 그 행은 대상자당 1건을 모든
    보호자가 공유하고 앱 알림 목록은 message_key로 자체 번역해 그리므로, 여기에
    걸음수를 넣으면 DB와 화면이 조용히 갈라진다.
    """
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
        lambda token, locale, alias: push_service.push_auto_report(token, user_id, invite_code=invite_code, locale=locale, alias=alias, steps=steps_delta),
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
        lambda token, locale, alias: push_service.push_manual_report(token, user_id, invite_code=invite_code, locale=locale, alias=alias),
    )
