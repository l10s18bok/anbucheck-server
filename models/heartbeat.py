from pydantic import BaseModel, Field
from typing import Optional


class HeartbeatIn(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=256)
    timestamp: str = Field(..., max_length=64)
    manual: bool = False
    # 걸음수는 음수가 될 수 없다(누적값). 비정상 음수는 차단.
    steps_delta: Optional[int] = Field(default=None, ge=0)  # 권한 거부 시 null
    suspicious: bool
    battery_level: Optional[int] = Field(default=None, ge=0, le=100)
    # HTTP 재전송 중복 차단용 idempotency key.
    # 자동 heartbeat: "YYYY-MM-DD_HH:MM" (예약 시각 기준). 수동 보고는 미전송(None).
    # 서버는 (device_id, scheduled_key) 조합이 이미 heartbeat_logs에 있으면
    # 부수효과(알림/Push) 없이 200 OK만 반환해 retry가 중복 알림을 만들지 않게 한다.
    scheduled_key: Optional[str] = Field(default=None, max_length=64)


class HeartbeatOut(BaseModel):
    status: str
    server_time: str
    heartbeat_hour: int
    heartbeat_minute: int
