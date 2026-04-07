from pydantic import BaseModel


class EmergencyIn(BaseModel):
    device_id: str


class EmergencyOut(BaseModel):
    status: str
    message: str
