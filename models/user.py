from pydantic import BaseModel, Field
from typing import Literal, Optional


class DeviceIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=256)
    fcm_token: Optional[str] = Field(default=None, max_length=4096)
    platform: Literal["android", "ios"]
    os_version: Optional[str] = Field(default=None, max_length=128)
    timezone: Optional[str] = Field(default="Asia/Seoul", max_length=64)  # IANA timezone
    locale: Optional[str] = Field(default="ko_KR", max_length=32)  # 기기 로케일


class UserRegisterIn(BaseModel):
    role: Literal["subject", "guardian"]
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
    existing_role: Optional[str] = None  # 재가입 시 기존 role (요청 role과 다를 때만 포함)
