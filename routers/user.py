import asyncio
import logging

from fastapi import APIRouter, Depends, Query
import asyncpg

from database import get_db
from middleware.auth import get_current_user, require_guardian
from models.user import UserRegisterIn, UserRegisterOut, SubscriptionOut
from services.user_service import register_user, generate_unique_invite_code
from services import push_service
from i18n.messages import get_message
from config import DEFAULT_HEARTBEAT_HOUR, DEFAULT_HEARTBEAT_MINUTE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/check-device")
async def check_device(
    device_id: str = Query(..., description="기기 고유 ID"),
    db: asyncpg.Connection = Depends(get_db),
):
    """기존 등록 여부 확인 (순수 조회, 데이터 수정 없음)"""
    row = await db.fetchrow(
        "SELECT u.role, u.invite_code FROM devices d JOIN users u ON u.id = d.user_id WHERE d.device_id = $1",
        device_id,
    )
    if row is None:
        return {"exists": False}
    return {"exists": True, "role": row["role"], "has_invite_code": row.get("invite_code") is not None}


@router.post("", response_model=UserRegisterOut, status_code=201)
async def register(body: UserRegisterIn, db: asyncpg.Connection = Depends(get_db)):
    if body.role not in ("subject", "guardian"):
        from fastapi import HTTPException, status
        raise HTTPException(status_code=400, detail="role은 'subject' 또는 'guardian'이어야 합니다")

    result = await register_user(db, body.role, body.device.model_dump())

    sub = None
    if result["subscription"]:
        sub = SubscriptionOut(**result["subscription"])

    return UserRegisterOut(
        user_id=result["user_id"],
        device_token=result["device_token"],
        invite_code=result["invite_code"],
        subscription=sub,
        existing_role=result.get("existing_role"),
    )


@router.post("/enable-subject")
async def enable_subject(
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """보호자가 대상자 기능을 활성화 (G+S)"""
    user_id = user["user_id"]

    # 이미 활성화 여부 확인
    existing = await db.fetchrow(
        "SELECT invite_code FROM users WHERE id = $1", user_id
    )
    if existing and existing["invite_code"]:
        return {
            "invite_code": existing["invite_code"],
            "heartbeat_hour": DEFAULT_HEARTBEAT_HOUR,
            "heartbeat_minute": DEFAULT_HEARTBEAT_MINUTE,
            "message": "이미 안부 보호가 활성화되어 있습니다",
        }

    # invite_code 발급
    invite_code = await generate_unique_invite_code(db)

    async with db.transaction():
        await db.execute(
            "UPDATE users SET invite_code = $1, updated_at = NOW() WHERE id = $2",
            invite_code, user_id,
        )
        # heartbeat 스케줄 기본값 설정
        await db.execute(
            """UPDATE devices SET heartbeat_hour = $1, heartbeat_minute = $2,
               suspicious_count = 0, updated_at = NOW()
               WHERE user_id = $3""",
            DEFAULT_HEARTBEAT_HOUR, DEFAULT_HEARTBEAT_MINUTE, user_id,
        )

    # 현재 heartbeat 시각 조회
    dev = await db.fetchrow(
        "SELECT heartbeat_hour, heartbeat_minute FROM devices WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 1",
        user_id,
    )

    return {
        "invite_code": invite_code,
        "heartbeat_hour": dev["heartbeat_hour"] if dev else DEFAULT_HEARTBEAT_HOUR,
        "heartbeat_minute": dev["heartbeat_minute"] if dev else DEFAULT_HEARTBEAT_MINUTE,
        "message": "안부 보호가 활성화되었습니다",
    }


@router.delete("/disable-subject")
async def disable_subject(
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    """보호자가 대상자 기능을 해제 (G+S → G)"""
    user_id = user["user_id"]

    # invite_code 확인
    row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    if not row or not row["invite_code"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="안부 보호가 활성화되어 있지 않습니다")

    invite_code = row["invite_code"]

    # 나를 보호하는 보호자에게 Push 알림
    guardians = await db.fetch(
        """SELECT DISTINCT ON (g.guardian_user_id) d.fcm_token, d.locale FROM guardians g
           JOIN devices d ON d.user_id = g.guardian_user_id
           WHERE g.subject_user_id = $1 AND d.fcm_token IS NOT NULL AND d.fcm_token != ''
           ORDER BY g.guardian_user_id, d.updated_at DESC""",
        user_id,
    )
    if guardians:
        coros = [
            push_service.send_push(
                g["fcm_token"],
                get_message(g.get("locale") or "ko_KR", "push_subject_withdrawn_title"),
                get_message(g.get("locale") or "ko_KR", "push_subject_withdrawn_body", invite_code=invite_code),
                data={"type": "subject_withdrawn", "invite_code": invite_code},
            )
            for g in guardians
        ]
        await asyncio.gather(*coros, return_exceptions=True)

    async with db.transaction():
        # invite_code 삭제
        await db.execute(
            "UPDATE users SET invite_code = NULL, updated_at = NOW() WHERE id = $1",
            user_id,
        )
        # 나를 보호하는 보호자 연결 해제
        await db.execute(
            "DELETE FROM guardians WHERE subject_user_id = $1", user_id
        )
        # 대상자 관련 경고/알림 삭제
        await db.execute("DELETE FROM alerts WHERE subject_user_id = $1", user_id)
        await db.execute("DELETE FROM notification_events WHERE subject_user_id = $1", user_id)
        # heartbeat 관련 필드 초기화
        await db.execute(
            """UPDATE devices SET suspicious_count = 0, steps_delta = NULL,
               battery_level = NULL, updated_at = NOW()
               WHERE user_id = $1""",
            user_id,
        )

    return {"message": "안부 보호가 해제되었습니다"}


@router.delete("/me", status_code=204)
async def delete_me(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """현재 사용자 계정 및 관련 데이터 전체 삭제 (탈퇴)"""
    user_id = user["user_id"]
    role = user["role"]

    # invite_code 유무 확인 — G+S 사용자의 대상자 데이터 삭제 판단용
    invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
    has_invite_code = invite_row and invite_row["invite_code"]

    # 대상자 데이터가 있으면 (role=subject 또는 G+S) 연결된 보호자에게 Push
    if has_invite_code:
        invite_code = invite_row["invite_code"]
        guardians = await db.fetch(
            """SELECT d.fcm_token, d.locale FROM guardians g
               JOIN devices d ON d.user_id = g.guardian_user_id
               WHERE g.subject_user_id = $1 AND d.fcm_token IS NOT NULL AND d.fcm_token != ''""",
            user_id,
        )
        if guardians:
            coros = [
                push_service.send_push(
                    g["fcm_token"],
                    get_message(g.get("locale") or "ko_KR", "push_subject_withdrawn_title"),
                    get_message(g.get("locale") or "ko_KR", "push_subject_withdrawn_body", invite_code=invite_code),
                    data={"type": "subject_withdrawn", "invite_code": invite_code},
                )
                for g in guardians
            ]
            await asyncio.gather(*coros, return_exceptions=True)

    # 연결된 기기의 device_id 목록 조회 (heartbeat_logs 삭제용)
    device_ids = await db.fetch(
        "SELECT device_id FROM devices WHERE user_id = $1", user_id
    )

    async with db.transaction():
        # heartbeat_logs (device_id 기준)
        for row in device_ids:
            await db.execute(
                "DELETE FROM heartbeat_logs WHERE device_id = $1", row["device_id"]
            )

        # ── 대상자 데이터 삭제 (invite_code 있으면) ──
        if has_invite_code:
            await db.execute("DELETE FROM notification_events WHERE subject_user_id = $1", user_id)
            await db.execute("DELETE FROM alerts WHERE subject_user_id = $1", user_id)
            await db.execute("DELETE FROM guardians WHERE subject_user_id = $1", user_id)

        # ── 보호자 데이터 삭제 (role=guardian이면) ──
        if role == "guardian":
            await db.execute("DELETE FROM guardian_notification_settings WHERE guardian_user_id = $1", user_id)
            await db.execute("DELETE FROM dismissed_notifications WHERE guardian_user_id = $1", user_id)
            await db.execute("DELETE FROM guardians WHERE guardian_user_id = $1", user_id)
            await db.execute("DELETE FROM subscriptions WHERE user_id = $1", user_id)

        # ── role=subject인 경우 (순수 대상자) 이전 호환 ──
        if role == "subject":
            await db.execute("DELETE FROM notification_events WHERE subject_user_id = $1", user_id)
            await db.execute("DELETE FROM alerts WHERE subject_user_id = $1", user_id)
            await db.execute("DELETE FROM guardians WHERE subject_user_id = $1", user_id)

        # ── 공통 삭제 ──
        await db.execute("DELETE FROM devices WHERE user_id = $1", user_id)
        await db.execute("DELETE FROM users WHERE id = $1", user_id)
