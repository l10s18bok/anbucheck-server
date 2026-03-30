from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
import asyncpg

from database import get_db
from middleware.auth import require_guardian

router = APIRouter(prefix="/api/v1", tags=["notifications"])


def _today_utc_start(utc_offset_str: str | None) -> datetime:
    """클라이언트 UTC 오프셋 기준 오늘 자정을 UTC datetime으로 반환.
    오프셋 형식: '+09:00' / '-05:00' / '+00:00'
    파싱 실패 시 UTC+9 (Asia/Seoul) 기본값 사용.
    """
    try:
        sign = 1 if utc_offset_str[0] == '+' else -1
        parts = utc_offset_str[1:].split(':')
        offset_minutes = sign * (int(parts[0]) * 60 + int(parts[1]))
    except Exception:
        offset_minutes = 9 * 60  # 기본값 KST

    tz = timezone(timedelta(minutes=offset_minutes))
    now_local = datetime.now(tz)
    today_local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_local_midnight.astimezone(timezone.utc)


@router.get("/notifications")
async def get_notifications(
    request: Request,
    user=Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """당일 보호자 알림 목록 조회 (시간순)
    헤더 X-Timezone-Offset: +09:00 형식으로 클라이언트 타임존 전달.
    """
    utc_offset = request.headers.get("X-Timezone-Offset")
    today_start = _today_utc_start(utc_offset)

    rows = await db.fetch(
        """SELECT id, subject_user_id, invite_code, alert_level, title, body, is_push_sent, created_at
           FROM guardian_notifications
           WHERE guardian_user_id = $1
             AND created_at >= $2
           ORDER BY created_at ASC""",
        user["user_id"],
        today_start,
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
    request: Request,
    user=Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """당일 보호자 알림 전체 삭제"""
    utc_offset = request.headers.get("X-Timezone-Offset")
    today_start = _today_utc_start(utc_offset)

    result = await db.execute(
        """DELETE FROM guardian_notifications
           WHERE guardian_user_id = $1
             AND created_at >= $2""",
        user["user_id"],
        today_start,
    )
    deleted_count = int(result.split()[-1]) if result else 0
    return {"deleted_count": deleted_count}
