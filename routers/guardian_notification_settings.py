from fastapi import APIRouter, Depends
import asyncpg

from database import get_db
from middleware.auth import require_guardian
from models.notification_settings import NotificationSettingsIn, NotificationSettingsOut

router = APIRouter(prefix="/api/v1/guardian/notification-settings", tags=["notification-settings"])


@router.get("", response_model=NotificationSettingsOut)
async def get_settings(
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        "SELECT * FROM guardian_notification_settings WHERE guardian_user_id = $1",
        user["user_id"],
    )

    if row is None:
        # 설정 없으면 기본값 반환
        return NotificationSettingsOut(
            all_enabled=True,
            urgent_enabled=True,
            warning_enabled=True,
            caution_enabled=True,
            info_enabled=True,
            dnd_enabled=False,
            dnd_start=None,
            dnd_end=None,
        )

    return NotificationSettingsOut(
        all_enabled=row["all_enabled"],
        urgent_enabled=row["urgent_enabled"],
        warning_enabled=row["warning_enabled"],
        caution_enabled=row["caution_enabled"],
        info_enabled=row["info_enabled"],
        dnd_enabled=row["dnd_enabled"],
        dnd_start=row["dnd_start"],
        dnd_end=row["dnd_end"],
    )


@router.put("", response_model=NotificationSettingsOut)
async def update_settings(
    body: NotificationSettingsIn,
    user: dict = Depends(require_guardian),
    db: asyncpg.Connection = Depends(get_db),
):
    await db.execute(
        """INSERT INTO guardian_notification_settings
               (guardian_user_id, all_enabled, urgent_enabled, warning_enabled,
                caution_enabled, info_enabled, dnd_enabled, dnd_start, dnd_end, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
           ON CONFLICT (guardian_user_id) DO UPDATE SET
               all_enabled     = EXCLUDED.all_enabled,
               urgent_enabled  = EXCLUDED.urgent_enabled,
               warning_enabled = EXCLUDED.warning_enabled,
               caution_enabled = EXCLUDED.caution_enabled,
               info_enabled    = EXCLUDED.info_enabled,
               dnd_enabled     = EXCLUDED.dnd_enabled,
               dnd_start       = EXCLUDED.dnd_start,
               dnd_end         = EXCLUDED.dnd_end,
               updated_at      = EXCLUDED.updated_at""",
        user["user_id"],
        body.all_enabled,
        body.urgent_enabled,
        body.warning_enabled,
        body.caution_enabled,
        body.info_enabled,
        body.dnd_enabled,
        body.dnd_start,
        body.dnd_end,
    )

    return NotificationSettingsOut(
        all_enabled=body.all_enabled,
        urgent_enabled=body.urgent_enabled,
        warning_enabled=body.warning_enabled,
        caution_enabled=body.caution_enabled,
        info_enabled=body.info_enabled,
        dnd_enabled=body.dnd_enabled,
        dnd_start=body.dnd_start,
        dnd_end=body.dnd_end,
    )
