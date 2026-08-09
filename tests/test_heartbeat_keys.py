"""scheduled_key 해석 — heartbeat 기록의 "원래 날짜" 판정 테스트.

이 판정이 틀리면 (1) 걸음수 차트가 남의 날짜에 그려지고, (2) 지난 기록으로
"오늘 안부 확인 완료"/"오늘 N보" 알림이 잘못 발송된다.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from services.heartbeat_keys import (
    intended_date,
    is_backfill,
    is_recovery_key,
    log_local_date,
)

KST = ZoneInfo("Asia/Seoul")


class TestIntendedDate:
    def test_정시_전송_키에서_날짜_추출(self):
        assert intended_date("2026-08-08_18:00") == date(2026, 8, 8)

    def test_예약시각이_달라도_날짜만_본다(self):
        assert intended_date("2026-08-08_09:30") == date(2026, 8, 8)

    def test_회복_전송_키에서_날짜_추출(self):
        assert intended_date("recovery_2026-08-09") == date(2026, 8, 9)

    def test_수동_보고는_키가_없다(self):
        assert intended_date(None) is None

    def test_빈_문자열은_None(self):
        assert intended_date("") is None

    def test_형식이_깨진_키는_None으로_폴백(self):
        # 알 수 없는 형식에 예외를 던지면 heartbeat 수신 전체가 500이 된다.
        assert intended_date("garbage") is None
        assert intended_date("2026-13-99_18:00") is None
        assert intended_date("recovery_nope") is None


class TestIsRecoveryKey:
    def test_회복_전송_판별(self):
        assert is_recovery_key("recovery_2026-08-09") is True

    def test_정시_전송은_회복이_아니다(self):
        assert is_recovery_key("2026-08-09_18:00") is False

    def test_수동_보고는_회복이_아니다(self):
        assert is_recovery_key(None) is False


class TestIsBackfill:
    ARRIVAL = date(2026, 8, 9)

    def test_지난_날짜_키는_보정_대상(self):
        assert is_backfill("2026-08-08_18:00", self.ARRIVAL) is True

    def test_오늘_키는_보정_대상이_아니다(self):
        assert is_backfill("2026-08-09_18:00", self.ARRIVAL) is False

    def test_미래_날짜_키는_보정_대상이_아니다(self):
        """기기 타임존 오차·시계 오차로 key_date가 앞설 수 있다.

        `!=`로 판정하면 정상적인 당일 heartbeat가 지난 기록으로 오분류되어
        "오늘 안부 확인 완료"·"오늘 N보" 알림이 조용히 사라진다.
        미래 날짜는 기존 동작(당일 처리)으로 흘려보내야 안전하다.
        """
        assert is_backfill("2026-08-10_18:00", self.ARRIVAL) is False

    def test_회복_전송은_같은_날이면_보정_대상이_아니다(self):
        assert is_backfill("recovery_2026-08-09", self.ARRIVAL) is False

    def test_수동_보고는_보정_대상이_아니다(self):
        assert is_backfill(None, self.ARRIVAL) is False

    def test_형식이_깨진_키는_보정_대상이_아니다(self):
        assert is_backfill("garbage", self.ARRIVAL) is False


class TestLogLocalDate:
    def test_지난_기록은_도착일이_아니라_원래_날짜로_귀속(self):
        """n일 heartbeat가 n+1일에 뒤늦게 도착해도 n일 막대에 들어가야 한다."""
        arrived = datetime(2026, 8, 9, 0, 30, tzinfo=timezone.utc)  # KST 09:30 (n+1일)
        assert log_local_date("2026-08-08_18:00", arrived, KST) == date(2026, 8, 8)

    def test_당일_기록은_그대로(self):
        arrived = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)  # KST 18:00
        assert log_local_date("2026-08-08_18:00", arrived, KST) == date(2026, 8, 8)

    def test_수동_보고는_도착_시각으로_폴백(self):
        arrived = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)  # KST 18:00
        assert log_local_date(None, arrived, KST) == date(2026, 8, 8)

    def test_폴백은_UTC가_아니라_기기_로컬_날짜(self):
        """UTC로는 8/8이지만 KST로는 8/9인 시각 — 기기 로컬 날짜를 써야 한다."""
        arrived = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)  # KST 8/9 01:00
        assert log_local_date(None, arrived, KST) == date(2026, 8, 9)

    def test_형식이_깨진_키도_폴백으로_동작(self):
        arrived = datetime(2026, 8, 9, 0, 30, tzinfo=timezone.utc)  # KST 09:30
        assert log_local_date("garbage", arrived, KST) == date(2026, 8, 9)
