from fastapi import APIRouter, Depends, HTTPException, status
import aiosqlite

from database import get_db
from middleware.auth import get_current_user
from models.device import FcmTokenIn, HeartbeatScheduleIn, HeartbeatScheduleOut, DeviceInfoOut
from services.push_service import push_schedule_updated

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])


@router.get("/me", response_model=DeviceInfoOut)
async def get_my_device(
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        """SELECT device_id, heartbeat_hour, heartbeat_minute, last_seen
           FROM devices WHERE user_id = ?
           ORDER BY updated_at DESC LIMIT 1""",
        (user["user_id"],),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="기기 정보를 찾을 수 없습니다")
    return DeviceInfoOut(
        device_id=row["device_id"],
        heartbeat_hour=row["heartbeat_hour"],
        heartbeat_minute=row["heartbeat_minute"],
        last_seen=row["last_seen"],
    )


@router.put("/fcm-token")
async def update_fcm_token(
    body: FcmTokenIn,
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    await db.execute(
        "UPDATE devices SET fcm_token = ?, updated_at = datetime('now') WHERE user_id = ?",
        (body.fcm_token, user["user_id"]),
    )
    await db.commit()
    return {"message": "FCM 토큰이 갱신되었습니다"}


@router.api_route("/{device_id}/heartbeat-schedule", methods=["PATCH", "PUT"], response_model=HeartbeatScheduleOut)
async def update_heartbeat_schedule(
    device_id: str,
    body: HeartbeatScheduleIn,
    user: dict = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    # 유효한 시간대 확인 (06:00 ~ 21:00)
    h, m = body.heartbeat_hour, body.heartbeat_minute
    if not (6 <= h <= 21):
        raise HTTPException(status_code=400, detail="heartbeat 시각은 06:00~21:00 사이여야 합니다")
    if not (0 <= m <= 59):
        raise HTTPException(status_code=400, detail="heartbeat 분은 0~59 사이여야 합니다")

    if user["role"] == "subject":
        # 본인 기기만 변경 가능
        async with db.execute(
            "SELECT id FROM devices WHERE device_id = ? AND user_id = ?",
            (device_id, user["user_id"]),
        ) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="권한이 없습니다")
    elif user["role"] == "guardian":
        # 연결된 대상자의 기기인지 확인
        async with db.execute(
            """SELECT d.id FROM devices d
               JOIN guardians g ON g.subject_user_id = d.user_id
               WHERE d.device_id = ? AND g.guardian_user_id = ?""",
            (device_id, user["user_id"]),
        ) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=403, detail="권한이 없습니다")

    await db.execute(
        """UPDATE devices SET heartbeat_hour = ?, heartbeat_minute = ?,
           updated_at = datetime('now') WHERE device_id = ?""",
        (h, m, device_id),
    )
    await db.commit()

    # 보호자가 변경한 경우 대상자 기기로 Silent Push 발송 (즉시 반영)
    if user["role"] == "guardian":
        async with db.execute(
            "SELECT fcm_token FROM devices WHERE device_id = ?",
            (device_id,),
        ) as cur:
            row = await cur.fetchone()
        if row and row["fcm_token"]:
            await push_schedule_updated(row["fcm_token"], h, m)

    return HeartbeatScheduleOut(
        device_id=device_id,
        heartbeat_hour=h,
        heartbeat_minute=m,
        message="heartbeat 시각이 변경되었습니다. 다음 확인부터 적용됩니다.",
    )
