# Anbu 서버 (anbucheck-server)

FastAPI + PostgreSQL(asyncpg) + Railway. 안부 확인 앱의 서버. 설계·API 명세는 `.ref/PRD-BackEnd.md`.

## 번역 규칙 (푸시·알림 문구)

- **`i18n/messages.py`(20개 언어)의 문구를 새로 쓰거나 고치기 전에 앱 저장소의 번역 용어집을 먼저 읽는다**:
  `../kr.co.anbucheck/.claude/translation_glossary.md`
  (언어별 안부·대상자·보호자 용어, 금지어 — 신호·후견·건강검진 계열, 경어, 숫자·복수형, 서버↔앱 대응표)
- 푸시 문구(`push_*_body`)와 앱 알림 목록 문구(`noti_*_body`)는 **같은 알림을 두 번 보여 주므로 글자까지 같게** 맞춘다 — 용어집 §6 대응표. 앱 번역은 `../kr.co.anbucheck/lib/app/core/translations/*.dart`.
- 푸시 본문의 숫자는 `format_number(locale, n)`(용어집 §3·§5, 앱 `NumberText`와 같은 표)로 만든다. 알림 목록 저장분(`notification_events`)은 ko 형식으로 저장하고 앱이 `NumberText`로 다시 표기한다.
- 별칭은 문장 안에 끼우지 않고 본문 앞에 ` · `로 붙인다(`decorate_body`).
- 용어를 바꾸려면 용어집을 먼저 고치고 앱·홈페이지·쇼츠에 같이 반영한다.

## 규칙

- 응답·주석·커밋 메시지는 **한글**.
