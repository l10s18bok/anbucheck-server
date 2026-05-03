import asyncio
import json
import logging
import os
from typing import Optional

from i18n.messages import get_message

logger = logging.getLogger(__name__)

_firebase_app = None


def _init_firebase() -> None:
    global _firebase_app
    if _firebase_app is not None:
        return

    try:
        import firebase_admin
        from firebase_admin import credentials

        creds_json = os.getenv("FIREBASE_CREDENTIALS", "")
        if not creds_json:
            logger.warning("FIREBASE_CREDENTIALS 환경변수가 설정되지 않았습니다. Push 기능이 비활성화됩니다.")
            return

        cred_dict = json.loads(creds_json)
        cred = credentials.Certificate(cred_dict)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("Firebase 초기화 완료")
    except Exception as e:
        logger.error(f"Firebase 초기화 실패: {e}")


def _get_messaging():
    _init_firebase()
    if _firebase_app is None:
        return None
    from firebase_admin import messaging
    return messaging


# FCM data["type"] → 로그 레벨 라벨. ASCII 코드로 grep 친화적.
_LEVEL_LABELS = {
    "alert_caution": "CAUTION",
    "alert_warning": "WARNING",
    "alert_urgent": "URGENT",
    "alert_emergency": "EMERGENCY",
    "alert_info": "INFO",
    "alert_resolved": "NORMAL",
    "alert_cleared": "NORMAL",
    "auto_report": "NORMAL",
    "manual_report": "NORMAL",
    "subscription_expired": "EXPIRED",
}

# 대상자 invite_code를 함께 로깅할 레벨 (보호자가 어느 대상자 때문에 알림을 받았는지 식별용)
_LEVELS_WITH_SUBJECT = {"CAUTION", "WARNING", "URGENT", "EMERGENCY"}


def _format_push_log_prefix(fcm_token: str, data: Optional[dict]) -> str:
    d = data or {}
    msg_type = str(d.get("type") or "")
    label = _LEVEL_LABELS.get(msg_type, msg_type or "OTHER")
    parts = [f"[보호자 알림] {label}"]
    if label in _LEVELS_WITH_SUBJECT:
        invite = str(d.get("invite_code") or "") or "?"
        parts.append(f"대상자={invite}")
    parts.append(f"({fcm_token[:10]}...)")
    return " ".join(parts)


async def send_push(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    sound: Optional[str] = "default",
) -> bool:
    """일반 Push 알림 발송"""
    messaging = _get_messaging()
    if messaging is None:
        return False
    try:
        msg_data = {k: str(v) for k, v in (data or {}).items()}

        # 대상자별 그룹화 키 — subject_user_id 우선, 없으면 invite_code, 둘 다 없으면 'default'
        # 앱이 포그라운드/백그라운드/종료 상태 모두에서 OS가 같은 키로 묶어 표시
        group_id = msg_data.get("subject_user_id") or msg_data.get("invite_code") or "default"
        group_key = f"anbu_subject_{group_id}"

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=msg_data,
            android=messaging.AndroidConfig(
                priority="high",  # Doze 모드에서도 즉시 전달
                notification=messaging.AndroidNotification(
                    sound=sound,
                    channel_id="anbu_alerts",  # 앱 종료 시 OS가 직접 표시할 채널
                    tag=group_key,  # 같은 대상자 알림 그룹화 (Android notification group)
                )
            ),
            apns=messaging.APNSConfig(
                headers={
                    "apns-priority": "10",  # 즉시 전달 (배터리 절약 무시)
                    "apns-push-type": "alert",  # 알림 표시형 Push
                },
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound=sound or "default",
                        content_available=True,  # 백그라운드 수신 보장
                        mutable_content=True,  # 알림 서비스 확장 허용
                        thread_id=group_key,  # iOS 알림센터 스레드 그룹화
                    )
                )
            ),
            token=fcm_token,
        )
        await asyncio.to_thread(messaging.send, message)
        logger.info(f"{_format_push_log_prefix(fcm_token, data)} 발송 완료")
        return True
    except Exception as e:
        logger.error(f"{_format_push_log_prefix(fcm_token, data)} 발송 실패: {e}")
        if _is_dead_token_error(e):
            await _invalidate_fcm_token(fcm_token)
        return False


def _is_dead_token_error(exc: Exception) -> bool:
    """FCM 토큰이 영구적으로 죽었는지 판정 (재시도 무의미)"""
    # firebase-admin 예외 타입 우선 판정
    try:
        from firebase_admin import messaging as _m, exceptions as _fx
        if isinstance(exc, (_m.UnregisteredError, _m.SenderIdMismatchError)):
            return True
        if isinstance(exc, _fx.NotFoundError):
            return True
    except Exception:
        pass
    # 문자열 폴백 (FCM v1 에러 메시지)
    msg = str(exc).lower()
    return (
        "registration-token-not-registered" in msg
        or "invalid-registration-token" in msg
        or "requested entity was not found" in msg
        or "unregistered" in msg
    )


async def _invalidate_fcm_token(fcm_token: str) -> None:
    """죽은 FCM 토큰을 devices 테이블에서 NULL 처리하여 이후 발송 시도 차단"""
    try:
        from database import get_pool
        pool = get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE devices SET fcm_token = NULL, updated_at = NOW() WHERE fcm_token = $1",
                fcm_token,
            )
        logger.info(f"[FCM 토큰 무효화] {fcm_token[:10]}... → NULL 처리 ({result})")
    except Exception as e:
        logger.error(f"[FCM 토큰 무효화 실패] {fcm_token[:10]}...: {e}")


# ── locale 기반 경고 Push 메시지 헬퍼 ──

async def push_battery_low(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_battery_low_title"),
        body=get_message(locale, "push_battery_low_body"),
        data={"type": "alert_info", "reason": "battery_low", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_battery_dead(fcm_token: str, subject_user_id: int, battery_level: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_battery_dead_title"),
        body=get_message(locale, "push_battery_dead_body", battery_level=battery_level),
        data={"type": "alert_info", "reason": "battery_dead", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_caution(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, reason: str = "missing", locale: str = "ko_KR") -> bool:
    if reason == "suspicious":
        body = get_message(locale, "push_caution_suspicious_body")
    else:
        body = get_message(locale, "push_caution_missing_body")
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_caution_title"),
        body=body,
        data={"type": "alert_caution", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_warning(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_warning_title"),
        body=get_message(locale, "push_warning_body"),
        data={"type": "alert_warning", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_urgent(fcm_token: str, subject_user_id: int, days: int = 3, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_urgent_title"),
        body=get_message(locale, "push_urgent_body", days=days),
        data={"type": "alert_urgent", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_urgent_secondary(fcm_token: str, subject_user_id: int, days: int = 3, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_urgent_title"),
        body=get_message(locale, "push_urgent_secondary_body", days=days),
        data={"type": "alert_urgent", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_resolved(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_resolved_title"),
        body=get_message(locale, "push_resolved_body"),
        data={"type": "alert_resolved", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_manual_report(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_manual_report_title"),
        body=get_message(locale, "push_manual_report_body"),
        data={"type": "manual_report", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_auto_report(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_auto_report_title"),
        body=get_message(locale, "push_auto_report_body"),
        data={"type": "auto_report", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_subscription_expired(fcm_token: str, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_subscription_expired_title"),
        body=get_message(locale, "push_subscription_expired_body"),
        data={"type": "subscription_expired"},
    )


async def push_alert_cleared(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_alert_cleared_title"),
        body=get_message(locale, "push_alert_cleared_body"),
        data={"type": "alert_cleared", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_emergency(
    fcm_token: str,
    subject_user_id: int,
    sound: Optional[str] = "default",
    invite_code: str | None = None,
    locale: str = "ko_KR",
    lat: float | None = None,
    lng: float | None = None,
    accuracy: float | None = None,
) -> bool:
    data: dict = {
        "type": "alert_emergency",
        "subject_user_id": str(subject_user_id),
        "invite_code": invite_code or "",
    }
    # FCM data는 모두 문자열이어야 하며, 값이 있을 때만 키를 포함한다.
    if lat is not None:
        data["lat"] = str(round(lat, 6))
    if lng is not None:
        data["lng"] = str(round(lng, 6))
    if accuracy is not None:
        data["accuracy"] = str(round(accuracy, 2))
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_emergency_title"),
        body=get_message(locale, "push_emergency_body"),
        data=data,
        sound=sound,
    )
