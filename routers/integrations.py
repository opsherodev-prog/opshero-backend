"""
User integration settings.

GET    /integrations/slack        — get current Slack config (masked URL)
PUT    /integrations/slack        — save / update webhook URL
DELETE /integrations/slack        — remove webhook
POST   /integrations/slack/test   — send a test message
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from database import get_db
from deps.auth import CurrentUser
from services.slack_notifier import send_slack_notification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations", tags=["integrations"])


# ── Request bodies ──────────────────────────────────────────────────────────

class SlackWebhookRequest(BaseModel):
    webhook_url: str

    @field_validator("webhook_url")
    @classmethod
    def must_be_slack_webhook(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://hooks.slack.com/"):
            raise ValueError(
                "Must be a Slack Incoming Webhook URL "
                "(starts with https://hooks.slack.com/)"
            )
        return v


# ── Helpers ─────────────────────────────────────────────────────────────────

def _mask_url(url: str) -> str:
    """Show only the first 40 chars + *** to avoid leaking the full secret."""
    if not url:
        return ""
    return url[:40] + "***"


# ── Slack integration ───────────────────────────────────────────────────────

@router.get("/slack")
async def get_slack_config(user: CurrentUser):
    """Return whether Slack is configured and a masked preview of the URL."""
    db = get_db()
    doc = await db.users.find_one({"id": user.id}, {"_id": 0, "slack_webhook_url": 1})
    webhook_url = doc.get("slack_webhook_url", "") if doc else ""
    return {
        "configured": bool(webhook_url),
        "webhook_url_preview": _mask_url(webhook_url) if webhook_url else None,
    }


@router.put("/slack")
async def save_slack_webhook(body: SlackWebhookRequest, user: CurrentUser):
    """Save or update the user's Slack Incoming Webhook URL."""
    db = get_db()
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"slack_webhook_url": body.webhook_url}},
    )
    logger.info("Slack webhook saved for user %s", user.id)
    return {
        "message": "Slack webhook saved",
        "webhook_url_preview": _mask_url(body.webhook_url),
    }


@router.delete("/slack")
async def delete_slack_webhook(user: CurrentUser):
    """Remove the Slack integration."""
    db = get_db()
    await db.users.update_one(
        {"id": user.id},
        {"$unset": {"slack_webhook_url": ""}},
    )
    logger.info("Slack webhook removed for user %s", user.id)
    return {"message": "Slack integration removed"}


@router.post("/slack/test")
async def test_slack_webhook(user: CurrentUser):
    """
    Send a test Block Kit message to verify the webhook URL works.
    Uses a synthetic analysis payload so the user can see exactly
    what a real notification looks like.
    """
    db = get_db()
    doc = await db.users.find_one({"id": user.id}, {"_id": 0, "slack_webhook_url": 1})
    webhook_url = doc.get("slack_webhook_url", "") if doc else ""

    if not webhook_url:
        raise HTTPException(400, "No Slack webhook configured — save a webhook URL first")

    # Synthetic test analysis
    test_analysis = {
        "id":          "test-preview-001",
        "severity":    "high",
        "category":    "docker",
        "pattern_id":  "docker_pull_rate_limit",
        "engine":      "regex",
        "confidence":  0.97,
        "raw_log":     "ERROR: toomanyrequests: You have reached your pull rate limit.",
        "solutions": [
            {
                "title":            "Authenticate with Docker Hub",
                "explanation":      "Unauthenticated pulls are limited to 100/6h. Log in to increase your quota to 200/6h (free) or unlimited (Pro).",
                "command_template": "docker login -u <username>",
            }
        ],
    }

    ok = await send_slack_notification(webhook_url, test_analysis)
    if ok:
        return {"message": "Test notification sent — check your Slack channel!"}

    raise HTTPException(
        502,
        "Failed to deliver the test message. "
        "Check that the webhook URL is correct and the app is still installed.",
    )
