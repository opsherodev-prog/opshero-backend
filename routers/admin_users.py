"""
Admin user management routes.
GET/PATCH/POST/DELETE /admin/users/*

Field mapping (User model → AdminUser API response):
  id            → user_id
  github_email  → email
  is_suspended  → status ("active" | "suspended")
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from database import get_db
from deps.admin_auth import CurrentAdmin, SuperAdmin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class ChangeTierRequest(BaseModel):
    tier: str


class SuspendRequest(BaseModel):
    reason: str


class QuotaOverrideRequest(BaseModel):
    monthly_limit: Optional[int] = None   # None = remove override, use tier default


def _to_admin_user(doc: dict, total_analyses: int = 0) -> dict:
    """Transform a raw User MongoDB document to the AdminUser API shape."""
    is_susp = doc.get("is_suspended", False)
    return {
        "user_id":            doc.get("id", ""),
        "email":              doc.get("github_email") or "",
        "github_login":       doc.get("github_login", ""),
        "tier":               doc.get("tier", "free"),
        "status":             "suspended" if is_susp else "active",
        "country":            doc.get("country"),
        "created_at":         doc["created_at"].isoformat() if hasattr(doc.get("created_at"), "isoformat") else str(doc.get("created_at", "")),
        "last_active_at":     doc["last_active_at"].isoformat() if hasattr(doc.get("last_active_at"), "isoformat") else doc.get("last_active_at"),
        "total_analyses":     total_analyses,
        "stripe_customer_id": doc.get("stripe_customer_id"),
    }


@router.get("/")
async def list_users(
    admin: CurrentAdmin,
    search: Optional[str] = None,
    tier: Optional[str] = None,
    status: Optional[str] = None,   # "active" | "suspended"
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    db = get_db()
    query: dict = {}

    # Search across email (github_email), id, and github_login
    if search:
        query["$or"] = [
            {"github_email": {"$regex": search, "$options": "i"}},
            {"id":           {"$regex": search, "$options": "i"}},
            {"github_login": {"$regex": search, "$options": "i"}},
        ]

    if tier:
        query["tier"] = tier

    # Map frontend "status" → is_suspended bool
    if status == "active":
        query["is_suspended"] = False
    elif status == "suspended":
        query["is_suspended"] = True

    skip = (page - 1) * page_size
    cursor = db.users.find(query, {"_id": 0, "password_hash": 0}).skip(skip).limit(page_size)
    raw_users = await cursor.to_list(page_size)
    total = await db.users.count_documents(query)

    # Batch-fetch analysis counts for the current page
    user_ids = [u.get("id") for u in raw_users if u.get("id")]
    count_pipeline = [
        {"$match": {"user_id": {"$in": user_ids}}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
    ]
    count_docs = await db.analyses.aggregate(count_pipeline).to_list(None)
    counts = {d["_id"]: d["count"] for d in count_docs}

    users = [_to_admin_user(u, counts.get(u.get("id"), 0)) for u in raw_users]

    return {"users": users, "total": total, "page": page, "page_size": page_size}


@router.get("/{user_id}")
async def get_user(user_id: str, admin: CurrentAdmin):
    db = get_db()
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(404, "User not found")

    total_analyses = await db.analyses.count_documents({"user_id": user_id})

    top_patterns = await db.analyses.aggregate([
        {"$match": {"user_id": user_id, "result.pattern_id": {"$exists": True}}},
        {"$group": {"_id": "$result.pattern_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 5},
    ]).to_list(5)

    return {
        **_to_admin_user(user, total_analyses),
        "top_patterns": [{"pattern_id": d["_id"], "count": d["count"]} for d in top_patterns],
        # Extra raw fields useful for detail view
        "github_avatar_url":  user.get("github_avatar_url"),
        "github_name":        user.get("github_name"),
        "analyses_this_month": user.get("analyses_this_month", 0),
        "team_id":            user.get("team_id"),
        "suspended_reason":   user.get("suspended_reason"),
        "subscription_expires_at": (
            user["subscription_expires_at"].isoformat()
            if hasattr(user.get("subscription_expires_at"), "isoformat")
            else user.get("subscription_expires_at")
        ),
    }


@router.patch("/{user_id}/tier")
async def change_tier(user_id: str, body: ChangeTierRequest, admin: CurrentAdmin):
    valid_tiers = {"free", "pro", "team", "enterprise"}
    if body.tier not in valid_tiers:
        raise HTTPException(400, f"Invalid tier. Must be one of: {', '.join(valid_tiers)}")

    db = get_db()
    result = await db.users.update_one(
        {"id": user_id},
        {"$set": {"tier": body.tier, "tier_changed_at": datetime.utcnow(), "tier_changed_by": admin.email}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")

    return {"message": f"Tier changed to {body.tier}"}


@router.post("/{user_id}/suspend")
async def suspend_user(
    user_id: str, 
    body: SuspendRequest, 
    admin: CurrentAdmin,
    background_tasks: BackgroundTasks
):
    if not body.reason.strip():
        raise HTTPException(400, "Suspension reason required")

    db = get_db()
    
    # Fetch user before suspending to get email
    user_doc = await db.users.find_one({"id": user_id})
    if not user_doc:
        raise HTTPException(404, "User not found")
    
    result = await db.users.update_one(
        {"id": user_id},
        {"$set": {
            "is_suspended":    True,
            "suspended_reason": body.reason,
            "suspended_at":    datetime.utcnow(),
            "suspended_by":    admin.email,
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")

    # Send suspension notification email via background task
    user_email = user_doc.get("github_email")
    if user_email:
        logger.info(f"Scheduling suspension email for {user_doc.get('github_login')} ({user_email})")
        from services.email import send_suspension_notification
        background_tasks.add_task(
            send_suspension_notification,
            to=user_email,
            username=user_doc.get("github_login", "User"),
            reason=body.reason,
        )
    else:
        logger.warning(f"User {user_id} has no email - skipping suspension notification")

    return {"message": "User suspended"}


@router.post("/{user_id}/activate")
async def activate_user(
    user_id: str, 
    admin: CurrentAdmin,
    background_tasks: BackgroundTasks
):
    db = get_db()
    
    # Fetch user before activating to get email
    user_doc = await db.users.find_one({"id": user_id})
    if not user_doc:
        raise HTTPException(404, "User not found")
    
    result = await db.users.update_one(
        {"id": user_id},
        {"$set": {"is_suspended": False, "suspended_reason": None, "reactivated_by": admin.email}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")

    # Send reactivation notification email via background task
    user_email = user_doc.get("github_email")
    if user_email:
        logger.info(f"Scheduling reactivation email for {user_doc.get('github_login')} ({user_email})")
        from services.email import send_reactivation_notification
        background_tasks.add_task(
            send_reactivation_notification,
            to=user_email,
            username=user_doc.get("github_login", "User"),
        )
    else:
        logger.warning(f"User {user_id} has no email - skipping reactivation notification")

    return {"message": "User activated"}


@router.patch("/{user_id}/quota")
async def set_quota_override(user_id: str, body: QuotaOverrideRequest, admin: CurrentAdmin):
    """
    Override the monthly analysis quota for a specific user.
    Set monthly_limit=None to reset to the tier default.
    """
    db = get_db()
    update_fields: dict = {
        "quota_override_by":  admin.email,
        "quota_override_at":  datetime.utcnow(),
    }
    if body.monthly_limit is not None:
        if body.monthly_limit < 0:
            raise HTTPException(400, "monthly_limit must be >= 0")
        update_fields["custom_monthly_limit"] = body.monthly_limit
    else:
        update_fields["custom_monthly_limit"] = None

    result = await db.users.update_one({"id": user_id}, {"$set": update_fields})
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")

    label = f"{body.monthly_limit} analyses/month" if body.monthly_limit is not None else "tier default"
    return {"message": f"Quota set to {label}"}


@router.get("/{user_id}/export")
async def export_user_gdpr(user_id: str, admin: CurrentAdmin):
    """GDPR data export for a single user."""
    db = get_db()
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(404, "User not found")

    analyses = await db.analyses.find(
        {"user_id": user_id}, {"_id": 0}
    ).limit(1000).to_list(1000)

    return {
        "user": _to_admin_user(user),
        "analyses": analyses,
        "export_date": datetime.utcnow().isoformat(),
        "total_analyses": len(analyses),
    }


@router.delete("/{user_id}")
async def delete_user(user_id: str, admin: SuperAdmin):
    """
    Hard-delete a user account. super_admin only.
    Analyses are anonymised, not deleted, to preserve analytics.
    """
    db = get_db()
    user = await db.users.find_one({"id": user_id})
    if not user:
        raise HTTPException(404, "User not found")

    await db.analyses.update_many(
        {"user_id": user_id},
        {"$set": {"user_id": "[deleted]", "github_email": "[deleted]"}},
    )
    await db.users.delete_one({"id": user_id})

    return {"message": "User permanently deleted"}
@router.post("/{user_id}/fetch-email")
async def fetch_user_email(user_id: str, admin: CurrentAdmin):
    """
    Manually fetch and update a user's email from GitHub.
    Useful for users who signed up before email fetching was implemented.
    """
    import httpx

    db = get_db()
    user = await db.users.find_one({"id": user_id})
    if not user:
        raise HTTPException(404, "User not found")

    github_token = user.get("github_token")
    if not github_token:
        raise HTTPException(400, "User has no GitHub token stored")

    # Fetch emails from GitHub
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )

        if emails_resp.status_code != 200:
            raise HTTPException(502, f"GitHub API error: {emails_resp.status_code}")

        emails = emails_resp.json()

        # Find primary verified email
        primary_email = next(
            (e["email"] for e in emails if e.get("primary") and e.get("verified")),
            None
        )

        if not primary_email:
            return {
                "message": "No primary verified email found on GitHub account",
                "emails_found": len(emails),
                "email_updated": False,
            }

        # Update user email
        await db.users.update_one(
            {"id": user_id},
            {"$set": {"github_email": primary_email}},
        )

        logger.info(f"Admin {admin.email} fetched email for user {user_id}: {primary_email}")

        return {
            "message": "Email fetched and updated successfully",
            "email": primary_email,
            "email_updated": True,
        }

    except httpx.HTTPError as exc:
        logger.error(f"Failed to fetch email from GitHub: {exc}")
        raise HTTPException(502, f"Failed to connect to GitHub: {str(exc)}")

