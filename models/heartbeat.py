from pydantic import BaseModel
from typing import Optional


class HeartbeatIn(BaseModel):
    device_id: str
    timestamp: str
    source: str  # workmanager | silent_push | foreground
    accel_x: Optional[float] = None
    accel_y: Optional[float] = None
    accel_z: Optional[float] = None
    gyro_x: Optional[float] = None
    gyro_y: Optional[float] = None
    gyro_z: Optional[float] = None
    suspicious: bool
    battery_level: Optional[int] = None


class HeartbeatOut(BaseModel):
    status: str
    server_time: str
    heartbeat_hour: int
    heartbeat_minute: int
