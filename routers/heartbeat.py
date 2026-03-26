from fastapi import APIRouter, Depends
import aiosqlite

from database import get_db
from middleware.auth import get_current_user
from models.heartbeat import HeartbeatIn, HeartbeatOut
from services.heartbeat_service import process_heartbeat

router = APIRouter(prefix="/api/v1", tags=["heartbeat"])


@router.post("/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    body: HeartbeatIn,
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    result = await process_heartbeat(db, user["user_id"], body.model_dump())
    return HeartbeatOut(**result)
