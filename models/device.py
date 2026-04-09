from pydantic import BaseModel


class FcmTokenIn(BaseModel):
    fcm_token: str
    locale: str | None = None  # 기기 로케일 (e.g. 'ko_KR', 'en_US')


class HeartbeatScheduleIn(BaseModel):
    heartbeat_hour: int
    heartbeat_minute: int


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
