from fastapi import APIRouter, Depends, Header, HTTPException, Query
import aiosqlite

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
    db: aiosqlite.Connection = Depends(get_db),
):
    if platform not in ("android", "ios"):
        raise HTTPException(status_code=400, detail="platform은 'android' 또는 'ios'여야 합니다")

    async with db.execute(
        "SELECT latest_version, min_version, store_url FROM app_versions WHERE platform = ?",
        (platform,),
    ) as cur:
        row = await cur.fetchone()

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
    db: aiosqlite.Connection = Depends(get_db),
):
    if body.platform not in ("android", "ios"):
        raise HTTPException(status_code=400, detail="platform은 'android' 또는 'ios'여야 합니다")

    if _compare_versions(body.latest_version, body.min_version) < 0:
        raise HTTPException(status_code=400, detail="latest_version은 min_version 이상이어야 합니다")

    async with db.execute(
        "SELECT store_url FROM app_versions WHERE platform = ?", (body.platform,)
    ) as cur:
        existing = await cur.fetchone()

    store_url = body.store_url or (existing["store_url"] if existing else "")

    await db.execute(
        """INSERT INTO app_versions (platform, latest_version, min_version, store_url, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(platform) DO UPDATE SET
             latest_version = excluded.latest_version,
             min_version = excluded.min_version,
             store_url = excluded.store_url,
             updated_at = excluded.updated_at""",
        (body.platform, body.latest_version, body.min_version, store_url),
    )
    await db.commit()

    async with db.execute(
        "SELECT platform, latest_version, min_version, store_url, updated_at FROM app_versions WHERE platform = ?",
        (body.platform,),
    ) as cur:
        row = await cur.fetchone()

    return AppVersionOut(**dict(row))


@router.get("/admin/app-version", response_model=AppVersionOut)
async def get_app_version(
    platform: str = Query(...),
    _: None = Depends(_verify_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        "SELECT platform, latest_version, min_version, store_url, updated_at FROM app_versions WHERE platform = ?",
        (platform,),
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="버전 정보를 찾을 수 없습니다")

    return AppVersionOut(**dict(row))
