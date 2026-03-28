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
        # users 레코드 생성
        user_id = await db.fetchval(
            "INSERT INTO users (role, invite_code, device_token) VALUES ($1, $2, $3) RETURNING id",
            role, invite_code, device_token,
        )

        # devices 레코드 생성 (동일 device_id 재가입 시 기존 레코드 교체)
        await db.execute(
            """INSERT INTO devices
               (user_id, device_id, platform, os_version, fcm_token,
                heartbeat_hour, heartbeat_minute)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (device_id) DO UPDATE SET
                   user_id = EXCLUDED.user_id,
                   platform = EXCLUDED.platform,
                   os_version = EXCLUDED.os_version,
                   fcm_token = EXCLUDED.fcm_token,
                   heartbeat_hour = EXCLUDED.heartbeat_hour,
                   heartbeat_minute = EXCLUDED.heartbeat_minute,
                   updated_at = NOW()""",
            user_id,
            device["device_id"],
            device["platform"],
            device.get("os_version"),
            device.get("fcm_token"),
            DEFAULT_HEARTBEAT_HOUR,
            DEFAULT_HEARTBEAT_MINUTE,
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
