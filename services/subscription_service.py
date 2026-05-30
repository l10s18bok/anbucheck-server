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

    이 식별자는 RTDN 수신(`services/iap_notification_service.py`) 시
    `subscriptions.receipt_data = $1`로 user를 역매핑하는 키로 사용된다.
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

    # 단일 활성 entitlement 불변식 — 같은 영수증(iOS originalTransactionId /
    # Android purchaseToken)이 다른 user_id에 이미 바인딩돼 있으면 그 바인딩을 회수한 뒤
    # 현재 호출자에게 이전한다.
    #   · 회수하지 않으면: A가 B의 purchaseToken을 /verify로 제출 → 영수증 정당성만
    #     검증할 뿐 "누가 샀는지"는 모르므로 A·B 둘 다 yearly 활성화되는 구독 도용/공유 성립.
    #   · 단순 거부하지 않는 이유: 기기 변경(새 device_id ⇒ 새 user_id) 후 정상 복원도
    #     "다른 user_id로의 이전"이라 막으면 정상 사용자가 깨진다. 스토어 의미론(구독 1개 =
    #     동시 1계정 활성)에 맞춰 last-writer-wins로 이전한다.
    #   · 이전 row의 receipt_data를 NULL로 비워 RTDN(receipt_data 기준 매핑)이 두 row를
    #     동시에 갱신하는 모호성도 함께 제거한다.
    # 회수+바인딩을 하나의 트랜잭션으로 묶어 원자적으로 처리한다.
    async with db.transaction():
        await db.execute(
            """UPDATE subscriptions SET plan = 'expired', receipt_data = NULL, updated_at = NOW()
               WHERE receipt_data = $1 AND user_id != $2""",
            normalized_receipt, user_id,
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
