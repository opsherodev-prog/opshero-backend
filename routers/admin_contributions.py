"""
Community contribution review endpoints.
GET/POST /admin/contributions/*

Workflow:
  pending_review → [approve | reject | request_changes]
  approved       → promote  (seeds pattern to MongoDB + Redis hot-reload)
"""

import json
import logging
import re
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_db, get_redis
from deps.admin_auth import CurrentAdmin, SuperAdmin
from engine.pattern_validator import validate_pattern_strict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/contributions", tags=["admin-contributions"])

REDIS_HOT_RELOAD_CHANNEL = "pattern_updates"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _serialize(doc: dict) -> dict:
    for field in ("created_at", "updated_at", "promoted_at"):
        if doc.get(field) and hasattr(doc[field], "isoformat"):
            doc[field] = doc[field].isoformat()
    doc.pop("_id", None)
    return doc


def _build_pattern_from_submission(doc: dict) -> dict:
    """
    Convert a form-submission contribution into a schema v2 pattern dict.
    The admin can refine it in the patterns editor after promotion.
    """
    title = doc.get("title", "Untitled Pattern")
    category = doc.get("category", "other")
    description = doc.get("description", "")
    suggested_fix = doc.get("suggested_fix", "")
    regex_hint = doc.get("regex_hint") or ""

    # Sanitize pattern_id from title
    raw_id = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60]
    pattern_id = f"community_{raw_id}" if raw_id else f"community_{str(uuid4())[:8]}"

    return {
        "pattern_id": pattern_id,
        "version": "1.0.0",
        "name": title,
        "category": category,
        "subcategory": "community",
        "severity": "high",
        "tags": ["community", category],
        "detection": {
            "regex": regex_hint,
            "keywords_required": [],
            "keywords_optional": [],
            "exclude_if": [],
            "file_patterns": [],
            "variables": {},
        },
        "solutions": [
            {
                "rank": 1,
                "title": "Community fix",
                "explanation": suggested_fix or description,
                "command_template": "",
                "confidence": 0.8,
                "risk": "low",
                "reversible": True,
                "affects_files": False,
                "requires_confirmation": False,
            }
        ],
        "causal_chain": {"often_caused_by": [], "often_causes": []},
        "metadata": {
            "source": "community_contribution",
            "author": doc.get("author_github", "community"),
            "contribution_id": doc.get("id"),
            "stats": {"match_count": 0, "helpful_count": 0, "not_helpful_count": 0},
        },
    }


# ── Request bodies ─────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    notes: Optional[str] = None
    reason: Optional[str] = None
    message: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_contributions(
    admin: CurrentAdmin,
    status: Optional[str] = Query(None),
    contribution_type: Optional[str] = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    db = get_db()
    query: dict = {}
    if status:
        query["status"] = status
    if contribution_type:
        query["type"] = contribution_type

    skip = (page - 1) * page_size
    docs = (
        await db.community_contributions.find(query, {"_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(page_size)
        .to_list(page_size)
    )
    total = await db.community_contributions.count_documents(query)
    return {
        "items": [_serialize(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats")
async def contribution_stats(admin: CurrentAdmin):
    """Summary counts by status and type."""
    db = get_db()
    pipeline = [
        {"$group": {"_id": {"status": "$status", "type": "$type"}, "count": {"$sum": 1}}}
    ]
    rows = await db.community_contributions.aggregate(pipeline).to_list(None)
    result: dict = {"by_status": {}, "by_type": {}, "total": 0}
    for row in rows:
        s = row["_id"].get("status", "unknown")
        t = row["_id"].get("type", "unknown")
        result["by_status"][s] = result["by_status"].get(s, 0) + row["count"]
        result["by_type"][t] = result["by_type"].get(t, 0) + row["count"]
        result["total"] += row["count"]
    return result


@router.get("/{contribution_id}")
async def get_contribution(contribution_id: str, admin: CurrentAdmin):
    db = get_db()
    doc = await db.community_contributions.find_one({"id": contribution_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Contribution not found")
    return _serialize(doc)


@router.post("/{contribution_id}/approve")
async def approve_contribution(contribution_id: str, body: ReviewRequest, admin: CurrentAdmin):
    """Mark contribution as approved — ready for promotion."""
    db = get_db()
    result = await db.community_contributions.update_one(
        {"id": contribution_id},
        {"$set": {
            "status": "approved",
            "reviewed_by": admin.email,
            "review_notes": body.notes,
            "updated_at": datetime.utcnow(),
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Contribution not found")
    return {"message": "Approved — call /promote to add the pattern to the live library"}


@router.post("/{contribution_id}/reject")
async def reject_contribution(contribution_id: str, body: ReviewRequest, admin: CurrentAdmin):
    db = get_db()
    if not body.reason:
        raise HTTPException(400, "Rejection reason is required")
    result = await db.community_contributions.update_one(
        {"id": contribution_id},
        {"$set": {
            "status": "rejected",
            "reviewed_by": admin.email,
            "rejection_reason": body.reason,
            "updated_at": datetime.utcnow(),
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Contribution not found")
    return {"message": "Rejected"}


@router.post("/{contribution_id}/request-changes")
async def request_changes(contribution_id: str, body: ReviewRequest, admin: CurrentAdmin):
    db = get_db()
    if not body.message:
        raise HTTPException(400, "Change request message is required")
    result = await db.community_contributions.update_one(
        {"id": contribution_id},
        {"$set": {
            "status": "changes_requested",
            "reviewed_by": admin.email,
            "change_request_message": body.message,
            "updated_at": datetime.utcnow(),
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Contribution not found")
    return {"message": "Changes requested"}


@router.post("/{contribution_id}/promote")
async def promote_contribution(contribution_id: str, admin: SuperAdmin):
    """
    Promote an approved contribution to the live pattern library.

    - form_submission: builds a draft pattern from form fields → seeds to MongoDB → Redis hot-reload
    - github_pr: already auto-seeded by webhook; marks the record as officially reviewed

    Requires super_admin role.
    """
    db = get_db()
    doc = await db.community_contributions.find_one({"id": contribution_id})
    if not doc:
        raise HTTPException(404, "Contribution not found")

    if doc.get("status") == "promoted":
        raise HTTPException(400, "Already promoted")

    if doc.get("status") not in ("approved", "pending_review"):
        raise HTTPException(
            400,
            f"Cannot promote a contribution with status '{doc.get('status')}'. "
            "Approve it first.",
        )

    contribution_type = doc.get("type", "form_submission")
    patterns_promoted: list[str] = []

    if contribution_type == "form_submission":
        pattern = _build_pattern_from_submission(doc)

        try:
            validate_pattern_strict(pattern)
        except ValueError as e:
            logger.warning(
                "Contribution %s: generated pattern has validation issues: %s",
                contribution_id, e,
            )
            # Continue — save as draft so admin can fix in pattern editor

        # Upsert into patterns collection
        await db.patterns.update_one(
            {"pattern_id": pattern["pattern_id"]},
            {"$set": {**pattern, "status": "active"}},
            upsert=True,
        )

        # Hot-reload via Redis
        redis = get_redis()
        try:
            await redis.publish(
                REDIS_HOT_RELOAD_CHANNEL,
                json.dumps({"action": "upsert", "pattern_id": pattern["pattern_id"]}),
            )
        except Exception as exc:
            logger.warning("Redis publish failed: %s", exc)

        patterns_promoted.append(pattern["pattern_id"])
        logger.info(
            "Contribution %s promoted by %s → pattern '%s'",
            contribution_id, admin.email, pattern["pattern_id"],
        )

    elif contribution_type == "github_pr":
        # Already seeded by webhook — just record the official review
        patterns_promoted = doc.get("files_changed", [])
        logger.info(
            "GitHub PR contribution %s marked as promoted by %s",
            contribution_id, admin.email,
        )

    await db.community_contributions.update_one(
        {"id": contribution_id},
        {"$set": {
            "status": "promoted",
            "promoted_by": admin.email,
            "promoted_pattern_ids": patterns_promoted,
            "promoted_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }},
    )

    return {
        "message": "Pattern promoted to the live library and hot-reloaded",
        "patterns": patterns_promoted,
        "promoted_by": admin.email,
    }
