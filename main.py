import logging
from contextlib import asynccontextmanager

import config  # .env 로드 (FIREBASE_CREDENTIALS 등)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_pool, close_pool
from services.scheduler import setup_scheduler
from routers import user, heartbeat, subject, alert, device, app_version, subscription
from routers import guardian_notification_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
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


app = FastAPI(
    title="Anbu (안부) API",
    description="안부(Anbu) 앱 서버 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"[main] Starting uvicorn on port {port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
