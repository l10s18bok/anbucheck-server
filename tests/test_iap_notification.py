"""iap_notification_service 단위 테스트 — RTDN 알림 분기 + UPSERT 멱등 + graceful 처리

OIDC JWT / Apple JWS 서명 검증 자체는 외부 라이브러리(google-auth / app-store-server-library)
신뢰 영역이므로 테스트하지 않는다. 이 파일은 페이로드가 검증을 통과한 이후의 처리 로직만 다룬다.

실행:
  python -m unittest tests.test_iap_notification
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import iap_notification_service


# ─────────────────────────────────────────
# FakeDB — execute가 "UPDATE N" 문자열을 돌려주도록 구성
# ─────────────────────────────────────────

class FakeDB:
    def __init__(self, affected: int = 1):
        self.affected = affected
        self.executed: list[tuple] = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return f"UPDATE {self.affected}"

    async def fetchrow(self, query, *args):
        return None


# ─────────────────────────────────────────
# Google Play RTDN
# ─────────────────────────────────────────

def _google_payload(notification_type: int, purchase_token: str = "token_abc",
                    package_name: str | None = "kr.co.anbucheck.live") -> dict:
    payload: dict = {
        "version": "1.0",
        "eventTimeMillis": "1709123456789",
        "subscriptionNotification": {
            "version": "1.0",
            "notificationType": notification_type,
            "purchaseToken": purchase_token,
            "subscriptionId": "anbu_yearly",
        },
    }
    if package_name:
        payload["packageName"] = package_name
    return payload


class HandleGoogleNotificationTest(unittest.IsolatedAsyncioTestCase):

    async def test_test_notification_ignored(self):
        db = FakeDB()
        result = await iap_notification_service.handle_google_notification(
            db, {"testNotification": {"version": "1.0"}, "packageName": "kr.co.anbucheck.live"}
        )
        self.assertEqual(result["kind"], "test")
        self.assertEqual(db.executed, [])

    async def test_one_time_product_ignored(self):
        db = FakeDB()
        result = await iap_notification_service.handle_google_notification(
            db, {"oneTimeProductNotification": {}, "packageName": "kr.co.anbucheck.live"}
        )
        self.assertEqual(result["kind"], "ignored")
        self.assertEqual(db.executed, [])

    async def test_missing_purchase_token_ignored(self):
        db = FakeDB()
        bad = {
            "subscriptionNotification": {
                "notificationType": 4,
                "subscriptionId": "anbu_yearly",
                # purchaseToken 누락
            }
        }
        result = await iap_notification_service.handle_google_notification(db, bad)
        self.assertEqual(result["kind"], "malformed")

    async def test_purchased_activates_and_updates_db(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        info = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB(affected=1)
        with patch.object(iap_notification_service, "verify_google_purchase", return_value=info):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(4)
            )

        self.assertEqual(result["kind"], "activated")
        self.assertEqual(result["affected"], 1)
        self.assertEqual(len(db.executed), 1)
        update_args = db.executed[0][1]
        self.assertIn(future_dt, update_args)
        self.assertIn("token_abc", update_args)

    async def test_renewed_activates(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        info = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB(affected=1)
        with patch.object(iap_notification_service, "verify_google_purchase", return_value=info):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(2)  # SUBSCRIPTION_RENEWED
            )
        self.assertEqual(result["kind"], "activated")

    async def test_expired_marks_db_expired(self):
        db = FakeDB(affected=1)
        # EXPIRED는 재조회 없이 expired 처리 — verify_google_purchase 호출 안 됨
        with patch.object(
            iap_notification_service, "verify_google_purchase",
            side_effect=AssertionError("EXPIRED는 재조회하지 않아야 함"),
        ):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(13)  # SUBSCRIPTION_EXPIRED
            )
        self.assertEqual(result["kind"], "revoked")
        self.assertEqual(len(db.executed), 1)
        # UPDATE에 'expired' 문자열 포함 확인
        update_query = db.executed[0][0]
        self.assertIn("expired", update_query)

    async def test_revoked_marks_db_expired(self):
        db = FakeDB(affected=1)
        with patch.object(
            iap_notification_service, "verify_google_purchase",
            side_effect=AssertionError("REVOKED는 재조회하지 않아야 함"),
        ):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(12)  # SUBSCRIPTION_REVOKED
            )
        self.assertEqual(result["kind"], "revoked")

    async def test_not_mapped_is_graceful(self):
        """클라이언트 verify 이전에 RTDN이 먼저 도착 — UPDATE affected=0."""
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        info = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB(affected=0)  # 매핑 실패
        with patch.object(iap_notification_service, "verify_google_purchase", return_value=info):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(4)
            )
        self.assertEqual(result["kind"], "not_mapped")

    async def test_verify_failure_is_graceful(self):
        db = FakeDB(affected=0)
        with patch.object(
            iap_notification_service, "verify_google_purchase",
            side_effect=RuntimeError("Google API 일시 장애"),
        ):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(2)  # RENEWED
            )
        self.assertEqual(result["kind"], "verify_failed")
        self.assertEqual(db.executed, [])  # DB 변경 없음

    async def test_wrong_product_is_graceful(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        info = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "other_product",
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB()
        with patch.object(iap_notification_service, "verify_google_purchase", return_value=info):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(4)
            )
        self.assertEqual(result["kind"], "wrong_product")
        self.assertEqual(db.executed, [])

    async def test_inactive_state_marks_expired(self):
        """RTDN 순서 역전: RENEWED 도착했으나 재조회 결과 이미 비활성."""
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        info = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_EXPIRED",
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB(affected=1)
        with patch.object(iap_notification_service, "verify_google_purchase", return_value=info):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(2)  # RENEWED
            )
        self.assertEqual(result["kind"], "revoked")

    async def test_unknown_notification_type_noop(self):
        db = FakeDB()
        result = await iap_notification_service.handle_google_notification(
            db, _google_payload(99)  # 존재하지 않는 type
        )
        self.assertEqual(result["kind"], "noop")
        self.assertEqual(db.executed, [])

    async def test_canceled_type_3_re_queries_to_keep_entitlement(self):
        """SUBSCRIPTION_CANCELED(3): 사용자가 취소했으나 expires_at까지 entitlement 유지.
        재조회로 만료 시각만 갱신, plan은 yearly 유지."""
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        info = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_CANCELED",  # canceled but not yet expired
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB(affected=1)
        with patch.object(iap_notification_service, "verify_google_purchase", return_value=info):
            result = await iap_notification_service.handle_google_notification(
                db, _google_payload(3)  # CANCELED
            )
        # CANCELED state는 ACTIVE_STATES에 없으므로 revoked로 처리됨
        self.assertEqual(result["kind"], "revoked")


# ─────────────────────────────────────────
# Apple S2S Notifications V2
# ─────────────────────────────────────────

def _apple_decoded(notification_type: str, subtype: str = "",
                   environment: str = "Production",
                   signed_tx: str = "fake_jws") -> dict:
    """SignedDataVerifier가 디코딩한 결과 형태의 dict."""
    return {
        "notificationType": notification_type,
        "subtype": subtype,
        "data": {
            "environment": environment,
            "signedTransactionInfo": signed_tx,
        },
    }


class HandleAppleNotificationTest(unittest.IsolatedAsyncioTestCase):

    async def test_subscribed_activates(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        verify_info = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "environment": "production",
            "raw_transaction": {},
        }
        tx_payload = {"originalTransactionId": "orig_999", "transactionId": "tx_111"}
        db = FakeDB(affected=1)

        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(iap_notification_service, "verify_apple_transaction", return_value=verify_info):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("SUBSCRIBED")
            )

        self.assertEqual(result["kind"], "activated")
        self.assertEqual(result["affected"], 1)
        update_args = db.executed[0][1]
        self.assertIn(future_dt, update_args)
        self.assertIn("orig_999", update_args)

    async def test_did_renew_activates(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        verify_info = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "environment": "sandbox",
            "raw_transaction": {},
        }
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB(affected=1)

        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(iap_notification_service, "verify_apple_transaction", return_value=verify_info):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("DID_RENEW", environment="Sandbox")
            )
        self.assertEqual(result["kind"], "activated")

    async def test_expired_marks_db_expired_without_reverify(self):
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB(affected=1)

        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(
                 iap_notification_service, "verify_apple_transaction",
                 side_effect=AssertionError("EXPIRED는 재조회하지 않아야 함"),
             ):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("EXPIRED")
            )
        self.assertEqual(result["kind"], "revoked")
        update_query = db.executed[0][0]
        self.assertIn("expired", update_query)

    async def test_refund_marks_db_expired(self):
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB(affected=1)
        with patch("jwt.decode", return_value=tx_payload):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("REFUND")
            )
        self.assertEqual(result["kind"], "revoked")

    async def test_missing_signed_tx_is_graceful(self):
        db = FakeDB()
        decoded = {"notificationType": "DID_RENEW", "data": {}}
        result = await iap_notification_service.handle_apple_notification(db, decoded)
        self.assertEqual(result["kind"], "malformed")
        self.assertEqual(db.executed, [])

    async def test_missing_original_tx_id_is_graceful(self):
        db = FakeDB()
        with patch("jwt.decode", return_value={}):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("DID_RENEW")
            )
        self.assertEqual(result["kind"], "malformed")

    async def test_unknown_notification_type_noop(self):
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB()
        with patch("jwt.decode", return_value=tx_payload):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("CONSUMPTION_REQUEST")  # 미처리 type
            )
        self.assertEqual(result["kind"], "noop")
        self.assertEqual(db.executed, [])

    async def test_wrong_product_is_graceful(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        verify_info = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "other_product",
            "environment": "production",
            "raw_transaction": {},
        }
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB()
        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(iap_notification_service, "verify_apple_transaction", return_value=verify_info):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("SUBSCRIBED")
            )
        self.assertEqual(result["kind"], "wrong_product")
        self.assertEqual(db.executed, [])

    async def test_verify_failure_is_graceful(self):
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB()
        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(
                 iap_notification_service, "verify_apple_transaction",
                 side_effect=RuntimeError("Apple API 일시 장애"),
             ):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("DID_RENEW")
            )
        self.assertEqual(result["kind"], "verify_failed")
        self.assertEqual(db.executed, [])

    async def test_not_mapped_is_graceful(self):
        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        verify_info = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "environment": "production",
            "raw_transaction": {},
        }
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB(affected=0)  # 매핑 실패
        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(iap_notification_service, "verify_apple_transaction", return_value=verify_info):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("SUBSCRIBED")
            )
        self.assertEqual(result["kind"], "not_mapped")

    async def test_re_verify_already_expired_marks_expired(self):
        """알림은 활성화 type이지만 재조회 결과 이미 만료."""
        past_dt = datetime(2020, 1, 1, tzinfo=timezone.utc)
        verify_info = {
            "original_transaction_id": "orig_999",
            "expires_at": past_dt,
            "product_id": "anbu_yearly",
            "environment": "production",
            "raw_transaction": {},
        }
        tx_payload = {"originalTransactionId": "orig_999"}
        db = FakeDB(affected=1)
        with patch("jwt.decode", return_value=tx_payload), \
             patch.object(iap_notification_service, "verify_apple_transaction", return_value=verify_info):
            result = await iap_notification_service.handle_apple_notification(
                db, _apple_decoded("DID_RENEW")
            )
        self.assertEqual(result["kind"], "revoked")


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

class ParseAffectedTest(unittest.TestCase):
    def test_update_n(self):
        self.assertEqual(iap_notification_service._parse_affected("UPDATE 3"), 3)

    def test_insert_0_n(self):
        self.assertEqual(iap_notification_service._parse_affected("INSERT 0 5"), 5)

    def test_empty(self):
        self.assertEqual(iap_notification_service._parse_affected(""), 0)

    def test_invalid(self):
        self.assertEqual(iap_notification_service._parse_affected("UPDATE foo"), 0)


if __name__ == "__main__":
    unittest.main()
