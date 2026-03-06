"""
Admin feedback hub management.
GET    /admin/feedback-hub           — List all submissions (filterable)
GET    /admin/feedback-hub/{id}      — Get single submission
PATCH  /admin/feedback-hub/{id}      — Update status / add admin reply
DELETE /admin/feedback-hub/{id}      — Delete submission
GET    /admin/feedback-hub/stats     — Aggregate counts by type/status
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_db
from deps.admin_auth import CurrentAdmin

router = APIRouter(prefix="/admin/feedback-hub", tags=["admin-feedback-hub"])

VALID_STATUSES = {"open", "in_review", "planned", "done", "declined"}


def _serialize(doc: dict) -> dict:
    for field in ("created_at", "updated_at", "admin_replied_at"):
        if doc.get(field) and hasattr(doc[field], "isoformat"):
            doc[field] = doc[field].isoformat()
    doc.pop("_id", None)
    return doc


class FeedbackUpdate(BaseModel):
    status: Optional[str] = None
    admin_reply: Optional[str] = None


@router.get("/stats")
async def get_feedback_stats(admin: CurrentAdmin):
    """Aggregate counts by status and type."""
    db = get_db()
    pipeline = [
        {"$group": {
            "_id": {"status": "$status", "type": "$type"},
            "count": {"$sum": 1},
        }},
    ]
    docs = await db.feedback_hub.aggregate(pipeline).to_list(None)

    by_status: dict = {}
    by_type: dict = {}
    for d in docs:
        s = d["_id"]["status"]
        t = d["_id"]["type"]
        by_status[s] = by_status.get(s, 0) + d["count"]
        by_type[t] = by_type.get(t, 0) + d["count"]

    total = await db.feedback_hub.count_documents({})
    open_count = await db.feedback_hub.count_documents({"status": "open"})
    return {"total": total, "open": open_count, "by_status": by_status, "by_type": by_type}


@router.get("")
async def list_feedback(
    admin: CurrentAdmin,
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """List all user feedback submissions."""
    db = get_db()
    query: dict = {}

    if status:
        query["status"] = status
    if type:
        query["type"] = type
    if search:
        query["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
            {"author_github": {"$regex": search, "$options": "i"}},
        ]

    skip = (page - 1) * per_page
    docs = (
        await db.feedback_hub
        .find(query, {"_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
        .to_list(per_page)
    )
    total = await db.feedback_hub.count_documents(query)

    return {
        "items": [_serialize(d) for d in docs],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/{feedback_id}")
async def get_feedback(feedback_id: str, admin: CurrentAdmin):
    db = get_db()
    doc = await db.feedback_hub.find_one({"id": feedback_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Feedback not found")
    return _serialize(doc)


@router.patch("/{feedback_id}")
async def update_feedback(feedback_id: str, body: FeedbackUpdate, admin: CurrentAdmin):
    """Update status and/or add an admin reply."""
    db = get_db()

    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(sorted(VALID_STATUSES))}")

    updates: dict = {"updated_at": datetime.utcnow()}
    if body.status is not None:
        updates["status"] = body.status
    if body.admin_reply is not None:
        updates["admin_reply"] = body.admin_reply.strip() or None
        updates["admin_replied_by"] = admin.email
        updates["admin_replied_at"] = datetime.utcnow()

    result = await db.feedback_hub.update_one({"id": feedback_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(404, "Feedback not found")

    return {"message": "Updated"}


@router.delete("/{feedback_id}", status_code=204)
async def delete_feedback(feedback_id: str, admin: CurrentAdmin):
    db = get_db()
    result = await db.feedback_hub.delete_one({"id": feedback_id})
    if result.deleted_count == 0:
        raise HTTPException(404, "Feedback not found")
