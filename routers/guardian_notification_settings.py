from fastapi import APIRouter, Depends
import aiosqlite

from database import get_db
from middleware.auth import require_guardian
from models.notification_settings import NotificationSettingsIn, NotificationSettingsOut

router = APIRouter(prefix="/api/v1/guardian/notification-settings", tags=["notification-settings"])


@router.get("", response_model=NotificationSettingsOut)
async def get_settings(
    user: dict = Depends(require_guardian),
    db: aiosqlite.Connection = Depends(get_db),
):
    async with db.execute(
        "SELECT * FROM guardian_notification_settings WHERE guardian_user_id = ?",
        (user["user_id"],),
    ) as cur:
        row = await cur.fetchone()

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
        all_enabled=bool(row["all_enabled"]),
        urgent_enabled=bool(row["urgent_enabled"]),
        warning_enabled=bool(row["warning_enabled"]),
        caution_enabled=bool(row["caution_enabled"]),
        info_enabled=bool(row["info_enabled"]),
        dnd_enabled=bool(row["dnd_enabled"]),
        dnd_start=row["dnd_start"],
        dnd_end=row["dnd_end"],
    )


@router.put("", response_model=NotificationSettingsOut)
async def update_settings(
    body: NotificationSettingsIn,
    user: dict = Depends(require_guardian),
    db: aiosqlite.Connection = Depends(get_db),
):
    await db.execute(
        """INSERT INTO guardian_notification_settings
               (guardian_user_id, all_enabled, urgent_enabled, warning_enabled,
                caution_enabled, info_enabled, dnd_enabled, dnd_start, dnd_end, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(guardian_user_id) DO UPDATE SET
               all_enabled     = excluded.all_enabled,
               urgent_enabled  = excluded.urgent_enabled,
               warning_enabled = excluded.warning_enabled,
               caution_enabled = excluded.caution_enabled,
               info_enabled    = excluded.info_enabled,
               dnd_enabled     = excluded.dnd_enabled,
               dnd_start       = excluded.dnd_start,
               dnd_end         = excluded.dnd_end,
               updated_at      = excluded.updated_at""",
        (
            user["user_id"],
            int(body.all_enabled),
            int(body.urgent_enabled),
            int(body.warning_enabled),
            int(body.caution_enabled),
            int(body.info_enabled),
            int(body.dnd_enabled),
            body.dnd_start,
            body.dnd_end,
        ),
    )
    await db.commit()

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
