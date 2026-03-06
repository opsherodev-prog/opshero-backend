"""
Analyses router — submit logs, retrieve results, pagination.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo import DESCENDING

from database import get_db
from deps.auth import CurrentUser, require_tier
from engine.analyzer import HybridAnalyzer, AnalysisResult
from models.analysis import (
    Analysis,
    AnalyzeRequest,
    AnalyzeResponse,
    AnalysisListResponse,
)
from services.slack_notifier import send_slack_notification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analyses", tags=["analyses"])

# Injected at startup by main.py lifespan
_analyzer: Optional[HybridAnalyzer] = None


def set_analyzer(analyzer: HybridAnalyzer) -> None:
    global _analyzer
    _analyzer = analyzer


def get_analyzer() -> HybridAnalyzer:
    if _analyzer is None:
        raise RuntimeError("Analyzer not initialised")
    return _analyzer


# ── Auto-learning: save LLM-discovered patterns as candidates ──────────────────

async def _notify_slack(
    db,
    user_id: str,
    analysis: "Analysis",
    result: "AnalysisResult",
    raw_log: str,
) -> None:
    """
    Fire-and-forget Slack notification.
    Fetches the user's webhook URL and sends a Block Kit message if configured.
    """
    try:
        user_doc = await db.users.find_one(
            {"id": user_id},
            {"_id": 0, "slack_webhook_url": 1},
        )
        webhook_url = user_doc.get("slack_webhook_url", "") if user_doc else ""
        if not webhook_url:
            return

        solutions = [
            s.model_dump() if hasattr(s, "model_dump")
            else (s.__dict__ if hasattr(s, "__dict__") else s)
            for s in result.solutions
        ]

        analysis_payload = {
            "id":          analysis.id,
            "severity":    getattr(result, "severity", None) or "medium",
            "category":    result.detected_category or "unknown",
            "pattern_id":  result.pattern_id or "unknown",
            "engine":      result.match_method,
            "confidence":  result.confidence,
            "raw_log":     raw_log[:400],
            "solutions":   solutions,
        }
        await send_slack_notification(webhook_url, analysis_payload)
    except Exception as exc:
        logger.debug("Slack notification error (non-critical): %s", exc)


async def _save_llm_candidate(
    db,
    result: AnalysisResult,
    raw_log: str,
    analysis_id: Optional[str] = None,
) -> None:
    """
    When the LLM identifies a pattern not in the regex library, extract a
    pattern candidate using PatternExtractor and upsert into pattern_candidates.

    Uses update_one with upsert=True keyed on llm_pattern_id — so if the same
    unknown error is seen multiple times, we increment unmatched_count rather
    than creating duplicates. This surfaces the most impactful candidates first.
    """
    try:
        from engine.pattern_extractor import extract_candidate
        from engine.groq_client import LLMResult as _LLMResult  # type: ignore

        # Reconstruct a minimal LLMResult-like object from the analysis result
        # (the actual LLMResult object was not persisted — we rebuild from stored fields)
        class _FakeLLMResult:
            pattern_id = result.pattern_id or "unknown_error"
            confidence = result.confidence
            error_type = result.pattern_id or "unknown"
            error_category = result.detected_category or "other"
            variables = result.extracted_vars or {}
            solutions = []  # solutions already stored separately
            model = result.llm_model or "unknown"
            input_tokens = result.llm_input_tokens or 0
            output_tokens = result.llm_output_tokens or 0
            latency_ms = result.llm_latency_ms or 0
            causal_hint = None

        fake_llm = _FakeLLMResult()

        candidate = extract_candidate(
            raw_log=raw_log,
            llm_result=fake_llm,  # type: ignore[arg-type]
            detected_category=result.detected_category,
            analysis_id=analysis_id,
        )

        if candidate is None:
            # Fallback to minimal upsert if extractor returned nothing
            candidate = {
                "id": str(uuid4()),
                "status": "pending",
                "origin": "llm_fallback",
                "llm_pattern_id": result.pattern_id,
                "llm_confidence": result.confidence,
                "llm_model": result.llm_model,
                "llm_category": result.detected_category or "other",
                "example_log_snippet": raw_log[:500],
                "pattern_data": None,
                "source_analysis_id": analysis_id,
                "unmatched_count": 1,
                "created_at": datetime.utcnow(),
                "last_seen_at": datetime.utcnow(),
            }

        await db.pattern_candidates.update_one(
            {"llm_pattern_id": result.pattern_id},
            {
                "$setOnInsert": {
                    k: v for k, v in candidate.items()
                    if k not in ("last_seen_at", "unmatched_count")
                },
                "$set": {"last_seen_at": datetime.utcnow()},
                "$inc": {"unmatched_count": 1},
            },
            upsert=True,
        )

    except Exception as e:
        logger.warning(f"Failed to save LLM candidate {result.pattern_id}: {e}")


# ── POST /analyses ─────────────────────────────────────────────────────────────

@router.post("", response_model=AnalyzeResponse, status_code=status.HTTP_201_CREATED)
async def analyze_log(
    body: AnalyzeRequest,
    user: CurrentUser,
):
    """
    Submit a CI/CD log for hybrid analysis.

    - Free tier: regex only, no LLM fallback
    - Pro/Team tier: LLM fallback enabled when confidence < threshold
    """
    analyzer = get_analyzer()
    db = get_db()

    # Idempotency: if client already sent this client_id, return the stored result
    if body.client_id:
        existing = await db.analyses.find_one(
            {"client_id": body.client_id, "user_id": user.id}, {"_id": 0}
        )
        if existing:
            stored = Analysis(**existing)
            return AnalyzeResponse(
                id=stored.id,
                pattern_id=stored.pattern_id,
                confidence=stored.confidence,
                match_method=stored.match_method,
                detected_category=stored.detected_category,
                extracted_vars=stored.extracted_vars,
                solutions=stored.solutions,
                causal_chain=stored.causal_chain,
                llm_model=stored.llm_model,
                llm_latency_ms=stored.llm_latency_ms,
                total_latency_ms=stored.total_latency_ms,
                error=stored.engine_error,
            )

    # Tier-aware analysis
    result = await analyzer.analyze(
        raw_log=body.log,
        user_id=user.id,
        user_tier=user.tier,
        client_id=body.client_id or "",
        context=body.context,
    )

    # Persist to MongoDB
    analysis = Analysis(
        user_id=user.id,
        client_id=body.client_id or None,  # None → excluded from sparse unique index
        log_hash=result.log_hash,
        log_size_chars=result.log_size_chars,
        pattern_id=result.pattern_id,
        confidence=result.confidence,
        match_method=result.match_method,
        detected_category=result.detected_category,
        extracted_vars=result.extracted_vars,
        solutions=[s.__dict__ if hasattr(s, "__dict__") else s for s in result.solutions],
        causal_chain=result.causal_chain.__dict__ if result.causal_chain else None,
        llm_model=result.llm_model,
        llm_latency_ms=result.llm_latency_ms,
        llm_input_tokens=result.llm_input_tokens,
        llm_output_tokens=result.llm_output_tokens,
        total_latency_ms=result.total_latency_ms,
        error=result.error,
        metadata=body.metadata or {},
    )
    analysis.set_log_ttl()

    await db.analyses.insert_one(analysis.model_dump())

    # Auto-learning: when the LLM identifies a NEW pattern (not in the regex library),
    # save it as a candidate for admin review and future promotion to regex library.
    if result.match_method == "groq_llm" and result.pattern_id:
        known = analyzer.index.get_pattern(result.pattern_id)
        if not known:
            await _save_llm_candidate(db, result, body.log, analysis_id=analysis.id)

    # Slack notification — fire-and-forget (never blocks the response)
    asyncio.create_task(
        _notify_slack(db=db, user_id=user.id, analysis=analysis, result=result, raw_log=body.log)
    )

    if result.error:
        logger.warning(
            f"Analysis error for user {user.id}: {result.error} "
            f"(method={result.match_method})"
        )

    # Convert engine Solution objects → dicts so Pydantic can coerce to SolutionOut
    solutions_out = [
        s.model_dump() if hasattr(s, "model_dump")
        else (s.__dict__ if hasattr(s, "__dict__") else s)
        for s in result.solutions
    ]

    return AnalyzeResponse(
        id=analysis.id,
        pattern_id=result.pattern_id,
        confidence=result.confidence,
        match_method=result.match_method,
        detected_category=result.detected_category,
        extracted_vars=result.extracted_vars,
        solutions=solutions_out,
        causal_chain=result.causal_chain.__dict__ if result.causal_chain and hasattr(result.causal_chain, "__dict__") else result.causal_chain,
        llm_model=result.llm_model,
        llm_latency_ms=result.llm_latency_ms,
        total_latency_ms=result.total_latency_ms,
        error=result.error,
    )


# ── GET /analyses ──────────────────────────────────────────────────────────────

@router.get("", response_model=AnalysisListResponse)
async def list_analyses(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    pattern_id: Optional[str] = Query(None),
    match_method: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),
):
    """Return paginated analysis history for the current user."""
    db = get_db()

    query: dict = {"user_id": user.id}
    if pattern_id:
        query["pattern_id"] = pattern_id
    if match_method:
        query["match_method"] = match_method
    if category:
        query["detected_category"] = category
    if client_id:
        query["client_id"] = client_id

    skip = (page - 1) * per_page
    total = await db.analyses.count_documents(query)
    cursor = (
        db.analyses.find(query, {"_id": 0})
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(per_page)
    )
    items = await cursor.to_list(length=per_page)

    return AnalysisListResponse(
        total=total,
        page=page,
        per_page=per_page,
        items=items,
    )


# ── GET /analyses/{id} ────────────────────────────────────────────────────────

@router.get("/{analysis_id}", response_model=Analysis)
async def get_analysis(analysis_id: str, user: CurrentUser):
    """Return a single analysis by ID (must belong to the requesting user)."""
    db = get_db()
    doc = await db.analyses.find_one(
        {"id": analysis_id, "user_id": user.id}, {"_id": 0}
    )
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
    return Analysis(**doc)


# ── DELETE /analyses/{id} ─────────────────────────────────────────────────────

@router.delete("/{analysis_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_analysis(analysis_id: str, user: CurrentUser):
    """Delete a single analysis (must belong to the requesting user)."""
    db = get_db()
    result = await db.analyses.delete_one({"id": analysis_id, "user_id": user.id})
    if result.deleted_count == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")


# ── GET /analyses/stats/summary ───────────────────────────────────────────────

@router.get("/stats/summary")
async def get_stats_summary(user: CurrentUser):
    """
    Aggregate stats for the dashboard home page.
    Returns counts per match_method and per category over the last 30 days.
    """
    db = get_db()
    from datetime import timedelta

    since = datetime.utcnow() - timedelta(days=30)
    pipeline = [
        {"$match": {"user_id": user.id, "created_at": {"$gte": since}}},
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "avg_confidence": {"$avg": "$confidence"},
                "avg_latency_ms": {"$avg": "$total_latency_ms"},
                "regex_matches": {
                    "$sum": {"$cond": [{"$eq": ["$match_method", "regex"]}, 1, 0]}
                },
                "llm_matches": {
                    "$sum": {"$cond": [{"$eq": ["$match_method", "groq_llm"]}, 1, 0]}
                },
                "no_matches": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$match_method", ["generic_fallback", "no_match"]]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    agg = await db.analyses.aggregate(pipeline).to_list(length=1)
    if not agg:
        return {
            "total": 0,
            "avg_confidence": 0.0,
            "avg_latency_ms": 0.0,
            "regex_matches": 0,
            "llm_matches": 0,
            "no_matches": 0,
        }
    row = agg[0]
    row.pop("_id", None)
    # Round floats
    row["avg_confidence"] = round(row.get("avg_confidence") or 0.0, 3)
    row["avg_latency_ms"] = round(row.get("avg_latency_ms") or 0.0, 1)
    return row
