from fastapi import APIRouter, Depends
import aiosqlite

from database import get_db
from middleware.auth import require_guardian
from models.subscription import SubscriptionOut, SubscriptionVerifyIn, SubscriptionVerifyOut, SubscriptionRestoreOut
from services.subscription_service import get_subscription, verify_subscription, restore_subscription

router = APIRouter(prefix="/api/v1/subscription", tags=["subscription"])


@router.get("", response_model=SubscriptionOut)
async def get_sub(
    user: dict = Depends(require_guardian),
    db: aiosqlite.Connection = Depends(get_db),
):
    result = await get_subscription(db, user["user_id"])
    return SubscriptionOut(**result)


@router.post("/verify", response_model=SubscriptionVerifyOut)
async def verify_sub(
    body: SubscriptionVerifyIn,
    user: dict = Depends(require_guardian),
    db: aiosqlite.Connection = Depends(get_db),
):
    result = await verify_subscription(db, user["user_id"], body.platform, body.product_id, body.receipt)
    return SubscriptionVerifyOut(**result)


@router.post("/restore", response_model=SubscriptionRestoreOut)
async def restore_sub(
    body: SubscriptionVerifyIn,
    user: dict = Depends(require_guardian),
    db: aiosqlite.Connection = Depends(get_db),
):
    result = await restore_subscription(db, user["user_id"], body.platform, body.product_id, body.receipt)
    return SubscriptionRestoreOut(**result)
