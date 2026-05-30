from pydantic import BaseModel, Field


class FcmTokenIn(BaseModel):
    fcm_token: str = Field(..., max_length=4096)
    locale: str | None = Field(default=None, max_length=32)  # 기기 로케일


class HeartbeatScheduleIn(BaseModel):
    heartbeat_hour: int = Field(..., ge=0, le=23)
    heartbeat_minute: int = Field(..., ge=0, le=59)


class HeartbeatScheduleOut(BaseModel):
    device_id: str
    heartbeat_hour: int
    heartbeat_minute: int
    message: str


class DeviceInfoOut(BaseModel):
    device_id: str
    heartbeat_hour: int
    heartbeat_minute: int
    last_seen: str | None = None
    subscription_active: bool = False
    subscription_plan: str | None = None
    guardian_count: int = 0
    is_also_subject: bool = False
    invite_code: str | None = None
