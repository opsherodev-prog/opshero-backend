"""
GeniusPay payment service.

Handles payment initiation and webhook verification for:
- Wave, Orange Money, MTN Money, Moov Money (XOF)
- Paystack (card payments)

Base URL: https://pay.geniuspay.io/api/v1/merchant
Auth: X-API-Key + X-API-Secret headers
"""

import hashlib
import hmac
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

GENIUSPAY_BASE_URL = "https://pay.geniuspay.io/api/v1/merchant"

# Tier pricing in XOF (West African CFA franc)
TIER_PRICES_XOF = {
    "pro":  9_900,   # ~15 USD
    "team": 29_900,  # ~45 USD
}

# Tier pricing in USD (for display)
TIER_PRICES_USD = {
    "pro":  15,
    "team": 45,
}


def _headers() -> dict:
    return {
        "X-API-Key":    settings.geniuspay_api_key,
        "X-API-Secret": settings.geniuspay_api_secret,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


async def create_payment(
    tier: str,
    user_id: str,
    user_email: Optional[str],
    user_name: Optional[str],
    success_url: str,
    error_url: str,
    payment_method: Optional[str] = None,
) -> dict:
    """
    Initiate a GeniusPay payment for a tier upgrade.
    Returns the payment data including payment_url.
    """
    amount = TIER_PRICES_XOF.get(tier)
    if not amount:
        raise ValueError(f"Invalid tier: {tier}")

    payload: dict = {
        "amount": amount,
        "currency": "XOF",
        "description": f"OpsHero {tier.capitalize()} Plan — 1 month",
        "customer": {
            "name":  user_name or "OpsHero User",
            "email": user_email or "",
        },
        "success_url": success_url,
        "error_url":   error_url,
        "metadata": {
            "user_id": user_id,
            "tier":    tier,
            "source":  "opshero_upgrade",
        },
    }

    if payment_method:
        payload["payment_method"] = payment_method

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{GENIUSPAY_BASE_URL}/payments",
            json=payload,
            headers=_headers(),
        )

    if resp.status_code not in (200, 201):
        logger.error("GeniusPay payment init failed: %s — %s", resp.status_code, resp.text[:300])
        error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise RuntimeError(
            error_data.get("error", {}).get("message", f"Payment init failed: HTTP {resp.status_code}")
        )

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", {}).get("message", "Payment init failed"))

    return data["data"]


async def get_payment(reference: str) -> dict:
    """Fetch payment details by reference."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GENIUSPAY_BASE_URL}/payments/{reference}",
            headers=_headers(),
        )
    resp.raise_for_status()
    return resp.json()["data"]


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify GeniusPay webhook signature.
    HMAC-SHA256 of the raw payload body using the webhook secret.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
