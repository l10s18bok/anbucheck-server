"""무료 체험 기기 단위 1회 부여 — 탈퇴 후 재등록 farming 차단 테스트.

이 로직은 "막혔는지"를 화면으로 확인할 방법이 없다(정상 사용자에게는 아무 변화도
보이지 않고, 뚫렸을 때만 90일이 더 생긴다). 그래서 분기를 여기서 고정한다.

DB 없이 돌리기 위해 resolve_trial이 실제로 쓰는 두 쿼리(SELECT / INSERT..ON CONFLICT)만
흉내내는 가짜 커넥션을 쓴다. SQL 문자열이 바뀌면 이 fake도 같이 고쳐야 한다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from config import FREE_TRIAL_DAYS
from services.trial_service import hash_device_id, resolve_trial


class FakeConn:
    """trial_grants 한 테이블만 흉내내는 최소 커넥션."""

    def __init__(self, rows: dict[str, datetime] | None = None):
        self.rows: dict[str, datetime] = dict(rows or {})

    async def fetchrow(self, sql: str, *args):
        assert "SELECT first_expires_at FROM trial_grants" in sql
        device_hash = args[0]
        if device_hash not in self.rows:
            return None
        return {"first_expires_at": self.rows[device_hash]}

    async def fetchval(self, sql: str, *args):
        assert "INSERT INTO trial_grants" in sql
        device_hash, expires_at = args
        # ON CONFLICT DO NOTHING 의미론: 먼저 쓴 값이 정본
        self.rows.setdefault(device_hash, expires_at)
        return self.rows[device_hash]


def _now():
    return datetime.now(timezone.utc)


class TestResolveTrial:
    @pytest.mark.asyncio
    async def test_최초_부여는_기존과_동일한_90일(self):
        db = FakeConn()
        plan, expires_at = await resolve_trial(db, "device-A")

        assert plan == "free_trial"
        expected = _now() + timedelta(days=FREE_TRIAL_DAYS)
        assert abs((expires_at - expected).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_최초_부여_시_이력이_기록된다(self):
        db = FakeConn()
        await resolve_trial(db, "device-A")
        assert hash_device_id("device-A") in db.rows

    @pytest.mark.asyncio
    async def test_탈퇴_재등록해도_최초_만료일이_복원된다(self):
        """farming 차단의 본체 — 이 테스트가 깨지면 구멍이 다시 열린 것이다."""
        db = FakeConn()
        _, first = await resolve_trial(db, "device-A")

        # 탈퇴로 users/devices/subscriptions가 사라져도 trial_grants는 남는다
        plan, second = await resolve_trial(db, "device-A")

        assert second == first
        assert plan == "free_trial"  # 아직 미래라 활성

    @pytest.mark.asyncio
    async def test_이미_지난_체험은_expired로_복원된다(self):
        """plan='free_trial' + 과거 만료일로 두면 job_subscription_expire_check가
        다음 00:00 KST에 이 행을 집어 '구독 만료' Push를 한 번 더 쏜다."""
        past = _now() - timedelta(days=3)
        db = FakeConn({hash_device_id("device-A"): past})

        plan, expires_at = await resolve_trial(db, "device-A")

        assert plan == "expired"
        assert expires_at == past

    @pytest.mark.asyncio
    async def test_잔여_기간은_그대로_돌려받는다(self):
        """모드 전환 오조작으로 탈퇴한 정상 사용자가 손해 보지 않아야 한다."""
        remaining = _now() + timedelta(days=70)
        db = FakeConn({hash_device_id("device-A"): remaining})

        plan, expires_at = await resolve_trial(db, "device-A")

        assert plan == "free_trial"
        assert expires_at == remaining

    @pytest.mark.asyncio
    async def test_다른_기기는_영향받지_않는다(self):
        db = FakeConn({hash_device_id("device-A"): _now() - timedelta(days=1)})

        plan, _ = await resolve_trial(db, "device-B")

        assert plan == "free_trial"

    @pytest.mark.asyncio
    async def test_device_id가_없으면_차단하지_않는다(self):
        """devices 행이 0건인 이론적 전환 경로 — 정상 사용자를 깨는 방향으로
        실패하지 않아야 하므로 기존 동작(90일)으로 흘린다."""
        db = FakeConn()

        plan, expires_at = await resolve_trial(db, None)

        assert plan == "free_trial"
        assert expires_at > _now() + timedelta(days=FREE_TRIAL_DAYS - 1)
        assert db.rows == {}  # 이력도 남기지 않는다

    @pytest.mark.asyncio
    async def test_동시_요청은_먼저_쓴_만료일로_수렴한다(self):
        """ON CONFLICT DO UPDATE ... RETURNING이 정본을 되돌려주는지."""
        db = FakeConn()
        _, first = await resolve_trial(db, "device-A")
        _, second = await resolve_trial(db, "device-A")
        assert first == second

    @pytest.mark.asyncio
    async def test_해시는_원본_device_id를_남기지_않는다(self):
        db = FakeConn()
        await resolve_trial(db, "device-A")
        assert "device-A" not in db.rows
        assert len(next(iter(db.rows))) == 64  # sha256 hex
