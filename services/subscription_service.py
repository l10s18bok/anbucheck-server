from datetime import datetime, timedelta, timezone
import logging

import asyncpg
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


async def get_subscription(db: asyncpg.Connection, user_id: int) -> dict:
    row = await db.fetchrow(
        "SELECT plan, started_at, expires_at FROM subscriptions WHERE user_id = $1",
        user_id,
    )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="구독 정보를 찾을 수 없습니다")

    expires_dt = row["expires_at"]  # TIMESTAMPTZ → datetime with tz
    now = datetime.now(timezone.utc)
    is_active = expires_dt > now and row["plan"] != "expired"
    days_remaining = max(0, (expires_dt - now).days) if is_active else 0

    return {
        "plan": row["plan"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "days_remaining": days_remaining,
        "is_active": is_active,
    }


async def verify_subscription(
    db: asyncpg.Connection,
    user_id: int,
    platform: str,
    product_id: str,
    receipt: str,
) -> dict:
    """인앱 결제 영수증 검증 (실제 검증 로직은 추후 Apple/Google API 연동)"""
    # TODO: Apple/Google 영수증 서버 검증 API 연동
    # 현재는 영수증을 DB에 저장하고 1년 구독으로 처리
    expires_at_dt = datetime.now(timezone.utc) + timedelta(days=365)

    existing = await db.fetchrow(
        "SELECT id FROM subscriptions WHERE user_id = $1", user_id
    )

    if existing:
        await db.execute(
            """UPDATE subscriptions SET plan = 'yearly', expires_at = $1,
               receipt_data = $2, platform = $3, updated_at = NOW()
               WHERE user_id = $4""",
            expires_at_dt, receipt, platform, user_id,
        )
    else:
        await db.execute(
            """INSERT INTO subscriptions (user_id, plan, expires_at, receipt_data, platform)
               VALUES ($1, 'yearly', $2, $3, $4)""",
            user_id, expires_at_dt, receipt, platform,
        )

    return {"plan": "yearly", "expires_at": expires_at_dt.isoformat(), "is_active": True}


async def restore_subscription(
    db: asyncpg.Connection,
    user_id: int,
    platform: str,
    product_id: str,
    receipt: str,
) -> dict:
    result = await verify_subscription(db, user_id, platform, product_id, receipt)
    return {**result, "restored": True}
