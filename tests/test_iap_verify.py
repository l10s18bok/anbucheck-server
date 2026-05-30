"""iap_verify_service 단위 테스트 — Apple/Google sandbox 응답 mocking

실행:
  python -m unittest tests.test_iap_verify

stdlib unittest만 사용 (pytest 의존성 추가 없음).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# repo 루트를 sys.path에 추가 (anbucheck-server를 working directory로 둔 경우 자동 인식)
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import HTTPException

from services import iap_verify_service


# ─────────────────────────────────────────
# Apple
# ─────────────────────────────────────────

def _make_apple_response(transactions: list[dict]) -> SimpleNamespace:
    """Apple StatusResponse 형태의 mock 객체.
    transactions: 각 항목이 signedTransactionInfo로 디코딩될 payload dict.
    """
    items = [SimpleNamespace(signedTransactionInfo=f"jws_{i}") for i in range(len(transactions))]
    group = SimpleNamespace(lastTransactions=items)
    return SimpleNamespace(data=[group])


class VerifyAppleTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_normalized_dict(self):
        # 2030년 만료 (밀리초 epoch)
        expires_ms = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        payload = {
            "transactionId": "tx_111",
            "originalTransactionId": "orig_999",
            "productId": "anbu_yearly",
            "expiresDate": expires_ms,
        }
        response = _make_apple_response([payload])

        with patch.object(
            iap_verify_service, "_call_apple_subscription_statuses",
            return_value=(response, "production"),
        ), patch.object(
            iap_verify_service, "_decode_jws_payload", return_value=payload,
        ):
            result = await iap_verify_service.verify_apple_transaction("tx_111")

        self.assertEqual(result["original_transaction_id"], "orig_999")
        self.assertEqual(result["product_id"], "anbu_yearly")
        self.assertEqual(result["environment"], "production")
        self.assertEqual(result["expires_at"].year, 2030)
        self.assertEqual(result["expires_at"].tzinfo, timezone.utc)

    async def test_picks_matching_transaction_when_multiple(self):
        expires_ms = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        first = {
            "transactionId": "tx_aaa",
            "originalTransactionId": "orig_aaa",
            "productId": "anbu_yearly",
            "expiresDate": expires_ms,
        }
        target = {
            "transactionId": "tx_bbb",
            "originalTransactionId": "orig_bbb",
            "productId": "anbu_yearly",
            "expiresDate": expires_ms,
        }
        response = _make_apple_response([first, target])

        # _decode_jws_payload는 호출 순서대로 first → target 반환
        with patch.object(
            iap_verify_service, "_call_apple_subscription_statuses",
            return_value=(response, "sandbox"),
        ), patch.object(
            iap_verify_service, "_decode_jws_payload", side_effect=[first, target],
        ):
            result = await iap_verify_service.verify_apple_transaction("tx_bbb")

        self.assertEqual(result["original_transaction_id"], "orig_bbb")
        self.assertEqual(result["environment"], "sandbox")

    async def test_empty_data_raises_400(self):
        response = SimpleNamespace(data=[])
        with patch.object(
            iap_verify_service, "_call_apple_subscription_statuses",
            return_value=(response, "production"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                await iap_verify_service.verify_apple_transaction("tx_111")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_missing_expires_date_raises_400(self):
        payload = {
            "transactionId": "tx_111",
            "originalTransactionId": "orig_999",
            "productId": "anbu_yearly",
            # expiresDate 누락
        }
        response = _make_apple_response([payload])
        with patch.object(
            iap_verify_service, "_call_apple_subscription_statuses",
            return_value=(response, "production"),
        ), patch.object(
            iap_verify_service, "_decode_jws_payload", return_value=payload,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await iap_verify_service.verify_apple_transaction("tx_111")
        self.assertEqual(ctx.exception.status_code, 400)


class CallAppleSubscriptionStatusesTest(unittest.TestCase):
    """production → sandbox fallback 검증."""

    def test_production_404_falls_back_to_sandbox(self):
        # production은 404, sandbox는 성공
        fake_response = SimpleNamespace(data=["sandbox_data"])

        class FakeAPIException(Exception):
            def __init__(self, code):
                self.http_status_code = code

        # APIException을 라이브러리 모듈로 패치
        fake_module = MagicMock()
        fake_module.APIException = FakeAPIException

        with patch.dict(sys.modules, {"appstoreserverlibrary.api_client": fake_module}):
            production_client = MagicMock()
            production_client.get_all_subscription_statuses.side_effect = FakeAPIException(404)
            sandbox_client = MagicMock()
            sandbox_client.get_all_subscription_statuses.return_value = fake_response

            clients = iter([production_client, sandbox_client])
            with patch.object(iap_verify_service, "_make_apple_client", side_effect=lambda _: next(clients)):
                response, env = iap_verify_service._call_apple_subscription_statuses("tx_111")

        self.assertIs(response, fake_response)
        self.assertEqual(env, "sandbox")

    def test_sandbox_404_raises_400(self):
        class FakeAPIException(Exception):
            def __init__(self, code):
                self.http_status_code = code

        fake_module = MagicMock()
        fake_module.APIException = FakeAPIException

        with patch.dict(sys.modules, {"appstoreserverlibrary.api_client": fake_module}):
            client = MagicMock()
            client.get_all_subscription_statuses.side_effect = FakeAPIException(404)
            with patch.object(iap_verify_service, "_make_apple_client", return_value=client):
                with self.assertRaises(HTTPException) as ctx:
                    iap_verify_service._call_apple_subscription_statuses("tx_missing")

        self.assertEqual(ctx.exception.status_code, 400)


# ─────────────────────────────────────────
# Google
# ─────────────────────────────────────────

class VerifyGooglePurchaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_active_state(self):
        fake_data = {
            "lineItems": [{
                "productId": "anbu_yearly",
                "expiryTime": "2030-01-01T00:00:00.000Z",
            }],
            "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
            "acknowledgementState": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        }
        with patch.object(
            iap_verify_service, "_call_google_subscriptionsv2_get",
            return_value=fake_data,
        ):
            result = await iap_verify_service.verify_google_purchase("token_abc")

        self.assertEqual(result["purchase_token"], "token_abc")
        self.assertEqual(result["product_id"], "anbu_yearly")
        self.assertEqual(result["state"], "SUBSCRIPTION_STATE_ACTIVE")
        self.assertEqual(result["ack_state"], "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED")
        self.assertEqual(result["expires_at"].year, 2030)
        self.assertEqual(result["expires_at"].tzinfo, timezone.utc)

    async def test_empty_line_items_raises_400(self):
        fake_data = {"lineItems": [], "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE"}
        with patch.object(
            iap_verify_service, "_call_google_subscriptionsv2_get",
            return_value=fake_data,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await iap_verify_service.verify_google_purchase("token_abc")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_missing_expiry_time_raises_400(self):
        fake_data = {
            "lineItems": [{"productId": "anbu_yearly"}],
            "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
        }
        with patch.object(
            iap_verify_service, "_call_google_subscriptionsv2_get",
            return_value=fake_data,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await iap_verify_service.verify_google_purchase("token_abc")
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_http_error_raises_400(self):
        # googleapiclient.errors.HttpError를 가짜로 흉내
        class FakeHttpError(Exception):
            def __init__(self):
                self.resp = SimpleNamespace(status=400)
                self.reason = "Bad Request"

        fake_module = MagicMock()
        fake_module.HttpError = FakeHttpError

        with patch.dict(sys.modules, {"googleapiclient.errors": fake_module}):
            with patch.object(
                iap_verify_service, "_call_google_subscriptionsv2_get",
                side_effect=FakeHttpError(),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await iap_verify_service.verify_google_purchase("token_bad")
        self.assertEqual(ctx.exception.status_code, 400)


# ─────────────────────────────────────────
# subscription_service 통합 (검증 → DB 반영)
# ─────────────────────────────────────────

class FakeDB:
    """asyncpg.Connection 흉내 — fetchrow/execute/transaction 캡처."""

    def __init__(self, existing_row=None):
        self._existing = existing_row
        self.executed: list[tuple] = []

    async def fetchrow(self, query, *args):
        return self._existing

    async def execute(self, query, *args):
        self.executed.append((query, args))

    def transaction(self):
        # asyncpg conn.transaction()의 async 컨텍스트 매니저 흉내 (no-op).
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()


class VerifySubscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_ios_success_persists_original_transaction_id(self):
        from services import subscription_service

        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        fake_verify_result = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "environment": "production",
            "raw_transaction": {},
        }
        db = FakeDB(existing_row=None)

        with patch.object(
            subscription_service, "verify_apple_transaction",
            return_value=fake_verify_result,
        ):
            result = await subscription_service.verify_subscription(
                db, user_id=42, platform="ios", product_id="anbu_yearly", receipt="tx_111",
            )

        self.assertEqual(result["plan"], "yearly")
        self.assertTrue(result["is_active"])
        # 단일 활성 entitlement 불변식 — 회수 UPDATE(다른 user에 바인딩된 동일 영수증
        # 제거) + 신규 INSERT 두 건이 실행된다.
        self.assertEqual(len(db.executed), 2)
        # 1번째: 회수 UPDATE — normalized_receipt + 호출자 user_id로 다른 바인딩 회수
        revoke_query, revoke_args = db.executed[0]
        self.assertIn("receipt_data = $1 AND user_id != $2", revoke_query)
        self.assertIn("expired", revoke_query)
        self.assertIn("orig_999", revoke_args)
        self.assertIn(42, revoke_args)
        # 2번째: INSERT — (expires_at, receipt_data, platform, ...) 순서
        insert_args = db.executed[1][1]
        self.assertIn("orig_999", insert_args)  # receipt_data 컬럼에 originalTransactionId 저장 확인
        self.assertIn("ios", insert_args)
        self.assertIn(future_dt, insert_args)

    async def test_wrong_product_id_raises_400(self):
        from services import subscription_service

        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        wrong_product_result = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "other_product",  # ← 잘못된 상품
            "environment": "production",
            "raw_transaction": {},
        }
        db = FakeDB(existing_row=None)

        with patch.object(
            subscription_service, "verify_apple_transaction",
            return_value=wrong_product_result,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await subscription_service.verify_subscription(
                    db, user_id=42, platform="ios", product_id="anbu_yearly", receipt="tx",
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(len(db.executed), 0)  # DB 변경 없음

    async def test_expired_subscription_raises_400(self):
        from services import subscription_service

        past_dt = datetime(2020, 1, 1, tzinfo=timezone.utc)
        result = {
            "original_transaction_id": "orig_999",
            "expires_at": past_dt,
            "product_id": "anbu_yearly",
            "environment": "production",
            "raw_transaction": {},
        }
        db = FakeDB(existing_row=None)

        with patch.object(
            subscription_service, "verify_apple_transaction", return_value=result,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await subscription_service.verify_subscription(
                    db, user_id=42, platform="ios", product_id="anbu_yearly", receipt="tx",
                )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_android_inactive_state_raises_400(self):
        from services import subscription_service

        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        cancelled_result = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_CANCELED",
            "ack_state": "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
            "raw": {},
        }
        db = FakeDB(existing_row=None)

        with patch.object(
            subscription_service, "verify_google_purchase", return_value=cancelled_result,
        ):
            with self.assertRaises(HTTPException) as ctx:
                await subscription_service.verify_subscription(
                    db, user_id=42, platform="android", product_id="anbu_yearly", receipt="token",
                )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_android_pending_ack_triggers_acknowledge(self):
        from services import subscription_service

        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        pending_result = {
            "purchase_token": "token_abc",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "state": "SUBSCRIPTION_STATE_ACTIVE",
            "ack_state": "ACKNOWLEDGEMENT_STATE_PENDING",
            "raw": {},
        }
        db = FakeDB(existing_row=None)

        ack_mock = MagicMock()
        async def ack_async(product_id, purchase_token):
            ack_mock(product_id, purchase_token)

        with patch.object(
            subscription_service, "verify_google_purchase", return_value=pending_result,
        ), patch.object(
            subscription_service, "acknowledge_google_purchase", side_effect=ack_async,
        ):
            result = await subscription_service.verify_subscription(
                db, user_id=42, platform="android", product_id="anbu_yearly", receipt="token_abc",
            )

        self.assertTrue(result["is_active"])
        ack_mock.assert_called_once_with("anbu_yearly", "token_abc")

    async def test_unsupported_platform_raises_400(self):
        from services import subscription_service
        db = FakeDB(existing_row=None)

        with self.assertRaises(HTTPException) as ctx:
            await subscription_service.verify_subscription(
                db, user_id=42, platform="windows", product_id="anbu_yearly", receipt="x",
            )
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_restore_adds_restored_flag(self):
        from services import subscription_service

        future_dt = datetime(2030, 1, 1, tzinfo=timezone.utc)
        result = {
            "original_transaction_id": "orig_999",
            "expires_at": future_dt,
            "product_id": "anbu_yearly",
            "environment": "production",
            "raw_transaction": {},
        }
        db = FakeDB(existing_row={"id": 7})  # 기존 row 존재 → UPDATE 경로

        with patch.object(
            subscription_service, "verify_apple_transaction", return_value=result,
        ):
            out = await subscription_service.restore_subscription(
                db, user_id=42, platform="ios", product_id="anbu_yearly", receipt="tx",
            )

        self.assertTrue(out["restored"])
        self.assertEqual(out["plan"], "yearly")


if __name__ == "__main__":
    unittest.main()
