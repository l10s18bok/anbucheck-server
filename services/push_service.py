import asyncio
import json
import logging
import os
from typing import Optional

from i18n.messages import get_message
from services.alias import clean_alias

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


def decorate_body(body: str, alias: Optional[str]) -> str:
    """보호자 Push 본문 앞에 대상자 별칭을 덧붙인다 — "삼촌 · 오늘 안부 확인이…".

    제목이 아니라 본문에 붙이는 이유:
    - 접힌 알림에서 제목은 절대 줄바꿈되지 않는다. 일부 기기(샤오미 등)는
      제목과 본문을 아예 한 줄에 이어붙이므로 제목이 길수록 별칭이 먼저 잘린다.
      펼친 알림에서 여러 줄로 늘어나는 건 본문뿐이라, 본문 앞에 둬야 별칭
      20자가 항상 온전히 읽힌다.
    - 접힌 상태에서는 제목 바로 뒤가 본문이므로, 본문 앞에 둬도 짧은 등급
      라벨("⚠ 주의") 직후에 그대로 붙어 나온다 — 잃는 게 없다.

    별칭은 문장 *바깥*에 구분자로만 붙인다. 문장 안에 끼워 넣으면 한국어
    주격조사 이/가(받침 의존), 러시아어·폴란드어 격변화, 터키어 소유격 모음조화,
    아랍어 정관사 같은 언어별 문법이 걸리는데, 별칭은 영문·숫자·이모지가
    올 수 있는 자유 입력이라 어떤 규칙도 세울 수 없다. 구분자로 ` · `를 쓰는 것도
    같은 이유다 — 쉼표는 아랍어 `،`·전각 `，`, 콜론은 프랑스어 앞 공백 규칙처럼
    로케일별 분기가 되살아나지만, 가운뎃점은 어느 언어의 문장부호도 아니다.

    이 함수는 모든 보호자 알림이 지나는 단일 통로(send_push) 앞단에 놓이므로
    어떤 입력에도 예외를 던지지 않는다 — alias가 없거나 비었거나 문자열이
    아니면 원본 본문을 그대로 반환해 기존 동작으로 폴백한다(구버전 앱 하위호환).
    """
    cleaned = clean_alias(alias)
    if not cleaned:
        return body
    return f"{cleaned} · {body}"


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
    notification_tag: Optional[str] = None,
    apns_collapse_id: Optional[str] = None,
) -> bool:
    """일반 Push 알림 발송.

    notification_tag: Android AndroidNotification.tag 직접 지정.
      None이면 data의 subject_user_id / invite_code 기반 자동 계산.
    apns_collapse_id: 지정 시 iOS에서 **같은 id의 이전 알림을 새 알림이 대체**한다.
      매일 반복되는 트리거 푸시가 알림센터에 쌓이지 않게 하는 용도.
      ⚠️ 보호자 경고 계열에는 절대 주지 말 것 — 경고끼리 서로 지운다.
    """
    messaging = _get_messaging()
    if messaging is None:
        return False
    try:
        msg_data = {k: str(v) for k, v in (data or {}).items()}

        # 대상자별 그룹화 키 — subject_user_id 우선, 없으면 invite_code, 둘 다 없으면 'default'
        # 앱이 포그라운드/백그라운드/종료 상태 모두에서 OS가 같은 키로 묶어 표시
        group_id = msg_data.get("subject_user_id") or msg_data.get("invite_code") or "default"
        group_key = notification_tag or f"anbu_subject_{group_id}"

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
                    **({"apns-collapse-id": apns_collapse_id} if apns_collapse_id else {}),
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

async def push_subject_safety_net(fcm_token: str, locale: str = "ko_KR") -> bool:
    """대상자 본인에게 보내는 안부유도 푸시 (Android 한정).

    heartbeat 미수신 체크 시점(예약시각 +2h)에 발송한다. 구독·보호자 유무와 무관
    (대상자 본인 안부 신호 유도이므로 보호자 경고 게이팅과 별개).
    클라는 data.type 'subject_safety_net'을 받아 safety_home으로 이동 후 미전송
    heartbeat 자동 재전송 + 안내 다이얼로그를 처리한다.
    iOS는 클라 정시 로컬알림(gs_deadman)이 PRIMARY 트리거이므로 호출부에서 제외한다.
    """
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_subject_safety_net_title"),
        body=get_message(locale, "push_subject_safety_net_body"),
        data={"type": "subject_safety_net"},
        sound="default",
        notification_tag="anbu_safety_net",  # 전용 태그 — 구독/보호자 알림의 anbu_subject_default와 분리
    )


async def push_heartbeat_trigger(fcm_token: str, locale: str = "ko_KR", collapse: bool = True) -> bool:
    """iOS 대상자 기기로 보내는 **예약시각 heartbeat 트리거 푸시**.

    iOS는 앱이 강제 종료되면 어떤 스케줄러도 돌지 않아, 킬 상태에서 실행되는 유일한
    경로가 **표시형 푸시가 띄우는 Notification Service Extension**이다(실측:
    kr.co.anbucheck/.claude/rules/ios_nse_field_notes.md). 그 확장이 이 푸시를 받아
    heartbeat를 직접 전송하므로, 사용자가 알림을 **탭하지 않아도** 안부가 전달된다.

    - 발송 시각: 예약시각 **정각**(+2h가 아니다). 미수신 체크(+2h)보다 먼저 도착해야
      거짓 미수신 경고를 막는다.
    - 대상: `platform='ios'` + G+S(`invite_code IS NOT NULL`) + **`supports_push_heartbeat`**
      + 오늘 미수신. 게이팅은 전제조건이다 — 확장이 없는 구버전에 보내면 기존
      gs_deadman 로컬 알림과 겹쳐 같은 시각에 알림이 2개 뜬다.
    - 문구는 기존 안전망 문구를 그대로 재사용한다. **확장이 성공하면 이 문구를
      "전달했습니다"로 덮어쓰므로**, 여기 있는 문구는 확장이 실행되지 못했을 때
      사용자가 보게 되는 **폴백**이다 — 그때는 탭 유도가 맞다.
    - `apns-collapse-id`로 전날 알림을 새 알림이 대체한다.
    """
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_subject_safety_net_title"),
        body=get_message(locale, "push_subject_safety_net_body"),
        data={"type": "heartbeat_push"},
        sound="default",
        notification_tag="anbu_heartbeat_push",
        # 진단용으로 끌 수 있게 둔다 — collapse-id가 전달을 막는지 A/B로 가르기 위함.
        apns_collapse_id="anbu_heartbeat_push" if collapse else None,
    )


async def push_battery_low(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None) -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_battery_low_title"),
        body=decorate_body(get_message(locale, "push_battery_low_body"), alias),
        data={"type": "alert_info", "reason": "battery_low", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_battery_dead(fcm_token: str, subject_user_id: int, battery_level: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None) -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_battery_dead_title"),
        body=decorate_body(get_message(locale, "push_battery_dead_body", battery_level=battery_level), alias),
        data={"type": "alert_info", "reason": "battery_dead", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_caution(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, reason: str = "missing", locale: str = "ko_KR", alias: str | None = None) -> bool:
    if reason == "suspicious":
        body = get_message(locale, "push_caution_suspicious_body")
    else:
        body = get_message(locale, "push_caution_missing_body")
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_caution_title"),
        body=decorate_body(body, alias),
        data={"type": "alert_caution", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_warning(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, reason: str = "missing", locale: str = "ko_KR", alias: str | None = None) -> bool:
    if reason == "suspicious":
        body = get_message(locale, "push_warning_suspicious_body")
    else:
        body = get_message(locale, "push_warning_body")
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_warning_title"),
        body=decorate_body(body, alias),
        data={"type": "alert_warning", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_urgent(fcm_token: str, subject_user_id: int, days: int = 3, sound: Optional[str] = "default", invite_code: str | None = None, reason: str = "missing", locale: str = "ko_KR", alias: str | None = None) -> bool:
    if reason == "suspicious":
        body = get_message(locale, "push_urgent_suspicious_body", days=days)
    else:
        body = get_message(locale, "push_urgent_body", days=days)
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_urgent_title"),
        body=decorate_body(body, alias),
        data={"type": "alert_urgent", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_urgent_secondary(fcm_token: str, subject_user_id: int, days: int = 3, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None) -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_urgent_title"),
        body=decorate_body(get_message(locale, "push_urgent_secondary_body", days=days), alias),
        data={"type": "alert_urgent", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_resolved(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None) -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_resolved_title"),
        body=decorate_body(get_message(locale, "push_resolved_body"), alias),
        data={"type": "alert_resolved", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_manual_report(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None) -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_manual_report_title"),
        body=decorate_body(get_message(locale, "push_manual_report_body"), alias),
        data={"type": "manual_report", "subject_user_id": str(subject_user_id), "invite_code": invite_code or ""},
        sound=sound,
    )


async def push_auto_report(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None, steps: int | None = None) -> bool:
    """정상 상태 자동 안부 확인 Push.

    본문은 걸음수가 있으면 "오늘 N보를 걸으셨습니다"로 나간다 — 보호자에게는
    "정상 수신되었습니다"보다 그날 무엇을 했는지가 더 구체적인 안심 신호이기 때문.
    제목("✅ 오늘 안부 확인 완료")은 바꾸지 않는다 — 다른 ✅ 계열 제목
    (정상 복귀·수동 안부 확인·안부 확인 완료)과 체계를 맞춰 둔 것이다.

    ⚠️ **걸음수 가드는 heartbeat_service의 steps 알림 조건과 문자 그대로 같아야 한다**
    (`steps_delta is not None and steps_delta > 0`, heartbeat_service._save_steps_info_notification
    호출부). steps==0인데 suspicious==false인 상태는 실제로 도달 가능하다 — worker
    발화 시점에 화면이 켜져 있으면(`isInteractiveAtTrigger=true`) 걸음이 0이어도
    정상으로 판정된다. 그때 "오늘 0보를 걸으셨습니다"가 나가면 안전 알림이 거짓
    안심 문구가 된다. 걸음수 권한 거부(None)도 마찬가지다. 두 경우 모두 기존
    정형 문구로 폴백한다. 가드를 두 곳에서 다르게 쓰면 푸시는 걸음수를 말하는데
    앱 알림 목록에는 걸음수 카드가 없는 불일치가 생긴다.

    ⚠️ notification_events에 저장되는 본문은 바꾸지 않는다 — 그 행은 대상자당 1건을
    모든 보호자가 공유하고, 앱 알림 목록은 message_key로 자체 번역해 그린다.
    보호자별로 달라지는 렌더링(별칭·걸음수)은 push_* 안에서만 일어난다.
    """
    if steps is not None and steps > 0:
        body = get_message(locale, "noti_steps_body", steps=f"{steps:,}")
    else:
        body = get_message(locale, "push_auto_report_body")
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_auto_report_title"),
        body=decorate_body(body, alias),
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


async def push_subscription_grace_period(fcm_token: str, locale: str = "ko_KR") -> bool:
    """Apple DID_FAIL_TO_RENEW(grace period 진입) 시점 보호자 안내.

    카드 한도초과·만료 등으로 결제 재시도 중인 동안 곧 안부 알림이 끊길 수 있음을
    보호자에게 알린다. iap_notification_service._apple_grace_period에서 호출.
    """
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_subscription_grace_period_title"),
        body=get_message(locale, "push_subscription_grace_period_body"),
        data={"type": "subscription_grace_period"},
    )


async def push_alert_cleared(fcm_token: str, subject_user_id: int, sound: Optional[str] = "default", invite_code: str | None = None, locale: str = "ko_KR", alias: str | None = None) -> bool:
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_alert_cleared_title"),
        body=decorate_body(get_message(locale, "push_alert_cleared_body"), alias),
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
    message: str | None = None,
    alias: str | None = None,
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
    # 대상자가 함께 남긴 말이 있으면 본문을 그 원문으로 치환한다(방식 A).
    # 제목은 로케일별 정형 문구를 유지하므로 긴급 상황임은 그대로 전달된다.
    note = message.strip() if message else None
    body = note if note else get_message(locale, "push_emergency_body")
    return await send_push(
        fcm_token,
        title=get_message(locale, "push_emergency_title"),
        body=decorate_body(body, alias),
        data=data,
        sound=sound,
    )
