"""전역 일관 rate limiting (Postgres 기반).

인메모리(slowapi 등) 대신 DB 카운터를 쓰는 이유: 앱이 여러 인스턴스
(Railway 레플리카 / uvicorn 워커)로 돌 때 인메모리 카운터는 인스턴스별이라
실효 한도가 N배로 샌다. Postgres 고정 윈도우 + UPSERT 원자 증가로 모든
인스턴스가 같은 카운터를 공유한다.

윈도우: 60초 고정. bucket 예) "register:1.2.3.4", "link:42".
"""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
from fastapi import HTTPException, Request, status

WINDOW_SECONDS = 60


def client_ip(request: Request) -> str:
    """클라이언트 IP 추정.

    ⚠️ best-effort: Railway 같은 리버스 프록시 뒤에서는 X-Forwarded-For를 봐야
    하지만, XFF의 leftmost는 클라이언트가 위조할 수 있다 (프록시가 append하므로).
    따라서 이 IP 기반 제한은 위조 가능한 공격자를 완전히 막지는 못하고,
    스크립트성 무차별 호출·재시도 폭주 같은 비정교한 남용을 차단하는 용도다.
    더 강한 보장이 필요하면 인증 기반 키(user_id)를 쓴다.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


async def enforce(db: asyncpg.Connection, bucket: str, limit: int) -> None:
    """고정 60s 윈도우에서 bucket의 요청 수를 1 증가시키고 한도 초과 시 429.

    UPSERT는 (bucket, window_start) PK 충돌 시 DO UPDATE로 원자 증가하므로
    여러 인스턴스가 같은 ms에 들어와도 Postgres 행 락으로 직렬화된다.
    """
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    window_start = datetime.fromtimestamp(epoch - (epoch % WINDOW_SECONDS), tz=timezone.utc)

    count = await db.fetchval(
        """INSERT INTO rate_limits (bucket, window_start, count)
           VALUES ($1, $2, 1)
           ON CONFLICT (bucket, window_start)
           DO UPDATE SET count = rate_limits.count + 1
           RETURNING count""",
        bucket, window_start,
    )

    if count > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        )
