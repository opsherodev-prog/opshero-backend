"""
Slack Incoming Webhook Notifier.

Sends a rich Block Kit message to a user-configured Slack webhook URL
after each analysis completes.

Message anatomy:
  ┌─ severity color bar
  │  🔴  OpsHero — Critical Error Detected
  │      docker_pull_rate_limit
  ├─ meta: category · severity · confidence · engine
  ├─ top fix (title + command)
  └─ [View Full Analysis →] button

Supports both regex-matched patterns and LLM fallback results.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Severity → Slack attachment color (left border)
_SEVERITY_COLOR = {
    "critical": "#ff4444",
    "high":     "#ff8c00",
    "medium":   "#ffb020",
    "low":      "#00d4ff",
    "info":     "#94a3b8",
}

# Severity → emoji
_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🔵",
    "info":     "⚪",
}


def build_slack_payload(analysis: dict) -> dict:
    """
    Build a Slack Block Kit payload from an analysis result document.

    Works with both regex-matched analyses (pattern data) and
    LLM-fallback analyses (llm_* fields).
    """
    # ── Extract core fields ──────────────────────────────────────────────────
    severity   = (analysis.get("severity") or "medium").lower()
    category   = analysis.get("category") or "unknown"
    pattern_id = analysis.get("pattern_id") or analysis.get("llm_pattern_id") or "unknown"
    engine     = analysis.get("engine") or "regex"
    confidence = analysis.get("confidence") or analysis.get("llm_confidence") or 0.0

    color = _SEVERITY_COLOR.get(severity, "#94a3b8")
    emoji = _SEVERITY_EMOJI.get(severity, "⚪")

    # ── Solutions ────────────────────────────────────────────────────────────
    solutions: list[dict] = analysis.get("solutions") or []
    top_fix = solutions[0] if solutions else None

    # Truncate long log
    raw_log_preview = (analysis.get("raw_log") or "")[:400]

    # ── Analysis URL ─────────────────────────────────────────────────────────
    analysis_id = analysis.get("id") or analysis.get("analysis_id") or ""
    analysis_url = f"https://opshero.me/dashboard/analyses/{analysis_id}" if analysis_id else None

    # ── Build blocks ─────────────────────────────────────────────────────────
    blocks: list[dict] = []

    # Header
    blocks.append({
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": f"{emoji}  OpsHero — {severity.capitalize()} Error Detected",
            "emoji": True,
        },
    })

    # Pattern ID + category row
    blocks.append({
        "type": "section",
        "fields": [
            {
                "type": "mrkdwn",
                "text": f"*Pattern*\n`{pattern_id}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Category*\n{category.title()}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Severity*\n{severity.capitalize()}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Confidence*\n{int(confidence * 100)}%  ·  {engine}",
            },
        ],
    })

    # Top fix
    if top_fix:
        fix_title = top_fix.get("title") or "See full analysis"
        fix_expl  = (top_fix.get("explanation") or "")[:200]
        fix_cmd   = top_fix.get("command_template") or top_fix.get("command") or ""

        fix_text = f"*💡 Top Fix: {fix_title}*\n{fix_expl}"
        if fix_cmd:
            fix_text += f"\n```{fix_cmd}```"

        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": fix_text},
        })

    # Log preview (optional)
    if raw_log_preview:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Log snippet*\n```{raw_log_preview}```",
            },
        })

    # CTA button
    if analysis_url:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Full Analysis →", "emoji": True},
                    "url": analysis_url,
                    "style": "primary",
                }
            ],
        })

    # ── Wrap in attachment for color bar ─────────────────────────────────────
    return {
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
            }
        ]
    }


async def send_slack_notification(
    webhook_url: str,
    analysis: dict,
) -> bool:
    """
    POST a Block Kit message to the given Incoming Webhook URL.

    Returns True on success, False on failure. Never raises — designed
    to be called fire-and-forget from the analysis save path.
    """
    if not webhook_url or not webhook_url.startswith("https://hooks.slack.com/"):
        logger.debug("Slack notification skipped — invalid or missing webhook URL")
        return False

    payload = build_slack_payload(analysis)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(webhook_url, json=payload)

        if resp.status_code == 200 and resp.text == "ok":
            logger.debug("Slack notification sent for analysis %s", analysis.get("id"))
            return True

        logger.warning(
            "Slack notification failed: HTTP %s — %s",
            resp.status_code, resp.text[:200],
        )
        return False

    except Exception as exc:
        logger.warning("Slack notification error: %s", exc)
        return False
