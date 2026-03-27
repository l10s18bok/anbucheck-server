from fastapi import APIRouter, Depends
import asyncpg

from database import get_db
from models.user import UserRegisterIn, UserRegisterOut, SubscriptionOut
from services.user_service import register_user

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.post("", response_model=UserRegisterOut, status_code=201)
async def register(body: UserRegisterIn, db: asyncpg.Connection = Depends(get_db)):
    if body.role not in ("subject", "guardian"):
        from fastapi import HTTPException, status
        raise HTTPException(status_code=400, detail="role은 'subject' 또는 'guardian'이어야 합니다")

    result = await register_user(db, body.role, body.device.model_dump())

    sub = None
    if result["subscription"]:
        sub = SubscriptionOut(**result["subscription"])

    return UserRegisterOut(
        user_id=result["user_id"],
        device_token=result["device_token"],
        invite_code=result["invite_code"],
        subscription=sub,
    )
