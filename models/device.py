from pydantic import BaseModel, Field

from config import HEARTBEAT_HOUR_MIN, HEARTBEAT_HOUR_MAX


class FcmTokenIn(BaseModel):
    fcm_token: str = Field(..., max_length=4096)
    locale: str | None = Field(default=None, max_length=32)  # 기기 로케일


class HeartbeatScheduleIn(BaseModel):
    # 상한이 23이 아니라 21인 이유는 config.HEARTBEAT_HOUR_MAX 주석 참조
    # (22시 이상은 서버 미수신 체크가 영원히 발화하지 않는다).
    heartbeat_hour: int = Field(..., ge=HEARTBEAT_HOUR_MIN, le=HEARTBEAT_HOUR_MAX)
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
