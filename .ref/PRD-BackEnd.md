# 안부 확인 앱 - BackEnd PRD


## 1. 개요


### 1.1 프로젝트명
**Anbu** (안부)

| 항목 | 값 |
|------|-----|
| 도메인 | `anbucheck.co.kr` |
| Android 패키지명 | `kr.co.anbucheck.app` |
| iOS Bundle ID | `kr.co.anbucheck.app` |
| 인앱 결제 상품 ID | `anbu_yearly` |


### 1.2 목적
스마트폰 사용 패턴을 기반으로 사용자의 안녕을 자동으로 확인하는 크로스 플랫폼(Android/iOS) 시스템의 서버 측 설계.
클라이언트로부터 heartbeat를 수신하고, 매일 고정 시각(기본 18:00) + 2시간 내 미수신 시 보호자에게 FCM Push 경고를 발송하는 역할을 담당한다. Heartbeat 트리거는 클라이언트 자체 스케줄링(WorkManager/BGTaskScheduler)으로 동작하며, 서버는 Silent Push를 발송하지 않는다.

**기술 스택:** Python + FastAPI + PostgreSQL (asyncpg) + Railway

**이 앱은 응급상황 알림 앱이 아니다.** 사용자의 일상적 안부 확인만을 목적으로 한다.


### 1.3 핵심 가치
- **제로 인터랙션**: 사용자가 별도 조작 없이 자동으로 안부 신호를 보고
- **최소 배터리 소모**: 상시 백그라운드 실행 없이, OS 네이티브 메커니즘으로 매일 1회 확인
- **신뢰성**: 거짓 경고(false alarm) 최소화
- **크로스 플랫폼**: Android/iOS 동시 지원, 앱 심사 통과 용이한 설계
- **단순함**: 대상 사용자(독거노인 등)가 설치 후 신경 쓸 것 없는 앱
- **개인정보 미수집**: 이름, 전화번호 등 개인정보를 서버에 저장하지 않음


### 1.4 개인정보 보호 원칙
- 서버에 **이름, 전화번호 등 개인정보를 일절 저장하지 않음**
- 대상자-보호자 연결은 서버가 발급한 **고유 코드(invite_code)**로 매칭
- DB가 유출되어도 개인 식별 불가 (device_id, invite_code만 존재)
- 보호자가 대상자를 식별하기 위한 별칭은 클라이언트 로컬에만 저장
- **위치정보는 정기 heartbeat에서 수집하지 않음.** 대상자가 `POST /api/v1/emergency` 호출 시 사용자 동의 하에 lat/lng/accuracy를 1회 첨부할 수 있으며, 서버는 `notification_events` 테이블에만 저장하고 대상자 기기 타임존 자정 스케줄러가 다른 알림과 함께 일괄 삭제 (§4.7 / §6.4 참조)


### 1.5 수익 모델
- **보호자가 결제**: 3개월 무료 체험 → 이후 연 $9.99 자동 갱신 구독
- **대상자 앱은 완전 무료** (결제 기능 없음, heartbeat 전송만 담당)
- **대상자 최대 5명** (현재 단일 요금·티어 구분 없음 — 단, 한도는 보호자별 `users.max_subjects`(기본 5)로 관리되며 향후 유료 결제로 상향 가능하도록 동적화됨. `get_max_subjects()` 헬퍼가 단일 출처)
- **하단 고정 배너 광고** (유료 구독 보호자는 광고 제거)


### 1.6 플랫폼별 모드 제한

| 플랫폼 | 대상자 모드 | 보호자 모드 | 비고 |
|--------|------------|------------|------|
| **Android** | ✅ 지원 | ✅ 지원 | heartbeat 전송 + 모니터링 모두 가능 |
| **iOS** | ❌ 미지원 | ✅ 전용 | 보호자 모니터링만 가능 (heartbeat 전송 안 함) |

- iOS는 BGTaskScheduler의 불안정성(앱 스와이프 종료 시 미실행)으로 대상자 heartbeat 전송 신뢰성을 보장할 수 없어 보호자 모드만 지원
- 서버 관점에서 iOS 기기의 role은 항상 `guardian` — heartbeat 수신 대상이 아님
- 대상자 등록은 Android 기기에서만 발생


---


## 2. 안부 확인 아키텍처 (서버 관점)

> 📊 **전체 플로우차트**: [heartbeat_flowchart.md](heartbeat_flowchart.md) 참조
> - 차트 2: 서버 Heartbeat 수신 후 판정 플로우 (suspicious 판정, 경고 해소/하향)
> - 차트 3: Heartbeat 미수신 시 경고 플로우 (배터리 정보/주의/경고/긴급 등급)
> - 차트 4: 경고 등급 상태도 (정상/주의/경고/긴급)
> - 차트 5: 경고 등급 최종 확정 테이블


### 2.1 서버의 역할

```
┌─────────────────────────────────────────────────────────────┐
│                   서버 (Python FastAPI)                       │
│                      Railway 배포                            │
│                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ API Router   │  │  Scheduler   │  │   Push Service     │  │
│  │             │  │ (APScheduler)│  │ (firebase-admin)   │  │
│  │ · 사용자 등록│  │              │  │                    │  │
│  │ · heartbeat │  │ · 미수신     │  │ · 일반 Push 발송   │  │
│  │ · 대상자    │  │   경고 체크  │  │   (보호자 알림)    │  │
│  │   연결 관리 │  │   (매 1분)   │  │                    │  │
│  │ · 구독 관리  │  │ · 경고 생성  │  │                    │  │
│  │ · FCM 토큰  │  │   (미수신 시) │  │                    │  │
│  │ · 알림 설정 │  │ · 구독 만료  │  │                    │  │
│  │   갱신      │  │   체크       │  │                    │  │
│  └──────┬──────┘  └──────┬───────┘  └─────────┬──────────┘  │
│         │               │                    │              │
│         └───────────────┼────────────────────┘              │
│                         │                                   │
│                  ┌──────▼───────┐                           │
│                  │  PostgreSQL   │                           │
│                  │  (Railway)   │                           │
│                  └──────────────┘                           │
└─────────────────────────────────────────────────────────────┘
```


### 2.2 OS별 서버 동작 차이

| | Android 기기 | iOS 기기 |
|--|-------------|----------|
| heartbeat 트리거 | **클라이언트 WorkManager** (OS 네이티브 스케줄링) | **클라이언트 BGTaskScheduler** (OS 네이티브 스케줄링) |
| 서버 역할 | heartbeat 수신 + 미수신 시 경고 생성 | heartbeat 수신 + 미수신 시 경고 생성 |
| 확인 주기 | 매일 고정 시각 (기본 18:00) | 매일 고정 시각 (기본 18:00) |
| 미수신 체크 | 기기별 heartbeat 시각 + 2시간 경과 시 서버가 매 1분 체크 | 동일 |
| 시각 변경 | 대상자가 변경 → 서버 DB 갱신 → 클라이언트 WorkManager 재예약 | 대상자가 변경 → 서버 DB 갱신 → 클라이언트 BGTask 재예약 |


### 2.3 대상자-보호자 연결 메커니즘

```
[1단계: 대상자 등록]
대상자 앱 → POST /api/v1/users { role: "subject", device: {...} }
서버:
  · users 레코드 생성
  · invite_code 생성: "K7M-4PXR" (7자리 영숫자, UNIQUE 보장)
  · device_token 발급
  · 응답에 invite_code 포함

[2단계: 보호자 등록]
보호자 앱 → POST /api/v1/users { role: "guardian", device: {...} }
서버:
  · users 레코드 생성
  · device_token 발급
  · 아직 연결된 대상자 없음

[3단계: 보호자가 대상자 연결]
보호자 앱 → POST /api/v1/subjects/link { invite_code: "K7M-4PXR" }
서버:
  · invite_code로 대상자 조회
  · guardians 테이블에 매핑 생성
  · 연결 완료

[서버 DB 상태]
users:     { id:1, role:"subject", invite_code:"K7M-4PXR" }
users:     { id:2, role:"guardian", invite_code: NULL }
devices:   { user_id:1, fcm_token:"토큰A" }
devices:   { user_id:2, fcm_token:"토큰B" }
guardians: { subject_user_id:1, guardian_user_id:2 }
```

**고유 코드(invite_code) 생성 규칙:**
- 7자리 영숫자 (대문자 A-Z + 숫자 0-9)
- 포맷: XXX-XXXX (3자리-4자리, 하이픈 구분)
- DB에 UNIQUE 제약조건 → 생성 시 중복 체크 후 발급
- 충돌 시 재생성 (36^7 = 약 783억 조합, 충돌 확률 극히 낮음)


### 2.4 경고 발생 흐름

> 📊 상세 플로우차트: [heartbeat_flowchart.md](heartbeat_flowchart.md) — 차트 2, 3, 5 참조

**경고 등급 최종 확정 테이블 (4단계):**

| 등급 | 조건 | 발송 |
|------|------|------|
| 🚨 긴급 | 미수신 3회+ OR suspicious 3회+ | 매일 반복, 보호자 확인까지 종료 없음 |
| ⚠ 경고 | 미수신 2회 OR suspicious 2회 | 1~2회 다음날 재발송 |
| ⚠ 주의 | 미수신 1회 OR suspicious 1회 | 1회 발송 |
| 🔵 정보 | 배터리 ≤ 10% / 자동 heartbeat 정상 수신 / 정상복귀 / 수동 heartbeat | DND 적용 (시간 외 소리, 시간 내 조용) |

```
[heartbeat 수신 시]
heartbeat 수신 → last_seen 갱신
  ├─ is_first_today 판정 (기기 로컬 타임존 자정 이후 수신 이력)
  │   · heartbeat_logs INSERT **전**에 조회해야 정확 (INSERT 이후엔 항상 false)
  │   · auto_report/steps 알림 **중복 방지에만** 사용. heartbeat_logs INSERT는 매번 수행 (이력·차트용)
  ├─ 오늘(기기 로컬 타임존) 이미 heartbeat 수신한 경우 → suspicious 강제 false (하루 첫 heartbeat만 판정)
  ├─ battery_level ≤ 10% + 기존 info 경고 없음 → 정보 등급 1회 발송 (DND 적용)
  └─ suspicious 판정:
      ├─ false → 활성 경고 해소 (resolve_active_alerts → resolved_levels 반환)
      │   ├─ caution/warning/urgent 중 **하나라도** 해소 → 보호자 Push "정상 복귀" (정보 등급 DND 적용)
      │   │                                              auto_report는 중복이라 스킵
      │   ├─ info(배터리)만 해소 또는 해소 없음
      │   │   ├─ manual = true  → 보호자 Push "수동 안부 확인" (사용자 의도적 액션이므로 매번 발송, is_first_today 가드 없음)
      │   │   └─ manual = false AND is_first_today → 보호자 Push "오늘 안부 확인 완료" (당일 1회)
      │   │                                          + steps_delta > 0이면 활동 정보 알림 동시 생성 (당일 1회)
      └─ true  → warning/urgent → caution 하향 (정상 복귀 알림 없음)
               → suspicious_count 기반 보호자 경고 에스컬레이션 (suspicious 전용 문구 사용):
                 ├─ 1회 (suspicious_count=1) → 주의(caution) 등급 + `caution_suspicious` + 보호자 Push (중복 방지)
                 ├─ 2회 (suspicious_count=2) → 경고(warning) 등급 + `warning_suspicious` + 보호자 Push (warning/urgent 없을 때만)
                 ├─ 3회+ (suspicious_count≥3) → 긴급(urgent) 등급 + `urgent_suspicious` + 보호자 Push (매일 반복)
                 └─ 보호자 경고 클리어 시 suspicious_count 리셋 → 다음 suspicious부터 1차 재시작
               ※ suspicious 경로는 heartbeat는 수신되었으나 활동 기록(걸음 + 발화 시점
                  기기 사용)이 없는 경우이므로 scheduler의 미수신 경로(`warning`/`urgent`)와
                  별도 문구로 분기됨

[heartbeat 미수신 시 (기기별 고정 시각 + 2시간 경과 시 체크)]
지정 시각 + 2시간 내 미수신 대상자 감지 (기본: 18:00 → 20:00 체크)
  ├─ 보호자 구독 만료 → 알림 미발송 (heartbeat는 계속 수신)
  ├─ battery_level ≤ 10% → 정보 등급 1회 발송 후 종료 (이후 상향 없음)
  └─ 누적 미수신 횟수 기반 (기존 활성 경고 상태로 결정):
      ├─ 활성 경고 없음   → 1회 미수신 → 주의 등급
      ├─ caution 활성    → 2회 미수신 → 경고 등급
      ├─ warning 활성    → 3회 이상   → 긴급 등급
      └─ urgent 활성     → 긴급 지속 (days_inactive++, 반복 발송)

[보호자가 경고 클리어 시]
활성 경고 전부 삭제 + suspicious_count 리셋
이후 heartbeat가 여전히 없으면 → 다음 날 같은 시각에 1차 경고부터 재시작

[대상자 긴급 도움 요청 (POST /api/v1/emergency)]
대상자가 앱에서 직접 긴급 버튼 탭 → 확인 다이얼로그 → 전송
  ├─ 기존 경고 에스컬레이션(suspicious_count, days_inactive)과 완전히 독립
  ├─ 즉시 urgent 등급 경고 생성 (caution→warning→urgent 단계 생략)
  │   · alerts 테이블: alert_level='urgent', note='emergency_request'
  │   · notification_events 테이블: message_key='emergency'
  ├─ 연결된 보호자 전원에게 긴급 Push 발송 (DND 무시, 구독 만료 무관)
  └─ 기존 카운터(suspicious_count, days_inactive) 변경하지 않음
```

- 경고는 **서버에서 직접 보호자 기기에 FCM Push**로 발송
- SMS, 카카오톡 등 외부 메시징 서비스는 사용하지 않음
- 보호자 구독 만료 시 해당 보호자에게 경고를 발송하지 않음

**경고 판정 및 발송:**
- 서버는 각 기기의 `heartbeat_hour` + 2시간 경과 시점에 미수신 여부를 체크
- **야간 발송 제한 (모든 등급 공통):** 22:00~09:00 사이에 판정된 경고는 즉시 발송하지 않고, **다음 날 오전 09:00에 일괄 발송**
  - 예: 새벽 02:00에 긴급 등급 판정 → 오전 09:00에 발송
  - 예: 밤 23:00에 주의 등급 판정 → 다음 날 오전 09:00에 발송
  - 서버는 판정 시점에 경고를 DB에 기록하되, `scheduled_send_at`을 다음 날 09:00으로 설정
- 보호자가 heartbeat 시각을 변경하면 경고 발송 시각도 자동으로 조정됨

**경고 Push 메시지 (개인정보 미포함):**
```json
// 정보 등급 — 배터리 방전 추정
{
  "notification": { "title": "🔋 배터리 방전 추정", "body": "대상자의 폰이 배터리 방전으로 꺼진 것 같습니다. 충전 후 자동으로 정상 복귀됩니다." },
  "data": { "type": "alert_info", "reason": "battery_dead", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 주의 등급 — 예정 시각+2시간 초과 1회 미수신
{
  "notification": { "title": "⚠ 안부 확인 필요", "body": "오늘 대상자의 안부 확인이 아직 없습니다. 직접 안부를 확인해 보시기 바랍니다." },
  "data": { "type": "alert_caution", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 경고 등급 — heartbeat 미수신
{
  "notification": { "title": "⚠ 안부 확인", "body": "대상자의 오늘 안부 확인이 없습니다. 통신 불가 상태일 수 있습니다." },
  "data": { "type": "alert_warning", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 긴급 등급 — 즉시 확인 필요
{
  "notification": { "title": "🚨 긴급: 대상자 확인 필요", "body": "N일 연속 활동 기록이 감지되지 않습니다. 즉시 확인이 필요합니다." },
  "data": { "type": "alert_urgent", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 긴급 등급 — 대상자 직접 긴급 도움 요청
{
  "notification": { "title": "🚨 긴급 도움 요청", "body": "보호 대상자가 직접 도움을 요청했습니다. 즉시 확인해 주세요." },
  "data": { "type": "alert_emergency", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 정보 등급 — 자동 heartbeat 정상 수신
{
  "notification": { "title": "✅ 오늘 안부 확인 완료", "body": "대상자의 오늘 안부 확인이 정상 수신되었습니다." },
  "data": { "type": "auto_report", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 정보 등급 — 수동 안부 확인
{
  "notification": { "title": "✅ 수동 안부 확인", "body": "대상자께서 직접 안부 확인을 보냈습니다." },
  "data": { "type": "manual_report", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}

// 정보 등급 — 경고 자동 해소 (heartbeat 복구 시)
{
  "notification": { "title": "✅ 안부 확인", "body": "대상자의 안부 확인이 정상 복귀되었습니다." },
  "data": { "type": "alert_resolved", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
}
```
- 서버에 이름이 없으므로 Push 본문에 "대상자"로 표시
- 보호자 앱이 Push 수신 시 로컬 별칭으로 치환하여 표시 가능


---


## 3. 프로젝트 구조

```
server/
├── main.py                         # 엔트리포인트 (FastAPI 앱 생성 + uvicorn 실행)
├── config.py                       # 환경변수 기반 설정
├── database.py                     # PostgreSQL 연결 풀 (asyncpg) + 테이블 초기화
├── routers/
│   ├── user.py                     # POST /api/v1/users, DELETE /api/v1/users/me
│   ├── heartbeat.py                # POST /api/v1/heartbeat
│   ├── subject.py                  # POST /api/v1/subjects/link, GET /api/v1/subjects, DELETE unlink
│   ├── alert.py                    # GET /api/v1/alerts, PUT clear, PUT clear-all
│   ├── device.py                   # GET /api/v1/devices/me, PUT fcm-token, PATCH/PUT heartbeat-schedule
│   ├── app_version.py              # GET /api/v1/app/version-check, PUT/GET /api/v1/admin/app-version
│   ├── subscription.py             # GET/POST /api/v1/subscription
│   ├── guardian_notification_settings.py  # GET/PUT /api/v1/guardian/notification-settings
│   ├── notifications.py            # GET/DELETE /api/v1/notifications
│   ├── emergency.py                # POST /api/v1/emergency (긴급 도움 요청)
│   └── iap_notification.py         # POST /api/v1/iap/google-notifications, /apple-notifications (RTDN 수신)
├── services/
│   ├── user_service.py             # 사용자 등록, invite_code 생성
│   ├── heartbeat_service.py        # heartbeat 비즈니스 로직
│   ├── alert_service.py            # 경고 생성/클리어/보호자 Push 발송, DND 판정
│   ├── emergency_service.py        # 긴급 도움 요청 처리 (즉시 urgent 경고 생성 + Push)
│   ├── subject_service.py          # 대상자-보호자 연결 관리
│   ├── push_service.py             # 일반 Push 발송 (보호자 경고 알림)
│   ├── subscription_service.py     # 구독 상태 관리 (영수증 검증 + DB 반영)
│   ├── iap_verify_service.py       # Apple/Google IAP API 호출 (verify + acknowledge)
│   ├── iap_notification_service.py # RTDN 페이로드 분기 + DB UPDATE (Google + Apple 공용)
│   ├── scheduler.py                # APScheduler 미수신 경고 체크 + 구독 만료 + 정리 작업 + 잡 오류 Discord 알림 리스너
│   └── notify.py                   # Discord 에러 웹훅 알림 + 최근 로그 링버퍼 (§10.5 운영 모니터링)
├── models/
│   ├── user.py                     # Pydantic 모델 (요청/응답 스키마)
│   ├── device.py
│   ├── guardian.py
│   ├── alert.py
│   ├── heartbeat.py
│   ├── subscription.py
│   ├── app_version.py
│   └── notification_settings.py
├── middleware/
│   └── auth.py                     # device_token 기반 인증 (FastAPI Depends)
├── requirements.txt                # Python 패키지 목록
└── Dockerfile                      # Railway 배포용 (python main.py)
```


---


## 4. API 설계


### 4.1 사용자 등록 (대상자)
```
POST /api/v1/users
Body:
{
  "role": "subject",
  "device": {
    "device_id": "uuid-v4",
    "fcm_token": "fcm-token-string",
    "platform": "android",
    "os_version": "Android 14",
    "timezone": "Asia/Seoul"
  }
}
Response: 201 Created
{
  "user_id": 1,
  "invite_code": "K7M-4PXR",
  "device_token": "generated-bearer-token",
  "existing_role": null
}
```

- 기기 정보와 역할만 전송
- `invite_code`��� 서버가 생성 (7자리 영숫자, UNIQUE 보장)
- `device_token`은 **만료 없이 무제한** ��효
- `timezone`: IANA 타임존 문자열 (기본값 `Asia/Seoul`)
- `existing_role`: 동일 device_id로 재등록 시 기존 역할 반환 (요청 role과 다를 때만 포함, 동일하면 `null`)
- 대��자는 구독(subscription) 없음 — 결제는 보���자가 담당
- 서버는 `users`, `devices` 테이블에 각각 레코드 생성


### 4.2 사용자 등록 (보호자)
```
POST /api/v1/users
Body:
{
  "role": "guardian",
  "device": {
    "device_id": "uuid-v4",
    "fcm_token": "fcm-token-string",
    "platform": "ios",
    "os_version": "iOS 18"
  }
}
Response: 201 Created
{
  "user_id": 2,
  "device_token": "generated-bearer-token",
  "subscription": {
    "plan": "free_trial",
    "expires_at": "2026-06-18T00:00:00+09:00",
    "is_active": true
  }
}
```

- 보호자는 `invite_code` 불필요 (대상자만 보유)
- 등록 시점에 3개월 무료 체험 구독 자동 생성
- 등록 시점에는 연결된 대상자 없음 → 이후 `/api/v1/subjects/link`로 연결
- 서버는 `users`, `devices`, `subscriptions` 테이블에 각각 레코드 생성


### 4.3 대상자 연결 (보호자 → 고유 코드 입력)
```
POST /api/v1/subjects/link
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "invite_code": "K7M-4PXR"
}
Response: 200 OK
{
  "guardian_id": 1,
  "subject": {
    "user_id": 1,
    "invite_code": "K7M-4PXR",
    "last_seen": "2026-03-18T14:32:00+09:00",
    "status": "normal"
  }
}
```

- invite_code가 존재하지 않으면 404 에러
- 이미 연결된 대상자이면 409 에러
- **자기 자신(보호자 본인의 invite_code)을 연결하려 하면 400 에러** (`"자기 자신을 대상자로 연결할 수 없습니다"`). G+S(Guardian+Subject) 보호자만 invite_code를 가지므로 이 경우에만 발생. 클라이언트는 본인 코드를 사전 비교해 API 호출 전에 차단하고(`add_subject_error_self`), 이 400은 백스톱
- 연결된 대상자가 **보호자별 최대 인원(`users.max_subjects`, 기본 5)** 이상이면 400 에러 (`"대상자는 최대 {max_subjects}명까지 등록 가능합니다"` — 메시지의 숫자도 보호자별 값으로 동적). 클라이언트는 `can_add_more`로 사전 차단(`add_subject_error_limit`, `@max`=서버 `max_subjects`)하고, 이 400은 stale 캐시 race용 백스톱
- 보호자의 구독이 만료된 경우에도 연결은 가능 (구독 복구 시 즉시 서비스 시작)


### 4.4 연결된 대상자 목록 조회 (보호자용)
```
GET /api/v1/subjects
Headers:
  Authorization: Bearer <device_token>
Response: 200 OK
{
  "subjects": [
    {
      "guardian_id": 1,
      "user_id": 1,
      "invite_code": "K7M-4PXR",
      "last_seen": "2026-03-18T14:32:00+09:00",
      "status": "normal",
      "alert": null,
      "device_id": "uuid-v4",
      "heartbeat_hour": 18,
      "heartbeat_minute": 0,
      "battery_level": 85
    }
  ],
  "max_subjects": 5,
  "can_add_more": true
}
```

- `max_subjects`: 보호자별 최대 대상자 등록 인원. `users.max_subjects` 컬럼(기본 5)에서 조회한 동적 값 — 과거 전역 상수 `MAX_SUBJECTS`는 폐지되고 `subject_service.get_max_subjects(db, guardian_user_id)` 헬퍼로 일원화됨(한도 체크·에러 문구·이 응답 4곳 공통). 유료 결제로 한도를 상향하는 기획 시 결제 검증 시점에 `UPDATE users SET max_subjects` 만으로 반영
- `can_add_more`: `len(subjects) < max_subjects`
- `status`: `normal` (정상), `warning` (경고)
- `alert`: 활성 경고가 있으면 `{ "id": 10, "days_inactive": 2 }`, 없으면 `null`
- `device_id`: 대상자 기기 고유 ID
- `heartbeat_hour`, `heartbeat_minute`: 대상자의 heartbeat 예약 시각
- `battery_level`: 마지막 heartbeat 시 배터리 잔량 (0~100, 없으면 `null`)
- `weekly_steps`: 대상자 로컬 타임존 기준 최근 7일 일별 최대 걸음수 배열 (index 0 = 6일 전, 마지막 = 오늘). `users.created_at` 이전 날짜는 `null`(등록 전), 이후 heartbeat 없는 날은 `0`
- 이름 없음 — 클라이언트가 로컬 별칭과 `invite_code`를 매칭하여 표시

**`weekly_steps` / 30일 걸음수 이력 타임존 처리 (`subject_service.get_step_history`)**:
- `devices.timezone`(IANA 문자열)을 Python `ZoneInfo`로 파싱해 대상자 현지 자정을 기준으로 날짜 경계(`start_utc`, `end_utc`)를 계산한다.
- SQL은 `SELECT server_ts, steps_delta FROM heartbeat_logs WHERE device_id=$1 AND server_ts >= $2 AND server_ts < $3`만 수행하며 `AT TIME ZONE`을 사용하지 않는다. 날짜 버킷팅은 Python에서 `row["server_ts"].astimezone(tz).date()`로 처리 — SQL `AT TIME ZONE`과 Python `ZoneInfo` 사이 불일치(아래 주의사항) 원천 차단.
- **legacy alias 처리**: Android `flutter_timezone`은 "US/Pacific", "Japan", "ROK", "PRC", "Brazil/East" 등 legacy IANA alias를 반환할 수 있다. `python:3.12-slim`에 `tzdata` 패키지를 설치(`requirements.txt`)해 `ZoneInfo(alias)` 호출이 성공하도록 한다. 파싱 실패 시 `except Exception`으로 `ZoneInfo("Asia/Seoul")` 폴백.
- ⚠️ `AT TIME ZONE`과 Python `ZoneInfo`를 혼용하면 안 된다: Railway PostgreSQL의 pg_timezone_names에는 legacy alias가 없어 SQL은 폴백(Seoul)으로, Python은 tzdata로 정확한 alias를 인식하면 날짜 키가 달라져 걸음수 차트가 하루 밀린다. 버킷팅은 반드시 Python 단독으로 처리할 것.


### 4.5 대상자 연결 해제 (보호자용)
```
DELETE /api/v1/subjects/{guardian_id}/unlink
Headers:
  Authorization: Bearer <device_token>
Response: 200 OK
{
  "message": "대상자 연결이 해제되었습니다"
}
```

**연결 해제 시 서버 삭제 정책:**

| 테이블 | 처리 | 이유 |
| ----------------------- | ------ | ------------------------------------------------- |
| `guardians` | 삭제 | 보호자-대상자 연결 레코드 자체 |
| `notification_events` | **유지** | `subject_user_id` 기준 공유 데이터. 다른 보호자가 같은 대상자를 볼 수 있으므로 삭제 금지 |
| `alerts` | **유지** | `subject_user_id` 기준 공유 데이터. 다른 보호자가 같은 대상자를 볼 수 있으므로 삭제 금지 |
| `heartbeat_logs` | **유지** | 대상자 활동 로그. 연결 해제와 무관 |

**재연결 시 동작:**
- 동일 `invite_code`로 재연결 가능 (`users` 테이블의 대상자 계정은 유지됨)
- 재연결 시 새 `guardian_id`가 발급되며, 대상자의 당일 알림 이벤트는 즉시 조회 가능


### 4.6 Heartbeat 수신
```
POST /api/v1/heartbeat
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "device_id": "uuid-v4",
  "timestamp": "2026-03-18T14:32:00+09:00",
  "manual": false,
  "steps_delta": 342,
  "suspicious": false,
  "battery_level": 85
}
// manual: 사용자가 직접 "안부 보고하기" 버튼을 눌렀을 때 true (기본값 false)
//         ※ 클라이언트가 동일 날짜 재시도를 lastManualReportDate로 차단하므로, 서버에 도달한 manual=true는 당일 첫 수동 보고
// steps_delta:
//   - 자동/수동 공통 → 오늘 자정 ~ 현재 시각 누적 걸음수 (권한 거부 시 null)
//   - 활동 정보 알림은 서버가 `manual=false AND steps_delta > 0` 조건일 때만 생성하므로
//     수동 보고가 걸음수를 실어 와도 "수동 안부 확인" 알림 외 추가 알림은 발생하지 않는다.
//   - 일별 걸음수 이력(`heartbeat_logs`)은 자동/수동 모두 집계 대상이라, 자동 heartbeat이 실패한 날에도
//     수동 보고로 당일 걸음수 기록을 확보할 수 있다.
Response: 200 OK
{
  "status": "ok",
  "server_time": "2026-03-18T14:32:01+09:00",
  "heartbeat_hour": 18,
  "heartbeat_minute": 0
}
```

#### Heartbeat 영구 하위호환 계약 (`HeartbeatIn`)

> **불변식: heartbeat 수신은 어떤 앱 버전이 보내도 절대 스키마 사유로 거부하지 않는다.**
> 이 계약 덕분에 클라이언트는 **heartbeat 전송 유도 알림(Android `subject_safety_net`, iOS `gs_deadman`, Android `send_failed`)으로 앱이 런치되면 버전 체크(강제 업데이트 포함)를 skip**할 수 있다 — 구버전이 보낸 안부 신호도 항상 수신되므로 "보낸 줄 알았는데 서버가 거부"하는 거짓 안심이 구조적으로 발생하지 않는다. (보호자 알림/일반 아이콘 실행 런치는 버전 체크 정상 적용 — 구버전 호환성이 깨진 화면을 잘못 보여주느니 업데이트를 유도. 클라 분기는 PRD-FrontEnd §9.0 / `splash_controller`.)

`HeartbeatIn` Pydantic 모델(`models/heartbeat.py`)은 이 계약을 다음으로 강제한다:

| 방향 | 시나리오 | 처리 | 구현 |
|------|----------|------|------|
| **상위호환 (forward)** | 신버전이 서버가 **모르는 새 필드**를 추가해 전송 | 무시하고 200 OK 수신 → 서버 스키마 마이그레이션 없이 클라가 자율 진화 | `model_config = ConfigDict(extra="ignore")` (Pydantic v2 기본값이나 **명시적으로 박아 계약화**) |
| **하위호환 (backward)** | 구버전이 일부 필드를 **누락**하고 전송 | 기본값으로 흡수, 400 미발생 | 아래 필드별 기본값 |

**필드별 required/기본값:**

| 필드 | required | 누락 시 | 비고 |
|------|----------|---------|------|
| `device_id` | ✅ 필수 | (없으면 처리 불가 — 기기 조회 키) | 유지 |
| `timestamp` | ✅ 필수 | (없으면 처리 불가) | 유지 |
| `manual` | optional | `False` | |
| `steps_delta` | optional | `None` | `ge=0`(음수 차단) |
| `suspicious` | optional | **`False`(정상/활동확인)** | 과거 required였으나 누락으로 400 내지 않도록 안전 기본값 부여 — 누락을 "정상"으로 흡수해 거짓 경고 방지 |
| `battery_level` | optional | `None` | `0~100` |
| `scheduled_key` | optional | `None` | idempotency key |

> ⚠️ **`extra="forbid"`로 절대 변경 금지** — 변경 시 신버전이 추가한 필드 때문에 구버전 호환이 깨지고, 위 "버전 체크 skip" 전제가 무너진다. 라우터는 `body.model_dump()`로 처리 로직에 넘기므로 extra 필드는 검증 단계에서 드롭되어 처리 로직은 알려진 필드만 받는다(부수효과 없음).
>
> **신규 필드 추가 시 규칙**: 새 heartbeat 필드는 **항상 optional + 안전 기본값**으로 추가한다(절대 required로 추가 금지). 그래야 구버전이 그 필드를 안 보내도 계속 수신되어 위 불변식이 유지된다.

- 서버는 heartbeat 수신 시 해당 기기의 `last_seen`을 갱신
- heartbeat 수신 시 해당 대상자의 active 경고가 있으면 → 자동 해소 (status: `resolved`) + 보호자에게 "정상 복귀" Push 발송
- 대상자는 구독과 무관하게 항상 heartbeat 전송 (구독은 보호자가 관리)
- 보호자 구독이 만료된 경우, heartbeat는 수신하되 보호자에게 경고/알림을 발송하지 않음
- **서버 판정 로직** (상세: [heartbeat_flowchart.md](heartbeat_flowchart.md) 차트 2):
  - `is_first_today` 판정: 기기 로컬 타임존 자정 이후 수신 이력 — `heartbeat_logs` INSERT **전**에 조회해야 정확 (INSERT 이후엔 항상 false). `auto_report` / `steps` 알림 **중복 방지에만** 사용. heartbeat_logs INSERT는 매번 수행 (이력·차트용)
    - **legacy timezone alias 처리**: Android `flutter_timezone`이 반환하는 "US/Pacific", "Japan", "ROK", "PRC", "Brazil/East" 등 legacy IANA alias는 Railway PostgreSQL `pg_timezone_names`에 존재하지 않아 `AT TIME ZONE` 사용 시 `InvalidParameterValueError`로 크래시. `heartbeat_service.py`의 `is_first_today` 쿼리에 scheduler.py와 동일한 `pg_timezone_names LEFT JOIN COALESCE(..., 'Asia/Seoul')` CTE 패턴을 적용 — 미인식 alias는 Asia/Seoul로 폴백(날짜 경계가 UTC+9 기준으로 계산되나 크래시 없음).
    - ⚠️ **불변 규칙**: 사용자 제공 timezone 문자열을 SQL `AT TIME ZONE`에 직접 전달하는 패턴은 사용 금지. 반드시 `WITH safe AS (SELECT COALESCE(z.name, 'Asia/Seoul') AS tz FROM (SELECT $N::text AS inp) t LEFT JOIN pg_timezone_names z ON z.name = t.inp)` CTE를 앞에 두고 `safe.tz`를 참조하거나, Python-side에서 버킷팅하여 SQL `AT TIME ZONE` 자체를 제거해야 한다.
  - `battery_level` ≤ 10% + 기존 info 경고 없으면 → 정보 등급 1회 발송 (중복 방지, DND 적용)
  - `suspicious` = false → `resolve_active_alerts` 호출 후 반환된 `resolved_levels` 기준 분기:
    - caution / warning / urgent 중 **하나라도** 해소 → "정상 복귀" Push (auto_report 중복이라 스킵)
    - info(배터리)만 해소 또는 해소 없음:
      - `manual = true` → "수동 안부 확인" Push (사용자 의도적 액션이라 매번 발송, is_first_today 가드 없음)
      - `manual = false AND is_first_today` → "오늘 안부 확인 완료" Push (당일 1회) + `steps_delta > 0`이면 활동 정보 알림 동시 생성 (당일 1회)
  - `suspicious` = true:
    - 기존 warning/urgent 경고 → caution으로 하향 (정상 복귀 알림 없음)
    - suspicious_count=1 → caution 등급 + `message_key="caution_suspicious"` + `push_caution(reason="suspicious")` (중복 방지)
    - suspicious_count=2 → warning 등급 + `message_key="warning_suspicious"` + `push_warning(reason="suspicious")` (warning/urgent 없을 때만)
    - suspicious_count≥3 → urgent 등급 + `message_key="urgent_suspicious"` + `push_urgent(reason="suspicious")` (매일 반복, days_inactive 반영)
    - 보호자 경고 클리어 시 suspicious_count 리셋 → 다음 suspicious부터 1차 재시작
    - suspicious 경로는 scheduler 미수신 경로(`warning`/`urgent`)와 별도 문구("활동 기록 없음" — 걸음 0 + 발화 시점 기기 미사용)로 분리됨


### 4.7 긴급 도움 요청 (대상자 전용)
```
POST /api/v1/emergency
Headers:
  Authorization: Bearer <device_token>  (대상자)
Body:
{
  "device_id": "uuid-v4",
  "location": {                    // optional — 사용자 동의 시에만 첨부
    "latitude": 37.5665,           // -90.0 ~ 90.0
    "longitude": 126.9780,         // -180.0 ~ 180.0
    "accuracy_meters": 12.5,       // optional, ge=0
    "captured_at": "2026-04-19T14:32:00+09:00"  // optional, ISO 8601
  }
}
Response: 200 OK
{
  "status": "ok",
  "message": "긴급 알림이 전송되었습니다"
}
```

- **대상자만** 호출 가능 (`require_subject` 인증, 보호자가 호출 시 403)
- 기존 heartbeat 경고 에스컬레이션(suspicious_count, days_inactive)과 **완전히 독립** 동작
- 즉시 urgent 등급 경고 생성 (caution→warning→urgent 단계를 거치지 않음)
- `alerts` 테이블에 `alert_level='urgent'`, `note='emergency_request'`로 저장 (위치 저장 안 함 — alerts는 활성 상태 추적 전용)
- `notification_events` 테이블에 `message_key='emergency'`로 저장. `location` 필드 첨부 시 `location_lat/lng/accuracy/captured_at` 컬럼에 같이 기록 (모두 nullable, 권한 거부/GPS 실패 시 생략 가능)
- 연결된 보호자 전원에게 긴급 Push 발송 (DND 무시, `asyncio.gather` 병렬). FCM `data` 필드에 `lat`/`lng`/`accuracy` 값이 있을 때만 문자열로 포함 — 보호자 앱이 이 값으로 지도 페이지 라우팅
- 보호자 구독 만료와 무관하게 항상 발송
- 클라이언트에서 확인 다이얼로그를 표시하여 오탐 방지
- 위치 프라이버시: 정기 heartbeat에는 수집하지 않으며, 저장 범위는 `notification_events` 1곳, 보관 기간은 최대 24시간 (자정 정리 스케줄러가 삭제)


### 4.8 구독 상태 확인 (보호자용) 
```
GET /api/v1/subscription
Headers:
  Authorization: Bearer <device_token>  (보호자)
Response: 200 OK
{
  "plan": "free_trial",
  "started_at": "2026-03-18T00:00:00+09:00",
  "expires_at": "2026-06-18T00:00:00+09:00",
  "days_remaining": 72,
  "is_active": true
}
```

- 보호자만 호출 가능 (대상자가 호출 시 403)


### 4.9 구독 갱신 (인앱 결제 영수증 검증, 보호자용)
```
POST /api/v1/subscription/verify
Headers:
  Authorization: Bearer <device_token>  (보호자만 — require_guardian)
Body:
{
  "platform": "android",                 // "android" | "ios"
  "product_id": "anbu_yearly",           // 클라이언트 표시용 (서버는 응답 productId로 재검증)
  "receipt": "purchase-token-string"
}
Response: 200 OK
{
  "plan": "yearly",
  "expires_at": "2027-03-18T00:00:00+00:00",  // ISO 8601 UTC
  "is_active": true
}
```

- 단일 상품 (`anbu_yearly`) — 대상자 최대 5명, 티어 구분 없음
- **`receipt` 포맷 (클라이언트 합의)**:
  - **iOS**: `PurchaseDetails.purchaseID` (= transactionId 문자열)
  - **Android**: `verificationData.serverVerificationData` (= purchaseToken)
- **검증 흐름** (`services/subscription_service.py::_verify_and_persist`):
  1. `platform ∈ {ios, android}` 검증 (그 외 → 400)
  2. 플랫폼별 검증:
     - **iOS**: `verify_apple_transaction(receipt)` — App Store Server API `get_all_subscription_statuses(transactionId)` 호출, Production 우선 → 404 시 Sandbox 재시도 (Apple 권장 fallback). JWS 페이로드 디코드 후 정규화 식별자로 `originalTransactionId` 추출
     - **Android**: `verify_google_purchase(receipt)` — Play Developer API `androidpublisher.purchases.subscriptionsv2.get` 호출. `subscriptionState ∈ {ACTIVE, IN_GRACE_PERIOD}` (= `GOOGLE_ACTIVE_STATES`)만 활성 인정 (그 외 → 400). `acknowledgementState == ACKNOWLEDGEMENT_STATE_PENDING`이면 서버에서 자동 `acknowledge` 호출 (3일 경과 시 Google 자동 환불 방지 안전망). 정규화 식별자로 `purchaseToken` 추출
  3. **응답 productId 재검증**: 클라이언트가 보낸 `product_id`는 무시하고, Apple/Google 응답의 productId가 `config.IAP_PRODUCT_ID`(=`anbu_yearly`)와 일치하는지 확인 (다른 상품 영수증으로 yearly entitlement 우회 차단). 불일치 → 400
  4. `expires_at > UTC now` 재검증 (이미 만료 → 400)
  5. `subscriptions` 테이블 upsert: `plan='yearly'`, `expires_at=응답값`, `receipt_data=정규화 식별자`, `platform=ios|android`
- `receipt_data` 컬럼에는 정규화된 식별자만 저장 (iOS: `originalTransactionId`, Android: `purchaseToken`). RTDN 수신 시 이 식별자로 user 역매핑하는 키로 사용 (§4.22 참조)
- **환경변수**: `APPLE_IAP_ISSUER_ID`, `APPLE_IAP_KEY_ID`, `APPLE_IAP_KEY_P8` (single-line `\n` 자동 복원), `APPLE_BUNDLE_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_PACKAGE_NAME`, `IAP_PRODUCT_ID`


### 4.10 구독 복원 (보호자 앱 재설치 시)
```
POST /api/v1/subscription/restore
Headers:
  Authorization: Bearer <device_token>  (보호자만 — require_guardian)
Body:
{
  "platform": "android",                 // "android" | "ios"
  "product_id": "anbu_yearly",
  "receipt": "purchase-token-string"
}
Response: 200 OK
{
  "plan": "yearly",
  "expires_at": "2027-03-18T00:00:00+00:00",
  "is_active": true,
  "restored": true                       // /restore 전용 플래그
}
```

- 보호자 앱 재설치 / 기기 교체 후 Apple/Google에서 기존 구독 영수증을 가져와 서버에 검증 요청
- 유효한 구독이 확인되면 새 보호자 계정에 구독 활성화
- **`/verify`와 동일한 `_verify_and_persist` 검증 로직 재사용** + 응답에 `restored: true` 플래그만 추가. 즉 검증 실패 케이스(미활성 state, productId 불일치, 만료된 구독, ACK pending 자동 호출 등)는 양 엔드포인트에서 동일하게 동작
- 인앱 결제는 보호자의 Apple ID / Google 계정에 귀속되므로 개인정보 없이도 복구 가능
- 클라이언트는 [구독 복원] 탭 → `restorePurchases()` → `purchaseStream`의 `PurchaseStatus.restored` 수신 시 이 엔드포인트 호출. 복원할 영수증이 없을 때는 클라이언트가 5초 안전망으로 사용자에게 "복원할 구독이 없습니다" 정보 안내 (서버 호출 발생하지 않음)


### 4.11 경고 목록 조회 (보호자용)
```
GET /api/v1/alerts?subject_user_id={id}
Headers:
  Authorization: Bearer <device_token>
Response: 200 OK
{
  "alerts": [
    {
      "id": 10,
      "subject_user_id": 1,
      "invite_code": "K7M-4PXR",
      "status": "active",
      "days_inactive": 2,
      "last_seen_at": "2026-03-16T14:32:00+09:00",
      "created_at": "2026-03-17T14:32:00+09:00"
    }
  ]
}
```

- 보호자 본인에게 연결된 대상자의 경고만 조회 가능
- `active` 상태 경고만 반환 (`cleared`/`resolved` 경고는 서버에서 즉시 삭제되므로 조회 불필요)
- 이름 없음 — `invite_code`로 식별, 클라이언트가 로컬 별칭과 매칭


### 4.12 경고 클리어 (보호자용)

#### 4.11.1 개별 경고 클리어
```
PUT /api/v1/alerts/{alert_id}/clear
Headers:
  Authorization: Bearer <device_token>
Response: 200 OK
{
  "message": "경고가 클리어되었습니다. 이후 다음 지정 시각에 미수신 시 새로운 경고가 발생합니다."
}
- 클리어 처리 시 해당 alert 행을 DB에서 즉시 삭제 (이력은 클라이언트 로컬에 보관)
```

#### 4.11.2 대상자별 일괄 클리어 (안부 확인 완료)
```
PUT /api/v1/alerts/clear-all
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "subject_user_id": 1
}
Response: 200 OK
{
  "cleared_count": 3,
  "cleared_levels": ["info", "caution", "warning"],
  "cleared_by": 2,
  "cleared_at": "2026-03-18T10:00:00+09:00",
  "message": "모든 경고가 클리어되었습니다."
}
```

**클리어 시 서버 처리 시퀀스:**
```
보호자가 [건강 확인 완료] 버튼 탭
    │
    ├─ 1. 해당 대상자의 active 상태인 모든 경고를 DB에서 즉시 삭제
    │      (정보/주의/경고/긴급 등급 모두 포함, 이력은 클라이언트 로컬에 보관)
    │
    ├─ 2. days_inactive 카운트 리셋
    │
    ├─ 3. devices 테이블의 suspicious_count 리셋 (0으로 초기화)
    │
    └─ 4. 예약된 미발송 알림(push_pending) 취소
```

- 보호자가 대상자의 건강을 직접 확인한 후 경고를 클리어
- 일괄 클리어 시 해당 대상자의 **모든 활성 경고**(정보/주의/경고/긴급)를 한 번에 DB에서 즉시 삭제
- `days_inactive` 카운트 리셋 + `suspicious_count` 리셋
- 클리어 후에도 대상자의 heartbeat가 여전히 없으면 → 다음 날 같은 시각에 **새로운 경고가 1차부터 다시 생성**됨
- 보호자 본인에게 연결된 대상자의 경고만 클리어 가능 (권한 검증)


### 4.13 FCM 토큰 갱신
```
PUT /api/v1/devices/fcm-token
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "fcm_token": "new-fcm-token-string"
}
Response: 200 OK
{
  "message": "FCM 토큰이 갱신되었습니다"
}
```

- FCM 토큰은 OS에 의해 주기적으로 변경될 수 있으므로 앱 시작 시마다 확인/갱신


### 4.14 Heartbeat 시각 변경
```
PATCH /api/v1/devices/{device_id}/heartbeat-schedule
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "heartbeat_hour": 8,
  "heartbeat_minute": 0
}
Response: 200 OK
{
  "device_id": "uuid-v4",
  "heartbeat_hour": 8,
  "heartbeat_minute": 0,
  "message": "heartbeat 시각이 변경되었습니다. 다음 확인부터 적용됩니다."
}
```

- **대상자 본인만** 변경 가능 (보호자가 호출 시 403 Forbidden)
- 대상자 본인 기기(device_id + user_id) 검증
- 허용 범위: 00:00 ~ 23:59 (시: 0~23, 분: 0~59)
- 서버에서 `devices.heartbeat_hour`, `devices.heartbeat_minute` 갱신
- 클라이언트가 응답 수신 후 WorkManager/BGTask + 로컬 안전망 알림 재예약


### 4.15 앱 버전 체크 (강제 업데이트)
```
GET /api/v1/app/version-check?platform=android&current_version=1.0.0
Response: 200 OK
{
  "platform": "android",
  "current_version": "1.0.0",
  "latest_version": "1.2.0",
  "min_version": "1.1.0",
  "force_update": true,
  "store_url": "https://play.google.com/store/apps/details?id=com.anbu.app"
}
```

**판정 로직:**
```
client_version < min_version   → force_update = true  (앱 사용 차단, 스토어 이동 강제)
client_version < latest_version → force_update = false (선택적 업데이트 안내 가능)
client_version >= latest_version → 최신 상태
```

- 앱 구동 시(Splash) 매번 호출
- `platform`: `android` 또는 `ios`
- `min_version`: 이 버전 미만은 강제 업데이트 (보안 패치, 호환성 문제 등)
- `latest_version`: 최신 배포 버전
- `store_url`: 플랫폼별 스토어 URL (서버에서 관리)
- 서버 응답 실패 시(네트워크 오류 등) → 버전 체크 건너뛰고 앱 정상 진행 (차단하지 않음)

**관리:** Admin API(4.15)로 Postman에서 직접 설정


### 4.16 앱 버전 설정 (Admin API)

> 별도 관리자 페이지 없이 **Postman에서 직접 호출**하여 버전을 관리한다.
> `ADMIN_SECRET_KEY` 환경변수로 인증한다.

```
PUT /api/v1/admin/app-version
Headers:
  X-Admin-Key: <ADMIN_SECRET_KEY>
Body:
{
  "platform": "android",
  "latest_version": "1.2.0",
  "min_version": "1.1.0",
  "store_url": "https://play.google.com/store/apps/details?id=com.anbu.app"
}
Response: 200 OK
{
  "platform": "android",
  "latest_version": "1.2.0",
  "min_version": "1.1.0",
  "store_url": "https://play.google.com/store/apps/details?id=com.anbu.app",
  "updated_at": "2026-03-20T10:00:00+09:00"
}
```

**인증:**
- `X-Admin-Key` 헤더가 서버 환경변수 `ADMIN_SECRET_KEY`와 일치해야 함
- 불일치 시 `403 Forbidden` 반환
- Railway 환경변수에 `ADMIN_SECRET_KEY=<충분히 긴 랜덤 문자열>` 설정

**사용 시나리오:**
```
# 일반 배포 — 선택적 업데이트
{
  "platform": "android",
  "latest_version": "1.2.0",
  "min_version": "1.0.0"        ← 1.0.0 미만만 강제 업데이트
}
→ 1.0.0~1.1.x 사용자: 업데이트 안내 (건너뛰기 가능)

# 긴급 보안 패치 — 강제 업데이트
{
  "platform": "android",
  "latest_version": "1.2.0",
  "min_version": "1.2.0"        ← 1.2.0 미만 전부 강제 업데이트
}
→ 1.1.x 이하 모든 사용자: 앱 사용 차단, 스토어 이동

# iOS도 동일하게 별도 호출
{
  "platform": "ios",
  "latest_version": "1.2.0",
  "min_version": "1.2.0",
  "store_url": "https://apps.apple.com/app/id000000000"
}
```

**현재 설정 조회:**
```
GET /api/v1/admin/app-version?platform=android
Headers:
  X-Admin-Key: <ADMIN_SECRET_KEY>
Response: 200 OK
{
  "platform": "android",
  "latest_version": "1.2.0",
  "min_version": "1.1.0",
  "store_url": "https://play.google.com/store/apps/details?id=com.anbu.app",
  "updated_at": "2026-03-20T10:00:00+09:00"
}
```

- `store_url`은 선택 — 생략 시 기존 값 유지
- `latest_version` ≥ `min_version` 이어야 함 (위반 시 `400 Bad Request`)
- 플랫폼별(android/ios) 독립 관리


### 4.17 기기 정보 조회
```
GET /api/v1/devices/me
Headers:
  Authorization: Bearer <device_token>
Response: 200 OK
{
  "device_id": "uuid-v4",
  "heartbeat_hour": 18,
  "heartbeat_minute": 0,
  "last_seen": "2026-03-18T14:32:00+09:00",
  "subscription_active": true,
  "subscription_plan": "free_trial",
  "guardian_count": 1
}
```

- 대상자/보호자 모두 호출 가능
- `subscription_active`: 대상자는 연결된 보호자 중 활성 구독 존재 여부, 보호자는 본인 구독 상태
- `subscription_plan`: 보호자의 현재 구독 플랜 (`free_trial` / `yearly` / `expired`, 대상자는 `null`)
  - `free_trial`: 보호자 등록 시 서버 자동 생성 (3개월)
  - `yearly`: 인앱 결제 완료 후 영수증 검증 시 서버가 변경
  - `expired`: 서버 스케줄러가 만료 시 자동 변경
- `guardian_count`: 대상자에 연결된 보호자 수
- 앱 포그라운드 진입 시 서버 스케줄 동기화 용도


### 4.18 보호자 알림 설정 조회/변경
```
GET /api/v1/guardian/notification-settings
Headers:
  Authorization: Bearer <device_token>
Response: 200 OK
{
  "all_enabled": true,
  "urgent_enabled": true,
  "warning_enabled": true,
  "caution_enabled": true,
  "info_enabled": true,
  "dnd_enabled": false,
  "dnd_start": null,
  "dnd_end": null
}
```

```
PUT /api/v1/guardian/notification-settings
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "all_enabled": true,
  "urgent_enabled": true,
  "warning_enabled": true,
  "caution_enabled": true,
  "info_enabled": true,
  "dnd_enabled": true,
  "dnd_start": "22:00",
  "dnd_end": "08:00"
}
Response: 200 OK
{ ... 동일 구조 ... }
```

- 보호자만 호출 가능
- `dnd_start`, `dnd_end`: HH:MM 형식, 자정 넘김(22:00~08:00) 지원
- 긴급 등급은 DND 무관하게 항상 Push 발송
- 알림 설정이 없으면 기본값(전체 ON) 자동 생성


### 4.19 알림 목록 조회 (보호자용)
```
GET /api/v1/notifications
Headers:
  Authorization: Bearer <device_token>
  X-Timezone-Offset: +09:00
Response: 200 OK
{
  "notifications": [
    {
      "id": 1,
      "subject_user_id": 1,
      "invite_code": "K7M-4PXR",
      "alert_level": "info",
      "title": "✅ 오늘 안부 확인 완료",
      "body": "대상자의 오늘 안부 확인이 정상 수신되었습니다.",
      "created_at": "2026-04-02T09:32:00+00:00"
    }
  ]
}
```

- 보호자에 연결된 **모든 대상자**의 당일 알림 이벤트를 조회
- 해당 보호자가 이미 숨김(dismiss) 처리한 알림은 제외 (`dismissed_notifications` 테이블 참조)
- 알림은 대상자 중심(`notification_events`) 테이블에 저장되므로, 같은 대상자를 보는 모든 보호자가 동일한 알림을 볼 수 있음
- 보호자별 알림 설정(`guardian_notification_settings`)에 따라 서버에서 필터링하여 반환
  - `all_enabled = false` → 전체 비활성
  - 등급별 ON/OFF (`urgent_enabled`, `warning_enabled`, `caution_enabled`, `info_enabled`)
- `X-Timezone-Offset` 헤더로 클라이언트 타임존 전달 → 오늘 자정 기준 필터링
- 파싱 실패 시 기본값 KST(+09:00) 적용


### 4.20 알림 전체 숨김 (보호자용)
```
DELETE /api/v1/notifications
Headers:
  Authorization: Bearer <device_token>
  X-Timezone-Offset: +09:00
Response: 200 OK
{
  "deleted_count": 5
}
```

- 보호자별 숨김(dismiss) 방식: `dismissed_notifications` 테이블에 해당 보호자의 숨김 기록을 저장
- **다른 보호자에게는 영향 없음** — 동일 대상자의 알림은 다른 보호자에게 여전히 표시됨
- `notification_events` 원본 데이터는 삭제되지 않음 (자정 정리 스케줄러가 삭제)


### 4.21 사용자 탈퇴
```
DELETE /api/v1/users/me
Headers:
  Authorization: Bearer <device_token>
Response: 204 No Content
```

**탈퇴 시 서버 삭제 정책:**

| 테이블 | 처리 | 비고 |
| ----------------------- | ------ | ------------------------------------------------- |
| `heartbeat_logs` | 삭제 | 해당 기기의 heartbeat 로그 |
| `guardian_notification_settings` | 삭제 | 보호자 알림 설정 |
| `notification_events` | 삭제 | `subject_user_id` 기준 대상자 알림 이벤트 |
| `alerts` | 삭제 | `subject_user_id` 기준 활성 경고 |
| `guardians` | 삭제 | 보호자-대상자 연결 (양방향) |
| `subscriptions` | 삭제 | 구독 정보 |
| `devices` | 삭제 | 기기 정보 |
| `users` | 삭제 | 사용자 계정 |

- 대상자 탈퇴 시 연결된 보호자에게 "대상자 탈퇴 알림" Push 발송 (DB 저장 없음)


### 4.22 인앱 결제 서버 알림 (RTDN / Server-to-Server Notifications)

> 외부 시스템(Google Cloud Pub/Sub, Apple App Store Connect)이 발신하는 비동기 알림을 수신해 `subscriptions` 테이블을 자동 갱신한다. **`Authorization: Bearer <device_token>` 인증 미사용** — 발신자는 device_token을 보유하지 않는다. 대신 발신자별 서명을 검증한다.

#### 4.22.1 Google Play RTDN (Pub/Sub Push)
```
POST /api/v1/iap/google-notifications
Headers:
  Authorization: Bearer <OIDC JWT (Pub/Sub Push가 자동 첨부)>
Body (Pub/Sub envelope):
{
  "message": {
    "data": "<base64-encoded JSON>",
    "messageId": "...",
    "publishTime": "..."
  },
  "subscription": "projects/{gcp-project}/subscriptions/{subscription}"
}
Response: 200 OK
{ "status": "ok", "kind": "activated|revoked|noop|...", ... }
```

- **인증**: `google.oauth2.id_token.verify_oauth2_token`로 OIDC JWT 검증
  - 서명: Google 공개키
  - audience: `config.PUBSUB_AUDIENCE` 일치 (Pub/Sub Push subscription 생성 시 지정한 값)
  - email claim: `config.PUBSUB_SERVICE_ACCOUNT_EMAIL` 일치 + `email_verified=true`
  - 검증 실패 → 401/403 반환
- **페이로드 디코딩**: `message.data`를 base64 디코딩한 JSON 사용
  - `testNotification` (Play Console 초기 검증용) / `oneTimeProductNotification` → graceful 무시 (200 반환)
  - `packageName != GOOGLE_PACKAGE_NAME` → graceful 무시
- **notificationType 분기** (`services/iap_notification_service.py::handle_google_notification`):
  | type | 이름 | 처리 |
  |---|---|---|
  | 1 | `SUBSCRIPTION_RECOVERED` | 재조회 후 expires_at 갱신 |
  | 2 | `SUBSCRIPTION_RENEWED` | 재조회 후 expires_at 갱신 |
  | 3 | `SUBSCRIPTION_CANCELED` | 재조회 — state가 ACTIVE면 expires만 갱신, 비활성이면 expired |
  | 4 | `SUBSCRIPTION_PURCHASED` | 재조회 후 expires_at 갱신 |
  | 5 | `SUBSCRIPTION_ON_HOLD` | 즉시 `plan='expired'` |
  | 6 | `SUBSCRIPTION_IN_GRACE_PERIOD` | 재조회 후 expires_at 갱신 |
  | 7 | `SUBSCRIPTION_RESTARTED` | 재조회 후 expires_at 갱신 |
  | 8 | `SUBSCRIPTION_PRICE_CHANGE_CONFIRMED` | noop |
  | 9 | `SUBSCRIPTION_DEFERRED` | noop |
  | 10 | `SUBSCRIPTION_PAUSED` | 즉시 `plan='expired'` |
  | 11 | `SUBSCRIPTION_PAUSE_SCHEDULE_CHANGED` | noop |
  | 12 | `SUBSCRIPTION_REVOKED` | 즉시 `plan='expired'` |
  | 13 | `SUBSCRIPTION_EXPIRED` | 즉시 `plan='expired'` |
- **재조회 = source of truth**: 활성화 type은 알림 본문의 정보를 신뢰하지 않고 `verify_google_purchase(purchaseToken)`로 Play Developer API 재조회 → 최신 `expires_at`/`subscriptionState` 반영. RTDN 순서 역전(RENEWED 직후 EXPIRED 도착 등) 케이스 방어
- **DB 매핑**: `UPDATE subscriptions SET ... WHERE receipt_data = $purchaseToken AND platform = 'android'`. `receipt_data`는 §4.9의 `/verify` 단계에서 채워진 값
- **멱등성**: 같은 알림이 재전송돼도 UPDATE 결과 동일
- **2xx 보장**: 서명 검증 통과 이후엔 어떤 실패도 200 반환 (Pub/Sub 무한 retry 폭주 방지)
  - 재조회 실패 → `kind=verify_failed`
  - `UPDATE affected=0` (클라이언트 `/verify` 이전에 RTDN이 먼저 도착한 race) → `kind=not_mapped`
  - 알 수 없는 type → `kind=noop`
  - 잘못된 productId → `kind=wrong_product`

#### 4.22.2 Apple App Store Server Notifications V2
```
POST /api/v1/iap/apple-notifications
Headers: (Apple은 별도 Authorization 헤더 없음 — 서명은 payload 내부 JWS)
Body:
{
  "signedPayload": "<JWS>"
}
Response: 200 OK
{ "status": "ok", "kind": "activated|revoked|noop|...", ... }
```

- **인증**: `app-store-server-library.SignedDataVerifier`로 JWS 서명 검증
  - x5c 헤더의 인증서 체인을 **Apple Root CA 3종(G2 + G3 + Apple Inc.)**으로 검증 (`apple_root_ca/*.cer` 디렉토리에서 자동 로드)
  - Apple Computer, Inc. Root Certificate(1995년 발급)는 현 S2S V2 JWS 체인에 사용 안 됨 → 3개로 충분 (2026-05-28 sandbox 테스트 알림 검증으로 확정)
  - `bundle_id` = `APPLE_BUNDLE_ID` 일치
  - environment 분기: 환경변수 `APPLE_ENVIRONMENT` 우선, 미설정 시 payload `data.environment` 자동 분기
  - 검증 실패 → 401 반환
- **페이로드 디코딩** (`routers/iap_notification.py::_shallow_to_dict`):
  - `SignedDataVerifier.verify_and_decode_notification()`이 반환하는 `ResponseBodyV2DecodedPayload`·`Data` 등은 `app-store-server-library`의 `attr.s(slots=True)` 기반 모델이라 `vars(obj)/__dict__`가 비어있음. `attr.has()` + `attr.fields()` 기반 재귀 변환 + `rawNotificationType` → `notificationType` 등 enum **raw alias 매핑**으로 처리하여 service 코드가 string 키로 접근 가능
  - raw alias 영역: `rawNotificationType`/`rawSubtype`/`rawEnvironment`/`rawStatus`/`rawConsumptionRequestReason` → enum 필드명
- **notificationType 분기** (`services/iap_notification_service.py::handle_apple_notification`):
  | type | 처리 |
  |---|---|
  | `SUBSCRIBED`, `DID_RENEW`, `DID_CHANGE_RENEWAL_STATUS`, `OFFER_REDEEMED`, `PRICE_INCREASE`, `RENEWAL_EXTENDED`, `RENEWAL_EXTENSION` | `verify_apple_transaction(originalTransactionId)` 재조회 후 expires_at 갱신 |
  | `EXPIRED`, `GRACE_PERIOD_EXPIRED`, `REVOKE`, `REFUND` | 즉시 `plan='expired'` |
  | `DID_FAIL_TO_RENEW`, `CONSUMPTION_REQUEST`, 그 외 | noop |
- **DB 매핑**: `UPDATE subscriptions SET ... WHERE receipt_data = $originalTransactionId AND platform = 'ios'`
- **2xx 보장**: 서명 검증 통과 후엔 무조건 200 (재시도 폭주 방지)

#### 4.22.3 운영 외부 작업

- Google: GCP Pub/Sub Topic 생성 → Push subscription에 OIDC 인증 + audience 지정 → Topic에 `google-play-developer-notifications@system.gserviceaccount.com` (Google Play가 모든 개발자에 공유로 쓰는 글로벌 system 계정)의 `roles/pubsub.publisher` 권한 부여 → Play Console Monetization setup에 topic 등록. UI는 system 계정을 검증 거부할 수 있어 `gcloud pubsub topics add-iam-policy-binding`으로 우회 처리하는 게 표준
- Apple: Apple Root CA **3종(G2/G3/AppleInc)** 다운로드 + 컨테이너에 번들 (Apple Computer Inc 1995년 인증서는 현 S2S V2 JWS 체인에 사용 안 됨 → 3개로 충분) → App Store Connect "앱 정보 → App Store 서버 알림"에서 Production/Sandbox URL 등록. **Version 선택 옵션은 최근 UI에서 제거되고 신규 등록은 자동 V2 적용** (V1 deprecated)
- Apple 테스트 알림 발송: App Store Connect UI에서 "Request a Test Notification" 버튼이 제거됨 → `AppStoreServerAPIClient.request_test_notification()` API 직접 호출이 표준 우회 경로 (반환된 `testNotificationToken`으로 서버 로그에서 수신 추적)
- Railway 환경변수: `PUBSUB_AUDIENCE`, `PUBSUB_SERVICE_ACCOUNT_EMAIL` (Google 필수), `APPLE_ENVIRONMENT` (Apple 선택, 미설정 시 payload `data.environment` 자동 분기), `APPLE_ROOT_CA_DIR` (Apple, 기본 `./apple_root_ca`)
- 운영 상세는 `.ref/인앱결제-연동-체크리스트.md §8` 참조


#### 4.22.4 구독 라이프사이클 통합 플로우

> 결제부터 만료까지의 전체 사이클을 Apple/Google 공통 관점에서 정리. type 매핑 디테일은 §4.22.1/§4.22.2 표 참조.

**설계 원칙 — Push 기반 (Apple/Google → 우리 서버)**

- Apple/Google이 구독 상태 변경 시 RTDN으로 우리 서버에 push (수 초 내 도달)
- **우리 서버는 Apple/Google API를 polling 하지 않음** — RTDN을 source of truth로 사용
- Push 선택 이유: 즉시 반영, Apple/Google API rate limit·비용 회피, 대규모 사용자 지원
- 안전망: `services/subscription_service.py:29` 응답 시점에 `is_active = expires_dt > now and plan != 'expired'`로 시간 기반 1회 비교 (polling 아님). RTDN 누락/지연 시에도 클라가 만료 카드 정확 표시

**정상 사이클 (Production)**

```
[결제 시점]
사용자 [구독하기] 탭 → Apple/Google 결제 다이얼로그 → 결제 완료
   ↓
RTDN: SUBSCRIBED(Apple) / PURCHASED(Google) 발송
   ↓
클라: POST /api/v1/subscription/verify (영수증 전송)
   ↓
서버: verify_apple_transaction / verify_google_purchase 호출로 검증
   ↓
DB: subscriptions UPSERT (plan='yearly', expires_at=<1년 후>, receipt_data=<originalTransactionId / purchaseToken>)
   ↓
클라: onVerified 콜백 → _loadSubscription() → 카드 "프리미엄 구독 중" + [구독 관리]

[1년 후 자동 갱신 시점 — 사용자가 자동 갱신 OFF 안 한 경우]
Apple/Google: 1년 결제 사이클 도달 → 자동 청구 → 갱신 처리
   ↓
RTDN: DID_RENEW(Apple) / RENEWED(Google) 발송
   ↓
서버: 재조회 후 expires_at 갱신 (다음 1년 연장)
   ↓
클라: 카드 "프리미엄 구독 중" 유지 (UI 변화 없음)

[사용자가 자동 갱신 OFF 한 경우 — iOS 설정 → 구독 / Google Play 구독에서 토글]
현재 기간(expires_at)까지는 active 유지
   ↓
expires_at 도달 → 자동 결제 안 됨 → 만료 처리
   ↓
RTDN: EXPIRED(Apple) / EXPIRED(Google) 발송
   ↓
서버: plan='expired' UPDATE
   ↓
클라: 카드 "구독 만료" + [구독하기] 재표시 (다음 _loadSubscription 호출 시점)

[환불/취소 케이스]
사용자 환불 요청 → Apple/Google 처리
   ↓
RTDN: REVOKE(Apple) / REVOKED(Google) 발송
   ↓
서버: plan='expired' UPDATE 즉시
```

**Sandbox 테스트 사이클 (시간 압축)**

| 환경 | 1년 압축 시간 | 자동 갱신 횟수 | 총 사이클 | 비고 |
|---|---|---|---|---|
| Apple Sandbox | **1년 = 1일(24h)** | 6회 후 자동 만료 | **7일** | TestFlight/Xcode Dev 공통 |
| Google Sandbox (License Tester) | **1년 = 30분** | 6회 후 자동 만료 | **3시간 30분** | §9-1 실측 검증 완료 |

**Apple Sandbox iOS 설정 UI 특이사항 (사용자 오해 유발 포인트)**

- iOS 설정 → Apple ID → 구독에서 **sandbox 영수증은 표시되지 않음** (Apple의 sandbox/production 채널 완전 분리 정책)
- "구독 중인 항목 없음" 표시는 sandbox에서 **만료 의미가 아니다** — 단순히 sandbox UI 분리
- 실 구독 상태 확인 경로:
  1. 우리 앱의 보호자 설정 카드 (가장 빠름)
  2. `AppStoreServerAPIClient.get_all_subscription_statuses(transactionId)` API 직접 조회 (정확한 expiresDate / autoRenewStatus / status 확인)

**클라이언트 카드 분기 (PRD-FrontEnd §9.7 참조)**

- `isPremium = plan == 'yearly' && isActive` — plan + isActive 둘 다 확인
- `isExpired = plan == 'expired' || (plan == 'yearly' && !isActive)` — RTDN 누락 시 시간 안전망 자동 트리거
- 이중 확인으로 RTDN 누락·지연 케이스에도 만료 카드 자동 전환 보장

**카드 상태 전환 시각화**

```
[가입 직후]
   회색 "무료 체험 중" (plan='free_trial', is_active=true)
   [구독하기] + [구독 복원]
        ↓
   [구독하기] → 결제 완료 → SUBSCRIBED/PURCHASED RTDN
        ↓
[활성 구독]
   인디고 "프리미엄 구독 중" (plan='yearly', is_active=true)
   [구독 관리] 버튼만
        ↓
   ┌─ 자동 갱신 (1년마다) ─→ "프리미엄 구독 중" 유지
   ├─ 자동 갱신 OFF + 기간 종료 ─→ EXPIRED RTDN
   ├─ 환불/취소 ─→ REVOKE/REVOKED RTDN
   └─ RTDN 누락 + expires_at < now ─→ is_active=false (시간 안전망)
        ↓
[만료]
   주황 "구독 만료" (plan='expired' OR plan='yearly' + is_active=false)
   [구독하기] 재표시
```

**핵심 invariant — 한 줄 정리**

> 우리 서버는 Apple/Google에 한 번도 가지 않는다. RTDN으로 받는다. 그리고 클라가 GET /subscription 호출하면 시간 비교 1회로 안전망까지 적용해 응답한다. 끝.


---


## 5. DB 스키마 (PostgreSQL)

**DB**: Railway 제공 PostgreSQL (asyncpg 비동기 드라이버 사용)

```sql
-- 사용자 테이블 (개인정보 미포함)
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    role            TEXT NOT NULL DEFAULT 'subject',
                    -- 'subject' (대상자) 또는 'guardian' (보호자)
    invite_code     TEXT UNIQUE,                       -- 대상자 고유 코드 (보호자는 NULL)
    device_token    TEXT NOT NULL UNIQUE,               -- API 인증용 Bearer 토큰 (무제한)
    max_subjects    INTEGER NOT NULL DEFAULT 5,         -- 보호자별 최대 대상자 등록 인원 (기본 5, 유료로 상향 가능). 과거 전역 상수 MAX_SUBJECTS 대체
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 기존 DB 대응: ALTER TABLE users ADD COLUMN IF NOT EXISTS max_subjects INTEGER NOT NULL DEFAULT 5
--   (NOT NULL DEFAULT 로 기존 보호자 행에도 5가 자동 백필)

CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);


-- 기기 테이블
CREATE TABLE IF NOT EXISTS devices (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    device_id       TEXT NOT NULL UNIQUE,
    platform        TEXT NOT NULL,                     -- 'android' 또는 'ios'
    os_version      TEXT,
    fcm_token       TEXT,
    steps_delta     INTEGER,                           -- 마지막 heartbeat 이후 걸음수 증가량 (권한 거부 시 NULL)
    last_steps      INTEGER,                           -- 마이그레이션 호환용 이전 걸음수
    battery_level   INTEGER,                           -- 마지막 배터리 잔량 (0~100)
    suspicious_count INTEGER DEFAULT 0,                -- 연속 suspicious 횟수
    heartbeat_hour  INTEGER NOT NULL DEFAULT 18,       -- heartbeat 시각 (시, 0~23, 기본 18)
    heartbeat_minute INTEGER NOT NULL DEFAULT 0,       -- heartbeat 시각 (분, 0~59, 기본 0)
    timezone        TEXT NOT NULL DEFAULT 'Asia/Seoul', -- 기기 시간대 (IANA timezone)
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices (user_id);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices (last_seen);
CREATE INDEX IF NOT EXISTS idx_devices_platform ON devices (platform);
CREATE INDEX IF NOT EXISTS idx_devices_heartbeat_time ON devices (heartbeat_hour, heartbeat_minute);


-- 보호자-대상자 매핑 테이블
CREATE TABLE IF NOT EXISTS guardians (
    id                  SERIAL PRIMARY KEY,
    subject_user_id     INTEGER NOT NULL REFERENCES users(id),
    guardian_user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(subject_user_id, guardian_user_id)
);

CREATE INDEX IF NOT EXISTS idx_guardians_subject ON guardians (subject_user_id);
CREATE INDEX IF NOT EXISTS idx_guardians_guardian ON guardians (guardian_user_id);


-- 구독 테이블 (보호자만 보유, 대상자는 구독 없음)
-- 단일 상품: anbu_yearly ($9.99/년), 대상자 최대 5명, 티어 없음
CREATE TABLE IF NOT EXISTS subscriptions (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
                    -- 보호자 user_id만 참조
    plan            TEXT NOT NULL DEFAULT 'free_trial',
                    -- free_trial, yearly, expired
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    receipt_data    TEXT,                             -- 인앱 결제 영수증
    platform        TEXT,                            -- 결제 플랫폼 (android/ios)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions (user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_expires ON subscriptions (expires_at);


-- 경고 테이블
CREATE TABLE IF NOT EXISTS alerts (
    id                  SERIAL PRIMARY KEY,
    subject_user_id     INTEGER NOT NULL REFERENCES users(id),
    alert_level         TEXT NOT NULL DEFAULT 'warning',
                        -- info, caution, warning, urgent
    status              TEXT NOT NULL DEFAULT 'active',
                        -- active, resolved, false_alarm
                        -- cleared/resolved/false_alarm 확정 시 행 즉시 삭제 (이력 미보관)
    days_inactive       INTEGER NOT NULL DEFAULT 1,
    last_seen_at        TIMESTAMPTZ NOT NULL,
    push_count          INTEGER NOT NULL DEFAULT 0,        -- 발송된 Push 횟수 (최대 5회)
    last_push_at        TIMESTAMPTZ,                       -- 마지막 Push 발송 시각
    resolved_at         TIMESTAMPTZ,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status);
CREATE INDEX IF NOT EXISTS idx_alerts_subject ON alerts (subject_user_id, created_at DESC);


-- Heartbeat 로그 (감사/디버깅용)
CREATE TABLE IF NOT EXISTS heartbeat_logs (
    id              SERIAL PRIMARY KEY,
    device_id       TEXT NOT NULL,
    steps_delta     INTEGER,                           -- heartbeat 이후 걸음수 증가량 (권한 거부 시 NULL)
    suspicious      INTEGER DEFAULT 0,                 -- 활동 지표 미달 (0/1)
    battery_level   INTEGER,
    client_ts       TEXT NOT NULL,                      -- 클라이언트 제공 타임스탬프 (문자열)
    server_ts       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 30일 보관 후 삭제 정책 권장
CREATE INDEX IF NOT EXISTS idx_heartbeat_device_ts ON heartbeat_logs (device_id, server_ts DESC);


-- 앱 버전 관리 테이블
CREATE TABLE IF NOT EXISTS app_versions (
    platform        TEXT PRIMARY KEY,                      -- android, ios
    latest_version  TEXT NOT NULL,                         -- 최신 배포 버전
    min_version     TEXT NOT NULL,                         -- 강제 업데이트 기준 버전 (미만이면 차단)
    store_url       TEXT NOT NULL,                         -- 플랫폼별 스토어 URL
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 초기 데이터
INSERT INTO app_versions (platform, latest_version, min_version, store_url)
VALUES
  ('android', '1.0.0', '1.0.0', 'https://play.google.com/store/apps/details?id=com.anbu.app'),
  ('ios', '1.0.0', '1.0.0', 'https://apps.apple.com/app/id000000000')
ON CONFLICT (platform) DO NOTHING;


-- 알림 이벤트 테이블 (대상자 중심)
-- 대상자별 1건 저장, 보호자 조회 시 guardians 테이블 JOIN으로 필터링
CREATE TABLE IF NOT EXISTS notification_events (
    id                    SERIAL PRIMARY KEY,
    subject_user_id       INTEGER NOT NULL,              -- 대상자 user_id
    invite_code           TEXT,                          -- 대상자 고유 코드 (조회 편의)
    alert_level           TEXT NOT NULL,                 -- info, caution, warning, urgent, health
    message_key           TEXT,                          -- 클라이언트 번역 키 (auto_report, emergency, ...)
    message_params        TEXT,                          -- JSON 파라미터 (예: {"days": 3})
    title                 TEXT NOT NULL,
    body                  TEXT NOT NULL,
    -- 긴급 도움 요청 시에만 첨부되는 위치 (사용자 동의 기반, 1회성)
    location_lat          DOUBLE PRECISION,
    location_lng          DOUBLE PRECISION,
    location_accuracy     DOUBLE PRECISION,
    location_captured_at  TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ne_subject_created ON notification_events (subject_user_id, created_at DESC);


-- 알림 숨김 테이블 (보호자별 — 다른 보호자에 영향 없음)
CREATE TABLE IF NOT EXISTS dismissed_notifications (
    id                  SERIAL PRIMARY KEY,
    guardian_user_id    INTEGER NOT NULL REFERENCES users(id),
    event_id            INTEGER NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
    dismissed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(guardian_user_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_dn_guardian ON dismissed_notifications (guardian_user_id);


-- 보호자 알림 설정 테이블
CREATE TABLE IF NOT EXISTS guardian_notification_settings (
    guardian_user_id  INTEGER PRIMARY KEY REFERENCES users(id),
    all_enabled       BOOLEAN NOT NULL DEFAULT TRUE,   -- 전체 알림 ON/OFF
    urgent_enabled    BOOLEAN NOT NULL DEFAULT TRUE,   -- 긴급 등급 ON/OFF
    warning_enabled   BOOLEAN NOT NULL DEFAULT TRUE,   -- 경고 등급 ON/OFF
    caution_enabled   BOOLEAN NOT NULL DEFAULT TRUE,   -- 주의 등급 ON/OFF
    info_enabled      BOOLEAN NOT NULL DEFAULT TRUE,   -- 정보 등급 ON/OFF
    dnd_enabled       BOOLEAN NOT NULL DEFAULT FALSE,  -- 방해금지 모드 ON/OFF
    dnd_start         TEXT,                            -- 방해금지 시작 시각 (HH:MM)
    dnd_end           TEXT,                            -- 방해금지 종료 시각 (HH:MM)
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**알림 아키텍처 설계 원칙:**
- 알림 이벤트는 **대상자 중심**으로 1건만 저장 (이전: 보호자마다 각각 저장)
- 보호자가 알림을 조회할 때 `guardians` 테이블 JOIN으로 연결된 대상자 필터링
- 보호자별 알림 설정(`guardian_notification_settings`)은 **조회 시점에 적용** (저장 시점 아님)
- Push 알림은 보호자마다 개별 발송 (FCM 토큰별), DB 저장과 분리
- 같은 대상자를 여러 보호자가 보는 경우 모두 동일한 알림 목록을 확인 가능

**PostgreSQL 참고사항:**
- `BOOLEAN` → 네이티브 타입 사용 (`TRUE`/`FALSE`)
- `TIMESTAMPTZ` → 타임존 포함 타임스탬프 (UTC 저장, 조회 시 KST 변환)
- `SERIAL` → 자동 증가 정수 (PostgreSQL 시퀀스)
- asyncpg 비동기 드라이버 사용 (`$1`, `$2` 파라미터 바인딩)
- Railway PostgreSQL 플러그인으로 배포 시 DB 영구 보존 (Volume 불필요)


---


## 6. 서버 스케줄러


### 6.1 경고 생성 스케줄러 (고정 시각 기반 + 등급별 판정)

> 📊 상세 플로우차트: [heartbeat_flowchart.md](heartbeat_flowchart.md) — 차트 3 참조

```
실행 주기: 매 분 정각 (APScheduler CronTrigger(second=0))

처리 흐름:
1. 현재 시각이 heartbeat 시각 + 2시간에 해당하는 기기 중
   오늘 heartbeat를 수신하지 못한 대상자 조회
   ※ 판정은 **기기 로컬 타임존(devices.timezone) 기준**으로 수행한다 — "오늘 자정"과
     "예약시각 +2h"를 모두 기기 로컬 시각으로 계산한 뒤 UTC instant로 환산해 now()와 비교.
     (과거 datetime.now(KST) 하드코딩은 비-KST 사용자에게 예약+2h를 (offset-9)시간
      어긋나게 발화시켰다. 자정 정리·걸음수 통계 등 다른 잡과 동일하게 devices.timezone 존중.)
     불량 tz 1건이 매분 전체 쿼리를 throw시키지 않도록 pg_timezone_names에 LEFT JOIN해
     유효하지 않은 zone은 'Asia/Seoul'로 폴백한다.
   SELECT u.id, d.device_id, d.last_seen, d.battery_level,
          d.suspicious_count, d.platform,
          d.heartbeat_hour, d.heartbeat_minute,
          d.fcm_token, d.locale          -- 대상자 본인 푸시용
   FROM users u
   JOIN devices d ON u.id = d.user_id
   LEFT JOIN pg_timezone_names z ON z.name = d.timezone
   CROSS JOIN LATERAL (SELECT COALESCE(z.name, 'Asia/Seoul') AS tz) zz
   WHERE u.invite_code IS NOT NULL      -- 활성 대상자/G+S (subject 역할 보유)
     AND date_trunc('minute', now()) = date_trunc('minute',
           (date_trunc('day', now() AT TIME ZONE zz.tz)
              + make_interval(mins => d.heartbeat_hour * 60 + d.heartbeat_minute + 120)
           ) AT TIME ZONE zz.tz)
     AND d.last_seen < (date_trunc('day', now() AT TIME ZONE zz.tz) AT TIME ZONE zz.tz)

   ※ 기본: heartbeat_hour=18, heartbeat_minute=0 → 기기 로컬 20:00에 체크

2. 대상자 본인 안부유도 푸시 (Android 한정 — 구독·보호자 유무와 무관):
   - row.platform == 'android' 이고 fcm_token이 있으면 push_subject_safety_net(fcm_token, locale)
     발송 (data.type = 'subject_safety_net', i18n push_subject_safety_net_title/body).
   - 아래 보호자 게이팅(3단계)보다 **먼저** 실행해, 보호자가 없거나 전원 구독 만료여도
     대상자에겐 "앱을 열어 안부를 보내달라"는 푸시가 전달되게 한다.
   - 미수신일마다 1회 발화 → 무시 시 매일 반복(클라의 버그 있던 Android 일일 로컬 안전망
     알림을 대체. iOS 대상자는 클라 정시 로컬알림 gs_deadman이 PRIMARY라 서버 푸시 제외).
   - 클라는 탭 시 safety_home으로 이동 후 미전송 heartbeat 자동 재전송(자세히는 FrontEnd §2.5.1).

3. 보호자 구독 확인:
   a. 보호자 구독 만료 → 보호자 알림 미발송 (heartbeat는 계속 수신, 대상자 푸시는 위에서 이미 발송)
   b. 보호자 구독 활성 → 등급 판정 진행

4. 등급 판정 (누적 미수신 횟수 기반):
   a. battery_level < 20%
      → 정보 등급 1회 발송 후 종료 (이후 상향 없음, 소리 없음)
      → heartbeat 수신 시 자동 해소

   b. 활성 경고 없음 (1회 미수신)
      → 주의 등급: "오늘 안부 확인이 없습니다" (1회 발송)

   c. caution 활성 (2회 미수신)
      → 경고 등급: "안부 확인이 없습니다. 통신 불가 상태일 수 있습니다"

   d. warning 활성 (3회 이상 미수신)
      → 긴급 등급: "즉시 확인이 필요합니다"

   e. urgent 활성 (긴급 지속)
      → 긴급 반복: days_inactive 증가, push_count 증가, Push 발송은 최대 5회까지

5. DND(방해금지) 설정 반영:
   - 보호자별 DND 시간대 설정 가능 (기본 OFF)
   - 긴급 등급은 DND 무관하게 항상 발송

6. 경고 반복 정책:
   - 정보 등급: 1회 발송, 이후 상향 없음
   - 주의 등급: 1회 발송
   - 경고 등급: 1~2회 다음날 재발송 → 3회 이상 긴급 상향
   - 긴급 등급: 보호자 확인까지 매일 반복 (종료 없음)

7. 보호자 Push 병렬 발송 (alert_service.send_alert_to_guardians):
   - 경고 등급 판정 후 연결된 보호자 전체에 Push 발송 시 `asyncio.gather + asyncio.to_thread` 패턴
   - FCM `send()`는 동기 블로킹 호출이라 `to_thread`로 풀어 이벤트 루프 차단 회피
   - 각 보호자의 FCM 발송은 독립적이므로 완전 병렬화 가능
   - DB 작업 없이 FCM I/O만 수행하는 함수이므로 커넥션 공유 문제 없음
   - **토큰 무효화 DB 업데이트는 순차 처리** (InvalidRegistration/NotRegistered 에러 수신 시 `fcm_token` NULL — 커넥션 공유 이슈 회피 위해 병렬화 안 함, §6.6 참조)
```


### 6.2 구독 만료 체크 (보호자 기준)
```
실행 주기: 매일 00:00 KST

처리 흐름:
1. SELECT u.id FROM users u
   JOIN subscriptions s ON u.id = s.user_id
   WHERE u.role = 'guardian'
     AND s.plan IN ('free_trial', 'yearly')
     AND s.expires_at < NOW()

2. 조회된 각 보호자:
   a. subscriptions.plan = 'expired'로 변경
   b. 보호자 기기에 일반 Push 발송 ("무료 체험이 종료되었습니다")

3. 구독 만료 시 동작:
   - 대상자 heartbeat는 계속 정상 수신 (대상자 앱에 영향 없음)
   - 해당 보호자에게 경고 알림 발송 중단
   - 보호자 대시보드는 정상 접근 가능 (대상자 목록·상태 조회 가능, 상단에 구독 만료 안내 배너 표시)
```


### 6.3 보호자 미연결 대상자 자동 정리
```
실행 주기: 매일 03:00 KST

처리 흐름:
1. SELECT u.id FROM users u
   WHERE u.role = 'subject'
     AND u.created_at < NOW() - INTERVAL '30 days'
     AND NOT EXISTS (
       SELECT 1 FROM guardians g WHERE g.subject_user_id = u.id
     )

2. 조회된 각 대상자:
   a. heartbeat_logs 삭제 (해당 device_id)
   b. alerts 삭제
   c. notification_events 삭제
   d. devices 삭제
   e. users 삭제

3. 별도 알림 없음 (앱은 그대로 남아있음)
   · 이후 대상자가 앱 실행 시 → API 호출 → 401 응답
   · 앱이 모드 선택 화면으로 이동 → 재등록 → 새 고유 코드 발급
   · 무료 체험은 device_id로 판단하므로 기존 기간 유지
```


### 6.4 당일 알림 자정 정리
```
실행 주기: 매일 00:00 KST

처리 흐름:
1. notification_events 테이블에서 대상자별 timezone 조회
   SELECT DISTINCT ne.subject_user_id,
          COALESCE(d.timezone, 'Asia/Seoul') AS tz
   FROM notification_events ne
   LEFT JOIN devices d ON d.user_id = ne.subject_user_id

2. 각 대상자의 기기 timezone 기준 자정 이전 알림 삭제
   DELETE FROM notification_events
   WHERE subject_user_id = $1 AND created_at < $2
   (자정 = 대상자 기기 timezone 기준 오늘 00:00 UTC 변환값)

※ 당일 알림만 보관하고 전날 이전 알림은 자정에 일괄 삭제
```


### 6.5 heartbeat_logs 30일 초과 삭제
```
실행 주기: 매일 04:00 KST

처리 흐름:
1. DELETE FROM heartbeat_logs WHERE server_ts < NOW() - INTERVAL '30 days'

※ 30일 이상 지난 heartbeat 로그를 삭제하여 DB 용량 관리
```


### 6.6 보호자 Push 발송 실패 처리
```
실행 주기: Push 발송 시마다

처리 흐름:
1. FCM 발송 시 InvalidRegistration 또는 NotRegistered 에러 수신
2. 해당 보호자 기기의 fcm_token을 NULL 처리
3. 대상자에게 "보호자 연결 끊김" 일반 Push 발송
```


### 6.7 보호자 Push 대상 조회 규칙 (중복 전송 방지)

heartbeat 수신·경고 발송·긴급 도움 요청·대상자 탈퇴 Push 등 **대상자 이벤트 → 연결된 보호자 전원에게 Push**를 보내는 모든 경로는 아래 조회 규칙을 따른다.

**규칙:** `guardians JOIN devices` 쿼리에 반드시 `DISTINCT ON (g.guardian_user_id)` + `ORDER BY g.guardian_user_id, d.updated_at DESC` 를 적용하여, 보호자 1명당 **가장 최근 갱신된 `devices` 1건의 `fcm_token`으로만** Push를 발송한다.

```sql
SELECT DISTINCT ON (g.guardian_user_id)
       g.guardian_user_id, d.fcm_token, d.locale
FROM guardians g
JOIN subscriptions s ON s.user_id = g.guardian_user_id
JOIN devices d ON d.user_id = g.guardian_user_id
WHERE g.subject_user_id = $1
  AND s.plan != 'expired'
  AND s.expires_at > NOW()
  AND d.fcm_token IS NOT NULL
  AND d.fcm_token != ''
ORDER BY g.guardian_user_id, d.updated_at DESC;
```

**배경:** `guardians` 테이블에는 `UNIQUE(subject_user_id, guardian_user_id)` 제약이 걸려 있어 보호자-대상자 매핑은 1행으로 유일하나, `devices` 테이블에는 동일 `user_id`에 대해 여러 행이 남아있을 수 있다 (OS/기기 교체, orphan 레코드 등). 단순 JOIN 시 보호자당 N행이 반환되어 **같은 보호자에게 Push가 N회 발송**되는 이슈가 발생하므로 `DISTINCT ON` + `updated_at DESC`로 최신 토큰 1건만 선택한다.

**적용 대상 쿼리 (예시):**
- `services/heartbeat_service.py::_get_active_guardians` — heartbeat 정상/suspicious 판정 시 보호자 Push (emergency_service도 이 함수를 재사용)
- `services/alert_service.py::send_alert_to_guardians` — 정보/주의/경고/긴급/배터리 Push
- `routers/user.py::delete_me` — 대상자 탈퇴 Push (2곳)

신규로 보호자 전원에게 Push를 보내는 쿼리를 추가할 때도 동일 규칙을 적용한다.


---


## 7. 인증


### 7.1 device_token 기반 인증
- 사용자 등록 시 서버에서 `device_token` 발급 (`secrets.token_urlsafe(32)` 기반)
- **만료 없음**: 한 번 발급하면 영구적으로 유효
- 이후 모든 API 호출에 `Authorization: Bearer <device_token>` 사용
- 구독 상태는 서버가 판단하여 heartbeat 응답에 포함

### 7.2 무료 체험 중복 방지 (보호자 기준)
- **보호자 device_id**로 판단 (대상자는 구독 없음)
- 동일 device_id + 보호자 앱 재설치 → 기존 무료 체험 기간 유지 (device_id로 식별)
- 다른 device_id → 새 무료 체험 허용 (기기 변경)
- iOS 추가 대응: Apple 구독 시스템이 자체적으로 "이 Apple ID는 이미 무료 체험을 사용했음"을 관리

### 7.3 앱 재설치 시 동작
**대상자 앱 재설치:**
- 재등록 시 서버가 device_id로 기존 계정 조회 → 기존 계정 자동 복원
- 기존 invite_code 유지, device_token만 재발급
- 보호자 연결 유지 → 재연결 불필요
- 결제 관련 동작 없음 (대상자는 무료)

**보호자 앱 재설치:**
- 재등록 시 서버가 device_id로 기존 계정 조회 → 기존 계정 자동 복원
- 기존 구독·대상자 연결 유지, device_token만 재발급
- 대상자 재연결 불필요 (로컬 별칭만 재설정 필요)

**device_id 영속성 보장 (클라이언트):**
- Android: SSAID(`Settings.Secure.ANDROID_ID`)는 공장 초기화 전까지 유지되므로 자연스럽게 동일 값 전달
- iOS: `identifierForVendor`는 vendor 앱 전부 삭제 후 재설치 시 값이 변경되므로, 클라이언트가 최초 발급 시 Keychain(`accessibility=unlocked_this_device`)에 백업 → 재설치 시 Keychain 우선 조회로 동일 device_id 복원
- 서버는 플랫폼과 무관하게 동일 device_id를 수신하므로 위 복원 로직(기존 계정 자동 복원)이 그대로 유효
- iOS Keychain 백업은 계정 복원 전용이며 iCloud 동기화 차단 → fingerprinting 정책과 무관


---


## 8. 보안


### 8.1 통신
- HTTPS 필수 (TLS 1.2+)
- heartbeat payload 최소화 (걸음수 + suspicious 플래그 포함, 민감 정보 없음)


### 8.2 데이터
- **개인정보 최소 저장**: 이름, 전화번호, 사용 앱 목록 일절 저장하지 않음. 위치정보는 정기 heartbeat에서 미수집이며, 대상자가 긴급 도움 요청 버튼을 누른 경우에만 사용자 동의 하에 `notification_events` 테이블에 1회 저장되고 대상자 기기 타임존 자정 스케줄러가 일괄 삭제
- 수집 데이터: device_id, 걸음수(steps_delta), suspicious 플래그, 배터리 잔량, 앱 버전, 긴급 요청 시 lat/lng/accuracy — 최소 수준
- DB 유출 시에도 개인 식별 불가 (invite_code, device_id만 존재)


### 8.3 고유 코드 보안
- invite_code는 7자리 영숫자 (36^7 = 약 783억 조합)
- 무작위 대입 방지: API 요청 제한 (rate limiting) 적용
  - `/api/v1/subjects/link` 엔드포인트: 분당 5회 제한
  - 연속 실패 시 일시 차단


### 8.4 결제
- 인앱 결제는 보호자만 수행 (대상자 앱에는 결제 기능 없음)
- 인앱 결제 영수증은 서버에서 Apple/Google 서버와 직접 검증
- 영수증 원본은 DB에 보관 (분쟁 대비)
- 대상자 사망 시 보호자가 본인 계정에서 직접 구독 취소 가능


---


## 9. 알림 다국어(i18n) 설계


### 9.1 개요

서버는 20개 언어로 Push 알림 메시지를 발송하며, `notification_events`에 `message_key`/`message_params`를 저장하여 클라이언트가 로컬 언어로 재렌더링할 수 있도록 한다.

**지원 언어 (20개):**
ko_KR, en_US, ja_JP, zh_CN, zh_TW, de_DE, fr_FR, es_ES, it_IT, nl_NL, pt_BR, ru_RU, ar_SA, tr_TR, pl_PL, vi_VN, th_TH, sv_SE, hi_IN, id_ID


### 9.2 기기 locale 저장

클라이언트가 기기 등록(`POST /api/v1/users`) 및 FCM 토큰 갱신(`PUT /api/v1/devices/fcm-token`) 시 `locale` 필드를 전달한다. 서버는 `devices.locale` 컬럼에 저장한다.

```sql
-- devices 테이블 locale 컬럼
locale TEXT NOT NULL DEFAULT 'ko_KR'
```

- 기본값: `ko_KR` (미전달 시)
- 형식: `{languageCode}_{countryCode}` (예: `en_US`, `ja_JP`)


### 9.3 서버 Push 메시지 다국어 발송

`i18n/messages.py`에 20개 언어별 메시지 딕셔너리를 관리한다.

```python
# i18n/messages.py
MESSAGES: dict[str, dict[str, str]] = {
    "ko_KR": {
        "push_battery_low_title": "🔋 폰 배터리 부족",
        "push_caution_title": "⚠ 주의",
        "push_warning_title": "⚠ 경고",
        "push_urgent_title": "🚨 긴급",
        "push_auto_report_title": "✅ 오늘 안부 확인 완료",
        ...
    },
    "en_US": {
        "push_battery_low_title": "🔋 Low Battery",
        ...
    },
    # ... 20개 언어
}

def get_message(locale: str, key: str, **kwargs) -> str:
    """locale별 메시지 반환. 미지원 locale → en_US fallback."""
    msgs = MESSAGES.get(locale, MESSAGES["en_US"])
    template = msgs.get(key, MESSAGES["en_US"].get(key, key))
    return template.format(**kwargs) if kwargs else template
```

**Push 발송 흐름:**
```
heartbeat 수신 / 미수신 판정
    ↓
보호자 목록 조회 (guardians + devices JOIN → fcm_token, locale 획득)
    ↓
각 보호자의 locale로 get_message() 호출 → 번역된 title/body 생성
    ↓
FCM Push 발송 (보호자별 locale 독립)
```

- 모든 push_service 함수는 `locale: str = "ko_KR"` 파라미터를 수신
- 탈퇴 Push, 구독 만료 Push 등도 동일하게 locale 적용


### 9.4 notification_events 다국어 저장

`notification_events` 테이블에 `message_key`와 `message_params`를 추가 저장한다.
`title`/`body` 컬럼은 ko_KR 기본값으로 저장하며, 클라이언트가 `message_key`로 로컬 번역을 렌더링할 수 있다.

```sql
-- notification_events 테이블 추가 컬럼
message_key    TEXT,          -- 클라이언트 번역 키 (예: 'auto_report', 'urgent')
message_params TEXT,          -- JSON 파라미터 (예: '{"days": 3}')
```

**message_key 목록:**

| message_key | alert_level | 설명 | params |
|---|---|---|---|
| `auto_report` | info | 자동 안부 확인 완료 | - |
| `manual_report` | info | 수동 안부 확인 | - |
| `battery_low` | info | 배터리 20% 미만 | - |
| `battery_dead` | info | 배터리 방전 추정 | `{"battery_level": 15}` |
| `caution_suspicious` | caution | 활동 기록 없음 (suspicious=true 1회) | - |
| `caution_missing` | caution | 안부 미수신 1회 (scheduler) | - |
| `warning` | warning | 연속 미수신 2회 (scheduler) | - |
| `warning_suspicious` | warning | 활동 기록 연속 없음 (suspicious=true 2회) | - |
| `urgent` | urgent | 긴급 미수신 3회+ (scheduler) | `{"days": 3}` |
| `urgent_suspicious` | urgent | 활동 기록 연속 없음 (suspicious=true 3회+) | `{"days": 3}` |
| `steps` | health | 걸음수 활동 정보 ("오늘 N보를 걸으셨습니다") | `{"steps": "342"}` |
| `emergency` | urgent | 긴급 도움 요청 (대상자 직접) | - |
| `cleared_by_guardian` | info | 보호자 수동 경고 클리어 (다른 보호자에게 발송) | - |

**API 응답 (GET /api/v1/notifications):**
```json
{
  "notifications": [
    {
      "id": 1,
      "subject_user_id": 1,
      "invite_code": "K7M-4PXR",
      "alert_level": "info",
      "title": "✅ 오늘 안부 확인 완료",
      "body": "보호 대상자가 오늘 예정시각에 알림을 보냈습니다.",
      "message_key": "auto_report",
      "message_params": null,
      "created_at": "2026-04-06T09:32:00+00:00"
    }
  ]
}
```


### 9.5 프로젝트 구조

```
server/
├── i18n/
│   ├── __init__.py
│   └── messages.py              # 20개 언어 Push 메시지 딕셔너리 + get_message()
├── ...
```


---


## 10. Python 핵심 패키지

| 패키지 | 용도 |
|--------|------|
| `fastapi` | 웹 프레임워크 (API 자동 문서화, Swagger UI) |
| `uvicorn` | ASGI 서버 (FastAPI 실행) |
| `asyncpg` | PostgreSQL 비동기 드라이버 |
| `apscheduler` | 스케줄러 (Silent Push, 경고 체크, 구독 만료) |
| `firebase-admin` | FCM/APNs Push 발송 (Silent Push + 일반 Push) |
| `pydantic` | 요청/응답 데이터 검증 (FastAPI 내장) |
| `python-dotenv` | 환경변수 로드 (.env 파일) |

**requirements.txt:**
```
fastapi==0.115.*
uvicorn==0.34.*
asyncpg==0.30.*
apscheduler==3.11.*
firebase-admin==6.6.*
python-dotenv==1.1.*
```


---


## 11. 배포 및 인프라


### 10.1 인프라 구성

| 구성요소 | 서비스 | 비고 |
|----------|--------|------|
| **Python 서버 + PostgreSQL** | Railway | Git push 자동 배포, HTTPS 자동, PostgreSQL 플러그인 포함 월 $5~10 |
| **Push** | FCM (Firebase Cloud Messaging) | Silent Push + 일반 Push, 무제한 무료 |


### 10.2 서버 배포 (Railway)

**배포 과정:**
```
1. GitHub에 서버 코드 push
2. railway.app 가입 → GitHub 연동
3. New Project → Deploy from GitHub Repo 선택
4. 환경변수 설정 (FIREBASE_CREDENTIALS 등)
5. 자동 빌드 + 배포 + HTTPS 설정
6. 이후 Git push 할 때마다 자동 재배포
```

- **HTTPS**: Railway가 자동 제공 (인증서 설정 불필요)
- **커스텀 도메인**: Railway 대시보드에서 `anbucheck.co.kr` 연결 가능
- **로그 확인**: Railway 웹 대시보드에서 실시간 확인
- **환경변수**: Railway 대시보드에서 설정 (코드에 비밀 정보 포함하지 않음)
  - `FIREBASE_CREDENTIALS`: FCM 인증 키 JSON
  - `ADMIN_SECRET_KEY`: Admin API 인증 키 (Postman에서 앱 버전 설정 시 사용)
  - `DISCORD_WEBHOOK_URL`: 에러 알림 Discord 웹훅 URL (§10.5 운영 모니터링). 미설정 시 알림 기능 자동 비활성(no-op)이라 로컬/테스트에 안전

**Dockerfile (Railway 배포 설정):**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

- `main.py`에서 `PORT` 환경변수를 읽어 uvicorn 직접 실행


### 10.3 데이터베이스 (PostgreSQL)
- **서비스**: Railway PostgreSQL 플러그인
- **드라이버**: asyncpg (비동기)
- **연결**: 환경변수 `DATABASE_URL` (Railway가 자동 주입)
- **영구 보존**: Railway PostgreSQL은 배포 시 데이터 유지 (Volume 불필요)
- **백업**: Railway 대시보드에서 수동 백업 가능
- **용량 관리**: `heartbeat_logs` 테이블 30일 보관 후 자동 삭제 (스케줄러)


### 10.4 비용 요약

| 항목 | 월 비용 |
|------|---------|
| Python 서버 + PostgreSQL (Railway) | **$5~10** (PostgreSQL 플러그인 포함) |
| FCM | **무료** |
| 도메인 | 연 11,000원 (~$8) |
| **합계** | **약 $2~6/월** |

**손익분기점:** 구독자 **7명**이면 서버 비용 충당 ($9.99 × 7 × 85% ÷ 12 = $4.95/월)


### 10.5 운영 모니터링 (Discord 에러 알림)

**목적:** 실서버에서 예외가 발생하면 **즉시 Discord로 실시간 경보**를 보내 조용한 실패를 막는다. 특히 ① **APScheduler 잡**(매 분 도는 미수신 체크 등)이 예외로 죽으면 보호자 경고가 통째로 멈추는데도 로그만 남고 아무도 모르던 문제, ② Railway가 재배포 시 로그를 휘발시켜 사후 추적이 어려운 문제를 함께 완화한다.

**구현:** `services/notify.py` (신규). **새 외부 의존성 없이** 표준 라이브러리(`urllib`)로 Discord 웹훅에 POST한다.

| 원칙 | 내용 |
|------|------|
| 비활성 안전 | `DISCORD_WEBHOOK_URL` 미설정 시 완전 no-op — 로컬/테스트 영향 없음 |
| 본 기능 보호 | 알림 전송 실패는 삼킨다(절대 예외를 밖으로 던지지 않음) — 알림이 본 로직을 깨면 안 됨 |
| 도배 방지 | 같은 컨텍스트는 인메모리 throttle로 **5분 1회**만 전송 (매 분 실패하는 잡 대응) |
| Discord 403 회피 | 기본 `Python-urllib` User-Agent가 403 차단되므로 명시 User-Agent 헤더 전송 |

**포착 지점 (2곳):**

1. **스케줄러 잡 오류** — `setup_scheduler()`에 `EVENT_JOB_ERROR` 리스너 1개를 달아 5개 잡 전부를 단일 포착한다. 리스너가 이벤트 루프 스레드에서 호출된다는 보장이 없어 동기 전송 진입점(`notify_error_sync`)을 쓰고, 스택은 예외 객체의 `__traceback__`(리스너까지 살아오지 않을 수 있음) 대신 APScheduler가 주는 사전 포맷 문자열 `event.traceback`을 쓴다.
2. **API 미처리 예외(500)** — `main.py`의 전역 `@app.exception_handler(Exception)`. `HTTPException`(401/404 등 의도된 4xx)·422 검증 오류는 FastAPI가 별도 처리하므로 여기로 오지 않는다(진짜 서버 오류만 포착). 비동기 컨텍스트라 전송은 `asyncio.to_thread`로 떼어 이벤트 루프를 막지 않는다.

**알림에 담기는 정보:** 예외 타입+메시지 · 발생 위치(잡 ID 또는 `METHOD /path`) · 스택트레이스 · **에러 직전 로그 N줄**.
- "에러 직전 로그"는 루트 로거에 부착한 고정 크기 deque 링버퍼(`_RingBufferHandler`, 기본 50줄)에서 가져온다. `emit`은 포맷된 한 줄을 deque에 append할 뿐이라 **O(1)·고정 메모리**이고, **네트워크 전송은 에러 순간에만** 일어난다 — 외부 로그 저장소(예: Axiom)로 모든 로그를 상시 전송하는 방식과 달리 **평소 자원 부담이 사실상 0**이다. 그 대신 보관 범위는 "에러 직전 맥락"으로 한정되며 장기 추세·검색은 제공하지 않는다(현 단계에선 의도된 트레이드오프).

**부담 / 한계:**
- throttle·링버퍼는 **프로세스 단위**다. Railway 레플리카가 여럿이면 같은 API 500이 레플리카당 1회씩 알림될 수 있다. 단 **스케줄러 잡은 advisory lock으로 단일 실행**되므로 잡 오류 알림은 중복되지 않는다.
- **개인정보:** "직전 로그"에 device_id 등 운영 데이터가 섞일 수 있으나, ⓐ 운영자 **비공개 Discord 채널**로만 가고 ⓑ **에러 순간에만** 전송된다(스택트레이스도 동일 성격). 외부 저장소로 상시 내보내는 방식이 아니므로 별도 마스킹 설계는 두지 않는다 — 외부 로그 저장소를 도입할 경우엔 민감값 마스킹을 함께 설계해야 한다.

**미채택 (참고):** 전체 로그를 외부 저장소(Axiom 등)로 상시 전송하는 영구 로그 보관은 상시 CPU·네트워크 부담과 버퍼 누적 위험이 있어 현 단계에선 보류한다. 간헐적 버그의 과거 시점 추적·응답시간 추세·로그 검색이 필요해지는 시점에 재검토한다.


---


## 12. 연동 API 목록 (FrontEnd 참조)

| API | 메서드 | 용도 |
|-----|--------|------|
| `/api/v1/users` | POST | 사용자 등록 (대상자/보호자) |
| `/api/v1/users/me` | DELETE | 사용자 탈퇴 (계정 및 관련 데이터 전체 삭제) |
| `/api/v1/heartbeat` | POST | 안부 확인 heartbeat 수신 |
| `/api/v1/subjects/link` | POST | 고유 코드로 대상자 연결 (보호자용) |
| `/api/v1/subjects` | GET | 연결된 대상자 목록 조회 (보호자용) |
| `/api/v1/subjects/{id}/unlink` | DELETE | 대상자 연결 해제 (보호자용) |
| `/api/v1/subscription` | GET | 구독 상태 확인 |
| `/api/v1/subscription/verify` | POST | 인앱 결제 영수증 검증 |
| `/api/v1/subscription/restore` | POST | 구독 복원 (앱 재설치 시) |
| `/api/v1/alerts` | GET | 대상자별 경고 목록 조회 (보호자용) |
| `/api/v1/alerts/{id}/clear` | PUT | 개별 경고 클리어 (보호자가 건강 확인 후) |
| `/api/v1/alerts/clear-all` | PUT | 대상자별 모든 활성 경고 일괄 클리어 + 적응형 주기 복원 |
| `/api/v1/devices/me` | GET | 기기 정보 조회 (스케줄 동기화, 구독 상태 확인) |
| `/api/v1/devices/fcm-token` | PUT | FCM 토큰 갱신 |
| `/api/v1/devices/{device_id}/heartbeat-schedule` | PATCH/PUT | heartbeat 시각 변경 (대상자만) |
| `/api/v1/notifications` | GET | 당일 알림 목록 조회 (보호자용, 대상자 중심 + 설정 필터링) |
| `/api/v1/notifications` | DELETE | 당일 알림 전체 숨김 (보호자별, 다른 보호자 영향 없음) |
| `/api/v1/guardian/notification-settings` | GET | 보호자 알림 설정 조회 |
| `/api/v1/guardian/notification-settings` | PUT | 보호자 알림 설정 변경 (등급별 ON/OFF, DND) |
| `/api/v1/app/version-check` | GET | 앱 버전 체크 (강제 업데이트 판정) |
| `/api/v1/admin/app-version` | PUT | 앱 버전 설정 (Admin, Postman용) |
| `/api/v1/admin/app-version` | GET | 앱 버전 설정 조회 (Admin, Postman용) |
| `/api/v1/emergency` | POST | 긴급 도움 요청 (대상자 → 보호자 전원 즉시 urgent Push, 에스컬레이션 독립. body.location optional: lat/lng/accuracy/captured_at 첨부 시 `notification_events`에 저장 + FCM data에 포함) |
| `/api/v1/iap/google-notifications` | POST | Google Play RTDN 수신 (Pub/Sub Push OIDC JWT 인증, device_token 미사용) |
| `/api/v1/iap/apple-notifications` | POST | Apple S2S Notifications V2 수신 (JWS 서명 검증, device_token 미사용) |
| `/health` | GET | 헬스체크 |

> FrontEnd 상세는 [PRD-FrontEnd.md](PRD-FrontEnd.md) 참조


---


## 13. 성공 지표 (서버)

| 지표 | 목표 |
|------|------|
| 서버 응답 시간 | heartbeat API p99 < 200ms |
| 서버 가용성 | ≥ 99.5% |
| 경고 Push 발송 성공률 | ≥ 98% |
| 경고 생성 정확도 | 거짓 경고 ≤ 5% |
| 보호자 Push 전달률 | ≥ 95% |


---


## 14. 향후 확장 (v2.0+, 참고용)

- **알림 채널 확장**: 경고 발생 시 SMS, 카카오톡, 이메일 자동 발송
- **보호자 웹 대시보드**: 관리 대상자 목록, 실시간 상태 모니터링 UI
- **경고 에스컬레이션**: 단계별 심각도 상향 + 외부 기관 연계
- **통계/리포트**: 주간/월간 활동 리포트 생성
- **다중 기기 통합**: 동일 사용자의 여러 기기를 하나의 프로필로 묶기
- **관리자 대시보드 API**: 웹 기반 관리자 화면용 API 확장
- **DB 스케일업**: 사용자 폭증 시 Railway PostgreSQL 플랜 업그레이드


---


## 15. 개발 일정 (예상)

| 단계 | 산출물 |
|------|--------|
| 1단계: DB + 사용자 등록/연결 API | FastAPI 서버, PostgreSQL 스키마, 사용자 등록, 고유 코드 생성, 대상자 연결 API |
| 2단계: Heartbeat + 경고 | heartbeat 수신, 경고 생성/클리어 로직 |
| 3단계: Push 서비스 | Silent Push 발송, 보호자 FCM Push 발송 |
| 4단계: 스케줄러 | APScheduler — iOS Silent Push 주기, 경고 체크, 구독 만료 체크 |
| 5단계: 구독/결제 API | 구독 상태 관리, 영수증 검증, 구독 복원 |
| 6단계: Railway 배포 + 통합 테스트 | Railway 배포, E2E 테스트 |
