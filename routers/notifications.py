from fastapi import APIRouter, Depends
import asyncpg

from database import get_db
from middleware.auth import require_guardian

router = APIRouter(prefix="/api/v1", tags=["notifications"])


@router.get("/notifications")
async def get_notifications(
    user=Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """당일 보호자 알림 목록 조회 (시간순)"""

    rows = await db.fetch(
        """SELECT id, subject_user_id, invite_code, alert_level, title, body, is_push_sent, created_at
           FROM guardian_notifications
           WHERE guardian_user_id = $1
             AND created_at >= CURRENT_DATE AT TIME ZONE 'Asia/Seoul'
           ORDER BY created_at ASC""",
        user["user_id"],
    )

    notifications = [
        {
            "id": row["id"],
            "subject_user_id": row["subject_user_id"],
            "invite_code": row["invite_code"],
            "alert_level": row["alert_level"],
            "title": row["title"],
            "body": row["body"],
            "is_push_sent": row["is_push_sent"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]

    return {"notifications": notifications}


@router.delete("/notifications")
async def delete_all_notifications(
    user=Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """당일 보호자 알림 전체 삭제"""
    result = await db.execute(
        """DELETE FROM guardian_notifications
           WHERE guardian_user_id = $1
             AND created_at >= CURRENT_DATE AT TIME ZONE 'Asia/Seoul'""",
        user["user_id"],
    )
    deleted_count = int(result.split()[-1]) if result else 0
    return {"deleted_count": deleted_count}
