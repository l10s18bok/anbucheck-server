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

# ─────────────────────────────────────────
# 인앱 결제 영수증 검증
# ─────────────────────────────────────────

# Apple App Store Server API
APPLE_IAP_ISSUER_ID: str = os.getenv("APPLE_IAP_ISSUER_ID", "")
APPLE_IAP_KEY_ID: str = os.getenv("APPLE_IAP_KEY_ID", "")
# Railway 등 single-line 환경변수는 PEM 줄바꿈을 `\n`으로 이스케이프해서 저장하므로 복원
APPLE_IAP_KEY_P8: str = os.getenv("APPLE_IAP_KEY_P8", "").replace("\\n", "\n")
APPLE_BUNDLE_ID: str = os.getenv("APPLE_BUNDLE_ID", "kr.co.anbucheck.live")

# Google Play Developer API
GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_PACKAGE_NAME: str = os.getenv("GOOGLE_PACKAGE_NAME", "kr.co.anbucheck.live")

# 단일 구독 상품 ID (Apple/Google 공통)
IAP_PRODUCT_ID: str = "anbu_yearly"
