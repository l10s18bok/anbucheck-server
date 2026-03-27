import json
import logging
import os
from typing import Optional

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


async def push_heartbeat_trigger(fcm_token: str, platform: str) -> bool:
    """Heartbeat 트리거 Silent Push 발송 (iOS/Android 공통)"""
    messaging = _get_messaging()
    if messaging is None:
        return False
    try:
        if platform == "ios":
            message = messaging.Message(
                data={"type": "heartbeat_trigger"},
                apns=messaging.APNSConfig(
                    headers={"apns-push-type": "background", "apns-priority": "5"},
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(content_available=True)
                    ),
                ),
                token=fcm_token,
            )
        else:
            message = messaging.Message(
                data={"type": "heartbeat_trigger"},
                android=messaging.AndroidConfig(priority="high"),
                token=fcm_token,
            )
        messaging.send(message)
        return False  # 정상 발송 = 토큰 유효 (token_invalid = False)
    except Exception as e:
        logger.error(f"Heartbeat 트리거 Push 발송 실패 ({fcm_token[:10]}...): {e}")
        return _is_token_error(e)  # 토큰 오류 시에만 True


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
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=msg_data,
            android=messaging.AndroidConfig(
                notification=messaging.AndroidNotification(sound=sound)
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound=sound or "")  # 빈 문자열 = iOS 무음
                )
            ),
            token=fcm_token,
        )
        messaging.send(message)
        return True
    except Exception as e:
        logger.error(f"Push 발송 실패 ({fcm_token[:10]}...): {e}")
        return _is_token_error(e)


def _is_token_error(exc: Exception) -> bool:
    """토큰 오류인지 여부 반환 (True = 토큰 무효화 필요)"""
    msg = str(exc).lower()
    return "registration-token-not-registered" in msg or "invalid-registration-token" in msg


# 경고 Push 메시지 헬퍼

async def push_battery_low(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default") -> bool:
    return await send_push(
        fcm_token,
        title="🔋 대상자 폰 배터리 부족",
        body="폰 배터리가 10% 이하입니다. 충전이 필요할 수 있습니다.",
        data={"type": "alert_info", "reason": "battery_low", "subject_user_id": str(subject_user_id)},
        sound=sound,
    )


async def push_battery_dead(fcm_token: str, subject_user_id: int, battery_level: int, sound: Optional[str] = "default") -> bool:
    return await send_push(
        fcm_token,
        title="🔋 배터리 방전 추정",
        body=f"대상자의 폰이 배터리 방전으로 꺼진 것 같습니다. 마지막 배터리 잔량: {battery_level}%. 충전 후 자동으로 정상 복귀됩니다.",
        data={"type": "alert_info", "reason": "battery_dead", "subject_user_id": str(subject_user_id)},
        sound=sound,
    )


async def push_app_closed(fcm_token: str, subject_user_id: int) -> bool:
    return await send_push(
        fcm_token,
        title="📱 대상자 앱 확인 필요",
        body="대상자의 앱이 종료된 것 같습니다. 앱을 다시 열어달라고 안내해 주세요.",
        data={"type": "alert_info", "reason": "app_closed", "subject_user_id": str(subject_user_id)},
    )


async def push_caution(fcm_token: str, subject_user_id: int) -> bool:
    return await send_push(
        fcm_token,
        title="⚠ 안부 확인 필요",
        body="오늘 대상자의 생존확인이 아직 없습니다. 직접 안부를 확인해 보시기 바랍니다.",
        data={"type": "alert_caution", "subject_user_id": str(subject_user_id)},
    )


async def push_warning(fcm_token: str, subject_user_id: int) -> bool:
    return await send_push(
        fcm_token,
        title="⚠ 안부 확인",
        body="대상자의 오늘 생존확인이 없습니다. 통신 불가 상태일 수 있습니다.",
        data={"type": "alert_warning", "subject_user_id": str(subject_user_id)},
    )


async def push_urgent(fcm_token: str, subject_user_id: int) -> bool:
    return await send_push(
        fcm_token,
        title="🚨 긴급: 대상자 확인 필요",
        body="생존확인이 없으며 마지막 확인 시 폰 사용 흔적도 없었습니다. 즉시 확인이 필요합니다.",
        data={"type": "alert_urgent", "subject_user_id": str(subject_user_id)},
    )


async def push_urgent_secondary(fcm_token: str, subject_user_id: int) -> bool:
    return await send_push(
        fcm_token,
        title="🚨 긴급: 대상자 확인 필요",
        body="대상자의 생존확인이 없으며 다른 보호자도 아직 확인하지 않았습니다. 즉시 확인이 필요합니다.",
        data={"type": "alert_urgent", "subject_user_id": str(subject_user_id)},
    )


async def push_resolved(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default") -> bool:
    return await send_push(
        fcm_token,
        title="✅ 안부 확인",
        body="대상자의 생존확인이 정상 복귀되었습니다.",
        data={"type": "alert_resolved", "subject_user_id": str(subject_user_id)},
        sound=sound,
    )


async def push_manual_report(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default") -> bool:
    return await send_push(
        fcm_token,
        title="✅ 수동 안부 확인",
        body="대상자께서 직접 안부 확인을 보냈습니다.",
        data={"type": "manual_report", "subject_user_id": str(subject_user_id)},
        sound=sound,
    )


async def push_schedule_updated(fcm_token: str, hour: int, minute: int) -> bool:
    """대상자 기기에 heartbeat 시각 변경 Silent Push 발송"""
    messaging = _get_messaging()
    if messaging is None:
        return False
    try:
        message = messaging.Message(
            data={"type": "schedule_updated", "hour": str(hour), "minute": str(minute)},
            apns=messaging.APNSConfig(
                headers={"apns-push-type": "background", "apns-priority": "5"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(content_available=True)
                ),
            ),
            android=messaging.AndroidConfig(priority="high"),
            token=fcm_token,
        )
        messaging.send(message)
        return True
    except Exception as e:
        logger.error(f"schedule_updated Silent Push 실패 ({fcm_token[:10]}...): {e}")
        return False


async def push_wellbeing_check(fcm_token: str) -> bool:
    """대상자에게 안부 확인 Push 발송"""
    return await send_push(
        fcm_token,
        title="💛 안부 확인",
        body="잘 지내고 계시죠? 화면을 한 번 터치해 주세요.",
        data={"type": "wellbeing_check"},
    )


async def push_auto_report(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default") -> bool:
    return await send_push(
        fcm_token,
        title="✅ 오늘 생존확인 완료",
        body="대상자의 오늘 생존확인이 정상 수신되었습니다.",
        data={"type": "auto_report", "subject_user_id": str(subject_user_id)},
        sound=sound,
    )


async def push_subscription_expired(fcm_token: str) -> bool:
    return await send_push(
        fcm_token,
        title="구독 만료",
        body="무료 체험이 종료되었습니다. 계속 이용하시려면 구독을 갱신해 주세요.",
        data={"type": "subscription_expired"},
    )
