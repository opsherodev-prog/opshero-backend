"""
Feedback router — thumbs up/down on analysis results.
Drives auto-learning Level 1: immediate pattern stats update.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pymongo import ReturnDocument

from database import get_db
from deps.auth import CurrentUser
from models.analysis import Analysis, FeedbackRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analyses", tags=["feedback"])


@router.post("/{analysis_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def submit_feedback(
    analysis_id: str,
    body: FeedbackRequest,
    user: CurrentUser,
):
    """
    Record helpful/not_helpful feedback for an analysis.

    - Updates the Analysis document with the feedback vote
    - Atomically increments pattern stats counters in the pattern collection
      (matched_count already incremented at analysis time; this updates
       helpful_count or not_helpful_count and recalculates success_rate)
    - Prevents duplicate votes per user per analysis
    """
    db = get_db()

    # Fetch the analysis (user must own it)
    doc = await db.analyses.find_one(
        {"id": analysis_id, "user_id": user.id}, {"_id": 0}
    )
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")

    analysis = Analysis(**doc)

    # Guard: already voted?
    if analysis.user_feedback is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Feedback already recorded for this analysis",
        )

    # 1. Persist feedback on the analysis document
    await db.analyses.update_one(
        {"id": analysis_id},
        {
            "$set": {
                "user_feedback": body.helpful,
                "feedback_comment": body.comment,
                "feedback_at": datetime.utcnow(),
            }
        },
    )

    # 2. Atomically update pattern stats (auto-learning Level 1)
    if analysis.pattern_id:
        # Determine which counter to increment
        inc_field = "helpful_count" if body.helpful else "not_helpful_count"

        updated = await db.patterns.find_one_and_update(
            {"pattern_id": analysis.pattern_id},
            {
                "$inc": {
                    "metadata.stats.matched_count": 0,  # ensure field exists
                    f"metadata.stats.{inc_field}": 1,
                },
                "$set": {"metadata.stats._needs_recalc": True},
            },
            return_document=ReturnDocument.AFTER,
            projection={"metadata.stats": 1, "_id": 0},
        )

        # Recalculate and persist success_rate atomically if we have the stats
        if updated:
            stats = (updated.get("metadata") or {}).get("stats") or {}
            helpful = stats.get("helpful_count", 0)
            not_helpful = stats.get("not_helpful_count", 0)
            total_feedback = helpful + not_helpful

            if total_feedback >= 1:
                success_rate = round(helpful / total_feedback, 4)
                await db.patterns.update_one(
                    {"pattern_id": analysis.pattern_id},
                    {
                        "$set": {
                            "metadata.stats.success_rate": success_rate,
                            "metadata.stats._needs_recalc": False,
                        }
                    },
                )
                logger.info(
                    f"Pattern {analysis.pattern_id} stats updated: "
                    f"helpful={helpful}, not_helpful={not_helpful}, "
                    f"success_rate={success_rate}"
                )
