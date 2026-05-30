import os
from contextlib import asynccontextmanager

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: asyncpg.Pool | None = None

# ─────────────────────────────────────────────────────────────
# Postgres advisory lock 키 (멀티 인스턴스 단일 실행 보장용)
#
# 앱을 N개 인스턴스(Railway 레플리카 또는 uvicorn --workers)로 띄워도
# 스케줄러 잡과 startup DDL이 단 하나의 인스턴스에서만 실행되도록 직렬화한다.
# 각 잡/작업마다 프로세스 전역에서 유일한 고정 정수 키를 부여한다.
# ─────────────────────────────────────────────────────────────
LOCK_DDL = 100               # startup CREATE TABLE / 마이그레이션
LOCK_HEARTBEAT_CHECK = 1     # 매 분 미수신 경고 체크
LOCK_CLEANUP_NOTI = 2        # 자정 알림 정리
LOCK_SUB_EXPIRE = 3          # 구독 만료 체크
LOCK_CLEANUP_SUBJECTS = 4    # 미연결 대상자 정리
LOCK_CLEANUP_LOGS = 5        # heartbeat_logs 30일 초과 삭제


async def get_db():
    """FastAPI 의존성 — 풀에서 연결 획득 후 요청 완료 시 반환"""
    async with _pool.acquire() as conn:
        yield conn


def get_pool() -> asyncpg.Pool:
    return _pool


@asynccontextmanager
async def try_advisory_lock(key: int):
    """세션 레벨 pg_try_advisory_lock을 전용 커넥션에 건다 (non-blocking).

    획득 성공 시 acquired=True를 yield하고, 실패하면 False를 yield해 호출부가
    즉시 양보(잡 스킵)하도록 한다. 멀티 인스턴스에서 같은 분에 여러 스케줄러가
    동시에 잡을 fire해도 락을 잡은 하나만 실행된다.

    asyncpg 함정 대응:
    - advisory lock은 세션(커넥션) 레벨이므로 잡는 커넥션과 푸는 커넥션이 같아야 한다.
      전용 커넥션 1개를 직접 acquire/release하고, 잡 본문은 별도 커넥션을 쓴다.
    - try/finally로 반드시 unlock + 커넥션 반납 → 풀로 돌아간 커넥션에 락이 남는
      누수를 차단. (커넥션을 그냥 닫아도 세션 종료로 락은 풀리지만, 풀 재사용을
      위해 명시적으로 unlock 후 release 한다.)
    """
    conn = await _pool.acquire()
    acquired = False
    try:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
        yield acquired
    finally:
        if acquired:
            try:
                await conn.fetchval("SELECT pg_advisory_unlock($1)", key)
            except Exception:
                # unlock 실패해도 커넥션 반납 시 세션 종료로 락은 풀린다.
                pass
        await _pool.release(conn)


async def init_pool() -> None:
    global _pool
    # 풀 크기는 env로 외부화 — N 인스턴스 × max_size가 Postgres max_connections를
    # 넘지 않도록 운영에서 조정한다 (스케일 시 "too many connections" 방지).
    min_size = int(os.environ.get("DB_POOL_MIN", "2"))
    max_size = int(os.environ.get("DB_POOL_MAX", "10"))
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=min_size, max_size=max_size)

    # startup DDL을 advisory lock으로 직렬화 — 멀티 인스턴스 동시 기동 시
    # CREATE INDEX / ALTER TABLE 동시 실행 레이스를 차단한다. blocking lock이라
    # 후발 인스턴스는 선행 인스턴스의 DDL 완료를 기다린 뒤 IF NOT EXISTS no-op만 수행.
    async with _pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_DDL)
        try:
            await _create_tables(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_DDL)


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
    last_steps       INTEGER,
    battery_level    INTEGER,
    suspicious_count INTEGER DEFAULT 0,
    heartbeat_hour   INTEGER NOT NULL DEFAULT 18,
    heartbeat_minute INTEGER NOT NULL DEFAULT 0,
    timezone         TEXT NOT NULL DEFAULT 'Asia/Seoul',
    locale           TEXT NOT NULL DEFAULT 'ko_KR',
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
    push_count      INTEGER NOT NULL DEFAULT 0,
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
    server_ts     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scheduled_key TEXT
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_heartbeat_device_ts ON heartbeat_logs (device_id, server_ts DESC)")

    # scheduled_key 마이그레이션 (기존 DB 대응) — HTTP 재전송 중복 차단용 idempotency key.
    # 자동 heartbeat: "YYYY-MM-DD_HH:MM" 형식, 수동 보고(manual=true)는 NULL.
    # NULL은 partial unique index에서 제외되므로 수동 보고 여러 건은 여전히 허용.
    await conn.execute("""
ALTER TABLE heartbeat_logs ADD COLUMN IF NOT EXISTS scheduled_key TEXT
""")
    await conn.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_heartbeat_device_schedkey
ON heartbeat_logs (device_id, scheduled_key)
WHERE scheduled_key IS NOT NULL
""")

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
CREATE TABLE IF NOT EXISTS notification_events (
    id                  SERIAL PRIMARY KEY,
    subject_user_id     INTEGER NOT NULL,
    invite_code         TEXT,
    alert_level         TEXT NOT NULL,
    message_key         TEXT,
    message_params      TEXT,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ne_subject_created ON notification_events (subject_user_id, created_at DESC)")

    # notification_events message_key/message_params 마이그레이션
    await conn.execute("""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='notification_events' AND column_name='message_key'
    ) THEN
        ALTER TABLE notification_events ADD COLUMN message_key TEXT;
        ALTER TABLE notification_events ADD COLUMN message_params TEXT;
    END IF;
END$$;
""")

    # notification_events 위치 컬럼 마이그레이션 (긴급 요청 시 위치 전달)
    await conn.execute("""
ALTER TABLE notification_events
    ADD COLUMN IF NOT EXISTS location_lat DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS location_lng DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS location_accuracy DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS location_captured_at TIMESTAMPTZ
""")

    # locale 컬럼 마이그레이션 (기존 DB 대응)
    await conn.execute("""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='devices' AND column_name='locale'
    ) THEN
        ALTER TABLE devices ADD COLUMN locale TEXT NOT NULL DEFAULT 'ko_KR';
    END IF;
END$$;
""")

    # last_steps 컬럼 마이그레이션 (기존 DB 대응)
    await conn.execute("""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='devices' AND column_name='last_steps'
    ) THEN
        ALTER TABLE devices ADD COLUMN last_steps INTEGER;
    END IF;
END$$;
""")

    # push_pending → push_count 컬럼 마이그레이션 (기존 DB 대응)
    await conn.execute("""
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='alerts' AND column_name='push_pending'
    ) THEN
        ALTER TABLE alerts RENAME COLUMN push_pending TO push_count;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='alerts' AND column_name='push_count'
    ) THEN
        ALTER TABLE alerts ADD COLUMN push_count INTEGER NOT NULL DEFAULT 0;
    END IF;
END$$;
""")

    await conn.execute("""
CREATE TABLE IF NOT EXISTS dismissed_notifications (
    id                  SERIAL PRIMARY KEY,
    guardian_user_id    INTEGER NOT NULL REFERENCES users(id),
    event_id            INTEGER NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
    dismissed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(guardian_user_id, event_id)
)
""")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_dn_guardian ON dismissed_notifications (guardian_user_id)")

    # 전역 일관 rate limiting — 인메모리 대신 Postgres에 저장해 멀티 인스턴스에서
    # 카운터가 공유된다 (인메모리면 인스턴스별이라 한도가 N배로 샌다).
    # 고정 윈도우(60s): UPSERT 원자 증가로 cross-instance 동시 요청을 직렬화.
    await conn.execute("""
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket       TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
)
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
