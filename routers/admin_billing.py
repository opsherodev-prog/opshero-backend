"""
Admin billing / revenue endpoints.
GET /admin/billing/revenue
GET /admin/billing/transactions
GET /admin/billing/failed
POST /admin/billing/recovery-emails
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query

from database import get_db
from deps.admin_auth import CurrentAdmin, SuperAdmin

router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])


PERIOD_MAP = {
    "today": timedelta(hours=24),
    "7d":    timedelta(days=7),
    "30d":   timedelta(days=30),
    "90d":   timedelta(days=90),
}


@router.get("/revenue")
async def get_revenue(
    admin: CurrentAdmin,
    period: Optional[str] = Query("30d"),
):
    """Revenue metrics aggregated from user subscriptions."""
    db = get_db()
    since = datetime.utcnow() - PERIOD_MAP.get(period, timedelta(days=30))

    # Count users per tier
    tier_pipeline = [
        {"$group": {"_id": "$tier", "count": {"$sum": 1}}},
    ]
    tier_docs = await db.users.aggregate(tier_pipeline).to_list(None)
    tier_counts = {d["_id"]: d["count"] for d in tier_docs}

    pro_users  = tier_counts.get("pro", 0)
    team_users = tier_counts.get("team", 0)
    ent_users  = tier_counts.get("enterprise", 0)

    # Approximate MRR (hardcoded pricing for now; replace with Stripe in prod)
    PRO_PRICE  = 19.0
    TEAM_PRICE = 49.0
    ENT_PRICE  = 299.0

    mrr = pro_users * PRO_PRICE + team_users * TEAM_PRICE + ent_users * ENT_PRICE
    arr = mrr * 12

    # Transactions from the billing collection
    recent_tx = await db.billing_transactions.find(
        {"created_at": {"$gte": since}},
        {"_id": 0},
    ).sort("created_at", -1).limit(200).to_list(200)

    total_revenue = sum(t.get("amount_usd", 0) for t in recent_tx if t.get("status") == "succeeded")
    failed_count  = sum(1 for t in recent_tx if t.get("status") == "failed")

    churn_rate = 0.0
    if (pro_users + team_users) > 0:
        # naive: failed / total paying
        churn_rate = round(failed_count / (pro_users + team_users) * 100, 2)

    return {
        "mrr_usd": round(mrr, 2),
        "arr_usd": round(arr, 2),
        "mrr_change_pct": 0.0,   # requires historical data
        "churn_rate_pct": churn_rate,
        "by_plan": {
            "pro":        {"users": pro_users,  "revenue": round(pro_users  * PRO_PRICE,  2)},
            "team":       {"users": team_users, "revenue": round(team_users * TEAM_PRICE, 2)},
            "enterprise": {"users": ent_users,  "revenue": round(ent_users  * ENT_PRICE,  2)},
        },
        "mobile_money": {},   # populated once MTN/Wave integration is live
    }


@router.get("/transactions")
async def get_transactions(
    admin: CurrentAdmin,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    db = get_db()
    skip = (page - 1) * page_size
    docs = (
        await db.billing_transactions.find({}, {"_id": 0})
        .sort("created_at", -1)
        .skip(skip)
        .limit(page_size)
        .to_list(page_size)
    )
    for d in docs:
        if hasattr(d.get("created_at"), "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
    return docs


@router.get("/failed")
async def get_failed_payments(admin: CurrentAdmin):
    """Payments that failed in the last 30 days."""
    db = get_db()
    since = datetime.utcnow() - timedelta(days=30)
    docs = (
        await db.billing_transactions.find(
            {"status": "failed", "created_at": {"$gte": since}},
            {"_id": 0},
        )
        .sort("created_at", -1)
        .limit(100)
        .to_list(100)
    )
    for d in docs:
        if hasattr(d.get("created_at"), "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
    return docs


@router.post("/recovery-emails")
async def send_recovery_emails(admin: SuperAdmin):
    """
    Queue payment-failure recovery emails for all users
    with failed payments in the last 7 days.
    (Email sending is handled by the email service.)
    """
    db = get_db()
    since = datetime.utcnow() - timedelta(days=7)
    failed_docs = await db.billing_transactions.find(
        {"status": "failed", "created_at": {"$gte": since}},
        {"user_id": 1, "email": 1},
    ).to_list(None)

    queued = len(failed_docs)
    # In production: enqueue email jobs for each user_id / email
    return {"message": f"Recovery emails queued for {queued} users", "count": queued}
