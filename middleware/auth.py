from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from database import get_db
import asyncpg

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: asyncpg.Connection = Depends(get_db),
) -> dict:
    token = credentials.credentials
    row = await db.fetchrow(
        "SELECT id, role FROM users WHERE device_token = $1", token
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 토큰입니다",
        )
    return {"user_id": row["id"], "role": row["role"]}


async def require_guardian(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "guardian":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="보호자만 접근할 수 있습니다",
        )
    return user


async def require_subject(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "subject":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="대상자만 접근할 수 있습니다",
        )
    return user
