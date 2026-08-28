from pydantic import BaseModel, Field

from config import HEARTBEAT_HOUR_MIN, HEARTBEAT_HOUR_MAX


class FcmTokenIn(BaseModel):
    fcm_token: str = Field(..., max_length=4096)
    locale: str | None = Field(default=None, max_length=32)  # 기기 로케일
    # 미지정(구버전 클라)이면 False로 기록한다 — 플래그가 **현재 실행 중인 클라**를
    # 반영해야 하므로, 신버전→구버전 다운그레이드 시 자가 치유되도록 항상 덮어쓴다.
    supports_push_heartbeat: bool = False


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


class StepsSnapshotIn(BaseModel):
    """[내 걸음수] 버튼이 올리는 그 시점까지의 당일 누적 걸음수.

    상한 200000은 하루 걸음수로 도달 불가능한 값이라 오염된 센서값·조작을 거른다
    (기네스 기록 수준의 하루 걸음도 이보다 훨씬 작다).
    """
    steps_delta: int = Field(..., ge=0, le=200000)
    days: int = Field(default=30, ge=7, le=30)


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
