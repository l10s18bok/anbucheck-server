from datetime import datetime, timezone
import logging

import asyncpg
from fastapi import HTTPException, status

import config
from services.iap_verify_service import (
    GOOGLE_ACTIVE_STATES,
    acknowledge_google_purchase,
    verify_apple_transaction,
    verify_google_purchase,
)

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


async def _verify_and_persist(
    db: asyncpg.Connection,
    user_id: int,
    platform: str,
    receipt: str,
) -> dict:
    """플랫폼별 영수증을 검증하고 subscriptions 테이블에 반영한다.

    검증 통과 조건:
      · 응답 productId == config.IAP_PRODUCT_ID
      · 만료일 > 현재 시각
      · (Android) state ∈ {ACTIVE, IN_GRACE_PERIOD}

    receipt_data 컬럼에는 정규화된 식별자만 저장한다:
      · iOS:     originalTransactionId
      · Android: purchaseToken

    이렇게 두면 향후 RTDN(8단계) 수신 시 식별자로 user를 역매핑할 수 있다.
    """
    if platform not in ("ios", "android"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="지원하지 않는 플랫폼입니다",
        )

    if platform == "ios":
        info = await verify_apple_transaction(receipt)
        normalized_receipt = info["original_transaction_id"]
    else:  # android
        info = await verify_google_purchase(receipt)
        # 활성 상태가 아니면 entitlement 부여 거부
        if info["state"] not in GOOGLE_ACTIVE_STATES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"구독이 활성 상태가 아닙니다: {info['state']}",
            )
        # ACK 대기 중이면 즉시 ack — 3일 경과 시 자동 환불 방지
        if info["ack_state"] == "ACKNOWLEDGEMENT_STATE_PENDING":
            await acknowledge_google_purchase(info["product_id"], receipt)
        normalized_receipt = info["purchase_token"]

    # 응답 productId가 우리가 기대한 상품과 일치하는지 서버에서 재검증
    # (클라이언트가 보낸 product_id를 그대로 믿지 않음 — 다른 상품 영수증으로 yearly entitlement 우회 차단)
    if info["product_id"] != config.IAP_PRODUCT_ID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"잘못된 구독 상품입니다: {info['product_id']}",
        )

    expires_at_dt: datetime = info["expires_at"]
    if expires_at_dt <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="구독이 이미 만료되었습니다",
        )

    existing = await db.fetchrow(
        "SELECT id FROM subscriptions WHERE user_id = $1", user_id
    )
    if existing:
        await db.execute(
            """UPDATE subscriptions SET plan = 'yearly', expires_at = $1,
               receipt_data = $2, platform = $3, updated_at = NOW()
               WHERE user_id = $4""",
            expires_at_dt, normalized_receipt, platform, user_id,
        )
    else:
        await db.execute(
            """INSERT INTO subscriptions (user_id, plan, expires_at, receipt_data, platform)
               VALUES ($1, 'yearly', $2, $3, $4)""",
            user_id, expires_at_dt, normalized_receipt, platform,
        )

    return {
        "plan": "yearly",
        "expires_at": expires_at_dt.isoformat(),
        "is_active": True,
    }


async def verify_subscription(
    db: asyncpg.Connection,
    user_id: int,
    platform: str,
    product_id: str,
    receipt: str,
) -> dict:
    """인앱 결제 영수증 검증 후 구독 활성화."""
    # 입력 product_id는 응답 productId 재검증에 사용되지 않는다 — 클라가 위조해도
    # _verify_and_persist 내부에서 Apple/Google 응답값으로 다시 검증한다.
    return await _verify_and_persist(db, user_id, platform, receipt)


async def restore_subscription(
    db: asyncpg.Connection,
    user_id: int,
    platform: str,
    product_id: str,
    receipt: str,
) -> dict:
    """앱 재설치 후 구독 복원 — 동일 검증을 거쳐 새 device_token에 entitlement 부여."""
    result = await _verify_and_persist(db, user_id, platform, receipt)
    return {**result, "restored": True}
