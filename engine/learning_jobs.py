"""
Learning Background Jobs.

These jobs run periodically in the background to continuously improve
the OpsHero pattern library without human intervention.

Jobs:
  auto_promote_job     — Find high-frequency candidates → generate pattern → promote
  rerank_solutions_job — Re-score solution confidence from user feedback data
  prune_candidates_job — Remove stale low-frequency candidates (cleanup)

Job scheduler:
  Called from main.py lifespan background task every `learning_job_interval_seconds`.
  Each job is idempotent — safe to run multiple times.

Auto-promote flow:
  candidate.unmatched_count >= MIN_SIGHTINGS
  AND candidate.llm_confidence >= MIN_CONFIDENCE
  AND candidate.status = "pending"
        ↓
  PatternGenerator.generate_and_validate(candidate, examples)
        ↓
  Result valid → db.patterns.upsert + Redis PUBLISH → live in seconds
  Result invalid → candidate.status = "ready_for_review" → admin queue
        ↓
  candidate.status = "auto_promoted" | "ready_for_review"

Rerank flow:
  For each live pattern with feedback data:
    helpful_rate = helpful_count / (helpful_count + not_helpful_count)
    solution.confidence *= (0.5 + helpful_rate)  # regress toward 0.5 if unknown
    Resort solutions by adjusted confidence
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from engine.pattern_generator import PatternGenerator, PatternGenerationResult
from engine.pattern_validator import validate_pattern

logger = logging.getLogger(__name__)

REDIS_HOT_RELOAD_CHANNEL = "pattern_updates"


# ── Auto-promote job ──────────────────────────────────────────────────────────

async def auto_promote_job(
    db,
    redis,
    generator: PatternGenerator,
    min_sightings: int = 10,
    min_confidence: float = 0.80,
    batch_size: int = 20,
) -> dict:
    """
    Find high-frequency pattern candidates and promote them to the live library.

    Returns a summary dict with counts of promoted/escalated/failed candidates.
    """
    summary = {
        "candidates_evaluated": 0,
        "auto_promoted": 0,
        "escalated_to_review": 0,
        "skipped_invalid": 0,
        "errors": 0,
    }

    # Find candidates that meet the promotion threshold
    candidates = (
        await db.pattern_candidates.find(
            {
                "status": "pending",
                "unmatched_count": {"$gte": min_sightings},
                "llm_confidence": {"$gte": min_confidence},
            },
            {"_id": 0},
        )
        .sort("unmatched_count", -1)
        .limit(batch_size)
        .to_list(batch_size)
    )

    if not candidates:
        logger.info("auto_promote_job: no candidates meet threshold (sightings≥%d, confidence≥%.0f%%)",
                    min_sightings, min_confidence * 100)
        return summary

    logger.info(
        "auto_promote_job: evaluating %d candidates (sightings≥%d, confidence≥%.0f%%)",
        len(candidates), min_sightings, min_confidence * 100,
    )

    for candidate in candidates:
        summary["candidates_evaluated"] += 1
        cid = candidate.get("id", "unknown")

        try:
            result = await _process_candidate(db, redis, generator, candidate)
            if result == "promoted":
                summary["auto_promoted"] += 1
            elif result == "escalated":
                summary["escalated_to_review"] += 1
            elif result == "invalid":
                summary["skipped_invalid"] += 1
        except Exception as e:
            logger.error("auto_promote_job: error processing candidate %s: %s", cid, e)
            summary["errors"] += 1

        # Small delay between LLM calls to avoid rate limits
        await asyncio.sleep(0.5)

    logger.info(
        "auto_promote_job complete: %d promoted, %d escalated, %d invalid, %d errors",
        summary["auto_promoted"], summary["escalated_to_review"],
        summary["skipped_invalid"], summary["errors"],
    )
    return summary


async def _process_candidate(
    db,
    redis,
    generator: PatternGenerator,
    candidate: dict,
) -> str:
    """
    Process a single candidate through the generation pipeline.
    Returns: "promoted" | "escalated" | "invalid" | "error"
    """
    cid = candidate.get("id", "unknown")
    llm_pid = candidate.get("llm_pattern_id", "unknown")

    # Fetch additional example logs from related sightings
    # (other analyses that matched the same llm_pattern_id)
    extra_examples = await _fetch_related_examples(db, candidate)

    logger.info(
        "Generating pattern for candidate %s (%s) with %d extra examples",
        cid, llm_pid, len(extra_examples),
    )

    # Run the generation LLM call
    result: PatternGenerationResult = await generator.generate_and_validate(
        candidate=candidate,
        extra_examples=extra_examples,
        retry_on_regex_mismatch=True,
    )

    now = datetime.utcnow()

    if not result.success:
        logger.warning("Generation failed for candidate %s: %s", cid, result.error)
        await db.pattern_candidates.update_one(
            {"id": cid},
            {"$set": {
                "last_generation_error": result.error,
                "last_generation_at": now,
                "generation_attempts": {"$inc": 1},
            }},
        )
        return "error"

    if result.validation_errors:
        # Pattern generated but has validation issues — escalate to admin review
        logger.info(
            "Candidate %s generated with %d validation error(s) → escalating",
            cid, len(result.validation_errors),
        )
        await db.pattern_candidates.update_one(
            {"id": cid},
            {"$set": {
                "status": "ready_for_review",
                "pattern_data": result.pattern_data,
                "validation_errors": result.validation_errors,
                "generation_model": result.model,
                "generation_latency_ms": result.latency_ms,
                "last_generation_at": now,
            }},
        )

        # Record job entry
        await _record_job_event(db, "auto_promote", cid, "escalated", {
            "validation_errors": result.validation_errors,
            "pattern_id": result.pattern_data.get("pattern_id") if result.pattern_data else None,
        })
        return "escalated"

    # Valid pattern — promote to live library
    pattern_data = result.pattern_data
    pattern_id = pattern_data["pattern_id"]

    await db.patterns.update_one(
        {"pattern_id": pattern_id},
        {"$set": {
            **pattern_data,
            "status": "active",
            "source": "auto_promoted",
            "promoted_from_candidate": cid,
            "promoted_at": now,
        }},
        upsert=True,
    )

    # Hot-reload via Redis
    try:
        await redis.publish(
            REDIS_HOT_RELOAD_CHANNEL,
            json.dumps({"action": "upsert", "pattern_id": pattern_id}),
        )
    except Exception as e:
        logger.warning("Redis publish failed for %s: %s", pattern_id, e)

    # Mark candidate as auto-promoted
    await db.pattern_candidates.update_one(
        {"id": cid},
        {"$set": {
            "status": "auto_promoted",
            "pattern_data": pattern_data,
            "promoted_pattern_id": pattern_id,
            "promoted_at": now,
            "generation_model": result.model,
            "generation_latency_ms": result.latency_ms,
            "last_generation_at": now,
        }},
    )

    await _record_job_event(db, "auto_promote", cid, "promoted", {
        "pattern_id": pattern_id,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    })

    logger.info(
        "✓ Candidate %s → live pattern '%s' (model=%s, latency=%dms)",
        cid, pattern_id, result.model, result.latency_ms,
    )
    return "promoted"


async def _fetch_related_examples(db, candidate: dict) -> list[str]:
    """
    Find up to 4 additional log examples from other candidates with the same
    llm_pattern_id (similar errors from different users).
    """
    llm_pid = candidate.get("llm_pattern_id")
    if not llm_pid:
        return []

    cid = candidate.get("id")
    related = (
        await db.pattern_candidates.find(
            {"llm_pattern_id": llm_pid, "id": {"$ne": cid}},
            {"_id": 0, "example_log_snippet": 1},
        )
        .sort("unmatched_count", -1)
        .limit(4)
        .to_list(4)
    )
    return [r["example_log_snippet"] for r in related if r.get("example_log_snippet")]


# ── Rerank solutions job ───────────────────────────────────────────────────────

async def rerank_solutions_job(db, min_feedback_count: int = 5) -> dict:
    """
    Re-score solution confidence based on user feedback (helpful/not helpful).

    For each live pattern with enough feedback:
      helpful_rate = helpful_count / (helpful_count + not_helpful_count)
      new_confidence = lerp(original_confidence, helpful_rate, 0.4)
      Re-sort solutions by new_confidence

    Requires min_feedback_count total ratings before adjusting (avoids noise).
    """
    summary = {"patterns_evaluated": 0, "patterns_updated": 0}

    patterns = await db.patterns.find(
        {"status": "active"},
        {"_id": 0, "pattern_id": 1, "solutions": 1, "metadata": 1},
    ).to_list(None)

    for pattern in patterns:
        summary["patterns_evaluated"] += 1
        pid = pattern.get("pattern_id", "")
        solutions = pattern.get("solutions", [])
        meta = pattern.get("metadata", {}).get("stats", {})

        helpful = meta.get("helpful_count", 0)
        not_helpful = meta.get("not_helpful_count", 0)
        total_feedback = helpful + not_helpful

        if total_feedback < min_feedback_count:
            continue

        helpful_rate = helpful / total_feedback

        # Adjust solution confidences
        updated_solutions = []
        for sol in solutions:
            orig_conf = float(sol.get("confidence", 0.7))
            # Lerp 40% toward the observed helpful_rate
            adjusted = orig_conf * 0.6 + helpful_rate * 0.4
            updated_solutions.append({**sol, "confidence": round(adjusted, 3)})

        # Re-sort by adjusted confidence
        updated_solutions.sort(key=lambda s: s.get("confidence", 0), reverse=True)
        # Renumber ranks
        for i, sol in enumerate(updated_solutions):
            sol["rank"] = i + 1

        await db.patterns.update_one(
            {"pattern_id": pid},
            {"$set": {
                "solutions": updated_solutions,
                "metadata.stats.last_reranked_at": datetime.utcnow().isoformat(),
                "metadata.stats.helpful_rate": round(helpful_rate, 3),
            }},
        )
        summary["patterns_updated"] += 1

    logger.info(
        "rerank_solutions_job: evaluated %d patterns, updated %d",
        summary["patterns_evaluated"], summary["patterns_updated"],
    )
    return summary


# ── Prune stale candidates job ─────────────────────────────────────────────────

async def prune_candidates_job(
    db,
    max_age_days: int = 90,
    min_sightings_to_keep: int = 2,
) -> dict:
    """
    Remove stale, low-value candidates:
    - Status 'pending'
    - Last seen more than max_age_days ago
    - Seen fewer than min_sightings_to_keep times
    These are likely one-off errors not worth promoting.
    """
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    result = await db.pattern_candidates.delete_many({
        "status": "pending",
        "last_seen_at": {"$lt": cutoff},
        "unmatched_count": {"$lt": min_sightings_to_keep},
    })
    logger.info("prune_candidates_job: removed %d stale candidates", result.deleted_count)
    return {"deleted": result.deleted_count}


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _record_job_event(
    db,
    job_type: str,
    candidate_id: str,
    outcome: str,
    details: dict,
) -> None:
    """Record a job event for audit trail and admin visibility."""
    try:
        await db.learning_jobs.update_one(
            {"type": job_type, "candidate_id": candidate_id},
            {"$set": {
                "type": job_type,
                "candidate_id": candidate_id,
                "outcome": outcome,
                "details": details,
                "updated_at": datetime.utcnow(),
            }, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("Failed to record job event: %s", e)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_learning_loop(
    db,
    redis,
    generator: Optional[PatternGenerator],
    interval_seconds: int = 3600,
    min_sightings: int = 10,
    min_confidence: float = 0.80,
) -> None:
    """
    Background coroutine that runs the full learning pipeline on a schedule.
    Designed to run as an asyncio task — cancelled cleanly on shutdown.

    Schedule:
      Every interval_seconds (default 1h):
        1. auto_promote_job  — promote high-frequency candidates
        2. rerank_solutions_job — re-score solutions from feedback
        3. prune_candidates_job  — remove stale low-value candidates
    """
    logger.info(
        "Learning loop started (interval=%ds, min_sightings=%d, min_confidence=%.0f%%)",
        interval_seconds, min_sightings, min_confidence * 100,
    )

    # Initial delay — wait 5 minutes before first run to let the server warm up
    await asyncio.sleep(300)

    while True:
        try:
            run_start = datetime.utcnow()
            logger.info("Learning loop: starting run at %s", run_start.isoformat())

            # 1. Auto-promote high-frequency candidates
            if generator:
                promote_summary = await auto_promote_job(
                    db=db,
                    redis=redis,
                    generator=generator,
                    min_sightings=min_sightings,
                    min_confidence=min_confidence,
                )
                logger.info("Learning loop: promote summary: %s", promote_summary)
            else:
                logger.info("Learning loop: PatternGenerator not configured — skipping auto-promote")

            # 2. Rerank solutions from feedback
            rerank_summary = await rerank_solutions_job(db)
            logger.info("Learning loop: rerank summary: %s", rerank_summary)

            # 3. Prune stale candidates (runs less frequently — every 7 runs ≈ weekly)
            run_count = await _get_run_count(db)
            if run_count % 7 == 0:
                prune_summary = await prune_candidates_job(db)
                logger.info("Learning loop: prune summary: %s", prune_summary)

            # Record the completed run
            await db.learning_jobs.insert_one({
                "type": "scheduled_run",
                "run_count": run_count + 1,
                "started_at": run_start,
                "completed_at": datetime.utcnow(),
                "duration_seconds": (datetime.utcnow() - run_start).total_seconds(),
                "promote_summary": promote_summary if generator else None,
                "rerank_summary": rerank_summary,
            })

        except asyncio.CancelledError:
            logger.info("Learning loop cancelled — shutting down")
            return
        except Exception as e:
            logger.error("Learning loop error: %s", e, exc_info=True)

        await asyncio.sleep(interval_seconds)


async def _get_run_count(db) -> int:
    """Return how many times the scheduled learning loop has run."""
    return await db.learning_jobs.count_documents({"type": "scheduled_run"})
