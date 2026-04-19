from fastapi import APIRouter, Depends
import asyncpg

from database import get_db
from middleware.auth import require_guardian
from models.guardian import (
    SubjectLinkIn, SubjectLinkOut, SubjectListOut, SubjectOut, AlertSummary, StepHistoryOut,
)
from services.subject_service import (
    link_subject, get_subjects, unlink_subject, get_step_history_for_subject,
)

router = APIRouter(prefix="/api/v1/subjects", tags=["subjects"])


@router.post("/link", response_model=SubjectLinkOut)
async def link(
    body: SubjectLinkIn,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    result = await link_subject(db, user["user_id"], body.invite_code)
    s = result["subject"]
    alert = AlertSummary(**s["alert"]) if s["alert"] else None
    return SubjectLinkOut(
        guardian_id=result["guardian_id"],
        subject=SubjectOut(
            guardian_id=s["guardian_id"],
            user_id=s["user_id"],
            invite_code=s["invite_code"],
            last_seen=s["last_seen"],
            status=s["status"],
            alert=alert,
        ),
    )


@router.get("", response_model=SubjectListOut)
async def list_subjects(
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    result = await get_subjects(db, user["user_id"])
    subjects = []
    for s in result["subjects"]:
        alert = AlertSummary(**s["alert"]) if s["alert"] else None
        subjects.append(
            SubjectOut(
                guardian_id=s["guardian_id"],
                user_id=s["user_id"],
                invite_code=s["invite_code"],
                last_seen=s["last_seen"],
                status=s["status"],
                alert=alert,
                device_id=s.get("device_id"),
                heartbeat_hour=s.get("heartbeat_hour", 18),
                heartbeat_minute=s.get("heartbeat_minute", 0),
                battery_level=s.get("battery_level"),
                weekly_steps=s.get("weekly_steps", []),
            )
        )
    return SubjectListOut(
        subjects=subjects,
        max_subjects=result["max_subjects"],
        can_add_more=result["can_add_more"],
        subscription_active=result["subscription_active"],
    )


@router.delete("/{guardian_id}/unlink")
async def unlink(
    guardian_id: int,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    await unlink_subject(db, guardian_id, user["user_id"])
    return {"message": "대상자 연결이 해제되었습니다"}


@router.get("/{subject_user_id}/step-history", response_model=StepHistoryOut)
async def step_history(
    subject_user_id: int,
    days: int = 30,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    # 7 ≤ days ≤ 30 으로 제한
    days = max(7, min(30, days))
    history = await get_step_history_for_subject(
        db, guardian_user_id=user["user_id"], subject_user_id=subject_user_id, days=days,
    )
    return StepHistoryOut(step_history=history)
