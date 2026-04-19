from pydantic import BaseModel
from typing import Optional


class SubjectLinkIn(BaseModel):
    invite_code: str


class AlertSummary(BaseModel):
    id: int
    days_inactive: int


class SubjectOut(BaseModel):
    guardian_id: int
    user_id: int
    invite_code: str
    last_seen: Optional[str] = None
    status: str  # normal | warning
    alert: Optional[AlertSummary] = None
    device_id: Optional[str] = None
    heartbeat_hour: int = 18
    heartbeat_minute: int = 0
    battery_level: Optional[int] = None
    # 최근 7일 일별 걸음수. index 0 = 6일 전, index 6 = 오늘 (대상자 로컬 타임존 기준).
    # users.created_at 이전 날짜는 None, 이후인데 heartbeat 없으면 0.
    weekly_steps: list[Optional[int]] = []


class SubjectLinkOut(BaseModel):
    guardian_id: int
    subject: SubjectOut


class SubjectListOut(BaseModel):
    subjects: list[SubjectOut]
    max_subjects: int
    can_add_more: bool
    subscription_active: bool = True


class StepHistoryOut(BaseModel):
    # 최근 30일 일별 걸음수. index 0 = 29일 전, index 29 = 오늘.
    step_history: list[Optional[int]]
