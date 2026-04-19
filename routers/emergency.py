from fastapi import APIRouter, Depends
import asyncpg

from database import get_db
from middleware.auth import require_subject_feature
from models.emergency import EmergencyIn, EmergencyOut
from services.emergency_service import process_emergency

router = APIRouter(prefix="/api/v1", tags=["emergency"])


@router.post("/emergency", response_model=EmergencyOut)
async def emergency(
    body: EmergencyIn,
    user: dict = Depends(require_subject_feature),
    db: asyncpg.Connection = Depends(get_db),
):
    result = await process_emergency(
        db,
        user["user_id"],
        body.device_id,
        location=body.location,
    )
    return EmergencyOut(**result)
