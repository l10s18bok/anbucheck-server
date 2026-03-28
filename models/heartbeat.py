from pydantic import BaseModel
from typing import Optional


class HeartbeatIn(BaseModel):
    device_id: str
    timestamp: str
    manual: bool = False
    steps_delta: Optional[int] = None  # 이전 heartbeat 이후 걸음수 증가량 (권한 거부 시 null)
    suspicious: bool
    battery_level: Optional[int] = None


class HeartbeatOut(BaseModel):
    status: str
    server_time: str
    heartbeat_hour: int
    heartbeat_minute: int
