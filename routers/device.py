from fastapi import APIRouter, Depends, HTTPException, status
import asyncpg

from database import get_db
from middleware.auth import get_current_user
from models.device import FcmTokenIn, HeartbeatScheduleIn, HeartbeatScheduleOut, DeviceInfoOut

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


@router.get("/me", response_model=DeviceInfoOut)
async def get_my_device(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        "SELECT device_id, heartbeat_hour, heartbeat_minute, last_seen FROM devices WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 1",
        user["user_id"],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="기기 정보를 찾을 수 없습니다")

    # 구독 활성 여부: 보호자는 본인 구독, 대상자는 연결된 보호자 중 활성 구독 존재 여부
    if user["role"] == "subject":
        sub_active = await db.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM guardians g
                 JOIN subscriptions s ON s.user_id = g.guardian_user_id
                 WHERE g.subject_user_id = $1
                   AND s.plan != 'expired'
                   AND s.expires_at > NOW()
               )""",
            user["user_id"],
        )
    else:
        sub_active = await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM subscriptions WHERE user_id = $1 AND plan != 'expired' AND expires_at > NOW())",
            user["user_id"],
        )

    return DeviceInfoOut(
        device_id=row["device_id"],
        heartbeat_hour=row["heartbeat_hour"],
        heartbeat_minute=row["heartbeat_minute"],
        last_seen=row["last_seen"].isoformat() if row["last_seen"] else None,
        subscription_active=sub_active or False,
    )


@router.put("/fcm-token")
async def update_fcm_token(
    body: FcmTokenIn,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    await db.execute(
        "UPDATE devices SET fcm_token = $1, updated_at = NOW() WHERE user_id = $2",
        body.fcm_token, user["user_id"],
    )
    return {"message": "FCM 토큰이 갱신되었습니다"}


@router.api_route("/{device_id}/heartbeat-schedule", methods=["PATCH", "PUT"], response_model=HeartbeatScheduleOut)
async def update_heartbeat_schedule(
    device_id: str,
    body: HeartbeatScheduleIn,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    h, m = body.heartbeat_hour, body.heartbeat_minute
    if not (0 <= h <= 23):
        raise HTTPException(status_code=400, detail="heartbeat 시각은 00:00~23:59 사이여야 합니다")
    if not (0 <= m <= 59):
        raise HTTPException(status_code=400, detail="heartbeat 분은 0~59 사이여야 합니다")

    # 대상자 본인 기기만 변경 가능
    if user["role"] != "subject":
        raise HTTPException(status_code=403, detail="대상자만 heartbeat 시각을 변경할 수 있습니다")
    row = await db.fetchrow(
        "SELECT id FROM devices WHERE device_id = $1 AND user_id = $2",
        device_id, user["user_id"],
    )
    if row is None:
        raise HTTPException(status_code=403, detail="권한이 없습니다")

    await db.execute(
        """UPDATE devices SET heartbeat_hour = $1, heartbeat_minute = $2,
           updated_at = NOW() WHERE device_id = $3""",
        h, m, device_id,
    )

    return HeartbeatScheduleOut(
        device_id=device_id,
        heartbeat_hour=h,
        heartbeat_minute=m,
        message="heartbeat 시각이 변경되었습니다. 다음 확인부터 적용됩니다.",
    )
