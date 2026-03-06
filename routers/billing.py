"""
User-facing billing router — Stripe Checkout, Customer Portal, Webhooks.

Endpoints:
  GET  /billing/subscription        current subscription info
  POST /billing/checkout            create Stripe Checkout Session (upgrade)
  POST /billing/portal              create Stripe Customer Portal session
  POST /billing/webhook             Stripe webhook (tier sync on payment events)
"""

import logging
from datetime import datetime
from typing import Optional

import stripe
from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from config import settings
from database import get_db
from deps.auth import CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])

# Pricing config (matches admin_billing.py hardcoded values)
TIER_PRICES = {
    "pro":  19.0,
    "team": 49.0,
}


def _stripe_client() -> stripe.StripeClient:
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing not configured on this server.",
        )
    return stripe.StripeClient(settings.stripe_secret_key)


# ── GET /billing/subscription ─────────────────────────────────────────────────

@router.get("/subscription")
async def get_subscription(user: CurrentUser):
    """Return current subscription details for the authenticated user."""
    db = get_db()

    # Usage stats
    analyses_count = await db.analyses.count_documents({"user_id": user.id})
    from models.user import TIER_LIMITS
    limits = TIER_LIMITS.get(user.tier, TIER_LIMITS["free"])

    subscription_info = {
        "tier": user.tier,
        "analyses_this_month": user.analyses_this_month,
        "analyses_limit": limits.get("analyses_per_day", 10),
        "llm_enabled": limits.get("llm_enabled", False),
        "sync_enabled": limits.get("sync_enabled", False),
        "team_enabled": limits.get("team_enabled", False),
        "history_days": limits.get("history_days", 7),
        "total_analyses": analyses_count,
        "stripe_customer_id": user.stripe_customer_id,
        "stripe_subscription_id": user.stripe_subscription_id,
        "subscription_expires_at": user.subscription_expires_at,
        "price_per_month": TIER_PRICES.get(user.tier, 0.0),
    }

    # If Stripe configured, fetch live subscription status
    if settings.stripe_secret_key and user.stripe_subscription_id:
        try:
            sc = _stripe_client()
            sub = sc.subscriptions.retrieve(user.stripe_subscription_id)
            subscription_info["stripe_status"] = sub.status
            subscription_info["current_period_end"] = datetime.fromtimestamp(
                sub.current_period_end
            ).isoformat()
            subscription_info["cancel_at_period_end"] = sub.cancel_at_period_end
        except Exception as e:
            logger.warning("Could not fetch Stripe subscription: %s", e)

    return subscription_info


# ── POST /billing/checkout ────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    tier: str               # "pro" | "team"
    success_url: str        # redirect after payment
    cancel_url:  str        # redirect on cancel
    interval: str = "month" # "month" | "year"


@router.post("/checkout")
async def create_checkout_session(body: CheckoutRequest, user: CurrentUser):
    """Create a Stripe Checkout Session for upgrading to pro or team tier."""
    if body.tier not in ("pro", "team"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid tier. Choose 'pro' or 'team'.")
    if user.tier == body.tier:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Already on {body.tier} tier.")

    price_id = settings.stripe_pro_price_id if body.tier == "pro" else settings.stripe_team_price_id
    if not price_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Stripe price ID for '{body.tier}' not configured. Set STRIPE_{body.tier.upper()}_PRICE_ID.",
        )

    sc = _stripe_client()

    # Ensure customer exists in Stripe
    customer_id = user.stripe_customer_id
    if not customer_id:
        customer = sc.customers.create(params={
            "email": user.github_email or f"{user.github_login}@github.com",
            "name": user.github_name or user.github_login,
            "metadata": {"user_id": user.id, "github_login": user.github_login},
        })
        customer_id = customer.id
        db = get_db()
        await db.users.update_one(
            {"id": user.id},
            {"$set": {"stripe_customer_id": customer_id}},
        )

    session = sc.checkout.sessions.create(params={
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "mode": "subscription",
        "success_url": body.success_url + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": body.cancel_url,
        "metadata": {"user_id": user.id, "tier": body.tier},
        "subscription_data": {
            "metadata": {"user_id": user.id, "tier": body.tier},
        },
        "allow_promotion_codes": True,
    })

    return {"checkout_url": session.url, "session_id": session.id}


# ── POST /billing/portal ──────────────────────────────────────────────────────

class PortalRequest(BaseModel):
    return_url: str


@router.post("/portal")
async def create_portal_session(body: PortalRequest, user: CurrentUser):
    """Create a Stripe Customer Portal session (manage/cancel subscription)."""
    if not user.stripe_customer_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No billing account found. Upgrade to a paid tier first.",
        )

    sc = _stripe_client()
    session = sc.billing_portal.sessions.create(params={
        "customer": user.stripe_customer_id,
        "return_url": body.return_url,
    })

    return {"portal_url": session.url}


# ── POST /billing/webhook ─────────────────────────────────────────────────────

@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(None, alias="stripe-signature"),
):
    """
    Handle Stripe webhook events to keep user tiers in sync.
    Events handled:
      - checkout.session.completed   → activate tier
      - customer.subscription.updated → sync tier changes
      - customer.subscription.deleted → downgrade to free
      - invoice.payment_failed        → log / alert
    """
    payload = await request.body()

    if settings.stripe_webhook_secret:
        try:
            event = stripe.Webhook.construct_event(
                payload, stripe_signature, settings.stripe_webhook_secret
            )
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Stripe signature")
    else:
        # Dev mode — no signature check
        import json
        event = json.loads(payload)

    event_type = event.get("type") if isinstance(event, dict) else event.type
    data_obj   = event.get("data", {}).get("object", {}) if isinstance(event, dict) else event.data.object

    logger.info("Stripe webhook: %s", event_type)

    db = get_db()

    if event_type == "checkout.session.completed":
        user_id = data_obj.get("metadata", {}).get("user_id")
        tier    = data_obj.get("metadata", {}).get("tier", "pro")
        sub_id  = data_obj.get("subscription")
        cust_id = data_obj.get("customer")
        if user_id:
            await db.users.update_one(
                {"id": user_id},
                {"$set": {
                    "tier": tier,
                    "stripe_customer_id": cust_id,
                    "stripe_subscription_id": sub_id,
                    "tier_changed_at": datetime.utcnow(),
                }},
            )
            logger.info("User %s upgraded to %s via checkout", user_id, tier)
            # Record billing transaction
            await db.billing_transactions.insert_one({
                "user_id": user_id,
                "type": "subscription_start",
                "tier": tier,
                "stripe_subscription_id": sub_id,
                "created_at": datetime.utcnow(),
            })

    elif event_type == "customer.subscription.updated":
        sub_id  = data_obj.get("id")
        status_ = data_obj.get("status")
        tier    = data_obj.get("metadata", {}).get("tier", "pro")
        user_id = data_obj.get("metadata", {}).get("user_id")
        cancel  = data_obj.get("cancel_at_period_end", False)
        period_end = data_obj.get("current_period_end")
        if user_id:
            update: dict = {"tier_changed_at": datetime.utcnow()}
            if status_ == "active" and not cancel:
                update["tier"] = tier
            if period_end:
                update["subscription_expires_at"] = datetime.fromtimestamp(period_end)
            await db.users.update_one({"id": user_id}, {"$set": update})

    elif event_type == "customer.subscription.deleted":
        sub_id  = data_obj.get("id")
        user_id = data_obj.get("metadata", {}).get("user_id")
        if user_id:
            await db.users.update_one(
                {"id": user_id},
                {"$set": {
                    "tier": "free",
                    "stripe_subscription_id": None,
                    "subscription_expires_at": None,
                    "tier_changed_at": datetime.utcnow(),
                }},
            )
            logger.info("User %s downgraded to free (subscription deleted)", user_id)

    elif event_type == "invoice.payment_failed":
        cust_id = data_obj.get("customer")
        logger.warning("Payment failed for customer %s", cust_id)
        user_doc = await db.users.find_one({"stripe_customer_id": cust_id})
        if user_doc:
            await db.billing_transactions.insert_one({
                "user_id": user_doc["id"],
                "type": "payment_failed",
                "created_at": datetime.utcnow(),
                "metadata": {"invoice_id": data_obj.get("id")},
            })

    return {"received": True}
