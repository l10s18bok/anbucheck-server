from pydantic import BaseModel
from typing import Optional


class AlertOut(BaseModel):
    id: int
    subject_user_id: int
    invite_code: str
    status: str
    days_inactive: int
    last_seen_at: str
    created_at: str


class AlertListOut(BaseModel):
    alerts: list[AlertOut]


class ClearAllIn(BaseModel):
    subject_user_id: int


class ClearAllOut(BaseModel):
    cleared_count: int
    cleared_levels: list[str]
    cleared_by: int
    cleared_at: str
    adaptive_cycle_reset: bool
    message: str
