from pydantic import BaseModel
from typing import Optional


class AppVersionCheckOut(BaseModel):
    platform: str
    current_version: str
    latest_version: str
    min_version: str
    force_update: bool
    store_url: str


class AppVersionUpdateIn(BaseModel):
    platform: str
    latest_version: str
    min_version: str
    store_url: Optional[str] = None


class AppVersionOut(BaseModel):
    platform: str
    latest_version: str
    min_version: str
    store_url: str
    updated_at: str
