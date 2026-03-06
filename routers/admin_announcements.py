"""
Admin announcements CRUD.
GET/POST/PUT/DELETE /admin/announcements/*
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db
from deps.admin_auth import CurrentAdmin
from models.admin import Announcement

router = APIRouter(prefix="/admin/announcements", tags=["admin-announcements"])


# ── Request models ─────────────────────────────────────────────────────────

class AnnouncementCreate(BaseModel):
    type: str = "info"
    title: str
    message: str
    target_tiers: list[str] = ["free", "pro", "team"]
    dismissible: bool = True
    show_from: Optional[datetime] = None
    show_until: Optional[datetime] = None
    cta_text: Optional[str] = None
    cta_url: Optional[str] = None
    active: bool = True


class AnnouncementUpdate(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    message: Optional[str] = None
    target_tiers: Optional[list[str]] = None
    dismissible: Optional[bool] = None
    show_from: Optional[datetime] = None
    show_until: Optional[datetime] = None
    cta_text: Optional[str] = None
    cta_url: Optional[str] = None
    active: Optional[bool] = None


def _serialize(doc: dict) -> dict:
    """Convert datetime fields to ISO strings for JSON serialisation."""
    for field in ("show_from", "show_until", "created_at"):
        if doc.get(field) and hasattr(doc[field], "isoformat"):
            doc[field] = doc[field].isoformat()
    doc.pop("_id", None)
    return doc


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/")
async def list_announcements(admin: CurrentAdmin):
    db = get_db()
    docs = await db.announcements.find({}, {"_id": 0}).sort("created_at", -1).to_list(None)
    return [_serialize(d) for d in docs]


@router.post("/", status_code=201)
async def create_announcement(body: AnnouncementCreate, admin: CurrentAdmin):
    db = get_db()
    announcement = Announcement(
        type=body.type,
        title=body.title,
        message=body.message,
        target_tiers=body.target_tiers,
        dismissible=body.dismissible,
        show_from=body.show_from or datetime.utcnow(),
        show_until=body.show_until,
        cta_text=body.cta_text,
        cta_url=body.cta_url,
        active=body.active,
        created_by=admin.id,
    )
    doc = announcement.model_dump()
    await db.announcements.insert_one(doc)
    doc.pop("_id", None)
    return _serialize(doc)


@router.put("/{announcement_id}")
async def update_announcement(announcement_id: str, body: AnnouncementUpdate, admin: CurrentAdmin):
    db = get_db()
    existing = await db.announcements.find_one({"id": announcement_id})
    if not existing:
        raise HTTPException(404, "Announcement not found")

    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(400, "No fields to update")

    await db.announcements.update_one(
        {"id": announcement_id},
        {"$set": update_data},
    )

    updated = await db.announcements.find_one({"id": announcement_id}, {"_id": 0})
    return _serialize(updated)


@router.delete("/{announcement_id}", status_code=204)
async def delete_announcement(announcement_id: str, admin: CurrentAdmin):
    db = get_db()
    result = await db.announcements.delete_one({"id": announcement_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Announcement not found")
