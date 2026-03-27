import aiosqlite
from fastapi import HTTPException, status
from config import MAX_SUBJECTS


async def link_subject(db: aiosqlite.Connection, guardian_user_id: int, invite_code: str) -> dict:
    # invite_code로 대상자 조회
    async with db.execute(
        "SELECT id, invite_code FROM users WHERE invite_code = ? AND role = 'subject'",
        (invite_code,),
    ) as cur:
        subject = await cur.fetchone()

    if subject is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유효하지 않은 고유 코드입니다")

    subject_user_id = subject["id"]

    # 이미 연결됐는지 확인
    async with db.execute(
        "SELECT id FROM guardians WHERE subject_user_id = ? AND guardian_user_id = ?",
        (subject_user_id, guardian_user_id),
    ) as cur:
        if await cur.fetchone() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 연결된 대상자입니다")

    # 현재 연결된 대상자 수 확인
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM guardians WHERE guardian_user_id = ?",
        (guardian_user_id,),
    ) as cur:
        row = await cur.fetchone()
        if row["cnt"] >= MAX_SUBJECTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"대상자는 최대 {MAX_SUBJECTS}명까지 등록 가능합니다",
            )

    # 연결 생성
    async with db.execute(
        "INSERT INTO guardians (subject_user_id, guardian_user_id) VALUES (?, ?)",
        (subject_user_id, guardian_user_id),
    ) as cur:
        guardian_id = cur.lastrowid

    await db.commit()

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


async def get_subjects(db: aiosqlite.Connection, guardian_user_id: int) -> dict:
    async with db.execute(
        """SELECT g.id AS guardian_id, u.id AS user_id, u.invite_code,
                  d.last_seen, d.device_id, d.heartbeat_hour, d.heartbeat_minute
           FROM guardians g
           JOIN users u ON g.subject_user_id = u.id
           LEFT JOIN devices d ON d.id = (
               SELECT id FROM devices WHERE user_id = u.id ORDER BY updated_at DESC LIMIT 1
           )
           WHERE g.guardian_user_id = ?""",
        (guardian_user_id,),
    ) as cur:
        rows = await cur.fetchall()

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


async def unlink_subject(db: aiosqlite.Connection, guardian_id: int, guardian_user_id: int) -> None:
    async with db.execute(
        "SELECT id FROM guardians WHERE id = ? AND guardian_user_id = ?",
        (guardian_id, guardian_user_id),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="연결된 대상자를 찾을 수 없습니다")

    await db.execute("DELETE FROM guardians WHERE id = ?", (guardian_id,))
    await db.commit()


def _to_utc_str(dt_str: str | None) -> str | None:
    """DB에서 가져온 UTC datetime 문자열을 ISO 8601 UTC(Z 접미사)로 변환.
    SQLite datetime()은 공백 구분자를 사용하므로 T로 정규화 후 Z 추가."""
    if dt_str is None:
        return None
    return dt_str.replace(' ', 'T') + 'Z'


async def _get_last_seen(db: aiosqlite.Connection, subject_user_id: int) -> str | None:
    async with db.execute(
        "SELECT last_seen FROM devices WHERE user_id = ?", (subject_user_id,)
    ) as cur:
        row = await cur.fetchone()
    return _to_utc_str(row["last_seen"]) if row else None


async def _get_active_alert(db: aiosqlite.Connection, subject_user_id: int) -> dict | None:
    async with db.execute(
        "SELECT id, days_inactive FROM alerts WHERE subject_user_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        (subject_user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "days_inactive": row["days_inactive"]}
