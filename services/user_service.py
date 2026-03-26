import secrets
import random
import string
from datetime import datetime, timedelta

import aiosqlite

from config import FREE_TRIAL_DAYS, DEFAULT_HEARTBEAT_HOUR, DEFAULT_HEARTBEAT_MINUTE


def _generate_invite_code() -> str:
    """XXX-XXXX 형식의 7자리 영숫자 코드 생성"""
    chars = string.ascii_uppercase + string.digits
    part1 = "".join(random.choices(chars, k=3))
    part2 = "".join(random.choices(chars, k=4))
    return f"{part1}-{part2}"


def _generate_device_token() -> str:
    return secrets.token_urlsafe(32)


async def register_user(db: aiosqlite.Connection, role: str, device: dict) -> dict:
    device_token = _generate_device_token()

    invite_code: str | None = None
    if role == "subject":
        for _ in range(10):
            candidate = _generate_invite_code()
            async with db.execute(
                "SELECT id FROM users WHERE invite_code = ?", (candidate,)
            ) as cur:
                if await cur.fetchone() is None:
                    invite_code = candidate
                    break

    # users 레코드 생성
    async with db.execute(
        "INSERT INTO users (role, invite_code, device_token) VALUES (?, ?, ?)",
        (role, invite_code, device_token),
    ) as cur:
        user_id = cur.lastrowid

    # devices 레코드 생성
    await db.execute(
        """INSERT INTO devices
           (user_id, device_id, platform, os_version, fcm_token,
            heartbeat_hour, heartbeat_minute)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            device["device_id"],
            device["platform"],
            device.get("os_version"),
            device.get("fcm_token"),
            DEFAULT_HEARTBEAT_HOUR,
            DEFAULT_HEARTBEAT_MINUTE,
        ),
    )

    subscription = None
    if role == "guardian":
        expires_at = (datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%S+09:00"
        )
        await db.execute(
            "INSERT INTO subscriptions (user_id, plan, expires_at) VALUES (?, ?, ?)",
            (user_id, "free_trial", expires_at),
        )
        subscription = {
            "plan": "free_trial",
            "expires_at": expires_at,
            "is_active": True,
        }

    await db.commit()

    return {
        "user_id": user_id,
        "device_token": device_token,
        "invite_code": invite_code,
        "subscription": subscription,
    }
