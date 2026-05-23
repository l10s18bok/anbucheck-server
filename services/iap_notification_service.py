"""인앱 결제 서버 알림(RTDN) — Apple S2S V2 / Google Pub/Sub 공용 처리

이 파일은 알림 페이로드 파싱 + DB 반영만 담당한다. 외부 IAP API 재조회는
`iap_verify_service`의 `verify_apple_transaction` / `verify_google_purchase`를 호출한다.

원칙:
- 서명 검증은 라우터에서 끝낸다 — 이 파일에 진입한 페이로드는 신뢰 가능.
- "알림 = 트리거", "source of truth = IAP API 재조회". 알림 본문의 expires/state는
  부가 정보일 뿐이고, 실제 상태는 재조회 결과로 결정한다 (RTDN 순서 역전 케이스 대비).
- subscriptions 매핑 키는 `receipt_data` (iOS: originalTransactionId, Android: purchaseToken).
- UPSERT로 멱등성 자연 처리 — 같은 알림이 재전송돼도 결과 동일.
- 알 수 없는 notificationType / 매핑 실패는 graceful 로그 후 정상 종료 (2xx 유지).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

import config
from services.iap_verify_service import (
    GOOGLE_ACTIVE_STATES,
    verify_apple_transaction,
    verify_google_purchase,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Google Play RTDN
# ─────────────────────────────────────────

# subscriptionNotification.notificationType — Google Play Developer API v3 정의
GOOGLE_NOTIFICATION_TYPES: dict[int, str] = {
    1: "SUBSCRIPTION_RECOVERED",
    2: "SUBSCRIPTION_RENEWED",
    3: "SUBSCRIPTION_CANCELED",
    4: "SUBSCRIPTION_PURCHASED",
    5: "SUBSCRIPTION_ON_HOLD",
    6: "SUBSCRIPTION_IN_GRACE_PERIOD",
    7: "SUBSCRIPTION_RESTARTED",
    8: "SUBSCRIPTION_PRICE_CHANGE_CONFIRMED",
    9: "SUBSCRIPTION_DEFERRED",
    10: "SUBSCRIPTION_PAUSED",
    11: "SUBSCRIPTION_PAUSE_SCHEDULE_CHANGED",
    12: "SUBSCRIPTION_REVOKED",
    13: "SUBSCRIPTION_EXPIRED",
}

# entitlement 부여(혹은 유지) 대상 type — 재조회 결과의 state로 최종 확정.
_GOOGLE_ACTIVATE_TYPES: set[int] = {1, 2, 4, 6, 7}  # RECOVERED, RENEWED, PURCHASED, IN_GRACE, RESTARTED

# entitlement 즉시 회수 대상 type — 재조회 없이 expired 처리.
_GOOGLE_REVOKE_TYPES: set[int] = {5, 10, 12, 13}  # ON_HOLD, PAUSED, REVOKED, EXPIRED


async def handle_google_notification(db: asyncpg.Connection, payload: dict) -> dict:
    """Google Play RTDN 페이로드 처리.

    payload: Pub/Sub message.data를 base64 디코딩한 JSON.
        {
          "version": "1.0",
          "packageName": "kr.co.anbucheck.live",
          "eventTimeMillis": "...",
          "subscriptionNotification": {
            "version": "1.0",
            "notificationType": 4,
            "purchaseToken": "...",
            "subscriptionId": "anbu_yearly"
          }
        }
      또는 `testNotification`(초기 설정 검증용) / `oneTimeProductNotification`(미지원).

    반환: 처리 결과 dict (라우터는 무시; 로그/테스트용).
    """
    # 1) testNotification — Play Console에서 RTDN topic 설정 검증 시 발송
    if "testNotification" in payload:
        logger.info("Google RTDN testNotification 수신 — 무시")
        return {"status": "ok", "kind": "test"}

    # 2) 구독 알림 외 페이로드(일회성 상품 등) 무시
    notif = payload.get("subscriptionNotification")
    if not notif:
        logger.info("Google RTDN subscriptionNotification 없음 — 무시 keys=%s", list(payload.keys()))
        return {"status": "ok", "kind": "ignored"}

    notification_type = notif.get("notificationType")
    purchase_token = notif.get("purchaseToken")
    subscription_id = notif.get("subscriptionId", "")

    if not purchase_token or notification_type is None:
        logger.warning("Google RTDN 필드 누락 type=%s token_present=%s",
                       notification_type, bool(purchase_token))
        return {"status": "ok", "kind": "malformed"}

    type_name = GOOGLE_NOTIFICATION_TYPES.get(notification_type, f"UNKNOWN_{notification_type}")
    logger.info("Google RTDN type=%s(%s) product=%s", notification_type, type_name, subscription_id)

    # 3) 알려진 type만 처리, 그 외는 graceful 무시 (8 PRICE_CHANGE, 9 DEFERRED, 11 PAUSE_SCHEDULE_CHANGED 등)
    if notification_type in _GOOGLE_ACTIVATE_TYPES:
        return await _google_activate(db, purchase_token, type_name)

    if notification_type in _GOOGLE_REVOKE_TYPES:
        return await _google_revoke(db, purchase_token, type_name)

    # SUBSCRIPTION_CANCELED(3): 사용자가 취소했으나 expires_at까지는 entitlement 유지.
    # 재조회로 expires_at만 갱신하고 plan은 그대로 둠 (만료 시 EXPIRED(13)로 expired 처리).
    if notification_type == 3:
        return await _google_activate(db, purchase_token, type_name)

    logger.info("Google RTDN type=%s(%s) — 처리 대상 아님, 무시", notification_type, type_name)
    return {"status": "ok", "kind": "noop", "type": type_name}


async def _google_activate(db: asyncpg.Connection, purchase_token: str, type_name: str) -> dict:
    """Google entitlement 활성화/유지 — IAP API 재조회로 최신 expires_at 반영."""
    try:
        info = await verify_google_purchase(purchase_token)
    except Exception as e:
        # 재조회 실패 시 raise하지 않고 graceful — Pub/Sub 무한 retry 방지.
        logger.warning("Google RTDN 재조회 실패 — token 매핑 불가 type=%s err=%s", type_name, e)
        return {"status": "ok", "kind": "verify_failed", "type": type_name}

    if info["product_id"] != config.IAP_PRODUCT_ID:
        logger.warning("Google RTDN 알 수 없는 상품 product=%s", info["product_id"])
        return {"status": "ok", "kind": "wrong_product", "type": type_name}

    state = info["state"]
    if state not in GOOGLE_ACTIVE_STATES:
        # 재조회 시점에 이미 비활성 — RTDN 순서 역전 (RENEWED 직후 EXPIRED 등)
        logger.info("Google RTDN 재조회 결과 비활성 state=%s — expired 처리", state)
        return await _mark_expired_by_token(db, purchase_token, type_name)

    expires_at_dt: datetime = info["expires_at"]
    rows = await db.execute(
        """UPDATE subscriptions
           SET plan = 'yearly', expires_at = $1, updated_at = NOW()
           WHERE receipt_data = $2 AND platform = 'android'""",
        expires_at_dt, purchase_token,
    )
    # asyncpg.execute UPDATE는 "UPDATE N" 형식 반환
    affected = _parse_affected(rows)
    if affected == 0:
        # 클라이언트 verify 이전에 RTDN이 먼저 도착하는 race — graceful 종료.
        # 클라이언트가 곧 /verify를 호출하면 receipt_data가 채워지고, 그 후 RTDN은 정상 매핑됨.
        logger.info("Google RTDN 매핑 실패 — receipt_data로 user 미발견 type=%s", type_name)
        return {"status": "ok", "kind": "not_mapped", "type": type_name}

    logger.info("Google RTDN 활성화 완료 type=%s affected=%d expires=%s",
                type_name, affected, expires_at_dt.isoformat())
    return {"status": "ok", "kind": "activated", "type": type_name, "affected": affected}


async def _google_revoke(db: asyncpg.Connection, purchase_token: str, type_name: str) -> dict:
    """Google entitlement 회수 — plan='expired', expires_at=현재 시각."""
    return await _mark_expired_by_token(db, purchase_token, type_name)


async def _mark_expired_by_token(
    db: asyncpg.Connection, purchase_token: str, type_name: str
) -> dict:
    now = datetime.now(timezone.utc)
    rows = await db.execute(
        """UPDATE subscriptions
           SET plan = 'expired', expires_at = $1, updated_at = NOW()
           WHERE receipt_data = $2 AND platform = 'android'""",
        now, purchase_token,
    )
    affected = _parse_affected(rows)
    logger.info("Google RTDN 회수 완료 type=%s affected=%d", type_name, affected)
    return {"status": "ok", "kind": "revoked", "type": type_name, "affected": affected}


# ─────────────────────────────────────────
# Apple S2S Notifications V2
# ─────────────────────────────────────────

# Apple ASN V2 notificationType — App Store Server Notifications V2 정의
# subtype과 조합해 처리. 활성화 대상은 entitlement 부여/유지.
_APPLE_ACTIVATE_TYPES: set[str] = {
    "SUBSCRIBED",          # 신규/재구독
    "DID_RENEW",           # 갱신
    "DID_CHANGE_RENEWAL_STATUS",  # 자동 갱신 ON 변경
    "OFFER_REDEEMED",      # 프로모 코드 사용
    "PRICE_INCREASE",      # 가격 인상 — 사용자 동의 시점까지 entitlement 유지
    "RENEWAL_EXTENDED",    # Apple이 갱신 연장 (서비스 장애 보상 등)
    "RENEWAL_EXTENSION",   # 갱신 연장 진행
}

# entitlement 회수 대상 type
_APPLE_REVOKE_TYPES: set[str] = {
    "EXPIRED",             # 자연 만료
    "GRACE_PERIOD_EXPIRED",
    "REVOKE",              # Apple 환불 강제 회수
    "REFUND",              # 환불 완료
}


async def handle_apple_notification(db: asyncpg.Connection, decoded: dict) -> dict:
    """Apple S2S V2 알림 처리.

    decoded: SignedDataVerifier로 검증된 signedPayload의 디코딩 결과.
        {
          "notificationType": "DID_RENEW",
          "subtype": "...",
          "data": {
            "environment": "Production" | "Sandbox",
            "signedTransactionInfo": "<JWS>",
            "signedRenewalInfo": "<JWS>",
            ...
          },
          ...
        }
    """
    notification_type = decoded.get("notificationType", "")
    subtype = decoded.get("subtype", "")
    data = decoded.get("data") or {}
    environment = data.get("environment", "")

    logger.info("Apple S2S V2 type=%s subtype=%s env=%s",
                notification_type, subtype, environment)

    # signedTransactionInfo 페이로드 추출 (originalTransactionId 매핑 키 확보)
    signed_tx_jws = data.get("signedTransactionInfo")
    if not signed_tx_jws:
        logger.warning("Apple S2S V2 signedTransactionInfo 누락 type=%s", notification_type)
        return {"status": "ok", "kind": "malformed"}

    import jwt as _jwt  # app-store-server-library transitive (PyJWT)
    tx_payload = _jwt.decode(signed_tx_jws, options={"verify_signature": False})
    original_tx_id = tx_payload.get("originalTransactionId") or tx_payload.get("transactionId")
    if not original_tx_id:
        logger.warning("Apple S2S V2 originalTransactionId 누락 type=%s", notification_type)
        return {"status": "ok", "kind": "malformed"}

    if notification_type in _APPLE_REVOKE_TYPES:
        return await _apple_revoke(db, str(original_tx_id), notification_type)

    if notification_type in _APPLE_ACTIVATE_TYPES:
        return await _apple_activate(db, str(original_tx_id), notification_type)

    # DID_FAIL_TO_RENEW: subtype에 따라 grace period 진입/외부. graceful 처리 — Apple이
    # 결국 EXPIRED 또는 DID_RENEW로 후속 알림을 보내므로 여기서는 무시.
    logger.info("Apple S2S V2 type=%s — 처리 대상 아님, 무시", notification_type)
    return {"status": "ok", "kind": "noop", "type": notification_type}


async def _apple_activate(db: asyncpg.Connection, original_tx_id: str, type_name: str) -> dict:
    try:
        info = await verify_apple_transaction(original_tx_id)
    except Exception as e:
        logger.warning("Apple S2S V2 재조회 실패 type=%s err=%s", type_name, e)
        return {"status": "ok", "kind": "verify_failed", "type": type_name}

    if info["product_id"] != config.IAP_PRODUCT_ID:
        logger.warning("Apple S2S V2 알 수 없는 상품 product=%s", info["product_id"])
        return {"status": "ok", "kind": "wrong_product", "type": type_name}

    expires_at_dt: datetime = info["expires_at"]
    if expires_at_dt <= datetime.now(timezone.utc):
        # 재조회 시점에 이미 만료 — expired 처리
        logger.info("Apple S2S V2 재조회 결과 이미 만료 type=%s", type_name)
        return await _mark_expired_by_ios_tx(db, original_tx_id, type_name)

    rows = await db.execute(
        """UPDATE subscriptions
           SET plan = 'yearly', expires_at = $1, updated_at = NOW()
           WHERE receipt_data = $2 AND platform = 'ios'""",
        expires_at_dt, original_tx_id,
    )
    affected = _parse_affected(rows)
    if affected == 0:
        logger.info("Apple S2S V2 매핑 실패 — receipt_data로 user 미발견 type=%s", type_name)
        return {"status": "ok", "kind": "not_mapped", "type": type_name}

    logger.info("Apple S2S V2 활성화 완료 type=%s affected=%d expires=%s",
                type_name, affected, expires_at_dt.isoformat())
    return {"status": "ok", "kind": "activated", "type": type_name, "affected": affected}


async def _apple_revoke(db: asyncpg.Connection, original_tx_id: str, type_name: str) -> dict:
    return await _mark_expired_by_ios_tx(db, original_tx_id, type_name)


async def _mark_expired_by_ios_tx(
    db: asyncpg.Connection, original_tx_id: str, type_name: str
) -> dict:
    now = datetime.now(timezone.utc)
    rows = await db.execute(
        """UPDATE subscriptions
           SET plan = 'expired', expires_at = $1, updated_at = NOW()
           WHERE receipt_data = $2 AND platform = 'ios'""",
        now, original_tx_id,
    )
    affected = _parse_affected(rows)
    logger.info("Apple S2S V2 회수 완료 type=%s affected=%d", type_name, affected)
    return {"status": "ok", "kind": "revoked", "type": type_name, "affected": affected}


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def _parse_affected(execute_result: str) -> int:
    """asyncpg.execute는 \"UPDATE N\" / \"INSERT 0 N\" 같은 status 문자열을 반환."""
    if not execute_result:
        return 0
    parts = execute_result.split()
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0
