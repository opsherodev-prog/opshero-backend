"""
User feedback hub — feature requests, bug reports, improvement ideas.
POST /feedback-hub          — Submit feedback
GET  /feedback-hub/mine     — List the authenticated user's submissions
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from database import get_db
from deps.auth import CurrentUser

router = APIRouter(prefix="/feedback-hub", tags=["feedback-hub"])

VALID_TYPES = {"feature_request", "bug_report", "improvement", "other"}


class FeedbackSubmit(BaseModel):
    type: str = Field(..., description="feature_request | bug_report | improvement | other")
    title: str = Field(..., min_length=5, max_length=120)
    description: str = Field(..., min_length=20, max_length=3000)
    # Optional extras
    url_or_page: Optional[str] = Field(None, max_length=300,
                                        description="Which page or endpoint is affected?")
    priority: Optional[str] = Field(None, description="low | medium | high (user's perception)")


class FeedbackMine(BaseModel):
    id: str
    type: str
    title: str
    description: str
    status: str
    priority: Optional[str]
    admin_reply: Optional[str]
    created_at: str
    updated_at: str


@router.post("", status_code=201)
async def submit_feedback(body: FeedbackSubmit, user: CurrentUser):
    """Submit a feature request, bug report or improvement suggestion."""
    if body.type not in VALID_TYPES:
        from fastapi import HTTPException
        raise HTTPException(400, f"Invalid type. Must be one of: {', '.join(sorted(VALID_TYPES))}")

    db = get_db()
    now = datetime.utcnow()

    doc = {
        "id": str(uuid4()),
        "type": body.type,
        "title": body.title,
        "description": body.description,
        "url_or_page": body.url_or_page,
        "priority": body.priority or "medium",

        # Author
        "author_user_id": user.id,
        "author_github": user.github_login,
        "author_tier": user.tier,

        # Status workflow
        "status": "open",          # open | in_review | planned | done | declined
        "admin_reply": None,
        "admin_replied_by": None,
        "admin_replied_at": None,

        # Votes (other users can upvote — future feature)
        "upvotes": 0,

        "created_at": now,
        "updated_at": now,
    }

    try:
        await db.feedback_hub.insert_one(doc)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to insert feedback: {e}")
        from fastapi import HTTPException
        raise HTTPException(500, "Failed to submit feedback. Please try again.")
    
    # Notify admins about new feedback (fire-and-forget)
    try:
        # Get admin user IDs (users with admin role)
        admin_users = await db.users.find(
            {"role": {"$in": ["admin", "super_admin"]}}, 
            {"id": 1}
        ).to_list(None)
        
        if admin_users:
            try:
                from routers.notifications import create_new_feedback_notification
                admin_ids = [admin["id"] for admin in admin_users]
                await create_new_feedback_notification(
                    admin_user_ids=admin_ids,
                    feedback_title=body.title,
                    author_github=user.github_login,
                    feedback_id=doc["id"]
                )
            except ImportError:
                # Notifications module not available, skip notification
                pass
    except Exception as e:
        # Don't fail the feedback submission if notification fails
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to notify admins about new feedback: {e}")
    
    return {"id": doc["id"], "message": "Feedback submitted — thank you!"}


@router.get("/mine")
async def my_feedback(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Return the authenticated user's feedback submissions (newest first)."""
    db = get_db()
    query = {"author_user_id": user.id}
    skip = (page - 1) * per_page

    docs = (
        await db.feedback_hub
        .find(query, {"_id": 0, "author_user_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
        .to_list(per_page)
    )
    total = await db.feedback_hub.count_documents(query)

    for doc in docs:
        for field in ("created_at", "updated_at", "admin_replied_at"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()

    return {"items": docs, "total": total, "page": page, "per_page": per_page}
