from pydantic import BaseModel, Field
from typing import Optional

# "HH:MM" 24시간 형식 — 잘못된 값이 들어오면 alert_service의 DND 파싱이 500으로
# 터지므로 모델 단계에서 차단한다.
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class NotificationSettingsOut(BaseModel):
    all_enabled: bool
    urgent_enabled: bool
    warning_enabled: bool
    caution_enabled: bool
    info_enabled: bool
    dnd_enabled: bool
    dnd_start: Optional[str] = None  # "HH:MM" 형식
    dnd_end: Optional[str] = None    # "HH:MM" 형식


class NotificationSettingsIn(BaseModel):
    all_enabled: bool
    urgent_enabled: bool
    warning_enabled: bool
    caution_enabled: bool
    info_enabled: bool
    dnd_enabled: bool
    dnd_start: Optional[str] = Field(default=None, pattern=_HHMM)  # "HH:MM"
    dnd_end: Optional[str] = Field(default=None, pattern=_HHMM)    # "HH:MM"
