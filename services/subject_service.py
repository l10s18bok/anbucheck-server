from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import HTTPException, status

from services.alias import clean_alias
from services.heartbeat_keys import log_local_date as _log_local_date


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


def sanitize_alias(raw: str | None) -> str | None:
    """별칭 정규화 (저장 시점) — 규칙은 services.alias.clean_alias가 소유한다.

    Push 본문에 그대로 실리는 값이라 저장 시점에 한 번 걸러둔다
    (렌더링 시점의 push_service.decorate_body가 같은 규칙으로 이중 방어).
    빈 값이 되면 None을 반환해 DB에 NULL로 남긴다 → 정형 문구 폴백.
    """
    return clean_alias(raw)


async def sync_aliases(
    db: asyncpg.Connection, guardian_user_id: int, aliases: dict[str, str]
) -> int:
    """보호자가 붙인 별칭을 guardians.alias에 반영 (invite_code → 별칭).

    개별 저장(1건)과 앱 업데이트 후 백필(전체)이 같은 경로를 쓴다. 요청한
    보호자 본인의 연결 행만 갱신하므로 남의 별칭을 덮어쓸 수 없고, 같은 요청을
    여러 번 보내도 결과가 같다(멱등).

    모르는 invite_code나 연결이 끊긴 항목은 조용히 무시한다 — 백필은 클라의
    로컬 맵을 통째로 올리는 방식이라 이미 해제된 대상자가 섞여 있을 수 있고,
    그것 때문에 전체 동기화가 실패하면 안 된다.
    """
    updated = 0
    for invite_code, raw_alias in aliases.items():
        code = (invite_code or "").strip().upper()
        if not code:
            continue
        result = await db.execute(
            """UPDATE guardians g
               SET alias = $1
               FROM users u
               WHERE g.subject_user_id = u.id
                 AND g.guardian_user_id = $2
                 AND u.invite_code = $3""",
            sanitize_alias(raw_alias), guardian_user_id, code,
        )
        # asyncpg의 execute는 "UPDATE n" 형태 문자열을 반환한다.
        if result.rsplit(" ", 1)[-1].isdigit():
            updated += int(result.rsplit(" ", 1)[-1])
    return updated


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

    **일자 귀속은 도착 시각(server_ts)이 아니라 그 기록이 원래 속한 날짜**(scheduled_key)로
    한다. 통신 장애로 n일 heartbeat가 보류 큐에 남았다가 n+1일에 뒤늦게 전송되면 server_ts는
    n+1일이지만 걸음수는 n일 것이므로, server_ts로 묶으면 n일 막대가 0이 되고 n+1일 막대에
    남의 값이 들어간다. scheduled_key는 기기 로컬 날짜를 담고 있어 이 오배정을 교정하며,
    이미 저장된 과거 기록에도 소급 적용된다(읽기 시점 재분류이므로 마이그레이션 불필요).
    수동 보고는 scheduled_key가 NULL이므로 server_ts로 폴백한다.
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

    # 조회 범위는 창 시작보다 하루 앞에서 뜬다 — 창 첫날에 속한 기록이 그 다음 날 뒤늦게
    # 도착했을 수 있고(server_ts가 창 안), 반대로 창 시작 직전에 도착한 기록이 창 첫날에
    # 귀속될 수도 있다(타임존 경계·시계 오차). 재분류 후 창 밖 날짜는 아래 루프에서 버려진다.
    start_utc = datetime.combine(start_date - timedelta(days=1), datetime.min.time(), tz)
    end_utc = datetime.combine(today + timedelta(days=1), datetime.min.time(), tz)

    rows = await db.fetch(
        """SELECT server_ts, steps_delta, scheduled_key
           FROM heartbeat_logs
           WHERE device_id = $1 AND server_ts >= $2 AND server_ts < $3""",
        device_id,
        start_utc,
        end_utc,
    )
    day_map: dict = {}
    for row in rows:
        local_date = _log_local_date(row["scheduled_key"], row["server_ts"], tz)
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


async def _get_own_device_row(db: asyncpg.Connection, user_id: int):
    """본인 계정의 최신 기기 행 + 가입 시각. 없으면 404."""
    row = await db.fetchrow(
        """SELECT u.created_at, d.device_id, d.timezone
           FROM users u
           LEFT JOIN devices d ON d.id = (
               SELECT id FROM devices WHERE user_id = u.id ORDER BY updated_at DESC LIMIT 1
           )
           WHERE u.id = $1""",
        user_id,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="사용자를 찾을 수 없습니다")
    return row


async def record_steps_snapshot(
    db: asyncpg.Connection,
    user_id: int,
    steps_delta: int,
    days: int,
) -> list[int | None]:
    """[내 걸음수] 버튼 — 그 시점까지의 당일 누적 걸음수를 적재하고 N일 이력을 돌려준다.

    ⚠️ **이것은 안부 보고가 아니다.** 다음 셋을 의도적으로 하지 않는다:
      · devices.last_seen 갱신 — 갱신하면 버튼을 누른 것만으로 미수신 체크(+2h)가
        무력화되어 대상자 안전망 푸시와 보호자 경고가 조용히 사라진다. 걸음수 조회가
        안부 보고를 대신했다고 착각하게 만드는 방향이라 "일관성 수정"으로 합치지 말 것.
      · devices.steps_delta / suspicious_count 갱신 — 안부 판정 입력값이다.
      · 보호자 Push(auto_report / steps) 발송.

    적재는 heartbeat_logs에 "steps_<기기 로컬 날짜>" 키로 **하루 1행**만 남긴다.
    걸음수는 자정 누적값이라 단조 증가하므로 GREATEST로 최댓값만 유지하며, 나중에 도착한
    진짜 heartbeat가 더 크면 차트의 MAX 집계에서 자연히 그쪽이 이긴다.
    이 행은 heartbeat_service의 is_first_today 판정에서 제외된다(steps% 예외).
    """
    row = await _get_own_device_row(db, user_id)
    device_id = row["device_id"]
    if not device_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="기기 정보를 찾을 수 없습니다")

    try:
        tz = ZoneInfo(row["timezone"] or "Asia/Seoul")
    except Exception:
        tz = ZoneInfo("Asia/Seoul")
    now_local = datetime.now(tz)
    key = f"steps_{now_local.date().isoformat()}"

    # ON CONFLICT 대상이 partial unique index라 추론 조건(WHERE)까지 그대로 적어야 한다.
    await db.execute(
        """INSERT INTO heartbeat_logs
           (device_id, steps_delta, suspicious, battery_level, client_ts, server_ts, scheduled_key)
           VALUES ($1, $2, 0, NULL, $3, NOW(), $4)
           ON CONFLICT (device_id, scheduled_key) WHERE scheduled_key IS NOT NULL
           DO UPDATE SET steps_delta = GREATEST(
               COALESCE(heartbeat_logs.steps_delta, 0), EXCLUDED.steps_delta
           )""",
        device_id,
        steps_delta,
        now_local.isoformat(),
        key,
    )

    return await get_step_history(
        db,
        device_id=device_id,
        tz_name=row["timezone"] or "Asia/Seoul",
        user_created_at=row["created_at"],
        days=days,
    )


async def get_step_history_for_self(
    db: asyncpg.Connection,
    user_id: int,
    days: int,
) -> list[int | None]:
    """본인(대상자 또는 G+S 보호자)의 N일 걸음수 이력.

    보호자용 get_step_history_for_subject는 guardians 링크 검증이 필수인데, 자기 자신은
    self-link이 막혀 있어 그 링크가 존재하지 않는다. 그래서 별도 진입점이 필요하다.
    """
    row = await _get_own_device_row(db, user_id)
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
