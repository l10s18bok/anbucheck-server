from pydantic import BaseModel
from typing import Optional


class HeartbeatIn(BaseModel):
    device_id: str
    timestamp: str
    manual: bool = False
    steps_delta: Optional[int] = None  # 이전 heartbeat 이후 걸음수 증가량 (권한 거부 시 null)
    suspicious: bool
    battery_level: Optional[int] = None
    # HTTP 재전송 중복 차단용 idempotency key.
    # 자동 heartbeat: "YYYY-MM-DD_HH:MM" (예약 시각 기준). 수동 보고는 미전송(None).
    # 서버는 (device_id, scheduled_key) 조합이 이미 heartbeat_logs에 있으면
    # 부수효과(알림/Push) 없이 200 OK만 반환해 retry가 중복 알림을 만들지 않게 한다.
    scheduled_key: Optional[str] = None


class HeartbeatOut(BaseModel):
    status: str
    server_time: str
    heartbeat_hour: int
    heartbeat_minute: int
