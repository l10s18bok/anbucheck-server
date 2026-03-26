from pydantic import BaseModel


class FcmTokenIn(BaseModel):
    fcm_token: str


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
