"""
Authentication dependencies for FastAPI routes.
Validates JWT tokens and returns the current user.
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from config import settings
from database import get_db, get_redis
from models.user import User

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> User:
    """
    Validate access token and return the authenticated user.
    Raises 401 on invalid/expired token.
    Raises 403 on suspended account.
    """
    token = credentials.credentials
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        raise credentials_error

    # Scope check — must be a user token (not admin)
    if payload.get("scope") != "user":
        raise credentials_error

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise credentials_error

    # JTI blacklist (logout/revocation)
    jti = payload.get("jti")
    if jti:
        redis = get_redis()
        if await redis.exists(f"jti:blacklist:{jti}"):
            raise credentials_error

    # Fetch user from DB
    db = get_db()
    user_doc = await db.users.find_one({"id": user_id})
    if not user_doc:
        raise credentials_error

    user = User(**user_doc)

    # Note: We don't block suspended users here anymore
    # The frontend will display a suspension modal overlay
    # This allows the user to see the suspension reason and contact support
    # without being completely logged out

    # Update last_active_at (non-blocking fire-and-forget)
    await db.users.update_one(
        {"id": user_id},
        {"$set": {"last_active_at": datetime.utcnow()}},
    )

    return user


async def get_current_user_optional(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> User | None:
    """Like get_current_user but returns None if no token provided."""
    if not credentials:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


def require_tier(*tiers: str):
    """
    Factory dependency: require the user to be on one of the given tiers.

    Usage:
        @router.get("/sync", dependencies=[Depends(require_tier("pro", "team"))])
    """
    async def _check_tier(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.tier not in tiers:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires one of: {', '.join(tiers)}",
            )
        return user
    return _check_tier


# Type aliases for route signatures
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_current_user_optional)]
