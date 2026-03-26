from pydantic import BaseModel
from typing import Optional


class DeviceIn(BaseModel):
    device_id: str
    fcm_token: Optional[str] = None
    platform: str  # android | ios
    os_version: Optional[str] = None


class UserRegisterIn(BaseModel):
    role: str  # subject | guardian
    device: DeviceIn


class SubscriptionOut(BaseModel):
    plan: str
    expires_at: str
    is_active: bool


class UserRegisterOut(BaseModel):
    user_id: int
    device_token: str
    invite_code: Optional[str] = None
    subscription: Optional[SubscriptionOut] = None
