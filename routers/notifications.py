from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
import asyncpg

from database import get_db
from middleware.auth import require_guardian
from services.alert_service import get_guardian_settings, should_send

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
    """당일 알림 목록 조회 — 보호자에 연결된 대상자의 notification_events를
    보호자 알림 설정으로 필터링하여 반환.
    헤더 X-Timezone-Offset: +09:00 형식으로 클라이언트 타임존 전달.
    """
    guardian_user_id = user["user_id"]
    utc_offset = request.headers.get("X-Timezone-Offset")
    today_start = _today_utc_start(utc_offset)

    # 보호자 알림 설정 조회
    settings = await get_guardian_settings(db, guardian_user_id)

    # 보호자에 연결된 대상자의 당일 알림 조회 (해당 보호자가 삭제한 알림 제외)
    rows = await db.fetch(
        """SELECT e.id, e.subject_user_id, e.invite_code,
                  e.alert_level, e.title, e.body,
                  e.message_key, e.message_params,
                  e.location_lat, e.location_lng,
                  e.location_accuracy, e.location_captured_at,
                  e.created_at
           FROM notification_events e
           WHERE e.subject_user_id IN (
               SELECT g.subject_user_id FROM guardians g
               WHERE g.guardian_user_id = $1
           )
             AND e.created_at >= $2
             AND NOT EXISTS (
               SELECT 1 FROM dismissed_notifications dn
               WHERE dn.guardian_user_id = $1 AND dn.event_id = e.id
             )
           ORDER BY e.created_at ASC""",
        guardian_user_id,
        today_start,
    )

    # 보호자 설정에 따라 필터링
    notifications = []
    for row in rows:
        if not should_send(settings, row["alert_level"]):
            continue
        notifications.append({
            "id": row["id"],
            "subject_user_id": row["subject_user_id"],
            "invite_code": row["invite_code"],
            "alert_level": row["alert_level"],
            "title": row["title"],
            "body": row["body"],
            "message_key": row["message_key"],
            "message_params": row["message_params"],
            "location_lat": row["location_lat"],
            "location_lng": row["location_lng"],
            "location_accuracy": row["location_accuracy"],
            "location_captured_at": row["location_captured_at"].isoformat() if row["location_captured_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })

    return {"notifications": notifications}


@router.delete("/notifications")
async def delete_all_notifications(
    request: Request,
    user=Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """당일 알림 전체 숨김 — 보호자별 dismissed_notifications에 기록 (다른 보호자에 영향 없음)"""
    guardian_user_id = user["user_id"]
    utc_offset = request.headers.get("X-Timezone-Offset")
    today_start = _today_utc_start(utc_offset)

    result = await db.execute(
        """INSERT INTO dismissed_notifications (guardian_user_id, event_id)
           SELECT $1, e.id
           FROM notification_events e
           WHERE e.subject_user_id IN (
               SELECT g.subject_user_id FROM guardians g
               WHERE g.guardian_user_id = $1
           )
             AND e.created_at >= $2
             AND NOT EXISTS (
               SELECT 1 FROM dismissed_notifications dn
               WHERE dn.guardian_user_id = $1 AND dn.event_id = e.id
             )""",
        guardian_user_id,
        today_start,
    )
    dismissed_count = int(result.split()[-1]) if result else 0
    return {"deleted_count": dismissed_count}
