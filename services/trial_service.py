"""무료 체험 부여 이력 — 탈퇴 후 재등록으로 체험을 반복 수령하는 것을 차단한다.

## 왜 device_id인가

탈퇴(`DELETE /api/v1/users/me`)는 `users`·`devices`·`subscriptions`를 하드 삭제한다.
따라서 "이 사람이 체험을 이미 썼는가"를 기억할 수 있는 키는 `user_id`가 아니라
기기 식별자뿐이다. `trial_grants`는 계정 삭제와 무관하게 남는 유일한 테이블이며,
⚠️ `delete_me`의 DELETE 목록에 절대 넣지 말 것 — 넣는 순간 구멍이 다시 열린다.

## 해시 — pepper를 쓰지 않는 이유

원본 `device_id`를 남기지 않는 것이 해싱의 목적이고, 그건 순수 sha256으로 달성된다.
pepper를 도입하면 방어력은 "DB 유출자가 특정 기기의 체험 사용 여부를 역산"하는
좁은 위협에만 더해지는 반면, **값을 잃거나 로테이션하는 순간 이력 전체가 무효화되어
farming이 조용히 재개된다**(로테이션 = 전원 사면). env 미설정 시 빈 문자열로
폴백하면 나중에 누가 값을 넣는 것만으로 같은 일이 아무 로그 없이 벌어진다.
그 운영 리스크가 얻는 것보다 크다고 판단해 pepper를 두지 않는다.

## 부여 지점은 2곳뿐이다 (둘 다 이 모듈을 경유해야 한다)

1. `services/user_service.register_user` — 신규 가입 + role=guardian
2. `routers/user.switch_to_guardian` — 대상자 → 보호자 전환

②를 빠뜨리면 "subject로 가입 → switch-to-guardian → 탈퇴" 루프로 ①이 그대로
우회된다. 새 부여 경로를 추가할 때는 반드시 여기를 통할 것.
(`services/subscription_service`의 INSERT는 `yearly`(유료)라 대상이 아니다.)
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import asyncpg

from config import FREE_TRIAL_DAYS


def hash_device_id(device_id: str) -> str:
    """device_id → sha256 hex. 이 규칙이 유일한 출처다(백필도 이 함수를 쓴다)."""
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()


async def resolve_trial(
    db: asyncpg.Connection, device_id: str | None
) -> tuple[str, datetime]:
    """이 기기에 부여할 (plan, expires_at)을 결정한다.

    · 이력 없음 → 신규 90일(`free_trial`) + 이력 기록
    · 이력 있음 → 최초 만료일을 그대로 복원.
      이미 지난 날짜면 plan을 'expired'로 넣는다 — 'free_trial' + 과거 만료일로
      두면 `scheduler.job_subscription_expire_check`가 다음 00:00 KST에 이 행을
      집어 "구독 만료" Push를 한 번 더 쏜다.

    device_id가 없으면(이론상 devices 행이 0건인 전환 경로) 기존 동작대로 90일을
    부여한다 — 차단은 정상 사용자를 깨는 방향으로 실패하지 않아야 한다.
    """
    now = datetime.now(timezone.utc)
    fresh = now + timedelta(days=FREE_TRIAL_DAYS)

    if not device_id:
        return "free_trial", fresh

    device_hash = hash_device_id(device_id)
    row = await db.fetchrow(
        "SELECT first_expires_at FROM trial_grants WHERE device_id_hash = $1",
        device_hash,
    )

    if row is not None:
        first_expires_at: datetime = row["first_expires_at"]
        plan = "expired" if first_expires_at <= now else "free_trial"
        return plan, first_expires_at

    # 최초 부여 — 이력 기록. 동시 요청 레이스는 ON CONFLICT로 흡수하되,
    # 먼저 쓴 쪽의 만료일이 정본이므로 그 값을 다시 읽어 반환한다.
    recorded = await db.fetchval(
        """INSERT INTO trial_grants (device_id_hash, first_expires_at, source)
           VALUES ($1, $2, 'grant')
           ON CONFLICT (device_id_hash) DO UPDATE
             SET device_id_hash = EXCLUDED.device_id_hash
           RETURNING first_expires_at""",
        device_hash, fresh,
    )
    expires_at = recorded or fresh
    plan = "expired" if expires_at <= now else "free_trial"
    return plan, expires_at
