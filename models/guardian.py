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
    heartbeat_hour: int = 9
    heartbeat_minute: int = 30
    battery_level: Optional[int] = None


class SubjectLinkOut(BaseModel):
    guardian_id: int
    subject: SubjectOut


class SubjectListOut(BaseModel):
    subjects: list[SubjectOut]
    max_subjects: int
    can_add_more: bool
