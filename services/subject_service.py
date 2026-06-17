from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException, status


async def get_max_subjects(db: asyncpg.Connection, guardian_user_id: int) -> int:
    """보호자별 최대 대상자 등록 인원 조회 (users.max_subjects, 기본 5).

    유료 결제로 한도를 상향하는 기획이 확정되면 결제 검증 시점에
    UPDATE users SET max_subjects = N 만 해주면 이 함수가 자동으로 반영한다.
    """
    value = await db.fetchval("SELECT max_subjects FROM users WHERE id = $1", guardian_user_id)
    return value if value is not None else 5


async def link_subject(db: asyncpg.Connection, guardian_user_id: int, invite_code: str) -> dict:
    # invite_code로 대상자 조회 (role 무관 — G+S도 대상자 기능 활성화 가능)
    subject = await db.fetchrow(
        "SELECT id, invite_code FROM users WHERE invite_code = $1",
        invite_code,
    )

    if subject is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="유효하지 않은 고유 코드입니다")

    subject_user_id = subject["id"]

    # 자기 자신 연결 방지
    if subject_user_id == guardian_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="자기 자신을 대상자로 연결할 수 없습니다",
        )

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
    max_subjects = await get_max_subjects(db, guardian_user_id)
    if cnt_row["cnt"] >= max_subjects:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"대상자는 최대 {max_subjects}명까지 등록 가능합니다",
        )

    # 연결 생성
    guardian_id = await db.fetchval(
        "INSERT INTO guardians (subject_user_id, guardian_user_id) VALUES ($1, $2) RETURNING id",
        subject_user_id, guardian_user_id,
    )

    last_seen = await _get_last_seen(db, subject_user_id)
    active_alert = await _get_active_alert(db, subject_user_id)
    subject_status = active_alert["alert_level"] if active_alert else "normal"

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
        """SELECT g.id AS guardian_id, u.id AS user_id, u.invite_code, u.created_at,
                  d.last_seen, d.device_id, d.heartbeat_hour, d.heartbeat_minute,
                  d.battery_level, d.timezone
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
        weekly_steps = await get_step_history(
            db,
            device_id=row["device_id"],
            tz_name=row["timezone"] or "Asia/Seoul",
            user_created_at=row["created_at"],
            days=7,
        )
        subjects.append(
            {
                "guardian_id": row["guardian_id"],
                "user_id": row["user_id"],
                "invite_code": row["invite_code"],
                "last_seen": _to_utc_str(row["last_seen"]),
                "status": active_alert["alert_level"] if active_alert else "normal",
                "alert": active_alert,
                "device_id": row["device_id"],
                "heartbeat_hour": row["heartbeat_hour"] if row["heartbeat_hour"] is not None else 18,
                "heartbeat_minute": row["heartbeat_minute"] if row["heartbeat_minute"] is not None else 0,
                "battery_level": row["battery_level"],
                "weekly_steps": weekly_steps,
            }
        )

    # 보호자 구독 상태 조회 — plan + expires_at 이중 체크로 RTDN 누락/지연 안전망 확보.
    # 이 응답은 대상자 앱(safety_home_base_controller.dart)이 guardianConnected 표시에 사용하므로
    # plan만 보고 active=true 잘못 반환하면 대상자가 "보호자 연결됨"으로 안심하지만 실제로는
    # 알림 안 가는 production hole 발생.
    sub_row = await db.fetchrow(
        "SELECT plan, expires_at FROM subscriptions WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
        guardian_user_id,
    )
    if sub_row:
        subscription_active = (
            sub_row["plan"] in ("free_trial", "yearly")
            and sub_row["expires_at"] is not None
            and sub_row["expires_at"] > datetime.now(timezone.utc)
        )
    else:
        subscription_active = False

    max_subjects = await get_max_subjects(db, guardian_user_id)
    return {
        "subjects": subjects,
        "max_subjects": max_subjects,
        "can_add_more": len(subjects) < max_subjects,
        "subscription_active": subscription_active,
    }


async def unlink_subject(db: asyncpg.Connection, guardian_id: int, guardian_user_id: int) -> None:
    row = await db.fetchrow(
        "SELECT id, subject_user_id FROM guardians WHERE id = $1 AND guardian_user_id = $2",
        guardian_id, guardian_user_id,
    )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="연결된 대상자를 찾을 수 없습니다")

    subject_user_id = row["subject_user_id"]

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


async def get_step_history(
    db: asyncpg.Connection,
    device_id: str | None,
    tz_name: str,
    user_created_at: datetime,
    days: int,
) -> list[int | None]:
    """대상자 로컬 타임존 기준 최근 N일 일별 걸음수.

    index 0 = (N-1)일 전, 마지막 index = 오늘.
    · users.created_at 이전 날짜 → None (등록 전, 빈 막대)
    · 이후인데 heartbeat 없음 → 0
    · heartbeat 존재 → 당일 MAX(steps_delta). steps_delta는 자정 누적값이므로 MAX가 일별 총 걸음수
    """
    if not device_id:
        return [None] * days

    try:
        tz = ZoneInfo(tz_name or "Asia/Seoul")
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
        tz_name = "Asia/Seoul"
    today = datetime.now(tz).date()
    start_date = today - timedelta(days=days - 1)
    created_date = user_created_at.astimezone(tz).date()

    start_utc = datetime.combine(start_date, datetime.min.time(), tz)
    end_utc = datetime.combine(today + timedelta(days=1), datetime.min.time(), tz)

    rows = await db.fetch(
        """SELECT server_ts, steps_delta
           FROM heartbeat_logs
           WHERE device_id = $1 AND server_ts >= $2 AND server_ts < $3""",
        device_id,
        start_utc,
        end_utc,
    )
    day_map: dict = {}
    for row in rows:
        local_date = row["server_ts"].astimezone(tz).date()
        steps = row["steps_delta"] if row["steps_delta"] is not None else 0
        if steps > day_map.get(local_date, 0):
            day_map[local_date] = steps

    result: list[int | None] = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        if d < created_date:
            result.append(None)
        else:
            result.append(day_map.get(d) or 0)
    return result


async def get_step_history_for_subject(
    db: asyncpg.Connection,
    guardian_user_id: int,
    subject_user_id: int,
    days: int,
) -> list[int | None]:
    """보호자가 연결된 대상자의 N일 걸음수 이력 조회 (권한 검증 포함)."""
    link = await db.fetchrow(
        "SELECT id FROM guardians WHERE subject_user_id = $1 AND guardian_user_id = $2",
        subject_user_id, guardian_user_id,
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="연결된 대상자를 찾을 수 없습니다",
        )

    row = await db.fetchrow(
        """SELECT u.created_at, d.device_id, d.timezone
           FROM users u
           LEFT JOIN devices d ON d.id = (
               SELECT id FROM devices WHERE user_id = u.id ORDER BY updated_at DESC LIMIT 1
           )
           WHERE u.id = $1""",
        subject_user_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="대상자를 찾을 수 없습니다")

    return await get_step_history(
        db,
        device_id=row["device_id"],
        tz_name=row["timezone"] or "Asia/Seoul",
        user_created_at=row["created_at"],
        days=days,
    )


async def _get_active_alert(db: asyncpg.Connection, subject_user_id: int) -> dict | None:
    row = await db.fetchrow(
        "SELECT id, alert_level, days_inactive FROM alerts WHERE subject_user_id = $1 AND status = 'active' ORDER BY created_at DESC LIMIT 1",
        subject_user_id,
    )
    if row is None:
        return None
    return {"id": row["id"], "alert_level": row["alert_level"], "days_inactive": row["days_inactive"]}
