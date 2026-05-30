from pydantic import BaseModel, Field
from typing import Literal


class SubscriptionOut(BaseModel):
    plan: str
    started_at: str
    expires_at: str
    days_remaining: int
    is_active: bool


class SubscriptionVerifyIn(BaseModel):
    platform: Literal["android", "ios"]
    product_id: str = Field(..., min_length=1, max_length=256)
    # iOS: PurchaseDetails.purchaseID (transactionId 문자열)
    # Android: PurchaseDetails.verificationData.serverVerificationData (purchaseToken)
    receipt: str = Field(..., min_length=1, max_length=8192)


class SubscriptionVerifyOut(BaseModel):
    plan: str
    expires_at: str
    is_active: bool


class SubscriptionRestoreOut(BaseModel):
    plan: str
    expires_at: str
    is_active: bool
    restored: bool
