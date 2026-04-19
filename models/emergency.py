from datetime import datetime

from pydantic import BaseModel, Field


class LocationPayload(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    accuracy_meters: float | None = Field(default=None, ge=0)
    captured_at: datetime | None = None


class EmergencyIn(BaseModel):
    device_id: str
    location: LocationPayload | None = None


class EmergencyOut(BaseModel):
    status: str
    message: str
