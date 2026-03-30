import secrets
import random
import string
from datetime import datetime, timedelta, timezone

import asyncpg

from config import FREE_TRIAL_DAYS, DEFAULT_HEARTBEAT_HOUR, DEFAULT_HEARTBEAT_MINUTE


def _generate_invite_code() -> str:
    """XXX-XXXX 형식의 7자리 영숫자 코드 생성"""
    chars = string.ascii_uppercase + string.digits
    part1 = "".join(random.choices(chars, k=3))
    part2 = "".join(random.choices(chars, k=4))
    return f"{part1}-{part2}"


def _generate_device_token() -> str:
    return secrets.token_urlsafe(32)


async def register_user(db: asyncpg.Connection, role: str, device: dict) -> dict:
    # 동일 device_id가 이미 등록된 기기인지 확인
    existing = await db.fetchrow(
        """SELECT u.id, u.role, u.invite_code, u.device_token
           FROM devices d
           JOIN users u ON u.id = d.user_id
           WHERE d.device_id = $1""",
        device["device_id"],
    )

    if existing is not None:
        # 기존 기기 재가입 — 새 device_token 발급 후 FCM 토큰만 갱신
        new_token = _generate_device_token()
        async with db.transaction():
            await db.execute(
                "UPDATE users SET device_token = $1, updated_at = NOW() WHERE id = $2",
                new_token, existing["id"],
            )
            await db.execute(
                "UPDATE devices SET fcm_token = $1, timezone = $2, updated_at = NOW() WHERE device_id = $3",
                device.get("fcm_token"),
                device.get("timezone") or "Asia/Seoul",
                device["device_id"],
            )

        subscription = None
        if existing["role"] == "guardian":
            sub_row = await db.fetchrow(
                "SELECT plan, expires_at FROM subscriptions WHERE user_id = $1 ORDER BY id DESC LIMIT 1",
                existing["id"],
            )
            if sub_row:
                subscription = {
                    "plan": sub_row["plan"],
                    "expires_at": sub_row["expires_at"].isoformat(),
                    "is_active": sub_row["expires_at"] > datetime.now(timezone.utc),
                }

        return {
            "user_id": existing["id"],
            "device_token": new_token,
            "invite_code": existing["invite_code"],
            "subscription": subscription,
            # 요청한 role과 기존 role이 다를 때만 포함 → 앱이 다이얼로그 표시
            "existing_role": existing["role"] if existing["role"] != role else None,
        }

    # 신규 가입
    device_token = _generate_device_token()

    invite_code: str | None = None
    if role == "subject":
        for _ in range(10):
            candidate = _generate_invite_code()
            row = await db.fetchrow(
                "SELECT id FROM users WHERE invite_code = $1", candidate
            )
            if row is None:
                invite_code = candidate
                break

    async with db.transaction():
        user_id = await db.fetchval(
            "INSERT INTO users (role, invite_code, device_token) VALUES ($1, $2, $3) RETURNING id",
            role, invite_code, device_token,
        )

        await db.execute(
            """INSERT INTO devices
               (user_id, device_id, platform, os_version, fcm_token,
                heartbeat_hour, heartbeat_minute, timezone)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
            user_id,
            device["device_id"],
            device["platform"],
            device.get("os_version"),
            device.get("fcm_token"),
            DEFAULT_HEARTBEAT_HOUR,
            DEFAULT_HEARTBEAT_MINUTE,
            device.get("timezone") or "Asia/Seoul",
        )

        subscription = None
        if role == "guardian":
            expires_at_dt = datetime.now(timezone.utc) + timedelta(days=FREE_TRIAL_DAYS)
            await db.execute(
                "INSERT INTO subscriptions (user_id, plan, expires_at) VALUES ($1, $2, $3)",
                user_id, "free_trial", expires_at_dt,
            )
            subscription = {
                "plan": "free_trial",
                "expires_at": expires_at_dt.isoformat(),
                "is_active": True,
            }

    return {
        "user_id": user_id,
        "device_token": device_token,
        "invite_code": invite_code,
        "subscription": subscription,
    }
