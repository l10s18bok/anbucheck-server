from typing import Optional
from fastapi import APIRouter, Depends, Query
import asyncpg

from database import get_db
from middleware.auth import require_guardian
from models.alert import AlertListOut, AlertOut, ClearAllIn, ClearAllOut
from services.alert_service import get_active_alerts, clear_alert, clear_all_alerts

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("", response_model=AlertListOut)
async def list_alerts(
    subject_user_id: Optional[int] = Query(None),
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await get_active_alerts(db, user["user_id"], subject_user_id)
    alerts = [
        AlertOut(
            id=r["id"],
            subject_user_id=r["subject_user_id"],
            invite_code=r["invite_code"],
            status=r["status"],
            days_inactive=r["days_inactive"],
            last_seen_at=r["last_seen_at"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return AlertListOut(alerts=alerts)


@router.put("/{alert_id}/clear")
async def clear_one(
    alert_id: int,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    await clear_alert(db, alert_id, user["user_id"])
    return {"message": "경고가 클리어되었습니다. 이후 다음 지정 시각에 미수신 시 새로운 경고가 발생합니다."}


@router.put("/clear-all", response_model=ClearAllOut)
async def clear_all(
    body: ClearAllIn,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    result = await clear_all_alerts(db, body.subject_user_id, user["user_id"])
    return ClearAllOut(**result)
