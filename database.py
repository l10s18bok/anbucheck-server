import os
import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: asyncpg.Pool | None = None


async def get_db():
    """FastAPI 의존성 — 풀에서 연결 획득 후 요청 완료 시 반환"""
    async with _pool.acquire() as conn:
        yield conn


def get_pool() -> asyncpg.Pool:
    return _pool


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await _create_tables(conn)


async def _create_tables(conn: asyncpg.Connection) -> None:
    await conn.execute("""
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    role            TEXT NOT NULL DEFAULT 'subject',
    invite_code     TEXT UNIQUE,
    device_token    TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users (role)")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS devices (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    device_id        TEXT NOT NULL UNIQUE,
    platform         TEXT NOT NULL,
    os_version       TEXT,
    fcm_token        TEXT,
    steps_delta      INTEGER,
    battery_level    INTEGER,
    suspicious_count INTEGER DEFAULT 0,
    heartbeat_hour   INTEGER NOT NULL DEFAULT 9,
    heartbeat_minute INTEGER NOT NULL DEFAULT 30,
    timezone         TEXT NOT NULL DEFAULT 'Asia/Seoul',
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices (last_seen)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_platform ON devices (platform)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_heartbeat_time ON devices (heartbeat_hour, heartbeat_minute)")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS guardians (
    id                  SERIAL PRIMARY KEY,
    subject_user_id     INTEGER NOT NULL REFERENCES users(id),
    guardian_user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(subject_user_id, guardian_user_id)
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_guardians_subject ON guardians (subject_user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_guardians_guardian ON guardians (guardian_user_id)")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    plan         TEXT NOT NULL DEFAULT 'free_trial',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    receipt_data TEXT,
    platform     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions (user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_expires ON subscriptions (expires_at)")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    subject_user_id INTEGER NOT NULL REFERENCES users(id),
    alert_level     TEXT NOT NULL DEFAULT 'warning',
    status          TEXT NOT NULL DEFAULT 'active',
    days_inactive   INTEGER NOT NULL DEFAULT 1,
    last_seen_at    TIMESTAMPTZ NOT NULL,
    push_pending    INTEGER NOT NULL DEFAULT 0,
    last_push_at    TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_subject ON alerts (subject_user_id, created_at DESC)")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS heartbeat_logs (
    id            SERIAL PRIMARY KEY,
    device_id     TEXT NOT NULL,
    steps_delta   INTEGER,
    suspicious    INTEGER DEFAULT 0,
    battery_level INTEGER,
    client_ts     TEXT NOT NULL,
    server_ts     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_heartbeat_device_ts ON heartbeat_logs (device_id, server_ts DESC)")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS app_versions (
    platform       TEXT PRIMARY KEY,
    latest_version TEXT NOT NULL,
    min_version    TEXT NOT NULL,
    store_url      TEXT NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("""
INSERT INTO app_versions (platform, latest_version, min_version, store_url)
VALUES
  ('android', '1.0.0', '1.0.0', 'https://play.google.com/store/apps/details?id=kr.co.anbucheck.app'),
  ('ios', '1.0.0', '1.0.0', 'https://apps.apple.com/app/id000000000')
ON CONFLICT DO NOTHING
""")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS guardian_notification_settings (
    guardian_user_id  INTEGER PRIMARY KEY REFERENCES users(id),
    all_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    urgent_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    warning_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    caution_enabled   BOOLEAN NOT NULL DEFAULT TRUE,
    info_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    dnd_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    dnd_start         TEXT,
    dnd_end           TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
