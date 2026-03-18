"""
Patterns router — public read-only access to the pattern library.
Used by the CLI for offline pattern caching and the dashboard pattern browser.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from database import get_db
from deps.auth import CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/patterns", tags=["patterns"])


# ── GET /patterns ─────────────────────────────────────────────────────────────

@router.get("/")
async def list_patterns(
    category: Optional[str] = Query(None, description="Filter by category"),
    subcategory: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Full-text search on name/description"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """
    Return a paginated list of patterns.
    Patterns are returned without raw regex to avoid leaking detection logic to clients.
    CLI uses this to build its local offline cache.
    """
    db = get_db()

    query: dict = {}
    if category:
        query["category"] = category
    if subcategory:
        query["subcategory"] = subcategory
    if severity:
        query["severity"] = severity
    if tag:
        query["tags"] = tag
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"pattern_id": {"$regex": search, "$options": "i"}},
        ]

    skip = (page - 1) * per_page
    total = await db.patterns.count_documents(query)

    # Exclude regex from public response (anti-bypass)
    projection = {
        "_id": 0,
        "detection.regex": 0,
        "detection.exclude_if": 0,
    }
    cursor = (
        db.patterns.find(query, projection)
        .skip(skip)
        .limit(per_page)
    )
    items = await cursor.to_list(length=per_page)

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": items,
    }


# ── GET /patterns/{pattern_id} ────────────────────────────────────────────────

@router.get("/{pattern_id}")
async def get_pattern(pattern_id: str):
    """Return a single pattern by ID (regex excluded from response)."""
    db = get_db()

    doc = await db.patterns.find_one(
        {"pattern_id": pattern_id},
        {
            "_id": 0,
            "detection.regex": 0,
            "detection.exclude_if": 0,
        },
    )
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pattern not found")
    return doc


# ── GET /patterns/meta/categories ────────────────────────────────────────────

@router.get("/meta/categories")
async def list_categories():
    """Return available categories and their counts."""
    db = get_db()
    pipeline = [
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    rows = await db.patterns.aggregate(pipeline).to_list(length=20)
    return [{"category": r["_id"], "count": r["count"]} for r in rows if r["_id"]]


# ── GET /patterns/meta/sync-manifest ─────────────────────────────────────────

@router.get("/meta/sync-manifest")
async def get_sync_manifest():
    """
    Return lightweight pattern manifest for CLI offline sync.
    CLI compares local cache version against this to decide whether to update.
    Returns: [{ pattern_id, version, category }]
    """
    db = get_db()
    cursor = db.patterns.find(
        {},
        {"_id": 0, "pattern_id": 1, "version": 1, "category": 1, "updated_at": 1},
    )
    items = await cursor.to_list(length=500)
    return {"count": len(items), "patterns": items}
