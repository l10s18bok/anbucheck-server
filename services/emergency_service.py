import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from i18n.messages import get_message
from services import alert_service, push_service
from services.alert_service import get_guardian_settings, should_send, should_push
from services.heartbeat_service import (
    _save_notification_event,
    _get_active_guardians,
    _get_invite_code,
)

logger = logging.getLogger(__name__)


async def process_emergency(db: asyncpg.Connection, user_id: int, device_id: str) -> dict:
    """대상자가 긴급 도움 요청 버튼을 눌렀을 때 처리"""
    now_dt = datetime.now(timezone.utc)

    # 1. 대상자 정보 조회
    invite_code = await _get_invite_code(db, user_id)

    # 2. 대상자 기기 last_seen 조회
    dev_row = await db.fetchrow(
        "SELECT last_seen FROM devices WHERE user_id = $1 AND device_id = $2",
        user_id, device_id,
    )
    last_seen_dt = dev_row["last_seen"] if dev_row else now_dt

    # 3. urgent alert 즉시 생성 (기존 경고 에스컬레이션 무시)
    alert_id = await alert_service.create_alert(
        db, user_id, "urgent", last_seen_dt, days_inactive=0,
    )
    # note에 emergency 표시
    await db.execute(
        "UPDATE alerts SET note = 'emergency_request' WHERE id = $1",
        alert_id,
    )

    # 4. notification_event 저장
    await _save_notification_event(
        db, user_id, invite_code,
        "urgent",
        get_message("ko_KR", "push_emergency_title"),
        get_message("ko_KR", "push_emergency_body"),
        message_key="emergency",
    )

    # 5. 활성 보호자에게 긴급 Push 발송 (DND 무시 — urgent 등급)
    guardians = await _get_active_guardians(db, user_id)
    if guardians:
        coros = []
        for g in guardians:
            settings = await get_guardian_settings(db, g["guardian_user_id"])
            # urgent는 should_send/should_push 모두 True 반환
            if not should_send(settings, "urgent"):
                continue
            locale = g.get("locale") or "ko_KR"
            coros.append(
                push_service.push_emergency(
                    g["fcm_token"], user_id,
                    invite_code=invite_code, locale=locale,
                )
            )
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    logger.info(f"[긴급 도움 요청] user_id={user_id}, alert_id={alert_id}, 보호자 {len(guardians)}명 발송")

    return {"status": "ok", "message": "긴급 알림이 발송되었습니다"}
