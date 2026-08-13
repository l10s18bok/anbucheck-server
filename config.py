import os
import json
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL: str = os.getenv("DATABASE_URL", "anbu.db")
ADMIN_SECRET_KEY: str = os.getenv("ADMIN_SECRET_KEY", "")
FIREBASE_CREDENTIALS: str = os.getenv("FIREBASE_CREDENTIALS", "")

# Default heartbeat schedule (18:00 — 퇴근 시각 기준, 하루 활동량 수집 완료 후 전송)
DEFAULT_HEARTBEAT_HOUR = 18
DEFAULT_HEARTBEAT_MINUTE = 0

# 대상자가 설정할 수 있는 heartbeat 예약 시각의 허용 범위(기기 로컬 시각, 시 단위).
#
# ⚠️ 상한 21시는 UX 취향이 아니라 **구조적 제약**이다. services.scheduler의 미수신
# 체크는 발화 시각을 "그날 로컬 자정 + (예약시각 + 2h)"로 계산하고 그 값을 매 분
# now()와 비교하는데, 이 "그날"이 매 tick마다 now()에서 다시 파생된다. 따라서
# `heartbeat_hour * 60 + heartbeat_minute + 120 >= 1440`(= 22시 이상)이면 우변이
# 항상 now()보다 미래라 **등호가 영원히 성립하지 않는다** → 그 대상자는 미수신
# 판정 자체가 실행되지 않아 대상자 안전망 푸시(subject_safety_net)도, 보호자
# caution→warning→urgent 에스컬레이션도, alerts 레코드 생성도 전부 사라진다.
# (조용히 실패하므로 로그로도 드러나지 않는다.)
#
# 상한을 21시로 두면 판정이 최대 23:59에 끝나 같은 날 안에 머문다.
#
# 22시 이상을 허용하려면 발화 시각뿐 아니라 미수신 판정 창(`last_seen < 로컬 자정`)
# 까지 같은 "예약 슬롯 기준"으로 함께 옮겨야 한다 — 발화 시각만 다음 날로 미루면
# 전날 정상 전송한 사용자에게 매일 거짓 미수신 경고가 나간다.
#
# ⚠️ **하한은 의도적으로 두지 않는다(0시 허용). 다시 넣지 말 것.**
# "새벽 예약은 걸음수가 0이라 suspicious 오탐이 난다"는 논리는 주간 생활자만
# 가정한 것이다. `steps_delta`는 "오늘 자정~현재" 누적이므로, 밤에 일하고 아침에
# 잠드는 사람에게는 00:00~07:00 구간이 곧 자기 활동 시간이라 오히려 걸음이 가장
# 잘 잡힌다 — 그들에게 06:00~08:00은 주간 생활자의 18:00과 정확히 같은 자리다.
# 하한을 두면 야간 노동 1인 가구(이 서비스가 특히 필요한 층)가 자기 생활에 맞는
# 시각을 못 고르게 된다. 오설정 방지는 이 배제를 정당화하지 못한다.
#
# 클라이언트 피커 제한(lib/app/core/mixins/heartbeat_schedule_mixin.dart)과
# **반드시 같은 값**을 유지할 것.
HEARTBEAT_HOUR_MIN = 0
HEARTBEAT_HOUR_MAX = 21

# Free trial duration in days
FREE_TRIAL_DAYS = 90

# 보호자별 최대 대상자 등록 인원은 users.max_subjects 컬럼(기본 5)으로 관리한다.
# (과거 MAX_SUBJECTS 전역 상수는 services.subject_service.get_max_subjects 로 대체됨)

# Rate limit (requests per 60s window)
LINK_RATE_LIMIT = int(os.getenv("LINK_RATE_LIMIT", "5"))           # /subjects/link — 보호자(user_id) 기준
REGISTER_RATE_LIMIT = int(os.getenv("REGISTER_RATE_LIMIT", "10"))  # POST /users — 클라이언트 IP 기준

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

# ─────────────────────────────────────────
# Google Cloud Pub/Sub Push (RTDN) 인증
# ─────────────────────────────────────────
# Push subscription 생성 시 지정한 OIDC audience 문자열과 일치해야 함.
# 일반적으로 RTDN 엔드포인트 URL (예: https://anbu.up.railway.app/api/v1/iap/google-notifications)
PUBSUB_AUDIENCE: str = os.getenv("PUBSUB_AUDIENCE", "")
# Pub/Sub Push subscription 생성 시 지정한 service account 이메일.
# 이메일 검증 우회 방지를 위해 토큰의 email claim과 정확히 일치해야 함.
PUBSUB_SERVICE_ACCOUNT_EMAIL: str = os.getenv("PUBSUB_SERVICE_ACCOUNT_EMAIL", "")
