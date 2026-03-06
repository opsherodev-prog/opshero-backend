"""
Public announcements endpoint.
GET /announcements — returns active announcements for the current user's tier and time.

Auth is OPTIONAL — unauthenticated requests receive announcements targeting all tiers.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from database import get_db

router = APIRouter(prefix="/announcements", tags=["announcements"])

_bearer = HTTPBearer(auto_error=False)


def _now() -> datetime:
    return datetime.utcnow()


def _serialize(doc: dict) -> dict:
    for field in ("show_from", "show_until", "created_at"):
        if doc.get(field) and hasattr(doc[field], "isoformat"):
            doc[field] = doc[field].isoformat()
    doc.pop("_id", None)
    return doc


async def _get_user_tier(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    """
    Extract user tier from JWT if present, otherwise return 'free'
    so unauthenticated visitors see general-audience announcements.
    """
    if not credentials:
        return "free"

    try:
        import jwt
        from config import settings
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("scope") != "user":
            return "free"

        db = get_db()
        user_id = payload.get("sub")
        user = await db.users.find_one({"id": user_id}, {"tier": 1})
        return user.get("tier", "free") if user else "free"
    except Exception:
        return "free"


@router.get("/")
async def list_announcements(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
):
    """
    Return active announcements visible to the current user.

    Filters:
    - active == True
    - show_from <= now
    - show_until is None OR show_until >= now
    - target_tiers includes the user's tier (or "free" for unauthenticated)
    """
    db = get_db()
    now = _now()
    tier = await _get_user_tier(credentials)

    query = {
        "active": True,
        "show_from": {"$lte": now},
        "$or": [
            {"show_until": {"$exists": False}},
            {"show_until": None},
            {"show_until": {"$gte": now}},
        ],
        "target_tiers": {"$in": [tier]},
    }

    docs = (
        await db.announcements
        .find(query, {"_id": 0, "created_by": 0})
        .sort("show_from", -1)
        .limit(5)
        .to_list(5)
    )

    return [_serialize(d) for d in docs]
