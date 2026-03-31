import logging

from fastapi import APIRouter, Depends, HTTPException, status
import asyncpg

from database import get_db
from middleware.auth import require_guardian
from models.guardian import SubjectLinkIn, SubjectLinkOut, SubjectListOut, SubjectOut, AlertSummary
from services.subject_service import link_subject, get_subjects, unlink_subject

logger = logging.getLogger(__name__)

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
                heartbeat_hour=s.get("heartbeat_hour", 9),
                heartbeat_minute=s.get("heartbeat_minute", 30),
            )
        )
    return SubjectListOut(
        subjects=subjects,
        max_subjects=result["max_subjects"],
        can_add_more=result["can_add_more"],
    )


@router.post("/{invite_code}/trigger-heartbeat")
async def trigger_heartbeat(
    invite_code: str,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """보호자가 대상자에게 수동으로 heartbeat 트리거 Silent Push 발송 (테스트/긴급 확인용)"""
    # 보호자 - 대상자 연결 확인
    row = await db.fetchrow(
        """SELECT d.fcm_token, d.platform, u.id AS subject_user_id
           FROM guardians g
           JOIN users u ON u.id = g.subject_user_id
           JOIN devices d ON d.user_id = u.id
           WHERE u.invite_code = $1
             AND g.guardian_user_id = $2
             AND d.fcm_token IS NOT NULL""",
        invite_code, user["user_id"],
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="연결된 대상자를 찾을 수 없거나 FCM 토큰이 없습니다",
        )

    logger.info(f"[Heartbeat 수동 트리거] 요청 수신 → invite_code={invite_code}, guardian_user_id={user['user_id']}, platform={row['platform']}, fcm_token={row['fcm_token'][:10]}...")

    from services.push_service import push_heartbeat_trigger
    token_invalid = await push_heartbeat_trigger(row["fcm_token"], row["platform"], label="수동 트리거")

    if token_invalid:
        logger.warning(f"[Heartbeat 수동 트리거] FCM 토큰 만료 → invite_code={invite_code}, subject_user_id={row['subject_user_id']}")
        await db.execute(
            "UPDATE devices SET fcm_token = NULL WHERE user_id = $1",
            row["subject_user_id"],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="FCM 토큰이 만료되었습니다. 대상자가 앱을 다시 열어야 합니다.",
        )

    logger.info(f"[Heartbeat 수동 트리거] 완료 → invite_code={invite_code}")
    return {"message": "heartbeat 트리거를 발송했습니다"}


@router.delete("/{guardian_id}/unlink")
async def unlink(
    guardian_id: int,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    await unlink_subject(db, guardian_id, user["user_id"])
    return {"message": "대상자 연결이 해제되었습니다"}
