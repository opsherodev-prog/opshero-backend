"""
Auto-learning system management endpoints.

GET  /admin/learning/candidates                      — list AI-generated pattern candidates
GET  /admin/learning/candidates/{id}                 — detail view with validation errors
PATCH /admin/learning/candidates/{id}                — edit pattern_data before promoting
POST /admin/learning/candidates/{id}/approve         — mark ready for promotion
POST /admin/learning/candidates/{id}/promote         — seed to live library + hot-reload
POST /admin/learning/candidates/{id}/reject          — reject candidate
POST /admin/learning/candidates/{id}/generate-pattern — on-demand PatternGenerator call
GET  /admin/learning/candidates/{id}/validate-regex  — test regex against example logs
GET  /admin/learning/stats                           — learning system stats
GET  /admin/learning/jobs                            — job execution history
POST /admin/learning/trigger-rerank                  — manually trigger solution rerank job
POST /admin/learning/trigger-cluster                 — manually trigger LLM clustering job
POST /admin/learning/trigger-auto-promote            — manually trigger auto-promote job
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_db, get_redis
from deps.admin_auth import SuperAdmin, CurrentAdmin
from engine.pattern_validator import validate_pattern
from engine.pattern_generator import get_pattern_generator
from engine.learning_jobs import auto_promote_job, _fetch_related_examples

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/learning", tags=["admin-learning"])

REDIS_HOT_RELOAD_CHANNEL = "pattern_updates"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _serialize(doc: dict) -> dict:
    for field in ("created_at", "last_seen_at", "reviewed_at"):
        if doc.get(field) and hasattr(doc[field], "isoformat"):
            doc[field] = doc[field].isoformat()
    doc.pop("_id", None)
    return doc


# ── Request bodies ─────────────────────────────────────────────────────────────

class CandidateEditRequest(BaseModel):
    pattern_data: Optional[dict] = None
    notes: Optional[str] = None


# ── Candidate endpoints ───────────────────────────────────────────────────────

@router.get("/candidates")
async def list_candidates(
    admin: CurrentAdmin,
    status: Optional[str] = Query("pending"),
    origin: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """List AI-generated pattern candidates sorted by frequency (most seen first)."""
    db = get_db()
    query: dict = {}
    if status:
        query["status"] = status
    if origin:
        query["origin"] = origin

    skip = (page - 1) * page_size
    docs = (
        await db.pattern_candidates.find(query, {"_id": 0})
        .sort([("unmatched_count", -1), ("created_at", -1)])
        .skip(skip)
        .limit(page_size)
        .to_list(page_size)
    )
    total = await db.pattern_candidates.count_documents(query)
    return {
        "items": [_serialize(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: str, admin: CurrentAdmin):
    """Get a single candidate with live schema validation errors."""
    db = get_db()
    doc = await db.pattern_candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Candidate not found")
    doc = _serialize(doc)
    pattern_data = doc.get("pattern_data")
    if pattern_data:
        doc["validation_errors"] = validate_pattern(pattern_data)
    return doc


@router.patch("/candidates/{candidate_id}")
async def update_candidate(
    candidate_id: str,
    body: CandidateEditRequest,
    admin: CurrentAdmin,
):
    """Edit the auto-extracted pattern_data before promoting to live."""
    db = get_db()
    doc = await db.pattern_candidates.find_one({"id": candidate_id})
    if not doc:
        raise HTTPException(404, "Candidate not found")

    updates: dict = {"reviewed_by": admin.email, "last_seen_at": datetime.utcnow()}
    if body.pattern_data is not None:
        errors = validate_pattern(body.pattern_data)
        updates["pattern_data"] = body.pattern_data
        updates["validation_errors"] = errors
    if body.notes is not None:
        updates["admin_notes"] = body.notes

    await db.pattern_candidates.update_one({"id": candidate_id}, {"$set": updates})
    return {"message": "Candidate updated"}


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str, admin: CurrentAdmin):
    """Approve candidate — marks it ready for promotion."""
    db = get_db()
    candidate = await db.pattern_candidates.find_one({"id": candidate_id})
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    # Promote the pattern_data to draft in patterns collection
    if candidate.get("pattern_data"):
        await db.patterns.update_one(
            {"pattern_id": candidate["pattern_data"].get("pattern_id")},
            {"$setOnInsert": {**candidate["pattern_data"], "status": "draft"}},
            upsert=True,
        )

    await db.pattern_candidates.update_one(
        {"id": candidate_id},
        {"$set": {
            "status": "approved",
            "reviewed_by": admin.email,
            "reviewed_at": datetime.utcnow(),
        }},
    )
    return {"message": "Candidate approved — call /promote to push it live"}


@router.post("/candidates/{candidate_id}/promote")
async def promote_candidate(candidate_id: str, admin: SuperAdmin):
    """
    Promote an approved candidate to the live regex library.

    1. Validates pattern_data against schema v2.0.0 (strict)
    2. Upserts into `patterns` collection with status=active
    3. Publishes to Redis → PatternIndex hot-reload (zero downtime)

    Requires super_admin role.
    """
    db = get_db()
    candidate = await db.pattern_candidates.find_one({"id": candidate_id})
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    if candidate.get("status") == "promoted":
        raise HTTPException(400, "Already promoted")

    if candidate.get("status") not in ("approved", "pending"):
        raise HTTPException(
            400,
            f"Cannot promote candidate with status '{candidate.get('status')}'. "
            "Approve it first.",
        )

    pattern_data = candidate.get("pattern_data")
    if not pattern_data:
        raise HTTPException(422, "Candidate has no pattern_data to promote")

    errors = validate_pattern(pattern_data)
    if errors:
        raise HTTPException(
            422,
            {"detail": "Pattern validation failed — fix errors before promoting", "errors": errors},
        )

    pattern_id = pattern_data["pattern_id"]

    # Upsert into patterns collection as active
    await db.patterns.update_one(
        {"pattern_id": pattern_id},
        {"$set": {
            **pattern_data,
            "status": "active",
            "source": "ai_promoted",
            "promoted_by": admin.email,
            "promoted_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    # Hot-reload via Redis pubsub
    redis = get_redis()
    try:
        await redis.publish(
            REDIS_HOT_RELOAD_CHANNEL,
            json.dumps({"action": "upsert", "pattern_id": pattern_id}),
        )
    except Exception as exc:
        logger.warning("Redis publish failed for %s: %s", pattern_id, exc)

    await db.pattern_candidates.update_one(
        {"id": candidate_id},
        {"$set": {
            "status": "promoted",
            "reviewed_by": admin.email,
            "reviewed_at": datetime.utcnow(),
            "promoted_pattern_id": pattern_id,
        }},
    )

    logger.info("Candidate %s promoted → live pattern '%s' by %s", candidate_id, pattern_id, admin.email)

    return {
        "message": "Pattern promoted to live library and hot-reloaded",
        "pattern_id": pattern_id,
        "promoted_by": admin.email,
    }


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(candidate_id: str, admin: SuperAdmin):
    db = get_db()
    result = await db.pattern_candidates.update_one(
        {"id": candidate_id},
        {"$set": {
            "status": "rejected",
            "reviewed_by": admin.email,
            "reviewed_at": datetime.utcnow(),
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(404, "Candidate not found")
    return {"message": "Candidate rejected"}


# ── Stats & background jobs ───────────────────────────────────────────────────

@router.get("/stats")
async def learning_stats(admin: CurrentAdmin):
    """Summary of the AI learning pipeline."""
    db = get_db()
    pipeline = [
        {"$group": {
            "_id": "$status",
            "count": {"$sum": 1},
            "avg_seen": {"$avg": "$unmatched_count"},
        }}
    ]
    rows = await db.pattern_candidates.aggregate(pipeline).to_list(None)
    by_status = {
        r["_id"]: {"count": r["count"], "avg_seen": round(r.get("avg_seen") or 0, 1)}
        for r in rows
    }

    # Top pending candidates by frequency (most impactful to promote)
    top_pending = (
        await db.pattern_candidates.find(
            {"status": "pending"},
            {"_id": 0, "id": 1, "llm_pattern_id": 1, "unmatched_count": 1, "llm_confidence": 1, "llm_category": 1},
        )
        .sort("unmatched_count", -1)
        .limit(10)
        .to_list(10)
    )

    return {
        "by_status": by_status,
        "top_pending_by_frequency": top_pending,
        "total": sum(r["count"] for r in rows),
    }


@router.post("/trigger-rerank")
async def trigger_rerank(admin: SuperAdmin):
    """
    Manually trigger the weekly pattern-solution rerank job.
    Re-scores solution confidence from feedback data.
    """
    db = get_db()
    await db.learning_jobs.insert_one({
        "type": "rerank",
        "triggered_by": admin.email,
        "triggered_at": datetime.utcnow(),
        "status": "queued",
    })
    return {"message": "Rerank job queued", "triggered_by": admin.email}


@router.post("/trigger-cluster")
async def trigger_cluster(admin: SuperAdmin):
    """
    Manually trigger the monthly LLM clustering job.
    Groups similar pattern_candidates to find new pattern families.
    """
    db = get_db()
    await db.learning_jobs.insert_one({
        "type": "cluster",
        "triggered_by": admin.email,
        "triggered_at": datetime.utcnow(),
        "status": "queued",
    })
    return {"message": "LLM clustering job queued", "triggered_by": admin.email}


# ── On-demand generation & validation ────────────────────────────────────────

@router.post("/candidates/{candidate_id}/generate-pattern")
async def generate_pattern_for_candidate(candidate_id: str, admin: SuperAdmin):
    """
    Immediately run PatternGenerator on this candidate — no need to wait for
    the background auto-promote loop.

    Requires the GROQ_API_KEY (or equivalent) to be configured and
    `learning_enabled = true`.  Returns the generated pattern_data and any
    validation errors so the admin can review / tweak before promoting.
    """
    generator = get_pattern_generator()
    if generator is None:
        raise HTTPException(
            503,
            "Pattern generator not available — check GROQ_API_KEY and learning_enabled settings",
        )

    db = get_db()
    candidate = await db.pattern_candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    if candidate.get("status") in ("promoted", "auto_promoted"):
        raise HTTPException(400, "Candidate has already been promoted")

    try:
        extra_examples = await _fetch_related_examples(db, candidate)
    except Exception as e:
        logger.warning(f"Failed to fetch related examples for candidate {candidate_id}: {e}")
        extra_examples = []

    logger.info(
        "Admin %s triggered on-demand pattern generation for candidate %s",
        admin.email, candidate_id,
    )

    try:
        result = await generator.generate_and_validate(
            candidate=candidate,
            extra_examples=extra_examples,
            retry_on_regex_mismatch=True,
        )
    except Exception as e:
        logger.error(f"Pattern generation failed for candidate {candidate_id}: {e}")
        return {
            "success": False,
            "error": f"Pattern generation failed: {str(e)}",
            "model": None,
            "latency_ms": 0,
        }

    now = datetime.utcnow()

    if not result:
        return {
            "success": False,
            "error": "Pattern generation failed - no result returned",
            "model": None,
            "latency_ms": 0,
        }
    
    if not getattr(result, 'success', False):
        error_msg = getattr(result, 'error', 'Pattern generation failed')
        return {
            "success": False,
            "error": error_msg,
            "model": getattr(result, 'model', None),
            "latency_ms": getattr(result, 'latency_ms', 0),
        }

    # Persist the generated pattern_data back to the candidate document
    pattern_data = getattr(result, 'pattern_data', {}) or {}
    validation_errors = getattr(result, 'validation_errors', []) or []
    
    updates: dict = {
        "pattern_data": pattern_data,
        "validation_errors": validation_errors,
        "generation_model": getattr(result, 'model', None),
        "generation_latency_ms": getattr(result, 'latency_ms', 0),
        "last_generation_at": now,
        "last_generation_triggered_by": admin.email,
    }
    
    if validation_errors:
        updates["status"] = "ready_for_review"
    else:
        updates["status"] = "ready_for_review"  # admin still confirms before /promote

    try:
        await db.pattern_candidates.update_one({"id": candidate_id}, {"$set": updates})
    except Exception as e:
        logger.error(f"Failed to update candidate {candidate_id}: {e}")
        return {
            "success": False,
            "error": f"Failed to save pattern data: {str(e)}",
            "model": getattr(result, 'model', None),
            "latency_ms": getattr(result, 'latency_ms', 0),
        }

    return {
        "success": True,
        "pattern_data": pattern_data,
        "validation_errors": validation_errors,
        "is_valid": getattr(result, 'is_valid', False),
        "model": getattr(result, 'model', None),
        "input_tokens": getattr(result, 'input_tokens', 0),
        "output_tokens": getattr(result, 'output_tokens', 0),
        "latency_ms": getattr(result, 'latency_ms', 0),
    }


@router.get("/candidates/{candidate_id}/validate-regex")
async def validate_regex_for_candidate(candidate_id: str, admin: CurrentAdmin):
    """
    Test the candidate's current `pattern_data.detection.regex` against all
    stored example log snippets (primary + related).

    Returns per-example match results so the admin can see exactly which log
    lines are (or are not) captured before promoting.
    """
    db = get_db()
    candidate = await db.pattern_candidates.find_one({"id": candidate_id}, {"_id": 0})
    if not candidate:
        raise HTTPException(404, "Candidate not found")

    pattern_data = candidate.get("pattern_data") or {}
    detection = pattern_data.get("detection") or {}
    regex_str = detection.get("regex", "")
    if not regex_str:
        raise HTTPException(422, "Candidate has no regex in pattern_data.detection.regex")

    # Compile the regex
    try:
        compiled = re.compile(regex_str, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return {
            "valid_regex": False,
            "compile_error": str(exc),
            "results": [],
        }

    # Collect examples: primary + related
    examples: list[dict] = []
    primary = candidate.get("example_log_snippet") or candidate.get("example_log", "")
    if primary:
        examples.append({"source": "primary", "snippet": primary})

    related = (
        await db.pattern_candidates.find(
            {"llm_pattern_id": candidate.get("llm_pattern_id"), "id": {"$ne": candidate_id}},
            {"_id": 0, "id": 1, "example_log_snippet": 1},
        )
        .sort("unmatched_count", -1)
        .limit(4)
        .to_list(4)
    )
    for r in related:
        snippet = r.get("example_log_snippet", "")
        if snippet:
            examples.append({"source": f"related:{r['id']}", "snippet": snippet})

    results = []
    all_matched = True
    for ex in examples:
        matched = bool(compiled.search(ex["snippet"]))
        if not matched:
            all_matched = False
        results.append({
            "source": ex["source"],
            "matched": matched,
            "snippet_preview": ex["snippet"][:200],
        })

    return {
        "valid_regex": True,
        "regex": regex_str,
        "all_matched": all_matched,
        "examples_tested": len(results),
        "results": results,
    }


# ── Job history ───────────────────────────────────────────────────────────────

@router.get("/jobs")
async def list_learning_jobs(
    admin: CurrentAdmin,
    job_type: Optional[str] = Query(None, alias="type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """
    Browse the auto-learning job audit log.

    Includes scheduled runs, per-candidate promote/escalate events, and
    any manually-triggered rerank / cluster / auto-promote jobs.
    """
    db = get_db()
    query: dict = {}
    if job_type:
        query["type"] = job_type

    skip = (page - 1) * page_size
    docs = (
        await db.learning_jobs.find(query, {"_id": 0})
        .sort("updated_at", -1)
        .skip(skip)
        .limit(page_size)
        .to_list(page_size)
    )
    total = await db.learning_jobs.count_documents(query)

    # Serialize datetimes
    for doc in docs:
        for field in ("created_at", "updated_at", "started_at", "completed_at", "triggered_at"):
            if doc.get(field) and hasattr(doc[field], "isoformat"):
                doc[field] = doc[field].isoformat()

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Manual auto-promote trigger ────────────────────────────────────────────────

@router.post("/trigger-auto-promote")
async def trigger_auto_promote(admin: SuperAdmin):
    """
    Immediately run the auto-promote job — finds all pending candidates that
    meet the sightings + confidence threshold and attempts to generate patterns
    for them right now, without waiting for the next scheduled loop tick.

    Runs asynchronously in a background task so the HTTP response is
    returned immediately.  Watch /jobs for the outcome.
    """
    from config import settings

    generator = get_pattern_generator()
    if generator is None:
        raise HTTPException(
            503,
            "Pattern generator not available — check GROQ_API_KEY and learning_enabled settings",
        )

    db = get_db()
    redis = get_redis()

    # Record intent
    job_id_doc = await db.learning_jobs.insert_one({
        "type": "manual_auto_promote",
        "triggered_by": admin.email,
        "triggered_at": datetime.utcnow(),
        "status": "running",
        "updated_at": datetime.utcnow(),
    })
    job_id = str(job_id_doc.inserted_id)

    async def _run():
        try:
            summary = await auto_promote_job(
                db=db,
                redis=redis,
                generator=generator,
                min_sightings=settings.learning_auto_promote_min_sightings,
                min_confidence=settings.learning_auto_promote_min_confidence,
                batch_size=settings.learning_auto_promote_batch_size,
            )
            await db.learning_jobs.update_one(
                {"_id": job_id_doc.inserted_id},
                {"$set": {
                    "status": "completed",
                    "summary": summary,
                    "completed_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }},
            )
            logger.info("Manual auto-promote job %s complete: %s", job_id, summary)
        except Exception as exc:
            logger.error("Manual auto-promote job %s failed: %s", job_id, exc)
            await db.learning_jobs.update_one(
                {"_id": job_id_doc.inserted_id},
                {"$set": {
                    "status": "failed",
                    "error": str(exc),
                    "updated_at": datetime.utcnow(),
                }},
            )

    asyncio.create_task(_run())

    return {
        "message": "Auto-promote job started",
        "job_id": job_id,
        "triggered_by": admin.email,
    }
