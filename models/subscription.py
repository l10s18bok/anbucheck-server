from pydantic import BaseModel


class SubscriptionOut(BaseModel):
    plan: str
    started_at: str
    expires_at: str
    days_remaining: int
    is_active: bool


class SubscriptionVerifyIn(BaseModel):
    platform: str  # android | ios
    product_id: str
    # iOS: PurchaseDetails.purchaseID (transactionId 문자열)
    # Android: PurchaseDetails.verificationData.serverVerificationData (purchaseToken)
    receipt: str


class SubscriptionVerifyOut(BaseModel):
    plan: str
    expires_at: str
    is_active: bool


class SubscriptionRestoreOut(BaseModel):
    plan: str
    expires_at: str
    is_active: bool
    restored: bool
