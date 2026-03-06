"""
User-facing pattern contribution endpoints.
POST /contributions          — Submit a pattern suggestion (form-based)
GET  /contributions/mine     — List the current user's submissions
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from database import get_db
from deps.auth import CurrentUser

router = APIRouter(prefix="/contributions", tags=["contributions"])

VALID_CATEGORIES = {
    "docker", "npm", "python", "github-actions", "gitlab-ci",
    "kubernetes", "terraform", "rust", "go", "java", "ci", "other",
}


class ContributionSubmit(BaseModel):
    title: str = Field(..., min_length=5, max_length=120,
                       description="Short name for the error pattern")
    category: str = Field(..., description="Error category (docker, npm, python…)")
    description: str = Field(..., min_length=20, max_length=1000,
                              description="Describe what error this pattern detects")
    example_log: str = Field(..., min_length=20, max_length=5000,
                              description="Paste a real CI/CD log that shows this error")
    suggested_fix: str = Field(..., min_length=10, max_length=2000,
                                description="What command or code change fixes this?")
    regex_hint: Optional[str] = Field(None, max_length=500,
                                       description="Regex pattern if you know one (optional)")


@router.post("", status_code=201)
async def submit_contribution(body: ContributionSubmit, user: CurrentUser):
    """
    Submit a new community pattern contribution for admin review.
    The submission goes into the `community_contributions` queue with status `pending_review`.
    """
    db = get_db()
    now = datetime.utcnow()

    # Count how many approved contributions this author already has
    previous_accepted = await db.community_contributions.count_documents({
        "author_github": user.github_login,
        "status": "approved",
    })

    doc = {
        "id": str(uuid4()),
        "type": "form_submission",

        # Author
        "author_github": user.github_login,
        "author_user_id": user.id,
        "author_previous_accepted": previous_accepted,

        # Pattern content (form fields)
        "title": body.title,
        "category": body.category,
        "description": body.description,
        "example_log": body.example_log,
        "suggested_fix": body.suggested_fix,
        "regex_hint": body.regex_hint,

        # Review workflow
        "status": "pending_review",
        "reviewed_by": None,
        "review_notes": None,
        "rejection_reason": None,
        "change_request_message": None,

        # GitHub PR fields — not applicable for form submissions
        "pr_number": None,
        "pr_url": None,
        "ci_passed": None,
        "quality_score": None,

        "created_at": now,
        "updated_at": now,
    }

    await db.community_contributions.insert_one(doc)
    return {"id": doc["id"], "message": "Your pattern suggestion has been submitted for review."}


@router.get("/mine")
async def my_contributions(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Return the authenticated user's own contributions (newest first)."""
    db = get_db()
    query = {"author_user_id": user.id}
    skip = (page - 1) * per_page

    docs = (
        await db.community_contributions
        .find(query, {"_id": 0, "example_log": 0, "author_user_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(per_page)
        .to_list(per_page)
    )
    total = await db.community_contributions.count_documents(query)

    for doc in docs:
        for field in ("created_at", "updated_at"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()

    return {"items": docs, "total": total, "page": page, "per_page": per_page}
