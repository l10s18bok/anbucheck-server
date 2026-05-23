"""인앱 결제 서버 알림(RTDN) 수신 엔드포인트.

두 외부 발신자가 호출하며, 둘 다 device_token 인증 대상이 아니다 (외부 시스템):
- Google Cloud Pub/Sub Push (RTDN) — OIDC JWT bearer 토큰으로 발신자 인증
- Apple S2S Notifications V2 — App Store Connect URL로 직접 호출, signedPayload JWS 서명 검증

원칙 (advisor 합의):
1) 인증 미들웨어(require_*) 사용 금지 — Pub/Sub/Apple은 device_token 없음.
2) 서명 검증 통과 후에는 비즈니스 로직 실패해도 무조건 2xx 반환 — Pub/Sub 무한 retry 방지.
   알 수 없는 type, 매핑 실패, IAP API 재조회 실패 모두 graceful 로그만 남기고 200.
3) 멱등성은 서비스 레이어의 UPSERT/UPDATE로 자연 처리.
4) subscriptions 매핑 키는 `receipt_data` (iOS originalTransactionId / Android purchaseToken).
5) 서명 검증 자체 실패만 401/403 — 외부 발신자가 부적절한 토큰을 보낸 진짜 거부 케이스.
"""
from __future__ import annotations

import base64
import json
import logging

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

import config
from database import get_db
from services.iap_notification_service import (
    handle_apple_notification,
    handle_google_notification,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/iap", tags=["iap_notification"])


# ─────────────────────────────────────────
# Google Pub/Sub Push (RTDN)
# ─────────────────────────────────────────

def _verify_pubsub_oidc(authorization: str | None) -> None:
    """Pub/Sub Push subscription의 OIDC JWT bearer 토큰 검증.

    Pub/Sub Push는 subscription 생성 시 지정한 service account의 ID 토큰을
    Authorization: Bearer <jwt> 헤더로 함께 전송한다.

    검증 항목:
    - 서명: Google 공개키 (google-auth가 자동 처리)
    - audience: subscription 생성 시 지정한 값과 일치
    - email: subscription 생성 시 지정한 service account 이메일과 일치
    """
    if not config.PUBSUB_AUDIENCE or not config.PUBSUB_SERVICE_ACCOUNT_EMAIL:
        # 환경변수 미설정 시 인증 우회를 막기 위해 명시적 거부.
        logger.error("PUBSUB_AUDIENCE 또는 PUBSUB_SERVICE_ACCOUNT_EMAIL 미설정")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pub/Sub 인증 설정이 누락되었습니다",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization 헤더 누락",
        )
    token = authorization.split(" ", 1)[1].strip()

    from google.oauth2 import id_token
    from google.auth.transport import requests as g_requests

    try:
        claims = id_token.verify_oauth2_token(
            token, g_requests.Request(), audience=config.PUBSUB_AUDIENCE
        )
    except Exception as e:
        logger.warning("Pub/Sub OIDC 검증 실패: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Pub/Sub OIDC 토큰 검증에 실패했습니다",
        )

    if claims.get("email") != config.PUBSUB_SERVICE_ACCOUNT_EMAIL:
        logger.warning("Pub/Sub 발신자 이메일 불일치: %s", claims.get("email"))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="허용되지 않은 발신자입니다",
        )

    if not claims.get("email_verified", False):
        logger.warning("Pub/Sub 발신자 email_verified=False")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="발신자 이메일이 검증되지 않았습니다",
        )


@router.post("/google-notifications")
async def google_rtdn(
    request: Request,
    authorization: str | None = Header(None),
    db: asyncpg.Connection = Depends(get_db),
):
    """Google Play RTDN 수신.

    Pub/Sub Push envelope:
      {
        "message": {
          "data": "<base64-encoded JSON>",
          "messageId": "...",
          "publishTime": "..."
        },
        "subscription": "projects/PROJECT/subscriptions/SUBSCRIPTION"
      }
    """
    _verify_pubsub_oidc(authorization)

    # 서명 검증 이후로는 어떤 실패도 2xx로 반환 — Pub/Sub retry 폭주 방지.
    try:
        envelope = await request.json()
    except Exception as e:
        logger.warning("Pub/Sub envelope JSON 파싱 실패: %s", e)
        return {"status": "ok", "kind": "bad_envelope"}

    message = envelope.get("message") or {}
    data_b64 = message.get("data")
    if not data_b64:
        logger.info("Pub/Sub message.data 없음 — 무시")
        return {"status": "ok", "kind": "empty"}

    try:
        decoded_bytes = base64.b64decode(data_b64)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as e:
        logger.warning("Pub/Sub data 디코딩 실패: %s", e)
        return {"status": "ok", "kind": "bad_data"}

    # 다른 앱의 알림이 잘못 들어오는 경우 방어
    package_name = payload.get("packageName", "")
    if package_name and package_name != config.GOOGLE_PACKAGE_NAME:
        logger.warning("Google RTDN 다른 패키지 알림 수신: %s", package_name)
        return {"status": "ok", "kind": "wrong_package"}

    try:
        result = await handle_google_notification(db, payload)
        return result
    except Exception as e:
        # 서비스 레이어에서 의도치 않은 예외 발생 — 2xx로 마무리하되 로그로 추적.
        logger.exception("Google RTDN 처리 중 예외: %s", e)
        return {"status": "ok", "kind": "handler_error"}


# ─────────────────────────────────────────
# Apple App Store Server Notifications V2
# ─────────────────────────────────────────

@router.post("/apple-notifications")
async def apple_s2s(
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """Apple S2S V2 알림 수신.

    Apple은 다음 형식으로 JSON 한 줄을 보낸다:
      { "signedPayload": "<JWS>" }

    JWS x5c 헤더의 인증서 체인을 Apple Root CA로 검증해야 한다.
    SignedDataVerifier(app-store-server-library)가 검증 + payload 디코딩을 한 번에 수행.
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.warning("Apple S2S body 파싱 실패: %s", e)
        # 서명 검증 이전이지만 형식 자체가 깨진 경우는 400.
        raise HTTPException(status_code=400, detail="잘못된 요청 본문")

    signed_payload = body.get("signedPayload")
    if not signed_payload:
        raise HTTPException(status_code=400, detail="signedPayload 누락")

    decoded = _verify_apple_signed_payload(signed_payload)

    # 서명 검증 통과 — 이후는 2xx 유지.
    try:
        result = await handle_apple_notification(db, decoded)
        return result
    except Exception as e:
        logger.exception("Apple S2S 처리 중 예외: %s", e)
        return {"status": "ok", "kind": "handler_error"}


def _verify_apple_signed_payload(signed_payload: str) -> dict:
    """Apple SignedDataVerifier로 signedPayload 검증 + 디코딩.

    Apple Root CA 번들을 디스크에서 로드한다. 위치는 환경변수 APPLE_ROOT_CA_DIR
    또는 기본 경로(./apple_root_ca/)에서 *.cer (DER) 파일을 모두 읽는다.

    환경변수 APPLE_ENVIRONMENT=sandbox/production으로 분기. 미설정 시 payload의
    data.environment 값을 신뢰하여 분기 (검증된 payload이므로 안전).
    """
    import os
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import (
        SignedDataVerifier,
        VerificationException,
    )

    # 1) Apple Root CA 로드
    ca_dir = os.getenv("APPLE_ROOT_CA_DIR", "./apple_root_ca")
    root_certs: list[bytes] = []
    if os.path.isdir(ca_dir):
        for name in sorted(os.listdir(ca_dir)):
            if name.lower().endswith(".cer") or name.lower().endswith(".der"):
                with open(os.path.join(ca_dir, name), "rb") as f:
                    root_certs.append(f.read())

    if not root_certs:
        logger.error("Apple Root CA 번들이 비어 있습니다: %s", ca_dir)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Apple Root CA 설정이 누락되었습니다",
        )

    # 2) Environment 분기 — 환경변수 우선, 없으면 payload의 data.environment 참고.
    env_str = os.getenv("APPLE_ENVIRONMENT", "").lower()
    if env_str == "production":
        env = Environment.PRODUCTION
    elif env_str == "sandbox":
        env = Environment.SANDBOX
    else:
        # payload 미리 peek (서명 검증 전이지만 environment 분기 용도만)
        import jwt as _jwt
        try:
            peek = _jwt.decode(signed_payload, options={"verify_signature": False})
            data_env = ((peek.get("data") or {}).get("environment") or "").lower()
            env = Environment.PRODUCTION if data_env == "production" else Environment.SANDBOX
        except Exception:
            env = Environment.PRODUCTION

    # 3) 검증
    try:
        verifier = SignedDataVerifier(
            root_certificates=root_certs,
            enable_online_checks=False,
            environment=env,
            bundle_id=config.APPLE_BUNDLE_ID,
        )
        notification = verifier.verify_and_decode_notification(signed_payload)
    except VerificationException as e:
        logger.warning("Apple signedPayload 검증 실패: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Apple 알림 서명 검증에 실패했습니다",
        )
    except Exception as e:
        logger.warning("Apple signedPayload 검증 예외: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Apple 알림 서명 검증 중 오류",
        )

    # SignedDataVerifier는 ResponseBodyV2DecodedPayload 객체를 반환 — dict로 정규화.
    if hasattr(notification, "to_dict"):
        return notification.to_dict()
    if hasattr(notification, "__dict__"):
        # dataclass-like 객체 fallback
        return _shallow_to_dict(notification)
    if isinstance(notification, dict):
        return notification
    logger.warning("Apple 알림 디코딩 결과 형식을 알 수 없음: %s", type(notification))
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Apple 알림 디코딩 결과 처리 실패",
    )


def _shallow_to_dict(obj) -> dict:
    """dataclass-like 객체를 한 단계 dict로 변환 (중첩 객체는 그대로 둠)."""
    result: dict = {}
    for key, value in vars(obj).items():
        if hasattr(value, "__dict__") and not isinstance(value, (str, bytes)):
            result[key] = _shallow_to_dict(value)
        else:
            result[key] = value
    return result
