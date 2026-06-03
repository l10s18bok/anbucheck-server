from pydantic import BaseModel, ConfigDict, Field
from typing import Optional


class HeartbeatIn(BaseModel):
    # ── heartbeat 영구 하위호환 계약 ──
    # heartbeat 수신은 어떤 앱 버전이 보내도 절대 스키마 사유로 거부하지 않는다.
    # 이 불변식 덕분에 클라이언트는 "heartbeat 전송 유도 알림(subject_safety_net/
    # gs_deadman/send_failed)으로 런치되면 버전 체크를 skip"할 수 있다 — 구버전이
    # 보낸 안부 신호도 항상 수신되므로 "보낸 줄 알았는데 실패"하는 거짓 안심이 없다.
    #   · 모르는 필드(신버전이 추가) → extra="ignore"로 무시하고 수신 (서버 스키마
    #     마이그레이션 없이 클라가 진화 가능). ⚠️ 절대 extra="forbid"로 바꾸지 말 것.
    #   · 빠진 필드(구버전이 미전송) → device_id/timestamp만 필수(없으면 처리 불가),
    #     나머지는 기본값으로 흡수.
    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(..., min_length=1, max_length=256)
    timestamp: str = Field(..., max_length=64)
    manual: bool = False
    # 걸음수는 음수가 될 수 없다(누적값). 비정상 음수는 차단.
    steps_delta: Optional[int] = Field(default=None, ge=0)  # 권한 거부 시 null
    # 빠지면 False(정상/활동확인)로 흡수 — 누락으로 400을 내지 않기 위한 안전 기본값.
    suspicious: bool = False
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
