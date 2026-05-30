import logging
from contextlib import asynccontextmanager

import config  # .env 로드 (FIREBASE_CREDENTIALS 등)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_pool, close_pool
from services.scheduler import setup_scheduler
from routers import user, heartbeat, subject, alert, device, app_version, subscription
from routers import guardian_notification_settings, notifications
from routers import emergency, iap_notification

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 시작
    await init_pool()
    logger.info("DB 초기화 완료")

    sched = setup_scheduler()
    sched.start()
    logger.info("스케줄러 시작")

    yield

    # 종료
    sched.shutdown(wait=False)
    await close_pool()
    logger.info("서버 종료")


# prod(ENV=production)에서는 스키마 노출을 줄이기 위해 /docs·/redoc·openapi.json 비활성화.
# 모든 엔드포인트가 토큰 보호 + 무 PII라 위험은 낮지만, 불필요한 공개 표면을 줄인다.
import os as _os
_is_prod = _os.environ.get("ENV", "").lower() == "production"

app = FastAPI(
    title="Anbu (안부) API",
    description="안부(Anbu) 앱 서버 API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# 네이티브 앱(Bearer 토큰) 전용 API라 쿠키를 쓰지 않으므로 credentials를 끈다.
# allow_credentials=True + allow_origins=["*"] 조합은 브라우저가 무시하는 무의미한
# 설정이기도 하다. 모바일 클라이언트는 CORS 대상이 아니므로 origin은 그대로 둔다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user.router)
app.include_router(heartbeat.router)
app.include_router(subject.router)
app.include_router(alert.router)
app.include_router(device.router)
app.include_router(app_version.router)
app.include_router(subscription.router)
app.include_router(guardian_notification_settings.router)
app.include_router(notifications.router)
app.include_router(emergency.router)
app.include_router(iap_notification.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # 한 컨테이너 내 워커 수. 워커>1이면 각 프로세스가 독립 lifespan(스케줄러+풀)을
    # 갖지만, 스케줄러 잡은 advisory lock으로 단일 실행되므로 안전하다.
    # 수평 확장은 Railway 레플리카 수를 늘려도 동일하게 동작한다(엣지 LB는 Railway 담당).
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    print(f"[main] Starting uvicorn on port {port} (workers={workers})", flush=True)
    if workers > 1:
        # workers>1은 app을 import 문자열로 넘겨야 한다.
        uvicorn.run("main:app", host="0.0.0.0", port=port, workers=workers)
    else:
        uvicorn.run(app, host="0.0.0.0", port=port)
