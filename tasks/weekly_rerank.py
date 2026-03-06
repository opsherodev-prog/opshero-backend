"""
Celery task — Weekly pattern rerank (auto-learning Level 2).

Runs every Sunday at 03:00 UTC.

Algorithm:
  For each pattern with ≥ 20 total feedback votes:
    new_success_rate = helpful_count / (helpful_count + not_helpful_count)
    Reorder solutions by new_success_rate × solution.confidence
    Persist updated ordering to MongoDB
    Publish "pattern_updated:{pattern_id}" to Redis so PatternIndex hot-reloads

Celery beat schedule is configured in celery_app.py (or via celerybeat-schedule).
"""

import asyncio
import json
import logging
from datetime import datetime

from celery import Celery

from config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "opshero",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "weekly-pattern-rerank": {
            "task": "tasks.weekly_rerank.rerank_patterns",
            "schedule": 604800,  # 7 days in seconds
        },
    },
)


# ── Async core ─────────────────────────────────────────────────────────────────

async def _rerank_async() -> dict:
    """
    Core rerank logic — must be awaited.
    Returns a summary dict with stats about the run.
    """
    from motor.motor_asyncio import AsyncIOMotorClient
    import redis.asyncio as aioredis

    mongo = AsyncIOMotorClient(settings.mongodb_url)
    db = mongo[settings.mongodb_db_name]
    redis = await aioredis.from_url(settings.redis_url, decode_responses=True)

    MIN_VOTES = 20
    updated = 0
    skipped = 0

    cursor = db.patterns.find(
        {
            "metadata.stats.helpful_count": {"$exists": True},
        },
        {
            "_id": 0,
            "pattern_id": 1,
            "solutions": 1,
            "metadata.stats": 1,
        },
    )

    async for doc in cursor:
        stats = (doc.get("metadata") or {}).get("stats") or {}
        helpful = stats.get("helpful_count", 0)
        not_helpful = stats.get("not_helpful_count", 0)
        total = helpful + not_helpful

        if total < MIN_VOTES:
            skipped += 1
            continue

        success_rate = helpful / total

        # Reorder solutions by (success_rate × solution.confidence) descending
        solutions = doc.get("solutions", [])
        reranked = sorted(
            solutions,
            key=lambda s: success_rate * s.get("confidence", 0.5),
            reverse=True,
        )
        # Reassign rank fields
        for rank, sol in enumerate(reranked, start=1):
            sol["rank"] = rank

        pattern_id = doc["pattern_id"]
        await db.patterns.update_one(
            {"pattern_id": pattern_id},
            {
                "$set": {
                    "solutions": reranked,
                    "metadata.stats.success_rate": round(success_rate, 4),
                    "metadata.updated_at": datetime.utcnow().isoformat(),
                }
            },
        )

        # Notify PatternIndex to hot-reload this pattern
        full_doc = await db.patterns.find_one(
            {"pattern_id": pattern_id}, {"_id": 0}
        )
        if full_doc:
            await redis.publish(
                f"pattern_updated:{pattern_id}",
                json.dumps(full_doc, default=str),
            )

        updated += 1
        logger.info(
            f"Reranked pattern {pattern_id}: success_rate={success_rate:.3f}, "
            f"votes={total}"
        )

    await redis.aclose()
    mongo.close()

    summary = {
        "run_at": datetime.utcnow().isoformat(),
        "updated": updated,
        "skipped_insufficient_votes": skipped,
    }
    logger.info(f"Weekly rerank complete: {summary}")
    return summary


# ── Celery task wrapper ────────────────────────────────────────────────────────

@celery_app.task(name="tasks.weekly_rerank.rerank_patterns", bind=True, max_retries=3)
def rerank_patterns(self):
    """
    Celery entry point — runs the async rerank in a new event loop.
    Retries up to 3 times on failure with exponential backoff.
    """
    try:
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_rerank_async())
        finally:
            loop.close()
        return result
    except Exception as exc:
        logger.exception(f"Weekly rerank failed: {exc}")
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)
