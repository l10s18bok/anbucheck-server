from datetime import datetime, timedelta
import logging

import aiosqlite
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


async def get_subscription(db: aiosqlite.Connection, user_id: int) -> dict:
    async with db.execute(
        "SELECT plan, started_at, expires_at FROM subscriptions WHERE user_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="구독 정보를 찾을 수 없습니다")

    expires_dt = datetime.fromisoformat(row["expires_at"].replace("+09:00", ""))
    now = datetime.utcnow()
    is_active = expires_dt > now and row["plan"] != "expired"
    days_remaining = max(0, (expires_dt - now).days) if is_active else 0

    return {
        "plan": row["plan"],
        "started_at": row["started_at"],
        "expires_at": row["expires_at"],
        "days_remaining": days_remaining,
        "is_active": is_active,
    }


async def verify_subscription(
    db: aiosqlite.Connection,
    user_id: int,
    platform: str,
    product_id: str,
    receipt: str,
) -> dict:
    """인앱 결제 영수증 검증 (실제 검증 로직은 추후 Apple/Google API 연동)"""
    # TODO: Apple/Google 영수증 서버 검증 API 연동
    # 현재는 영수증을 DB에 저장하고 1년 구독으로 처리
    expires_at = (datetime.utcnow() + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    async with db.execute("SELECT id FROM subscriptions WHERE user_id = ?", (user_id,)) as cur:
        existing = await cur.fetchone()

    if existing:
        await db.execute(
            """UPDATE subscriptions SET plan = 'yearly', expires_at = ?,
               receipt_data = ?, platform = ?, updated_at = datetime('now')
               WHERE user_id = ?""",
            (expires_at, receipt, platform, user_id),
        )
    else:
        await db.execute(
            """INSERT INTO subscriptions (user_id, plan, expires_at, receipt_data, platform)
               VALUES (?, 'yearly', ?, ?, ?)""",
            (user_id, expires_at, receipt, platform),
        )

    await db.commit()
    return {"plan": "yearly", "expires_at": expires_at, "is_active": True}


async def restore_subscription(
    db: aiosqlite.Connection,
    user_id: int,
    platform: str,
    product_id: str,
    receipt: str,
) -> dict:
    result = await verify_subscription(db, user_id, platform, product_id, receipt)
    return {**result, "restored": True}
