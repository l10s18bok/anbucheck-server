from fastapi import APIRouter, Depends, HTTPException, status
import asyncpg

from config import HEARTBEAT_HOUR_MIN, HEARTBEAT_HOUR_MAX
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

    # 구독 플랜 조회 (보호자 본인만)
    sub_plan = None
    if user["role"] == "guardian":
        sub_plan = await db.fetchval(
            "SELECT plan FROM subscriptions WHERE user_id = $1 ORDER BY expires_at DESC LIMIT 1",
            user["user_id"],
        )

    # 연결된 보호자 수
    guardian_count = await db.fetchval(
        "SELECT COUNT(*) FROM guardians WHERE subject_user_id = $1",
        user["user_id"],
    ) or 0

    # 보호자+대상자(G+S) 여부
    user_row = await db.fetchrow(
        "SELECT invite_code FROM users WHERE id = $1", user["user_id"]
    )
    is_also_subject = (
        user["role"] == "guardian"
        and user_row is not None
        and user_row["invite_code"] is not None
    )
    invite_code = user_row["invite_code"] if user_row else None

    return DeviceInfoOut(
        device_id=row["device_id"],
        heartbeat_hour=row["heartbeat_hour"],
        heartbeat_minute=row["heartbeat_minute"],
        last_seen=row["last_seen"].isoformat() if row["last_seen"] else None,
        subscription_active=sub_active or False,
        subscription_plan=sub_plan,
        guardian_count=guardian_count,
        is_also_subject=is_also_subject,
        invite_code=invite_code,
    )


@router.put("/fcm-token")
async def update_fcm_token(
    body: FcmTokenIn,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    # supports_push_heartbeat는 locale 유무와 무관하게 항상 기록한다 — 이 플래그가
    # 현재 실행 중인 클라를 반영해야 게이팅이 정확해진다(§구버전 하위호환).
    if body.locale:
        await db.execute(
            "UPDATE devices SET fcm_token = $1, locale = $2, "
            "supports_push_heartbeat = $3, updated_at = NOW() WHERE user_id = $4",
            body.fcm_token, body.locale, body.supports_push_heartbeat, user["user_id"],
        )
    else:
        await db.execute(
            "UPDATE devices SET fcm_token = $1, "
            "supports_push_heartbeat = $2, updated_at = NOW() WHERE user_id = $3",
            body.fcm_token, body.supports_push_heartbeat, user["user_id"],
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
    # 모델(HeartbeatScheduleIn)이 이미 같은 범위로 거르지만, 여기서도 한 번 더 막는다 —
    # 22시 이상이 DB에 들어가면 그 대상자는 미수신 판정 자체가 실행되지 않아 경고가
    # 조용히 사라진다(config.HEARTBEAT_HOUR_MAX 주석 참조). 이 값이 새는 경로는
    # 이 엔드포인트 하나뿐이므로(등록은 DEFAULT_HEARTBEAT_HOUR 상수 고정) 여기가 유일한 관문이다.
    if not (HEARTBEAT_HOUR_MIN <= h <= HEARTBEAT_HOUR_MAX):
        raise HTTPException(
            status_code=400,
            detail=f"heartbeat 시각은 {HEARTBEAT_HOUR_MIN:02d}:00~{HEARTBEAT_HOUR_MAX:02d}:59 사이여야 합니다",
        )
    if not (0 <= m <= 59):
        raise HTTPException(status_code=400, detail="heartbeat 분은 0~59 사이여야 합니다")

    # 대상자(또는 대상자 기능 활성화된 보호자)만 변경 가능
    if user["role"] == "subject":
        pass  # 대상자는 항상 허용
    elif user["role"] == "guardian":
        has_invite = await db.fetchval(
            "SELECT invite_code FROM users WHERE id = $1", user["user_id"]
        )
        if not has_invite:
            raise HTTPException(status_code=403, detail="대상자만 heartbeat 시각을 변경할 수 있습니다")
    else:
        raise HTTPException(status_code=403, detail="대상자만 heartbeat 시각을 변경할 수 있습니다")
    row = await db.fetchrow(
        "SELECT id FROM devices WHERE device_id = $1 AND user_id = $2",
        device_id, user["user_id"],
    )
    if row is None:
        raise HTTPException(status_code=403, detail="권한이 없습니다")

    await db.execute(
        """UPDATE devices SET heartbeat_hour = $1, heartbeat_minute = $2,
           updated_at = NOW() WHERE device_id = $3 AND user_id = $4""",
        h, m, device_id, user["user_id"],
    )

    return HeartbeatScheduleOut(
        device_id=device_id,
        heartbeat_hour=h,
        heartbeat_minute=m,
        message="heartbeat 시각이 변경되었습니다. 다음 확인부터 적용됩니다.",
    )
