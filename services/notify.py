"""Discord 웹훅 에러 알림 — 서버 핵심 경로에서 예외 발생 시 Discord로 즉시 통보.

설계 원칙:
- `DISCORD_WEBHOOK_URL` 환경변수가 없으면 완전 비활성(no-op) — 로컬/테스트 안전.
- 알림 전송 자체는 절대 예외를 밖으로 던지지 않는다 (알림 실패가 본 기능을 깨면 안 됨).
- 새 의존성 없이 표준 라이브러리(urllib)로 전송한다.
- 같은 컨텍스트의 연속 오류는 인메모리 throttle로 5분에 1회만 보낸다 — 매 분 도는
  잡이 지속 실패할 때 Discord 채널이 도배되는 것을 막는다.
  (throttle은 프로세스 단위라 Railway 레플리카가 여러 개면 API 500이 레플리카당 1회씩
   알림될 수 있다. 스케줄러 잡은 advisory lock으로 단일 실행되므로 영향 없음.)
- 에러 직전 맥락 제공: 최근 로그 N줄을 고정 크기 인메모리 링버퍼에 순환 보관하다가,
  에러 알림 시 함께 첨부한다. 평소 부담은 "로그 1줄을 deque에 덮어쓰기"뿐이고
  네트워크 전송은 에러 순간에만 일어난다(외부 로그 저장소 상시 전송과 다름).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import traceback
import urllib.request

logger = logging.getLogger(__name__)

# 같은 컨텍스트 재알림 최소 간격(초)
_THROTTLE_SECONDS = 300
# context 키 → 마지막 전송 시각(monotonic)
_last_sent: dict[str, float] = {}

# Discord content 길이 제한은 2000자 — 여유를 두고 스택트레이스를 자른다.
_MAX_TRACE = 1500
# 에러 알림에 첨부할 최근 로그 텍스트 최대 길이(Discord embed 여유분)
_MAX_RECENT = 1500


class _RingBufferHandler(logging.Handler):
    """최근 로그 N줄을 고정 크기 deque에 순환 보관하는 경량 핸들러.

    emit은 포맷된 한 줄 문자열을 deque(maxlen)에 append할 뿐이라 O(1)·고정 메모리다.
    실제 네트워크 전송은 하지 않으므로 상시 부담이 사실상 없다.
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._buf: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 — 로깅 핸들러는 절대 throw하지 않는다
            return
        # deque.append는 GIL 하에서 원자적. 스냅샷과의 경쟁은 핸들러 lock으로 직렬화.
        self.acquire()
        try:
            self._buf.append(line)
        finally:
            self.release()

    def text(self) -> str:
        self.acquire()
        try:
            lines = list(self._buf)
        finally:
            self.release()
        joined = "\n".join(lines)
        return joined[-_MAX_RECENT:]


_ring_handler: _RingBufferHandler | None = None


def install_log_buffer(capacity: int = 50, level: int = logging.INFO) -> None:
    """루트 로거에 링버퍼 핸들러를 1회 부착한다(서버 시작 시 호출). 멱등."""
    global _ring_handler
    if _ring_handler is not None:
        return
    _ring_handler = _RingBufferHandler(capacity)
    _ring_handler.setLevel(level)
    logging.getLogger().addHandler(_ring_handler)


def _recent_logs() -> str:
    return _ring_handler.text() if _ring_handler is not None else ""


def _webhook_url() -> str:
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def _post(payload: dict) -> None:
    """Discord 웹훅으로 1회 POST. 절대 예외를 던지지 않는다."""
    url = _webhook_url()
    if not url:
        return
    try:
        data = json.dumps(payload).encode("utf-8")
        # Discord는 기본 Python-urllib User-Agent를 403으로 차단하므로 명시 헤더가 필요하다.
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AnbuServer-AlertBot/1.0",
            },
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001 — 알림 실패는 삼킨다
        logger.warning("Discord 알림 전송 실패: %s", e)


def _build_payload(context: str, type_name: str | None, message: str | None, tb_text: str | None) -> dict:
    env = os.getenv("ENV", "dev")
    content = f"🔴 **[안부 서버 오류]** `{env}`\n**위치:** {context}"
    if type_name:
        msg = (message or "").strip()
        content += f"\n**예외:** `{type_name}: {msg}`"
    payload: dict = {"content": content}
    embeds: list[dict] = []
    if tb_text:
        trace = tb_text.strip()[-_MAX_TRACE:]
        embeds.append({"title": "스택트레이스", "description": f"```\n{trace}\n```", "color": 15158332})
    recent = _recent_logs()
    if recent:
        embeds.append({"title": "에러 직전 로그", "description": f"```\n{recent}\n```", "color": 9807270})
    if embeds:
        payload["embeds"] = embeds
    return payload


def _should_send(context: str, throttle: bool) -> bool:
    if not throttle:
        return True
    now = time.monotonic()
    last = _last_sent.get(context, 0.0)
    if now - last < _THROTTLE_SECONDS:
        return False
    _last_sent[context] = now
    return True


def notify_error_sync(
    context: str,
    exc: BaseException | None = None,
    *,
    tb_text: str | None = None,
    throttle: bool = True,
) -> None:
    """동기 진입점 — 이벤트 루프가 없는 곳(스케줄러 리스너)에서 사용.

    tb_text를 직접 받을 수 있다. APScheduler는 사전 포맷된 스택 문자열
    (event.traceback)을 주므로, 그 경우 exc의 __traceback__ 대신 이 값을 쓴다.
    """
    if not _webhook_url() or not _should_send(context, throttle):
        return
    type_name = type(exc).__name__ if exc is not None else None
    message = str(exc) if exc is not None else None
    if tb_text is None and exc is not None:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _post(_build_payload(context, type_name, message, tb_text))


async def notify_error(
    context: str,
    exc: BaseException | None = None,
    *,
    throttle: bool = True,
) -> None:
    """비동기 진입점 — FastAPI 핸들러 등 async 컨텍스트에서 사용.

    전송(blocking urllib)은 to_thread로 떼어내 이벤트 루프를 막지 않는다.
    """
    if not _webhook_url() or not _should_send(context, throttle):
        return
    type_name = type(exc).__name__ if exc is not None else None
    message = str(exc) if exc is not None else None
    tb_text = None
    if exc is not None:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    payload = _build_payload(context, type_name, message, tb_text)
    await asyncio.to_thread(_post, payload)
