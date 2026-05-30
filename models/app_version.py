from pydantic import BaseModel, Field
from typing import Literal, Optional


class AppVersionCheckOut(BaseModel):
    platform: str
    current_version: str
    latest_version: str
    min_version: str
    force_update: bool
    store_url: str


class AppVersionUpdateIn(BaseModel):
    platform: Literal["android", "ios"]
    latest_version: str = Field(..., min_length=1, max_length=32)
    min_version: str = Field(..., min_length=1, max_length=32)
    store_url: Optional[str] = Field(default=None, max_length=512)


class AppVersionOut(BaseModel):
    platform: str
    latest_version: str
    min_version: str
    store_url: str
    updated_at: str
