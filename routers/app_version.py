import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Query
import asyncpg

from config import ADMIN_SECRET_KEY
from database import get_db
from models.app_version import AppVersionCheckOut, AppVersionUpdateIn, AppVersionOut

router = APIRouter(prefix="/api/v1", tags=["app_version"])


def _parse_version(v: str) -> list[int]:
    """버전 문자열을 정수 리스트로 파싱. 비정상 토큰(숫자 아님)은 0으로 처리해
    공개 엔드포인트가 잘못된 입력에 500으로 터지지 않게 한다."""
    parts: list[int] = []
    for x in v.split("."):
        try:
            parts.append(int(x))
        except (ValueError, TypeError):
            parts.append(0)
    return parts


def _compare_versions(v1: str, v2: str) -> int:
    """v1 < v2 → -1, v1 == v2 → 0, v1 > v2 → 1"""
    parts1 = _parse_version(v1)
    parts2 = _parse_version(v2)
    for a, b in zip(parts1, parts2):
        if a < b:
            return -1
        if a > b:
            return 1
    if len(parts1) < len(parts2):
        return -1
    if len(parts1) > len(parts2):
        return 1
    return 0


@router.get("/app/version-check", response_model=AppVersionCheckOut)
async def version_check(
    platform: str = Query(...),
    current_version: str = Query(..., max_length=32),
    db: asyncpg.Connection = Depends(get_db),
):
    if platform not in ("android", "ios"):
        raise HTTPException(status_code=400, detail="platform은 'android' 또는 'ios'여야 합니다")

    row = await db.fetchrow(
        "SELECT latest_version, min_version, store_url FROM app_versions WHERE platform = $1",
        platform,
    )

    if row is None:
        raise HTTPException(status_code=404, detail="버전 정보를 찾을 수 없습니다")

    force_update = _compare_versions(current_version, row["min_version"]) < 0

    return AppVersionCheckOut(
        platform=platform,
        current_version=current_version,
        latest_version=row["latest_version"],
        min_version=row["min_version"],
        force_update=force_update,
        store_url=row["store_url"],
    )


def _verify_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> None:
    # ADMIN_SECRET_KEY 미설정("")이면 fail-closed(전부 거부). 비교는 상수시간으로
    # 수행해 타이밍 사이드채널을 차단한다.
    if not ADMIN_SECRET_KEY or not secrets.compare_digest(x_admin_key, ADMIN_SECRET_KEY):
        raise HTTPException(status_code=403, detail="관리자 인증에 실패했습니다")


@router.put("/admin/app-version", response_model=AppVersionOut)
async def set_app_version(
    body: AppVersionUpdateIn,
    _: None = Depends(_verify_admin),
    db: asyncpg.Connection = Depends(get_db),
):
    if body.platform not in ("android", "ios"):
        raise HTTPException(status_code=400, detail="platform은 'android' 또는 'ios'여야 합니다")

    if _compare_versions(body.latest_version, body.min_version) < 0:
        raise HTTPException(status_code=400, detail="latest_version은 min_version 이상이어야 합니다")

    existing = await db.fetchrow(
        "SELECT store_url FROM app_versions WHERE platform = $1", body.platform
    )

    store_url = body.store_url or (existing["store_url"] if existing else "")

    await db.execute(
        """INSERT INTO app_versions (platform, latest_version, min_version, store_url, updated_at)
           VALUES ($1, $2, $3, $4, NOW())
           ON CONFLICT (platform) DO UPDATE SET
             latest_version = EXCLUDED.latest_version,
             min_version = EXCLUDED.min_version,
             store_url = EXCLUDED.store_url,
             updated_at = EXCLUDED.updated_at""",
        body.platform, body.latest_version, body.min_version, store_url,
    )

    row = await db.fetchrow(
        "SELECT platform, latest_version, min_version, store_url, updated_at FROM app_versions WHERE platform = $1",
        body.platform,
    )

    return AppVersionOut(
        platform=row["platform"],
        latest_version=row["latest_version"],
        min_version=row["min_version"],
        store_url=row["store_url"],
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )


@router.get("/admin/app-version", response_model=AppVersionOut)
async def get_app_version(
    platform: str = Query(...),
    _: None = Depends(_verify_admin),
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        "SELECT platform, latest_version, min_version, store_url, updated_at FROM app_versions WHERE platform = $1",
        platform,
    )

    if row is None:
        raise HTTPException(status_code=404, detail="버전 정보를 찾을 수 없습니다")

    return AppVersionOut(
        platform=row["platform"],
        latest_version=row["latest_version"],
        min_version=row["min_version"],
        store_url=row["store_url"],
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
    )
