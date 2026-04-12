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
        logger.info(f"[보호자 알림] 발송 완료 → {title} ({fcm_token[:10]}...)")
        return True
    except Exception as e:
        logger.error(f"[보호자 알림] 발송 실패 → {title} ({fcm_token[:10]}...): {e}")
        return _is_token_error(e)


def _is_token_error(exc: Exception) -> bool:
    """토큰 오류인지 여부 반환 (True = 토큰 무효화 필요)"""
    msg = str(exc).lower()
    return "registration-token-not-registered" in msg or "invalid-registration-token" in msg


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


async def push_emergency(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR") -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_emergency_title"),
        body=get_message(locale, "push_emergency_body"),
        data={"type": "alert_emergency", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )
