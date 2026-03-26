from pydantic import BaseModel
from typing import Optional


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
    dnd_start: Optional[str] = None  # "HH:MM" 형식
    dnd_end: Optional[str] = None    # "HH:MM" 형식
