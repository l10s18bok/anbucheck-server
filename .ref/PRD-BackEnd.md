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
클라이언트로부터 heartbeat를 수신하고, 매일 고정 시각(기본 09:30) + 2시간 내 미수신 시 보호자에게 FCM Push 경고를 발송하며, Android/iOS 기기에 매일 고정 시각에 FCM Silent Push(heartbeat_trigger)를 발송하는 역할을 담당한다.

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


### 1.5 수익 모델
- **보호자가 결제**: 3개월 무료 체험 → 이후 연 $9.99 자동 갱신 구독
- **대상자 앱은 완전 무료** (결제 기능 없음, heartbeat 전송만 담당)
- **대상자 최대 5명** (단일 요금, 티어 구분 없음)
- **하단 고정 배너 광고** (유료 구독 보호자는 광고 제거)


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
│  │ · heartbeat │  │ · heartbeat  │  │ · Silent Push 발송 │  │
│  │ · 대상자    │  │   트리거 FCM │  │   (iOS/Android     │  │
│  │   연결 관리 │  │   발송 (매일 │  │    heartbeat 트리거│  │
│  │ · 구독 관리  │  │   고정 시각) │  │ · 일반 Push 발송   │  │
│  │ · FCM 토큰  │  │ · 경고 생성  │  │   (보호자 알림)    │  │
│  │ · FCM 토큰  │  │ · 경고 생성  │  │                    │  │
│  │   갱신      │  │   (미수신 시) │  │                    │  │
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
| heartbeat 트리거 | **서버가 매일 고정 시각에 FCM Silent Push 발송** | **서버가 매일 고정 시각에 FCM Silent Push 발송** |
| 서버 추가 작업 | 기기별 고정 시각에 heartbeat_trigger Push 발송 | 기기별 고정 시각에 heartbeat_trigger Push 발송 |
| 확인 주기 | 매일 고정 시각 (기본 09:30) | 매일 고정 시각 (기본 09:30) |
| 시각 변경 | 서버가 변경된 시각에 맞춰 FCM Push 발송 | 서버가 변경된 시각에 맞춰 FCM Push 발송 |
| 클라이언트 스케줄러 | 불필요 (WorkManager 제거) | 불필요 |


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
| 🚨 긴급 | 경고 3회 이상 누적 | 매일 반복, 보호자 확인까지 종료 없음 |
| ⚠ 경고 | 미수신 2회 이상 | 1~2회 다음날 재발송 |
| ⚠ 주의 | 미수신 1회 | 1회 발송 |
| 🔵 정보 | 배터리 ≤ 10% / 자동 heartbeat 정상 수신 / 정상복귀 / 수동 heartbeat | DND 적용 (시간 외 소리, 시간 내 조용) |

```
[heartbeat 수신 시]
heartbeat 수신 → last_seen 갱신
  ├─ 오늘(KST) 이미 heartbeat 수신한 경우 → suspicious 강제 false (하루 첫 heartbeat만 판정)
  ├─ battery_level ≤ 10% + 기존 info 경고 없음 → 정보 등급 1회 발송 (DND 적용)
  └─ suspicious 판정:
      ├─ false → 활성 경고 해소 여부 확인
      │   ├─ 활성 경고 있었음 → 완전 해소 + 보호자 Push "정상 복귀" (정보 등급 DND 적용)
      │   └─ 활성 경고 없었음
      │       ├─ manual = true  → 보호자 Push "수동 안부 확인" (정보 등급 DND 적용)
      │       └─ manual = false → 보호자 Push "오늘 안부 확인 완료" (정보 등급 DND 적용)
      └─ true  → warning/urgent → caution 하향 (정상 복귀 알림 없음)
               → 대상자에게 wellbeing_check 발송 (보호자 경고 없음)
               → 보호자 경고는 heartbeat 미수신 시에만 발생

[heartbeat 미수신 시 (기기별 고정 시각 + 2시간 경과 시 체크)]
지정 시각 + 2시간 내 미수신 대상자 감지 (기본: 09:30 → 11:30 체크)
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
  "notification": { "title": "🚨 긴급: 대상자 확인 필요", "body": "안부 확인이 없으며 마지막 확인 시 폰 사용 흔적도 없었습니다. 즉시 확인이 필요합니다." },
  "data": { "type": "alert_urgent", "subject_user_id": "1", "invite_code": "K7M-4PXR" }
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
│   ├── user.py                     # POST /api/v1/users (사용자 등록)
│   ├── heartbeat.py                # POST /api/v1/heartbeat
│   ├── subject.py                  # POST /api/v1/subjects/link, GET /api/v1/subjects
│   ├── alert.py                    # GET /api/v1/alerts, PUT /api/v1/alerts/{id}/clear, PUT /api/v1/alerts/clear-all
│   ├── device.py                   # PUT /api/v1/devices/fcm-token, PATCH heartbeat-schedule
│   ├── app_version.py              # GET /api/v1/app/version-check, PUT/GET /api/v1/admin/app-version
│   └── subscription.py             # GET/POST /api/v1/subscription
├── services/
│   ├── user_service.py             # 사용자 등록, invite_code 생성
│   ├── heartbeat_service.py        # heartbeat 비즈니스 로직
│   ├── alert_service.py            # 경고 생성/클리어/보호자 Push 발송
│   ├── subject_service.py          # 대상자-보호자 연결 관리
│   ├── push_service.py             # Silent Push / 일반 Push 발송
│   ├── subscription_service.py     # 구독 상태 관리
│   └── scheduler.py                # APScheduler 경고 체크 + Silent Push + 구독 만료
├── models/
│   ├── user.py                     # Pydantic 모델 (요청/응답 스키마)
│   ├── device.py
│   ├── guardian.py
│   ├── alert.py
│   ├── heartbeat.py
│   └── subscription.py
├── middleware/
│   └── auth.py                     # device_token 기반 인증 (FastAPI Depends)
├── requirements.txt                # Python 패키지 목록
├── Procfile                        # Railway 배포용 (web: uvicorn main:app)
└── firebase-service-account.json   # FCM 인증 키 (환경변수로 대체 가능)
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
    "os_version": "Android 14"
  }
}
Response: 201 Created
{
  "user_id": 1,
  "invite_code": "K7M-4PXR",
  "device_token": "generated-bearer-token"
}
```

- 기기 정보와 역할만 전송
- `invite_code`는 서버가 생성 (7자리 영숫자, UNIQUE 보장)
- `device_token`은 **만료 없이 무제한** 유효
- 대상자는 구독(subscription) 없음 — 결제는 보호자가 담당
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
- 연결된 대상자가 5명 이상이면 400 에러 (`"대상자는 최대 5명까지 등록 가능합니다"`)
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
      "alert": null
    }
  ],
  "max_subjects": 5,
  "can_add_more": true
}
```

- `status`: `normal` (정상), `warning` (경고)
- `alert`: 활성 경고가 있으면 `{ "id": 10, "days_inactive": 2 }`, 없으면 `null`
- 이름 없음 — 클라이언트가 로컬 별칭과 `invite_code`를 매칭하여 표시


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


### 4.6 Heartbeat 수신
```
POST /api/v1/heartbeat
Headers:
  Authorization: Bearer <device_token>
Body:
{
  "device_id": "uuid-v4",
  "timestamp": "2026-03-18T14:32:00+09:00",
  "steps_delta": 342,
  "suspicious": false,
  "battery_level": 85
}
// steps_delta: 이전 heartbeat 이후 걸음수 증가량 (권한 거부 시 null)
Response: 200 OK
{
  "status": "ok",
  "server_time": "2026-03-18T14:32:01+09:00",
  "heartbeat_hour": 9,
  "heartbeat_minute": 30
}
```

- 서버는 heartbeat 수신 시 해당 기기의 `last_seen`을 갱신
- heartbeat 수신 시 해당 대상자의 active 경고가 있으면 → 자동 해소 (status: `resolved`) + 보호자에게 "정상 복귀" Push 발송
- 대상자는 구독과 무관하게 항상 heartbeat 전송 (구독은 보호자가 관리)
- 보호자 구독이 만료된 경우, heartbeat는 수신하되 보호자에게 경고/알림을 발송하지 않음
- **서버 판정 로직** (상세: [heartbeat_flowchart.md](heartbeat_flowchart.md) 차트 2):
  - `battery_level` ≤ 10% + 기존 info 경고 없으면 → 정보 등급 1회 발송 (중복 방지, DND 적용)
  - `suspicious` = false:
    - 활성 경고 있으면 → 완전 해소 + 보호자 Push "정상 복귀" (정보 등급 DND 적용)
    - 활성 경고 없고 `manual` = true → 보호자 Push "수동 안부 확인" (정보 등급 DND 적용)
    - 활성 경고 없고 `manual` = false → 보호자 Push "오늘 안부 확인 완료" (정보 등급 DND 적용)
  - `suspicious` = true → warning/urgent 경고를 caution으로 하향 (정상 복귀 알림 없음)
    - 1회 → 주의 등급 발생 (caution 중복 방지)
    - 2회 이상 → 경고 등급 발생 (warning/urgent 없을 때만)
  - 보호자 설정 "안부 확인 알림 ON" 시 → 대상자에게 안부 확인 Push 발송 (suspicious 2회+)


### 4.7 구독 상태 확인 (보호자용)
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


### 4.8 구독 갱신 (인앱 결제 영수증 검증, 보호자용)
```
POST /api/v1/subscription/verify
Headers:
  Authorization: Bearer <device_token>  (보호자)
Body:
{
  "platform": "android",
  "product_id": "anbu_yearly",
  "receipt": "purchase-token-string"
}
Response: 200 OK
{
  "plan": "yearly",
  "expires_at": "2027-03-18T00:00:00+09:00",
  "is_active": true
}
```

- 단일 상품 (`anbu_yearly`) — 대상자 최대 5명, 티어 구분 없음


### 4.9 구독 복원 (보호자 앱 재설치 시)
```
POST /api/v1/subscription/restore
Headers:
  Authorization: Bearer <device_token>  (보호자)
Body:
{
  "platform": "android",
  "product_id": "anbu_yearly",
  "receipt": "purchase-token-string"
}
Response: 200 OK
{
  "plan": "yearly",
  "expires_at": "2027-03-18T00:00:00+09:00",
  "is_active": true,
  "restored": true
}
```

- 보호자 앱 재설치 후 Apple/Google에서 기존 구독 영수증을 가져와 서버에 검증 요청
- 유효한 구독이 확인되면 새 보호자 계정에 구독 활성화
- 인앱 결제는 보호자의 Apple ID / Google 계정에 귀속되므로 개인정보 없이도 복구 가능


### 4.10 경고 목록 조회 (보호자용)
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


### 4.11 경고 클리어 (보호자용)

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


### 4.12 FCM 토큰 갱신
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


### 4.13 Heartbeat 시각 변경
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

- **대상자 본인** 또는 **보호자** 모두 호출 가능
- 보호자가 호출 시: 해당 대상자가 본인에게 연결된 대상자인지 권한 검증
- 대상자가 호출 시: 본인 기기만 변경 가능
- 선택 범위: 06:00 ~ 21:00 (30분 단위)
- 서버에서 `devices.heartbeat_hour`, `devices.heartbeat_minute` 갱신
- iOS: 다음 Silent Push가 변경된 시각에 발송됨
- Android: 대상자 기기가 다음 heartbeat 시 응답의 `heartbeat_hour/minute`를 확인하여 WorkManager 재스케줄링


### 4.14 앱 버전 체크 (강제 업데이트)
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


### 4.15 앱 버전 설정 (Admin API)

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
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
    battery_level   INTEGER,                           -- 마지막 배터리 잔량 (0~100)
    suspicious_count INTEGER DEFAULT 0,                -- 연속 suspicious 횟수
    heartbeat_hour  INTEGER NOT NULL DEFAULT 9,        -- heartbeat 시각 (시, 0~23, 기본 9)
    heartbeat_minute INTEGER NOT NULL DEFAULT 30,      -- heartbeat 시각 (분, 0~59, 기본 30)
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
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_user_id     INTEGER NOT NULL REFERENCES users(id),
    guardian_user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
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
    push_pending        BOOLEAN NOT NULL DEFAULT FALSE,    -- 발송 시간대 외 보류 플래그
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
    client_ts       TIMESTAMPTZ NOT NULL,
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
```

**PostgreSQL 참고사항:**
- `BOOLEAN` → 네이티브 타입 사용 (`TRUE`/`FALSE`)
- `TIMESTAMPTZ` → 타임존 포함 타임스탬프 (UTC 저장, 조회 시 KST 변환)
- `SERIAL` → 자동 증가 정수 (PostgreSQL 시퀀스)
- asyncpg 비동기 드라이버 사용 (`$1`, `$2` 파라미터 바인딩)
- Railway PostgreSQL 플러그인으로 배포 시 DB 영구 보존 (Volume 불필요)


---


## 6. 서버 스케줄러


### 6.1 iOS/Android Silent Push 발송 (매일 고정 시각)
```
실행 주기: 매 분 정각 (APScheduler CronTrigger(second=0))

처리 흐름:
1. 현재 시각(KST)이 각 기기의 heartbeat 시각(heartbeat_hour:heartbeat_minute)에
   해당하는 대상자 기기 조회 (iOS/Android 공통)
   SELECT d.fcm_token, d.device_id, d.platform
   FROM devices d
   JOIN users u ON d.user_id = u.id
   WHERE u.role = 'subject'
     AND d.heartbeat_hour = :current_hour
     AND d.heartbeat_minute = :current_minute
     AND d.fcm_token IS NOT NULL

2. 조회된 각 기기에 Silent Push 발송 (type: heartbeat_trigger)
3. FCM 토큰 무효화 시 해당 기기의 fcm_token을 NULL로 갱신

※ 기본값: heartbeat_hour=9, heartbeat_minute=30 (KST 기준)
※ 보호자/대상자가 시각 변경 시 devices 테이블의 heartbeat_hour/minute 갱신
※ CronTrigger(second=0): 매 분 0초에 실행 — 서버 재시작 후에도 정각에 동기화됨
```


### 6.2 경고 생성 스케줄러 (고정 시각 기반 + 등급별 판정)

> 📊 상세 플로우차트: [heartbeat_flowchart.md](heartbeat_flowchart.md) — 차트 3 참조

```
실행 주기: 매 분 정각 (APScheduler CronTrigger(second=0))

처리 흐름:
1. 현재 시각이 heartbeat 시각 + 2시간에 해당하는 기기 중
   오늘 heartbeat를 수신하지 못한 대상자 조회
   SELECT u.id, d.last_seen, d.battery_level,
          d.heartbeat_hour, d.heartbeat_minute
   FROM users u
   JOIN devices d ON u.id = d.user_id
   WHERE u.role = 'subject'
     AND (d.heartbeat_hour * 60 + d.heartbeat_minute + 120) = :current_minutes_of_day
     AND d.last_seen < datetime('now', 'start of day')

   ※ 기본: heartbeat_hour=9, heartbeat_minute=30 → 11:30(690분)에 체크

2. 보호자 구독 확인:
   a. 보호자 구독 만료 → 알림 미발송 (heartbeat는 계속 수신)
   b. 보호자 구독 활성 → 등급 판정 진행

3. 등급 판정 (누적 미수신 횟수 기반):
   a. battery_level ≤ 10%
      → 정보 등급 1회 발송 후 종료 (이후 상향 없음, 소리 없음)
      → heartbeat 수신 시 자동 해소

   b. 활성 경고 없음 (1회 미수신)
      → 주의 등급: "오늘 안부 확인이 없습니다" (1회 발송)

   c. caution 활성 (2회 미수신)
      → 경고 등급: "안부 확인이 없습니다. 통신 불가 상태일 수 있습니다"

   d. warning 활성 (3회 이상 미수신)
      → 긴급 등급: "즉시 확인이 필요합니다"

   e. urgent 활성 (긴급 지속)
      → 긴급 반복: days_inactive 증가, 매일 반복 발송

4. DND(방해금지) 설정 반영:
   - 보호자별 DND 시간대 설정 가능 (기본 OFF)
   - 긴급 등급은 DND 무관하게 항상 발송

5. 경고 반복 정책:
   - 정보 등급: 1회 발송, 이후 상향 없음
   - 주의 등급: 1회 발송
   - 경고 등급: 1~2회 다음날 재발송 → 3회 이상 긴급 상향
   - 긴급 등급: 보호자 확인까지 매일 반복 (종료 없음)
```


### 6.3 구독 만료 체크 (보호자 기준)
```
실행 주기: 매일 00:00 KST

처리 흐름:
1. SELECT u.id FROM users u
   JOIN subscriptions s ON u.id = s.user_id
   WHERE u.role = 'guardian'
     AND s.plan IN ('free_trial', 'yearly')
     AND s.expires_at < datetime('now')

2. 조회된 각 보호자:
   a. subscriptions.plan = 'expired'로 변경
   b. 보호자 기기에 일반 Push 발송 ("무료 체험이 종료되었습니다")

3. 구독 만료 시 동작:
   - 대상자 heartbeat는 계속 정상 수신 (대상자 앱에 영향 없음)
   - 해당 보호자에게 경고 알림 발송 중단
   - 보호자 앱 실행 시 결제 페이지로 자동 전환 (클라이언트 처리)
```


### 6.4 보호자 미연결 대상자 자동 정리
```
실행 주기: 매일 03:00 KST

처리 흐름:
1. SELECT u.id FROM users u
   WHERE u.role = 'subject'
     AND u.created_at < datetime('now', '-30 days')
     AND NOT EXISTS (
       SELECT 1 FROM guardians g WHERE g.subject_user_id = u.id
     )

2. 조회된 각 대상자:
   a. heartbeat_logs 삭제 (해당 device_id)
   b. alerts 삭제
   c. devices 삭제
   e. users 삭제

3. 별도 알림 없음 (앱은 그대로 남아있음)
   · 이후 대상자가 앱 실행 시 → API 호출 → 401 응답
   · 앱이 모드 선택 화면으로 이동 → 재등록 → 새 고유 코드 발급
   · 무료 체험은 device_id로 판단하므로 기존 기간 유지
```


### 6.5 보호자 Push 발송 실패 처리
```
실행 주기: Push 발송 시마다

처리 흐름:
1. FCM 발송 시 InvalidRegistration 또는 NotRegistered 에러 수신
2. 해당 보호자 기기의 fcm_token을 NULL 처리
3. 대상자에게 "보호자 연결 끊김" 일반 Push 발송
```


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
- 재등록하여 새 계정 생성 (새 invite_code, 새 device_token)
- 보호자와 재연결 필요 (새 고유 코드 전달)
- 결제 관련 동작 없음 (대상자는 무료)

**보호자 앱 재설치:**
- 재등록하여 새 계정 생성 (새 device_token)
- 대상자와 재연결 필요 (고유 코드 재입력)
- 유료 구독은 Apple/Google `restoreTransactions`로 복구 가능
- 개인정보를 수집하지 않으므로 계정 복구 로직 자체가 불필요


---


## 8. 보안


### 8.1 통신
- HTTPS 필수 (TLS 1.2+)
- heartbeat payload 최소화 (가속도 센서 값 + 의심 플래그만 포함, 민감 정보 없음)


### 8.2 데이터
- **개인정보 미수집**: 이름, 전화번호, 위치정보, 사용 앱 목록 등 일절 저장하지 않음
- 수집 데이터: device_id, 가속도+자이로 센서 값, suspicious 플래그, 앱 버전 — 최소 수준
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


## 9. Python 핵심 패키지

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
aiosqlite==0.21.*
apscheduler==3.11.*
firebase-admin==6.6.*
python-dotenv==1.1.*
```


---


## 10. 배포 및 인프라


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

**Procfile (Railway 배포 설정):**
```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```


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


---


## 11. 연동 API 목록 (FrontEnd 참조)

| API | 메서드 | 용도 |
|-----|--------|------|
| `/api/v1/users` | POST | 사용자 등록 (대상자/보호자) |
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
| `/api/v1/devices/fcm-token` | PUT | FCM 토큰 갱신 |
| `/api/v1/devices/{device_id}/heartbeat-schedule` | PATCH | heartbeat 시각 변경 (대상자/보호자) |
| `/api/v1/app/version-check` | GET | 앱 버전 체크 (강제 업데이트 판정) |
| `/api/v1/admin/app-version` | PUT | 앱 버전 설정 (Admin, Postman용) |
| `/api/v1/admin/app-version` | GET | 앱 버전 설정 조회 (Admin, Postman용) |

> FrontEnd 상세는 [PRD-FrontEnd.md](PRD-FrontEnd.md) 참조


---


## 12. 성공 지표 (서버)

| 지표 | 목표 |
|------|------|
| 서버 응답 시간 | heartbeat API p99 < 200ms |
| 서버 가용성 | ≥ 99.5% |
| Silent Push 발송 성공률 | ≥ 98% |
| 경고 생성 정확도 | 거짓 경고 ≤ 5% |
| 보호자 Push 전달률 | ≥ 95% |


---


## 13. 향후 확장 (v2.0+, 참고용)

- **알림 채널 확장**: 경고 발생 시 SMS, 카카오톡, 이메일 자동 발송
- **보호자 웹 대시보드**: 관리 대상자 목록, 실시간 상태 모니터링 UI
- **경고 에스컬레이션**: 단계별 심각도 상향 + 외부 기관 연계
- **통계/리포트**: 주간/월간 활동 리포트 생성
- **다중 기기 통합**: 동일 사용자의 여러 기기를 하나의 프로필로 묶기
- **관리자 대시보드 API**: 웹 기반 관리자 화면용 API 확장
- **DB 스케일업**: 사용자 폭증 시 Railway PostgreSQL 플랜 업그레이드


---


## 14. 개발 일정 (예상)

| 단계 | 산출물 |
|------|--------|
| 1단계: DB + 사용자 등록/연결 API | FastAPI 서버, PostgreSQL 스키마, 사용자 등록, 고유 코드 생성, 대상자 연결 API |
| 2단계: Heartbeat + 경고 | heartbeat 수신, 경고 생성/클리어 로직 |
| 3단계: Push 서비스 | Silent Push 발송, 보호자 FCM Push 발송 |
| 4단계: 스케줄러 | APScheduler — iOS Silent Push 주기, 경고 체크, 구독 만료 체크 |
| 5단계: 구독/결제 API | 구독 상태 관리, 영수증 검증, 구독 복원 |
| 6단계: Railway 배포 + 통합 테스트 | Railway 배포, E2E 테스트 |
