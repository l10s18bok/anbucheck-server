"""scheduled_key 해석 유틸 — heartbeat 기록이 "원래 어느 날 것인가"를 판정한다.

heartbeat는 도착 시각(server_ts)과 원래 속한 날짜가 다를 수 있다. 통신 장애로 n일
기록이 보류 큐에 남았다가 n+1일에 뒤늦게 전송되는 경우가 대표적이다. 이때 도착 시각을
기준으로 처리하면 (1) 걸음수 차트에서 n일 막대가 0이 되고 n+1일 막대에 남의 값이 들어가며,
(2) "오늘 안부 확인 완료" 같은 당일 알림이 지난 기록 때문에 잘못 발송된다.

클라이언트가 보내는 scheduled_key가 그 판정의 근거다:
  · "YYYY-MM-DD_HH:MM"   정시 전송 — 그 날짜의 안부 확인
  · "recovery_YYYY-MM-DD" 회복 전송 — 예약시각 이전에 보내는 "살아있음" 신호.
                          걸음수를 싣지 않으며 당일 안부 확인으로 치지 않는다
  · "steps_YYYY-MM-DD"    걸음수 스냅샷 — 사용자가 [내 걸음수]를 눌러 그 시점까지의
                          누적 걸음수만 올린 것. 안부 판정·알림과 무관하며 차트에만 쓴다.
  · None                  수동 보고 — 사용자가 직접 누른 것이라 항상 당일 취급

이 모듈은 순수 함수만 두어 heartbeat_service / subject_service 양쪽이 순환 import 없이
같은 규칙을 공유하게 한다.
"""

from datetime import date, datetime, tzinfo

_RECOVERY_PREFIX = "recovery_"

# 걸음수 스냅샷 키 접두사. POST /api/v1/devices/me/steps 가 남기는 행에만 붙는다.
# ⚠️ 접두사를 벗기지 않으면 `raw[:10]`이 "steps_2026"이 되어 ValueError → None으로
#    떨어지고, log_local_date가 server_ts로 폴백해 자정 근처에서 날짜가 어긋난다.
_STEPS_PREFIX = "steps_"


def intended_date(scheduled_key: str | None) -> date | None:
    """scheduled_key가 가리키는 기기 로컬 날짜. 해석 불가하면 None."""
    if not scheduled_key:
        return None
    raw = scheduled_key
    for prefix in (_RECOVERY_PREFIX, _STEPS_PREFIX):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def is_recovery_key(scheduled_key: str | None) -> bool:
    """회복 전송(살아있음 신호) 여부."""
    return bool(scheduled_key) and scheduled_key.startswith(_RECOVERY_PREFIX)


def is_backfill(scheduled_key: str | None, arrival_date: date) -> bool:
    """이 heartbeat가 "지난 기록 보정"인가 — 이력 적재 + 경고 해소만 할 대상.

    ⚠️ **엄격히 과거일 때만 True다(`<`, `!=` 아님).** arrival_date는 `devices.timezone`
    으로 계산하는데, 기기가 이동했거나 시계가 어긋나 이 타임존이 실제와 다르면 자정 근처에서
    key_date가 arrival_date보다 **앞설** 수 있다. `!=`로 판정하면 그때 정상적인 당일
    heartbeat가 지난 기록으로 오분류되어 "오늘 안부 확인 완료"·"오늘 N보" 알림이 조용히
    사라진다. 미래 날짜/시계 오차는 기존 동작(당일 처리)으로 흘려보내는 쪽이 안전하다.
    """
    key_date = intended_date(scheduled_key)
    return key_date is not None and key_date < arrival_date


def log_local_date(
    scheduled_key: str | None,
    server_ts: datetime,
    tz: tzinfo,
) -> date:
    """heartbeat_logs 한 행을 귀속시킬 기기 로컬 날짜.

    scheduled_key를 우선하고, 없거나(수동 보고) 형식이 깨졌으면 도착 시각으로 폴백한다.
    """
    return intended_date(scheduled_key) or server_ts.astimezone(tz).date()


def is_steps_snapshot(scheduled_key: str | None) -> bool:
    """걸음수 스냅샷 행 여부 — 안부 확인으로 세지 않는다."""
    return bool(scheduled_key) and scheduled_key.startswith(_STEPS_PREFIX)
