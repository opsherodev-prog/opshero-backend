"""
Billing router — GeniusPay payment integration.

Supports Wave, Orange Money, MTN Money, Moov Money (XOF)
and Paystack (card payments).

Endpoints:
  GET  /billing/subscription        — current subscription info
  POST /billing/checkout            — initiate GeniusPay payment
  POST /billing/webhook             — GeniusPay webhook (tier upgrade on payment.success)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel

from config import settings
from database import get_db
from deps.auth import CurrentUser
from services.geniuspay import (
    TIER_PRICES_XOF,
    TIER_PRICES_USD,
    create_payment,
    get_payment,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


# ── GET /billing/subscription ─────────────────────────────────────────────────

@router.get("/subscription")
async def get_subscription(user: CurrentUser):
    """Return current subscription details for the authenticated user."""
    db = get_db()
    analyses_count = await db.analyses.count_documents({"user_id": user.id})

    from models.user import TIER_LIMITS
    limits = TIER_LIMITS.get(user.tier, TIER_LIMITS["free"])

    return {
        "tier":                  user.tier,
        "analyses_this_month":   user.analyses_this_month,
        "analyses_limit":        limits.get("analyses_per_day", 10),
        "llm_enabled":           limits.get("llm_enabled", False),
        "sync_enabled":          limits.get("sync_enabled", False),
        "team_enabled":          limits.get("team_enabled", False),
        "history_days":          limits.get("history_days", 7),
        "total_analyses":        analyses_count,
        "stripe_customer_id":    None,
        "stripe_subscription_id": None,
        "subscription_expires_at": user.subscription_expires_at,
        "price_per_month":       TIER_PRICES_USD.get(user.tier, 0),
        "price_per_month_xof":   TIER_PRICES_XOF.get(user.tier, 0),
    }


# ── POST /billing/checkout ────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    tier: str                          # "pro" | "team"
    success_url: str
    cancel_url: str
    payment_method: Optional[str] = None  # "wave" | "orange_money" | "mtn_money" | "moov_money" | "paystack"


@router.post("/checkout")
async def create_checkout(body: CheckoutRequest, user: CurrentUser):
    """
    Initiate a GeniusPay payment for upgrading to pro or team tier.
    Returns a payment_url to redirect the user to.
    """
    if body.tier not in ("pro", "team"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid tier. Choose 'pro' or 'team'.")
    if user.tier == body.tier:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Already on {body.tier} tier.")

    if not settings.geniuspay_api_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Payment not configured on this server.",
        )

    try:
        payment_data = await create_payment(
            tier=body.tier,
            user_id=user.id,
            user_email=user.github_email,
            user_name=user.github_name or user.github_login,
            success_url=body.success_url,
            error_url=body.cancel_url,
            payment_method=body.payment_method,
        )
    except RuntimeError as e:
        logger.error("GeniusPay checkout error: %s", str(e))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    except Exception as e:
        logger.error("Unexpected checkout error: %s", str(e), exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Payment error: {str(e)}")

    # Store pending payment reference in DB
    db = get_db()
    await db.billing_transactions.insert_one({
        "user_id":   user.id,
        "type":      "payment_initiated",
        "tier":      body.tier,
        "reference": payment_data.get("reference"),
        "amount":    payment_data.get("amount"),
        "currency":  "XOF",
        "status":    "pending",
        "created_at": datetime.utcnow(),
    })

    return {
        "checkout_url": payment_data["payment_url"],
        "reference":    payment_data["reference"],
        "amount_xof":   payment_data["amount"],
        "expires_at":   payment_data.get("expires_at"),
    }


# ── POST /billing/webhook ─────────────────────────────────────────────────────

@router.post("/webhook", include_in_schema=False)
async def geniuspay_webhook(
    request: Request,
    x_geniuspay_signature: Optional[str] = Header(None, alias="X-GeniusPay-Signature"),
    x_geniuspay_event: Optional[str] = Header(None, alias="X-GeniusPay-Event"),
):
    """
    Handle GeniusPay webhook events.

    Events handled:
      payment.success  → upgrade user tier (1 month)
      payment.failed   → log failure
      payment.refunded → downgrade to free
    """
    payload = await request.body()

    # Verify signature if webhook secret is configured
    if settings.geniuspay_webhook_secret:
        if not x_geniuspay_signature:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing webhook signature")
        if not verify_webhook_signature(payload, x_geniuspay_signature, settings.geniuspay_webhook_secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")

    import json
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON payload")

    event_type  = event.get("event") or x_geniuspay_event
    transaction = event.get("data", {}).get("transaction", {})
    metadata    = transaction.get("metadata", {})

    user_id = metadata.get("user_id")
    tier    = metadata.get("tier", "pro")
    ref     = transaction.get("reference", "")
    amount  = transaction.get("amount", 0)

    logger.info("GeniusPay webhook: %s | ref=%s | user=%s | tier=%s", event_type, ref, user_id, tier)

    db = get_db()

    if event_type == "payment.success":
        if not user_id:
            logger.warning("payment.success webhook missing user_id in metadata")
            return {"received": True}

        # Upgrade user tier for 30 days
        expires_at = datetime.utcnow() + timedelta(days=30)
        await db.users.update_one(
            {"id": user_id},
            {"$set": {
                "tier":                   tier,
                "subscription_expires_at": expires_at,
                "tier_changed_at":         datetime.utcnow(),
            }},
        )

        # Record successful transaction
        await db.billing_transactions.update_one(
            {"reference": ref},
            {"$set": {
                "status":       "completed",
                "completed_at": datetime.utcnow(),
                "amount":       amount,
            }},
            upsert=True,
        )

        logger.info("✅ User %s upgraded to %s (expires %s)", user_id, tier, expires_at.date())

        # Send confirmation email
        user_doc = await db.users.find_one({"id": user_id})
        if user_doc and user_doc.get("github_email"):
            try:
                from services.email import send_upgrade_confirmation_email
                from fastapi import BackgroundTasks
                # Fire and forget
                import asyncio
                asyncio.create_task(
                    send_upgrade_confirmation_email(
                        to=user_doc["github_email"],
                        username=user_doc.get("github_login", ""),
                        tier=tier,
                        expires_at=expires_at,
                    )
                )
            except Exception as e:
                logger.warning("Could not send upgrade email: %s", e)

    elif event_type == "payment.failed":
        await db.billing_transactions.update_one(
            {"reference": ref},
            {"$set": {"status": "failed", "failed_at": datetime.utcnow()}},
            upsert=True,
        )
        logger.warning("❌ Payment failed: ref=%s user=%s", ref, user_id)

    elif event_type == "payment.refunded":
        if user_id:
            await db.users.update_one(
                {"id": user_id},
                {"$set": {
                    "tier":                   "free",
                    "subscription_expires_at": None,
                    "tier_changed_at":         datetime.utcnow(),
                }},
            )
            await db.billing_transactions.update_one(
                {"reference": ref},
                {"$set": {"status": "refunded", "refunded_at": datetime.utcnow()}},
                upsert=True,
            )
            logger.info("↩️ User %s refunded and downgraded to free", user_id)

    elif event_type == "payment.cancelled":
        await db.billing_transactions.update_one(
            {"reference": ref},
            {"$set": {"status": "cancelled", "cancelled_at": datetime.utcnow()}},
            upsert=True,
        )

    elif event_type in ("cashout.approved", "cashout.completed", "cashout.expired"):
        # Cashout events — logged but no action needed for OpsHero
        logger.info("GeniusPay cashout event received: %s (ignored)", event_type)

    return {"received": True}


# ── GET /billing/verify/{reference} ──────────────────────────────────────────

@router.get("/verify/{reference}")
async def verify_payment(reference: str, user: CurrentUser):
    """
    Verify a payment status by reference.
    Called by the frontend after redirect from GeniusPay.
    """
    if not settings.geniuspay_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Payment not configured.")

    try:
        payment = await get_payment(reference)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not verify payment: {e}")

    payment_status = payment.get("status")
    metadata = payment.get("metadata", {})

    # Security: ensure this payment belongs to this user
    if metadata.get("user_id") != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Payment does not belong to this user.")

    # If completed but tier not yet updated (webhook may be delayed), update now
    if payment_status == "completed":
        tier = metadata.get("tier", "pro")
        db = get_db()
        user_doc = await db.users.find_one({"id": user.id})
        if user_doc and user_doc.get("tier") != tier:
            expires_at = datetime.utcnow() + timedelta(days=30)
            await db.users.update_one(
                {"id": user.id},
                {"$set": {
                    "tier":                   tier,
                    "subscription_expires_at": expires_at,
                    "tier_changed_at":         datetime.utcnow(),
                }},
            )
            logger.info("Tier updated via verify endpoint: user=%s tier=%s", user.id, tier)

    return {
        "reference": reference,
        "status":    payment_status,
        "tier":      metadata.get("tier"),
        "amount":    payment.get("amount"),
        "currency":  payment.get("currency", "XOF"),
    }
