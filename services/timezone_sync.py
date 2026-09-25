"""기기 시간대(devices.timezone) 동기화 — 해외 여행·이주·출장 대응.

devices.timezone은 원래 가입 때 한 번만 저장됐다. 그런데 폰은 **현재 있는 곳의 현지
시각**으로 동작한다(WorkManager 예약, scheduled_key의 날짜·시각, iOS 확장). 대상자가
해외로 가면 서버와 폰의 "오늘"과 "예약시각"이 어긋나 매일 미수신 판정이 나거나
(서쪽) 정시 전송이 지난 기록 보정으로 오분류된다(동쪽→서쪽 큰 시차).

그래서 클라가 heartbeat·FCM 토큰 갱신에 현재 IANA 시간대를 싣고, 서버는 저장값과
다르면 갱신한다. heartbeat_hour/minute의 의미는 **"현재 있는 곳의 현지 시각"**으로
유지한다 — 폰 동작과 일치시키는 쪽이다.

⚠️ 불변 규칙
  · **이 모듈은 절대 raise하지 않는다.** heartbeat는 영구 하위호환 계약이라 시간대
    갱신 실패가 안부 수신을 막아서는 안 된다. 실패하면 옛 값으로 계속 진행한다.
  · **유효성은 Python ZoneInfo와 pg_timezone_names 양쪽에서 확인한다.** heartbeat
    분류(heartbeat_service)는 ZoneInfo로, 미수신 체크·오늘 첫 안부 판정(SQL)은
    pg_timezone_names로 시간대를 해석한다. 한쪽에만 있는 이름을 저장하면 두 판정이
    서로 다른 시간대를 쓰게 되므로(한쪽은 서울 폴백) 양쪽 모두 통과한 값만 저장한다.
  · **값이 없거나 저장값과 같으면 쿼리를 실행하지 않는다.** 시간대를 보내지 않는
    구버전 앱의 처리 경로가 지금과 글자 하나 다르지 않게 하기 위함이다.
"""

import logging
from zoneinfo import ZoneInfo

import asyncpg


logger = logging.getLogger(__name__)

# IANA 이름 최장은 30자 남짓이다. 모델 필드에 max_length를 걸면 초과 시 heartbeat
# 자체가 422로 거부되므로(하위호환 계약 위반) 길이 검사는 여기서 한다.
_MAX_TZ_LEN = 64


async def validated_new_timezone(
    db: asyncpg.Connection,
    requested: str | None,
    current: str | None,
) -> str | None:
    """저장해야 할 새 시간대 이름을 반환한다. 갱신이 불필요하거나 무효면 None.

    절대 raise하지 않는다.
    """
    try:
        if not requested or not isinstance(requested, str):
            return None
        requested = requested.strip()
        if not requested or len(requested) > _MAX_TZ_LEN or requested == current:
            return None
        try:
            ZoneInfo(requested)
        except Exception:
            logger.info(f"[tz change] 무시 — ZoneInfo 로드 실패: {requested!r}")
            return None
        exists = await db.fetchval(
            "SELECT 1 FROM pg_timezone_names WHERE name = $1 LIMIT 1",
            requested,
        )
        if not exists:
            logger.info(f"[tz change] 무시 — pg_timezone_names에 없음: {requested!r}")
            return None
        return requested
    except Exception as e:
        logger.warning(f"[tz change] 검증 실패 — 옛 값 유지: {e}")
        return None
