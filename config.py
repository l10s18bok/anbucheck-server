import os
import json
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "anbu.db")
ADMIN_SECRET_KEY: str = os.getenv("ADMIN_SECRET_KEY", "")
FIREBASE_CREDENTIALS: str = os.getenv("FIREBASE_CREDENTIALS", "")

# Night quiet hours (KST): 22:00 ~ 09:00
QUIET_HOUR_START = 22
QUIET_HOUR_END = 9

# Default heartbeat schedule (18:00 — 퇴근 시각 기준, 하루 활동량 수집 완료 후 전송)
DEFAULT_HEARTBEAT_HOUR = 18
DEFAULT_HEARTBEAT_MINUTE = 0

# Free trial duration in days
FREE_TRIAL_DAYS = 90

# Max subjects per guardian
MAX_SUBJECTS = 5

# Rate limit for /subjects/link (requests per minute)
LINK_RATE_LIMIT = 5
