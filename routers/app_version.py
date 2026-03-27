from fastapi import APIRouter, Depends, Header, HTTPException, Query
import asyncpg

from config import ADMIN_SECRET_KEY
from database import get_db
from models.app_version import AppVersionCheckOut, AppVersionUpdateIn, AppVersionOut

router = APIRouter(prefix="/api/v1", tags=["app_version"])


def _compare_versions(v1: str, v2: str) -> int:
    """v1 < v2 → -1, v1 == v2 → 0, v1 > v2 → 1"""
    parts1 = [int(x) for x in v1.split(".")]
    parts2 = [int(x) for x in v2.split(".")]
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
    current_version: str = Query(...),
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
    if not ADMIN_SECRET_KEY or x_admin_key != ADMIN_SECRET_KEY:
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
