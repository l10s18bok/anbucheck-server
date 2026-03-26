import aiosqlite
from config import DATABASE_URL

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DATABASE_URL)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL;")
        await _db.execute("PRAGMA foreign_keys=ON;")
        await _db.commit()
    return _db


async def init_db() -> None:
    db = await get_db()
    await db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    role            TEXT NOT NULL DEFAULT 'subject',
    invite_code     TEXT UNIQUE,
    device_token    TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);


CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    device_id        TEXT NOT NULL UNIQUE,
    platform         TEXT NOT NULL,
    os_version       TEXT,
    fcm_token        TEXT,
    accel_x          REAL,
    accel_y          REAL,
    accel_z          REAL,
    gyro_x           REAL,
    gyro_y           REAL,
    gyro_z           REAL,
    battery_level    INTEGER,
    suspicious_count INTEGER DEFAULT 0,
    heartbeat_hour   INTEGER NOT NULL DEFAULT 9,
    heartbeat_minute INTEGER NOT NULL DEFAULT 30,
    timezone         TEXT NOT NULL DEFAULT 'Asia/Seoul',
    last_seen        TEXT NOT NULL DEFAULT (datetime('now')),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices (last_seen);
CREATE INDEX IF NOT EXISTS idx_devices_platform ON devices (platform);
CREATE INDEX IF NOT EXISTS idx_devices_heartbeat_time ON devices (heartbeat_hour, heartbeat_minute);


CREATE TABLE IF NOT EXISTS guardians (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_user_id     INTEGER NOT NULL REFERENCES users(id),
    guardian_user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(subject_user_id, guardian_user_id)
);

CREATE INDEX IF NOT EXISTS idx_guardians_subject ON guardians (subject_user_id);
CREATE INDEX IF NOT EXISTS idx_guardians_guardian ON guardians (guardian_user_id);


CREATE TABLE IF NOT EXISTS subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    plan         TEXT NOT NULL DEFAULT 'free_trial',
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    receipt_data TEXT,
    platform     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_expires ON subscriptions (expires_at);


CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_user_id INTEGER NOT NULL REFERENCES users(id),
    alert_level     TEXT NOT NULL DEFAULT 'warning',
    status          TEXT NOT NULL DEFAULT 'active',
    days_inactive   INTEGER NOT NULL DEFAULT 1,
    last_seen_at    TEXT NOT NULL,
    push_pending    INTEGER NOT NULL DEFAULT 0,
    last_push_at    TEXT,
    resolved_at     TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status);
CREATE INDEX IF NOT EXISTS idx_alerts_subject ON alerts (subject_user_id, created_at DESC);


CREATE TABLE IF NOT EXISTS heartbeat_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL,
    source        TEXT,
    accel_x       REAL,
    accel_y       REAL,
    accel_z       REAL,
    gyro_x        REAL,
    gyro_y        REAL,
    gyro_z        REAL,
    suspicious    INTEGER DEFAULT 0,
    battery_level INTEGER,
    client_ts     TEXT NOT NULL,
    server_ts     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_heartbeat_device_ts ON heartbeat_logs (device_id, server_ts DESC);


CREATE TABLE IF NOT EXISTS app_versions (
    platform       TEXT PRIMARY KEY,
    latest_version TEXT NOT NULL,
    min_version    TEXT NOT NULL,
    store_url      TEXT NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO app_versions (platform, latest_version, min_version, store_url)
VALUES
  ('android', '1.0.0', '1.0.0', 'https://play.google.com/store/apps/details?id=kr.co.anbucheck.app'),
  ('ios', '1.0.0', '1.0.0', 'https://apps.apple.com/app/id000000000');


CREATE TABLE IF NOT EXISTS guardian_notification_settings (
    guardian_user_id  INTEGER PRIMARY KEY REFERENCES users(id),
    all_enabled       INTEGER NOT NULL DEFAULT 1,
    urgent_enabled    INTEGER NOT NULL DEFAULT 1,
    warning_enabled   INTEGER NOT NULL DEFAULT 1,
    caution_enabled   INTEGER NOT NULL DEFAULT 1,
    info_enabled      INTEGER NOT NULL DEFAULT 1,
    dnd_enabled       INTEGER NOT NULL DEFAULT 0,
    dnd_start         TEXT,
    dnd_end           TEXT,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
""")
    await db.commit()

    # 컬럼 마이그레이션 — 기존 테이블에 누락된 컬럼 추가
    migrations = [
        "ALTER TABLE guardian_notification_settings ADD COLUMN dnd_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE guardian_notification_settings ADD COLUMN dnd_start TEXT",
        "ALTER TABLE guardian_notification_settings ADD COLUMN dnd_end TEXT",
    ]
    for sql in migrations:
        try:
            await db.execute(sql)
        except Exception:
            pass  # 이미 존재하는 컬럼이면 무시
    await db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
