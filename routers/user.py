import asyncio
import logging

from fastapi import APIRouter, Depends
import asyncpg

from database import get_db
from middleware.auth import get_current_user
from models.user import UserRegisterIn, UserRegisterOut, SubscriptionOut
from services.user_service import register_user
from services import push_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/users", tags=["users"])


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
    )


@router.delete("/me", status_code=204)
async def delete_me(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """현재 사용자 계정 및 관련 데이터 전체 삭제 (탈퇴)"""
    user_id = user["user_id"]
    role = user["role"]

    # 대상자 탈퇴 시 연결된 보호자에게 Push 알림 (DB 저장 없음)
    if role == "subject":
        invite_row = await db.fetchrow("SELECT invite_code FROM users WHERE id = $1", user_id)
        invite_code = invite_row["invite_code"] if invite_row else "알 수 없음"
        guardians = await db.fetch(
            """SELECT d.fcm_token FROM guardians g
               JOIN devices d ON d.user_id = g.guardian_user_id
               WHERE g.subject_user_id = $1 AND d.fcm_token IS NOT NULL AND d.fcm_token != ''""",
            user_id,
        )
        if guardians:
            coros = [
                push_service.send_push(
                    g["fcm_token"],
                    "대상자 탈퇴 알림",
                    f"대상자({invite_code})가 서비스를 탈퇴했습니다.",
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

        # 보호자 알림 설정
        await db.execute("DELETE FROM guardian_notification_settings WHERE guardian_user_id = $1", user_id)

        # 해당 대상자 관련 알림 이벤트 정리
        await db.execute("DELETE FROM notification_events WHERE subject_user_id = $1", user_id)

        # 경고 알림
        await db.execute("DELETE FROM alerts WHERE subject_user_id = $1", user_id)

        # 보호자-대상자 연결
        await db.execute(
            "DELETE FROM guardians WHERE subject_user_id = $1 OR guardian_user_id = $1",
            user_id,
        )

        # 구독
        await db.execute("DELETE FROM subscriptions WHERE user_id = $1", user_id)

        # 기기
        await db.execute("DELETE FROM devices WHERE user_id = $1", user_id)

        # 사용자
        await db.execute("DELETE FROM users WHERE id = $1", user_id)
