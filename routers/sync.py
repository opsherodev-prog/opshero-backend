"""
Sync router — bidirectional CLI ↔ backend synchronisation (pro/team only).

Endpoints:
  POST /sync/push   — CLI pushes a batch of analyses (offline mode)
  POST /sync/pull   — CLI pulls analyses it doesn't have locally
  GET  /sync/status — per client_id dedup cursor

Design:
- client_id uniquely identifies a workstation (set by CLI on first run, stored in ~/.opshero/config)
- Push deduplicates by log_hash + client_id (idempotent)
- Pull returns analyses created on OTHER clients since a given cursor (ISO timestamp)
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from database import get_db
from deps.auth import CurrentUser, require_tier
from models.analysis import Analysis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class PushItem(BaseModel):
    log_hash: str
    log_size_chars: int
    client_id: str
    pattern_id: Optional[str] = None
    confidence: float = 0.0
    match_method: str = "no_match"
    detected_category: Optional[str] = None
    extracted_vars: dict = {}
    solutions: list = []
    total_latency_ms: int = 0
    created_at: datetime


class PushRequest(BaseModel):
    items: list[PushItem]


class PushResponse(BaseModel):
    inserted: int
    skipped: int  # duplicates


class PullRequest(BaseModel):
    since: datetime  # ISO timestamp cursor
    client_id: str   # exclude analyses from this client (already local)


class SyncStatusResponse(BaseModel):
    client_id: str
    local_count: int    # analyses pushed by this client
    remote_count: int   # analyses from other clients
    last_push_at: Optional[datetime]
    last_pull_at: Optional[datetime]


# ── POST /sync/push ───────────────────────────────────────────────────────────

@router.post(
    "/push",
    response_model=PushResponse,
    dependencies=[Depends(require_tier("pro", "team"))],
)
async def sync_push(body: PushRequest, user: CurrentUser):
    """
    Upload analyses recorded offline on this client.
    Deduplicates by (user_id, client_id, log_hash).
    Max 500 items per batch.
    """
    if len(body.items) > 500:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Batch size exceeds limit of 500",
        )

    db = get_db()
    inserted = 0
    skipped = 0

    for item in body.items:
        # Dedup check
        existing = await db.analyses.find_one(
            {
                "user_id": user.id,
                "client_id": item.client_id,
                "log_hash": item.log_hash,
            },
            {"id": 1},
        )
        if existing:
            skipped += 1
            continue

        analysis = Analysis(
            user_id=user.id,
            client_id=item.client_id,
            log_hash=item.log_hash,
            log_size_chars=item.log_size_chars,
            pattern_id=item.pattern_id,
            confidence=item.confidence,
            match_method=item.match_method,
            detected_category=item.detected_category,
            extracted_vars=item.extracted_vars,
            solutions=item.solutions,
            total_latency_ms=item.total_latency_ms,
            created_at=item.created_at,
            synced_from_client=True,
        )
        analysis.set_log_ttl()
        await db.analyses.insert_one(analysis.model_dump())
        inserted += 1

    # Record last push timestamp per client
    await db.sync_cursors.update_one(
        {"user_id": user.id, "client_id": body.items[0].client_id if body.items else ""},
        {"$set": {"last_push_at": datetime.utcnow()}},
        upsert=True,
    )

    logger.info(
        f"Sync push user={user.id}: inserted={inserted}, skipped={skipped}"
    )
    return PushResponse(inserted=inserted, skipped=skipped)


# ── POST /sync/pull ───────────────────────────────────────────────────────────

@router.post(
    "/pull",
    dependencies=[Depends(require_tier("pro", "team"))],
)
async def sync_pull(body: PullRequest, user: CurrentUser):
    """
    Return analyses created on OTHER clients since the cursor timestamp.
    CLI uses this to populate its local DB with analyses from teammates (team tier)
    or other machines (pro tier).
    """
    db = get_db()

    query = {
        "user_id": user.id,
        "created_at": {"$gt": body.since},
        "client_id": {"$ne": body.client_id},  # exclude this client's own data
    }

    cursor = (
        db.analyses.find(query, {"_id": 0, "log_hash": 1})
        .sort("created_at", 1)
        .limit(500)
    )
    items = await cursor.to_list(length=500)

    # Record last pull timestamp
    await db.sync_cursors.update_one(
        {"user_id": user.id, "client_id": body.client_id},
        {"$set": {"last_pull_at": datetime.utcnow()}},
        upsert=True,
    )

    return {
        "count": len(items),
        "items": items,
        "has_more": len(items) == 500,
    }


# ── GET /sync/status ──────────────────────────────────────────────────────────

@router.get(
    "/status",
    response_model=SyncStatusResponse,
    dependencies=[Depends(require_tier("pro", "team"))],
)
async def sync_status(
    user: CurrentUser,
    client_id: str = Query(..., description="Client identifier from ~/.opshero/config"),
):
    """Return sync status for a specific client."""
    db = get_db()

    local_count = await db.analyses.count_documents(
        {"user_id": user.id, "client_id": client_id}
    )
    remote_count = await db.analyses.count_documents(
        {"user_id": user.id, "client_id": {"$ne": client_id}}
    )

    cursor_doc = await db.sync_cursors.find_one(
        {"user_id": user.id, "client_id": client_id}, {"_id": 0}
    )

    return SyncStatusResponse(
        client_id=client_id,
        local_count=local_count,
        remote_count=remote_count,
        last_push_at=cursor_doc.get("last_push_at") if cursor_doc else None,
        last_pull_at=cursor_doc.get("last_pull_at") if cursor_doc else None,
    )
