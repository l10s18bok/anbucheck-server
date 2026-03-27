from datetime import datetime

import asyncpg
from fastapi import HTTPException, status
from config import MAX_SUBJECTS


async def link_subject(db: asyncpg.Connection, guardian_user_id: int, invite_code: str) -> dict:
    # invite_code로 대상자 조회
    subject = await db.fetchrow(
        "SELECT id, invite_code FROM users WHERE invite_code = $1 AND role = 'subject'",
        invite_code,
    )

    if subject is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유효하지 않은 고유 코드입니다")

    subject_user_id = subject["id"]

    # 이미 연결됐는지 확인
    existing = await db.fetchrow(
        "SELECT id FROM guardians WHERE subject_user_id = $1 AND guardian_user_id = $2",
        subject_user_id, guardian_user_id,
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 연결된 대상자입니다")

    # 현재 연결된 대상자 수 확인
    cnt_row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM guardians WHERE guardian_user_id = $1",
        guardian_user_id,
    )
    if cnt_row["cnt"] >= MAX_SUBJECTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"대상자는 최대 {MAX_SUBJECTS}명까지 등록 가능합니다",
        )

    # 연결 생성
    guardian_id = await db.fetchval(
        "INSERT INTO guardians (subject_user_id, guardian_user_id) VALUES ($1, $2) RETURNING id",
        subject_user_id, guardian_user_id,
    )

    last_seen = await _get_last_seen(db, subject_user_id)
    active_alert = await _get_active_alert(db, subject_user_id)
    subject_status = "warning" if active_alert else "normal"

    return {
        "guardian_id": guardian_id,
        "subject": {
            "guardian_id": guardian_id,
            "user_id": subject_user_id,
            "invite_code": invite_code,
            "last_seen": last_seen,
            "status": subject_status,
            "alert": active_alert,
        },
    }


async def get_subjects(db: asyncpg.Connection, guardian_user_id: int) -> dict:
    rows = await db.fetch(
        """SELECT g.id AS guardian_id, u.id AS user_id, u.invite_code,
                  d.last_seen, d.device_id, d.heartbeat_hour, d.heartbeat_minute
           FROM guardians g
           JOIN users u ON g.subject_user_id = u.id
           LEFT JOIN devices d ON d.id = (
               SELECT id FROM devices WHERE user_id = u.id ORDER BY updated_at DESC LIMIT 1
           )
           WHERE g.guardian_user_id = $1""",
        guardian_user_id,
    )

    subjects = []
    for row in rows:
        active_alert = await _get_active_alert(db, row["user_id"])
        subjects.append(
            {
                "guardian_id": row["guardian_id"],
                "user_id": row["user_id"],
                "invite_code": row["invite_code"],
                "last_seen": _to_utc_str(row["last_seen"]),
                "status": "warning" if active_alert else "normal",
                "alert": active_alert,
                "device_id": row["device_id"],
                "heartbeat_hour": row["heartbeat_hour"] if row["heartbeat_hour"] is not None else 9,
                "heartbeat_minute": row["heartbeat_minute"] if row["heartbeat_minute"] is not None else 30,
            }
        )

    return {
        "subjects": subjects,
        "max_subjects": MAX_SUBJECTS,
        "can_add_more": len(subjects) < MAX_SUBJECTS,
    }


async def unlink_subject(db: asyncpg.Connection, guardian_id: int, guardian_user_id: int) -> None:
    row = await db.fetchrow(
        "SELECT id FROM guardians WHERE id = $1 AND guardian_user_id = $2",
        guardian_id, guardian_user_id,
    )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="연결된 대상자를 찾을 수 없습니다")

    await db.execute("DELETE FROM guardians WHERE id = $1", guardian_id)


def _to_utc_str(dt) -> str | None:
    """DB에서 가져온 datetime 값을 ISO 8601 UTC(Z 접미사)로 변환."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    return str(dt).replace(" ", "T") + "Z"


async def _get_last_seen(db: asyncpg.Connection, subject_user_id: int) -> str | None:
    row = await db.fetchrow(
        "SELECT last_seen FROM devices WHERE user_id = $1", subject_user_id
    )
    return _to_utc_str(row["last_seen"]) if row else None


async def _get_active_alert(db: asyncpg.Connection, subject_user_id: int) -> dict | None:
    row = await db.fetchrow(
        "SELECT id, days_inactive FROM alerts WHERE subject_user_id = $1 AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        subject_user_id,
    )
    if row is None:
        return None
    return {"id": row["id"], "days_inactive": row["days_inactive"]}
