"""
Admin patterns CRUD + stats.
All routes require require_admin; delete requires require_super_admin.
"""

import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from database import get_db, get_redis
from deps.admin_auth import CurrentAdmin, SuperAdmin, require_permission

router = APIRouter(prefix="/admin/patterns", tags=["admin-patterns"])


# ── Request models ─────────────────────────────────────────────────────────

class PatternUpdateRequest(BaseModel):
    pattern_data: dict
    bump: str = "patch"   # "patch" | "minor" | "major"


class PatternTestRequest(BaseModel):
    pattern_data: dict
    log_text: str


# ── Helpers ────────────────────────────────────────────────────────────────

def _increment_semver(version: str, bump: str) -> str:
    """Increment a semver string: patch/minor/major."""
    try:
        major, minor, patch = (int(x) for x in version.split(".")[:3])
    except Exception:
        return version + ".1"
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


async def _invalidate_pattern(pattern_id: str) -> None:
    """Publish pattern invalidation event to Redis pub/sub for hot reload."""
    try:
        redis = get_redis()
        await redis.publish("pattern:invalidate", pattern_id)
    except Exception:
        pass


# ── Routes ─────────────────────────────────────────────────────────────────

@router.get("")  # Changed from "/" to "" to avoid trailing slash issues
async def list_patterns(
    admin: CurrentAdmin,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    source: Optional[str] = None,
    min_success_rate: Optional[float] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """List all patterns with filters and stats."""
    db = get_db()
    query: dict = {}
    if category:
        query["category"] = category
    if severity:
        query["severity"] = severity
    if source:
        query["metadata.source"] = source
    if min_success_rate is not None:
        query["metadata.stats.success_rate"] = {"$gte": min_success_rate}

    skip = (page - 1) * page_size
    cursor = db.patterns.find(query, {"_id": 0}).skip(skip).limit(page_size)
    patterns = await cursor.to_list(page_size)
    total = await db.patterns.count_documents(query)

    return {"patterns": patterns, "total": total, "page": page, "page_size": page_size}


@router.post("")
async def create_pattern(body: dict, admin: CurrentAdmin):
    """Create a new pattern."""
    db = get_db()
    pattern_id = body.get("pattern_id")
    if not pattern_id:
        raise HTTPException(400, "pattern_id is required")

    existing = await db.patterns.find_one({"pattern_id": pattern_id})
    if existing:
        raise HTTPException(409, f"Pattern {pattern_id!r} already exists")

    body.setdefault("version", "1.0.0")
    body.setdefault("status", "active")
    body.setdefault("metadata", {})
    body["metadata"]["created_at"] = datetime.utcnow().isoformat()
    body["metadata"]["author"] = admin.email
    body["metadata"]["source"] = body["metadata"].get("source", "core")

    await db.patterns.insert_one({**body, "_id": body["pattern_id"]})
    await _invalidate_pattern(pattern_id)

    return {**body}


@router.get("/test")
async def test_pattern_get(admin: CurrentAdmin):
    """Info endpoint for pattern testing."""
    return {"detail": "Use POST /admin/patterns/test to test a pattern"}


@router.post("/test")
async def test_pattern(body: PatternTestRequest, admin: CurrentAdmin):
    """
    Test a pattern JSON against a log snippet.
    Returns match result, confidence, and extracted variables.
    """
    pattern = body.pattern_data
    log = body.log_text

    try:
        keywords_required = pattern.get("detection", {}).get("keywords_required", [])
        keywords_optional = pattern.get("detection", {}).get("keywords_optional", [])
        regex_str = pattern.get("detection", {}).get("regex", "")
        exclude_if = pattern.get("detection", {}).get("exclude_if", [])

        # Exclusion check
        for excl in exclude_if:
            if excl.lower() in log.lower():
                return {"matched": False, "confidence": 0.0, "extracted_vars": {}}

        # Required keywords check
        for kw in keywords_required:
            if kw.lower() not in log.lower():
                return {"matched": False, "confidence": 0.0, "extracted_vars": {}}

        # Optional keywords (boost confidence)
        optional_hits = sum(1 for kw in keywords_optional if kw.lower() in log.lower())
        base_confidence = pattern.get("detection", {}).get("min_confidence", 0.65)

        # Regex match
        extracted_vars: dict[str, str] = {}
        regex_matched = False
        if regex_str:
            try:
                m = re.search(regex_str, log, re.IGNORECASE | re.MULTILINE)
                if m:
                    regex_matched = True
                    extracted_vars = {k: v for k, v in (m.groupdict() or {}).items() if v}
            except re.error:
                pass

        if not regex_matched and regex_str:
            return {"matched": False, "confidence": 0.0, "extracted_vars": {}}

        # Compute confidence
        confidence = base_confidence
        if keywords_optional:
            confidence += (optional_hits / len(keywords_optional)) * (1 - base_confidence) * 0.5
        confidence = min(0.99, confidence)

        return {
            "matched": True,
            "confidence": round(confidence, 3),
            "extracted_vars": extracted_vars,
        }

    except Exception as exc:
        raise HTTPException(400, f"Pattern test error: {exc}") from exc


@router.get("/{pattern_id}")
async def get_pattern(pattern_id: str, admin: CurrentAdmin):
    db = get_db()
    doc = await db.patterns.find_one({"pattern_id": pattern_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, f"Pattern {pattern_id!r} not found")
    return doc


@router.put("/{pattern_id}")
async def update_pattern(pattern_id: str, body: PatternUpdateRequest, admin: CurrentAdmin, request: Request):
    """Update a pattern; saves version history before writing."""
    db = get_db()

    existing = await db.patterns.find_one({"pattern_id": pattern_id})
    if not existing:
        raise HTTPException(404, f"Pattern {pattern_id!r} not found")

    # Archive current version
    existing.pop("_id", None)
    await db.pattern_history.insert_one({
        "pattern_id": pattern_id,
        "version": existing.get("version", "1.0.0"),
        "snapshot": existing,
        "archived_at": datetime.utcnow(),
        "archived_by": admin.email,
    })

    new_version = _increment_semver(existing.get("version", "1.0.0"), body.bump)
    update_data = {
        **body.pattern_data,
        "version": new_version,
    }
    update_data.setdefault("metadata", {})
    update_data["metadata"]["updated_at"] = datetime.utcnow().isoformat()
    update_data["metadata"]["author"] = admin.email

    await db.patterns.update_one(
        {"pattern_id": pattern_id},
        {"$set": update_data},
    )
    await _invalidate_pattern(pattern_id)

    return {"message": "Pattern updated", "version": new_version}


@router.delete("/{pattern_id}", dependencies=[])
async def disable_pattern(pattern_id: str, admin: SuperAdmin):
    """Soft-delete (status=disabled). Only super_admin can call this."""
    db = get_db()
    result = await db.patterns.update_one(
        {"pattern_id": pattern_id},
        {"$set": {"status": "disabled", "disabled_at": datetime.utcnow().isoformat()}},
    )
    if result.matched_count == 0:
        raise HTTPException(404, f"Pattern {pattern_id!r} not found")
    await _invalidate_pattern(pattern_id)
    return {"message": "Pattern disabled"}


@router.post("/{pattern_id}/rollback/{version}")
async def rollback_pattern(pattern_id: str, version: str, admin: CurrentAdmin):
    """Rollback a pattern to a specific version."""
    db = get_db()
    snapshot_doc = await db.pattern_history.find_one(
        {"pattern_id": pattern_id, "version": version}
    )
    if not snapshot_doc:
        raise HTTPException(404, f"Version {version!r} not found for {pattern_id!r}")

    snapshot = snapshot_doc["snapshot"]
    snapshot["version"] = f"{version}-rollback"
    snapshot.pop("_id", None)

    await db.patterns.replace_one({"pattern_id": pattern_id}, {**snapshot, "_id": pattern_id})
    await _invalidate_pattern(pattern_id)

    return {"message": f"Rolled back to {version}"}


@router.get("/{pattern_id}/stats")
async def get_pattern_stats(pattern_id: str, admin: CurrentAdmin, days: int = 30):
    """Daily match and feedback stats for a single pattern."""
    db = get_db()
    since = datetime.utcnow() - timedelta(days=days)

    pipeline = [
        {"$match": {"pattern_id": pattern_id, "created_at": {"$gte": since}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "matches": {"$sum": 1},
            "helpful": {"$sum": {"$cond": [{"$eq": ["$rating", "helpful"]}, 1, 0]}},
            "not_helpful": {"$sum": {"$cond": [{"$eq": ["$rating", "not_helpful"]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    daily_stats = await db.feedback.aggregate(pipeline).to_list(None)
    return {"pattern_id": pattern_id, "days": days, "daily_stats": daily_stats}


@router.get("/{pattern_id}/history")
async def get_pattern_history(pattern_id: str, admin: CurrentAdmin):
    """List version history for a pattern."""
    db = get_db()
    docs = await db.pattern_history.find(
        {"pattern_id": pattern_id},
        {"_id": 0, "snapshot": 0},  # exclude full snapshot from list
    ).sort("archived_at", -1).limit(20).to_list(20)
    return {"pattern_id": pattern_id, "history": docs}
