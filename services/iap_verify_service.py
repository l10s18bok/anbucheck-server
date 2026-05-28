"""인앱 결제 영수증 검증 — Apple App Store Server API + Google Play Developer API

이 파일은 외부 IAP API 호출만 담당한다. DB 접근/저장은 subscription_service에서 수행.

클라이언트 합의:
- iOS: `receipt` = `PurchaseDetails.purchaseID` (transactionId 문자열)
- Android: `receipt` = `verificationData.serverVerificationData` (purchaseToken)

반환 dict의 `expires_at`는 timezone-aware datetime (UTC).
실패는 모두 HTTPException으로 전파한다 — 라우터에서 그대로 받아 응답한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException, status

import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Apple App Store Server API
# ─────────────────────────────────────────

def _make_apple_client(environment_str: str):
    """Apple API 클라이언트 생성. 라이브러리 import는 지연(테스트에서 패치 용이)."""
    from appstoreserverlibrary.api_client import AppStoreServerAPIClient
    from appstoreserverlibrary.models.Environment import Environment

    if not config.APPLE_IAP_KEY_P8 or not config.APPLE_IAP_ISSUER_ID or not config.APPLE_IAP_KEY_ID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Apple IAP 설정이 누락되었습니다",
        )

    env = Environment.PRODUCTION if environment_str == "production" else Environment.SANDBOX
    return AppStoreServerAPIClient(
        signing_key=config.APPLE_IAP_KEY_P8.encode("utf-8"),
        key_id=config.APPLE_IAP_KEY_ID,
        issuer_id=config.APPLE_IAP_ISSUER_ID,
        bundle_id=config.APPLE_BUNDLE_ID,
        environment=env,
    )


def _decode_jws_payload(jws: str) -> dict:
    """JWS signedTransactionInfo 페이로드 디코딩.

    이 경로는 우리가 직접 Apple HTTPS 엔드포인트를 호출해 받은 응답이므로
    TLS가 이미 인증을 보장한다. 외부 입력의 JWS(예: S2S Notifications V2)는
    `routers/iap_notification.py::_verify_apple_signed_payload`에서
    SignedDataVerifier로 서명 검증을 거친다.
    """
    import jwt as _jwt  # app-store-server-library의 transitive 의존성 (PyJWT)
    return _jwt.decode(jws, options={"verify_signature": False})


def _call_apple_subscription_statuses(transaction_id: str):
    """Production 우선 호출, 404면 Sandbox 재시도. 동기 함수 — to_thread로 실행."""
    from appstoreserverlibrary.api_client import APIException

    last_error: APIException | None = None
    for env_str in ("production", "sandbox"):
        try:
            client = _make_apple_client(env_str)
            response = client.get_all_subscription_statuses(transaction_id)
            return response, env_str
        except APIException as e:
            last_error = e
            code = getattr(e, "http_status_code", None)
            if code in (401, 404) and env_str == "production":
                # 401: production endpoint가 sandbox transaction을 인증 거부 (Apple 공식 동작)
                # 404: production endpoint에 transaction 없음
                # 둘 다 sandbox 영수증 추정 → sandbox endpoint로 재시도
                # (진짜 키 자격증명 문제면 sandbox에서도 같은 에러 → 자연 중단)
                logger.info("Apple production status=%s — sandbox 재시도 (sandbox receipt 추정)", code)
                continue
            # production 외 에러 또는 sandbox에서 발생한 에러는 즉시 중단
            logger.warning("Apple API 에러 env=%s status=%s msg=%s", env_str, code, str(e))
            break

    code = getattr(last_error, "http_status_code", None) if last_error else None
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Apple 영수증 검증 실패 (status={code})",
    )


async def verify_apple_transaction(transaction_id: str) -> dict:
    """Apple App Store Server API로 구독 상태 조회.

    반환:
      {
        "original_transaction_id": str,
        "expires_at": datetime (UTC),
        "product_id": str,
        "environment": "production" | "sandbox",
        "raw_transaction": dict,
      }
    """
    response, env_str = await asyncio.to_thread(_call_apple_subscription_statuses, transaction_id)

    if not getattr(response, "data", None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Apple 구독 정보가 비어 있습니다",
        )

    # response.data: List[SubscriptionGroupIdentifierItem]
    # 각 group의 lastTransactions에서 입력 transaction_id에 해당하는 항목 탐색
    target: dict | None = None
    fallback: dict | None = None
    for group in response.data:
        for item in (getattr(group, "lastTransactions", None) or []):
            jws = getattr(item, "signedTransactionInfo", None)
            if not jws:
                continue
            payload = _decode_jws_payload(jws)
            if fallback is None:
                fallback = payload
            if (
                str(payload.get("transactionId")) == str(transaction_id)
                or str(payload.get("originalTransactionId")) == str(transaction_id)
            ):
                target = payload
                break
        if target is not None:
            break

    chosen = target if target is not None else fallback
    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Apple 거래 정보를 찾을 수 없습니다",
        )

    expires_ms = chosen.get("expiresDate")
    if not expires_ms:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Apple expiresDate가 누락되었습니다",
        )

    expires_at_dt = datetime.fromtimestamp(int(expires_ms) / 1000, tz=timezone.utc)

    original_tx_id = chosen.get("originalTransactionId") or chosen.get("transactionId")
    if not original_tx_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Apple originalTransactionId가 누락되었습니다",
        )

    return {
        "original_transaction_id": str(original_tx_id),
        "expires_at": expires_at_dt,
        "product_id": chosen.get("productId", ""),
        "environment": env_str,
        "raw_transaction": chosen,
    }


# ─────────────────────────────────────────
# Google Play Developer API
# ─────────────────────────────────────────

_ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

# subscriptionsv2.get 응답의 subscriptionState 중 entitlement 부여 대상
GOOGLE_ACTIVE_STATES: set[str] = {
    "SUBSCRIPTION_STATE_ACTIVE",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
}


def _make_google_client():
    """Google Play API 서비스 객체 생성. 라이브러리 import는 지연."""
    if not config.GOOGLE_SERVICE_ACCOUNT_JSON:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Google IAP 설정이 누락되었습니다",
        )
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[_ANDROID_PUBLISHER_SCOPE]
    )
    # cache_discovery=False — 디스크 캐시 비활성(서버리스/컨테이너 환경 안정성)
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


def _call_google_subscriptionsv2_get(purchase_token: str) -> dict:
    service = _make_google_client()
    return (
        service.purchases()
        .subscriptionsv2()
        .get(packageName=config.GOOGLE_PACKAGE_NAME, token=purchase_token)
        .execute()
    )


def _call_google_acknowledge(product_id: str, purchase_token: str) -> None:
    service = _make_google_client()
    service.purchases().subscriptions().acknowledge(
        packageName=config.GOOGLE_PACKAGE_NAME,
        subscriptionId=product_id,
        token=purchase_token,
        body={},
    ).execute()


async def verify_google_purchase(purchase_token: str) -> dict:
    """Google Play Developer API로 구독 구입 상태 조회.

    반환:
      {
        "purchase_token": str,
        "expires_at": datetime (UTC),
        "product_id": str,
        "state": str (SUBSCRIPTION_STATE_*),
        "ack_state": str (ACKNOWLEDGEMENT_STATE_*),
        "raw": dict,
      }
    """
    from googleapiclient.errors import HttpError
    try:
        data = await asyncio.to_thread(_call_google_subscriptionsv2_get, purchase_token)
    except HttpError as e:
        http_status = getattr(getattr(e, "resp", None), "status", None)
        logger.warning("Google API HttpError status=%s reason=%s", http_status, getattr(e, "reason", ""))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google 영수증 검증 실패 (status={http_status})",
        )

    line_items = data.get("lineItems") or []
    if not line_items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google 구독 정보(lineItems)가 비어 있습니다",
        )

    # 단일 상품(anbu_yearly) 구조 — 첫 lineItem만 사용
    item = line_items[0]
    expires_iso = item.get("expiryTime")
    product_id = item.get("productId", "")
    if not expires_iso:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google expiryTime이 누락되었습니다",
        )

    # ISO 8601 (예: "2026-04-19T14:32:00.123Z") → timezone-aware UTC datetime
    expires_at_dt = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
    if expires_at_dt.tzinfo is None:
        expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)

    return {
        "purchase_token": purchase_token,
        "expires_at": expires_at_dt,
        "product_id": product_id,
        "state": data.get("subscriptionState", ""),
        "ack_state": data.get("acknowledgementState", ""),
        "raw": data,
    }


async def acknowledge_google_purchase(product_id: str, purchase_token: str) -> None:
    """구독 acknowledgement 호출. 3일 내 호출하지 않으면 자동 환불됨.

    이미 ack된 경우 Google이 400/410을 반환할 수 있어 예외는 경고 로그만 남기고 삼킨다.
    클라이언트의 completePurchase()도 ack를 수행하므로 양쪽이 안전망이다.
    """
    from googleapiclient.errors import HttpError
    try:
        await asyncio.to_thread(_call_google_acknowledge, product_id, purchase_token)
    except HttpError as e:
        http_status = getattr(getattr(e, "resp", None), "status", None)
        logger.warning(
            "Google acknowledge 실패 (이미 ack되었거나 일시 오류일 수 있음) status=%s reason=%s",
            http_status, getattr(e, "reason", ""),
        )
