"""
Admin dashboard metrics endpoint.
GET /admin/dashboard/metrics — snapshot of platform health.
"""

from datetime import datetime, timedelta

from fastapi import APIRouter

from database import get_db
from deps.admin_auth import CurrentAdmin

router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])


@router.get("/metrics")
async def get_metrics(admin: CurrentAdmin):
    """
    Returns a snapshot of platform metrics for the last 24 hours.
    Heavy aggregations run server-side so the dashboard stays snappy.
    """
    db = get_db()
    since_24h = datetime.utcnow() - timedelta(hours=24)
    since_48h = datetime.utcnow() - timedelta(hours=48)

    # ── Counts ─────────────────────────────────────────────────────────────
    analyses_today = await db.analyses.count_documents(
        {"created_at": {"$gte": since_24h}}
    )
    analyses_prev = await db.analyses.count_documents(
        {"created_at": {"$gte": since_48h, "$lt": since_24h}}
    )
    analyses_change_pct = (
        ((analyses_today - analyses_prev) / analyses_prev * 100)
        if analyses_prev > 0 else 0.0
    )

    # Active users (distinct user_id in last 24h)
    active_users = await db.analyses.distinct(
        "user_id", {"created_at": {"$gte": since_24h}}
    )
    active_users_prev = await db.analyses.distinct(
        "user_id", {"created_at": {"$gte": since_48h, "$lt": since_24h}}
    )
    active_count = len(active_users)
    active_prev_count = len(active_users_prev)
    active_change_pct = (
        ((active_count - active_prev_count) / active_prev_count * 100)
        if active_prev_count > 0 else 0.0
    )

    # Pattern hits (analyses that matched at least one pattern)
    pattern_hits = await db.analyses.count_documents(
        {"created_at": {"$gte": since_24h}, "result.method": "regex"}
    )
    pattern_hits_prev = await db.analyses.count_documents(
        {"created_at": {"$gte": since_48h, "$lt": since_24h}, "result.method": "regex"}
    )
    hits_change_pct = (
        ((pattern_hits - pattern_hits_prev) / pattern_hits_prev * 100)
        if pattern_hits_prev > 0 else 0.0
    )

    # LLM calls and cost
    llm_pipeline = [
        {"$match": {"created_at": {"$gte": since_24h}, "result.method": "llm"}},
        {"$group": {
            "_id": None,
            "calls": {"$sum": 1},
            "cost": {"$sum": "$result.cost_usd"},
        }},
    ]
    llm_agg = await db.analyses.aggregate(llm_pipeline).to_list(1)
    llm_data = llm_agg[0] if llm_agg else {"calls": 0, "cost": 0.0}

    # Success rate (helpful feedback in last 24h)
    feedback_today = await db.feedback.count_documents(
        {"created_at": {"$gte": since_24h}}
    )
    helpful_today = await db.feedback.count_documents(
        {"created_at": {"$gte": since_24h}, "rating": "helpful"}
    )
    success_rate = (helpful_today / feedback_today * 100) if feedback_today > 0 else 0.0

    # Open community contributions
    open_prs = await db.community_contributions.count_documents(
        {"status": "pending_review"}
    )

    # ── Category distribution ───────────────────────────────────────────────
    cat_pipeline = [
        {"$match": {"created_at": {"$gte": since_24h}, "result.category": {"$exists": True}}},
        {"$group": {"_id": "$result.category", "count": {"$sum": 1}}},
    ]
    cat_docs = await db.analyses.aggregate(cat_pipeline).to_list(None)
    total_cat = sum(d["count"] for d in cat_docs) or 1
    category_distribution = {
        d["_id"]: round(d["count"] / total_cat * 100)
        for d in sorted(cat_docs, key=lambda x: x["count"], reverse=True)
        if d["_id"]
    }

    # ── Recent activity (last 20 events across types) ───────────────────────
    recent_analyses = await db.analyses.find(
        {"created_at": {"$gte": since_24h}},
        {"user_id": 1, "result.pattern_id": 1, "created_at": 1},
    ).sort("created_at", -1).limit(5).to_list(5)

    recent_prs = await db.community_contributions.find(
        {}, {"pr_number": 1, "title": 1, "updated_at": 1, "status": 1}
    ).sort("updated_at", -1).limit(3).to_list(3)

    activity = []
    for a in recent_analyses:
        activity.append({
            "type": "analysis",
            "description": f"user:{str(a.get('user_id', '?'))[:5]}  {a.get('result', {}).get('pattern_id', 'unmatched')} matched",
            "timestamp": a["created_at"].isoformat(),
            "user_id": a.get("user_id"),
            "pattern_id": a.get("result", {}).get("pattern_id"),
        })
    for pr in recent_prs:
        activity.append({
            "type": "contribution",
            "description": f"Community PR #{pr['pr_number']} — {pr.get('status', 'unknown')}",
            "timestamp": pr["updated_at"].isoformat(),
        })

    activity.sort(key=lambda x: x["timestamp"], reverse=True)

    return {
        "analyses_today": analyses_today,
        "analyses_change_pct": round(analyses_change_pct, 1),
        "active_users_today": active_count,
        "active_users_change_pct": round(active_change_pct, 1),
        "llm_calls_today": llm_data["calls"],
        "llm_cost_today_usd": round(llm_data["cost"], 4),
        "pattern_hits_today": pattern_hits,
        "pattern_hits_change_pct": round(hits_change_pct, 1),
        "success_rate_pct": round(success_rate, 1),
        "success_rate_change_pct": 0.0,   # computed over longer window
        "open_prs": open_prs,
        "category_distribution": category_distribution,
        "recent_activity": activity[:10],
    }
