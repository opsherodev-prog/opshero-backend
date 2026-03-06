"""
Admin email router — send transactional & broadcast emails to users.

Endpoints:
  GET  /admin/email/templates           — list available templates
  GET  /admin/email/preview/{template}  — return rendered HTML (for iframe preview)
  POST /admin/email/send                — send to specific address(es)
  POST /admin/email/broadcast           — send to a user segment (async background)
  GET  /admin/email/stats               — sent / failed counts from Redis
"""

import asyncio
import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pydantic import BaseModel, EmailStr

from database import get_db, get_redis
from deps.admin_auth import require_admin
from services.email import send_email, render_template, TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/email", tags=["admin-email"])

# Redis key prefix for stats
_STAT_KEY = "admin:email:stats"


# ── Schemas ───────────────────────────────────────────────────────────────────

class SendRequest(BaseModel):
    to: list[EmailStr]
    template: str
    subject: Optional[str] = None       # required for template="custom"
    body: Optional[str] = None          # required for template="custom"
    # Optional overrides for template vars
    username: Optional[str] = None

    model_config = {"extra": "allow"}   # forward extra fields as template vars


class BroadcastRequest(BaseModel):
    segment: Literal["all", "free", "pro", "team"]
    template: str
    subject: Optional[str] = None       # required for template="custom"
    body: Optional[str] = None          # required for template="custom"
    dry_run: bool = False               # if True, returns recipient count without sending


class SendResult(BaseModel):
    sent: int
    failed: int
    recipients: list[str]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _track(redis, success: bool) -> None:
    """Increment sent/failed counters in Redis (TTL 30 days)."""
    try:
        key = f"{_STAT_KEY}:{'sent' if success else 'failed'}"
        await redis.incr(key)
        await redis.expire(key, 60 * 60 * 24 * 30)
    except Exception:
        pass


def _build_template_vars(req: SendRequest | BroadcastRequest, recipient_email: str,
                         username: str | None) -> dict:
    extra = {}
    if hasattr(req, "model_extra") and req.model_extra:
        extra = req.model_extra
    return {
        "username": username or recipient_email.split("@")[0],
        "subject": req.subject or "",
        "body": req.body or "",
        **extra,
    }


async def _send_one(
    to: str,
    template: str,
    subject: str,
    vars_: dict,
    redis,
) -> bool:
    try:
        html = render_template(template, **vars_)
        ok = await send_email(to=to, subject=subject, html=html)
        await _track(redis, ok)
        return ok
    except Exception as exc:
        logger.error(f"Error rendering/sending to {to!r}: {exc}")
        await _track(redis, False)
        return False


async def _broadcast_task(
    emails: list[tuple[str, str | None]],   # (email, username)
    template: str,
    subject: str,
    body: str,
    redis,
) -> None:
    """Background task: send in chunks of 10 with small delay to avoid SMTP rate limits."""
    chunk = 10
    for i in range(0, len(emails), chunk):
        batch = emails[i: i + chunk]
        tasks = []
        for email, uname in batch:
            vars_ = {
                "username": uname or email.split("@")[0],
                "subject": subject,
                "body": body,
            }
            tasks.append(_send_one(email, template, subject, vars_, redis))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sent = sum(1 for r in results if r is True)
        logger.info(f"Broadcast batch {i // chunk + 1}: {sent}/{len(batch)} sent")
        if i + chunk < len(emails):
            await asyncio.sleep(1)   # 1s pause between batches


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(_=Depends(require_admin)):
    """Return the list of available email templates."""
    return [
        {"id": k, **v}
        for k, v in TEMPLATES.items()
    ]


@router.get("/preview/{template}")
async def preview_template(
    template: str,
    username: str = "alex",
    analyses_used: int = 41,
    limit: int = 50,
    subject: str = "Test subject",
    body: str = "This is a test message from OpsHero admin.",
    _=Depends(require_admin),
):
    """Return rendered HTML for a template — used by the admin preview iframe."""
    if template not in TEMPLATES and template not in ("admin_alert",):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Template {template!r} not found")
    try:
        pct = int(analyses_used / limit * 100) if limit else 0
        html = render_template(
            template,
            username=username,
            analyses_used=analyses_used,
            limit=limit,
            pct=pct,
            subject=subject,
            body=body,
        )
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post("/send", response_model=SendResult)
async def send_to_addresses(
    req: SendRequest,
    _=Depends(require_admin),
):
    """
    Send an email to one or more specific addresses.
    For template='custom', subject and body are required.
    """
    if template_requires_body(req.template) and not req.body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "body is required for template='custom'")
    if req.template not in TEMPLATES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown template: {req.template!r}")

    subject = _resolve_subject(req.template, req.subject)
    redis = get_redis()
    sent, failed = 0, []

    for addr in req.to:
        vars_ = _build_template_vars(req, addr, req.username)
        ok = await _send_one(addr, req.template, subject, vars_, redis)
        if ok:
            sent += 1
        else:
            failed.append(addr)

    return SendResult(sent=sent, failed=len(failed), recipients=list(req.to))


@router.post("/broadcast")
async def broadcast_email(
    req: BroadcastRequest,
    background_tasks: BackgroundTasks,
    _=Depends(require_admin),
):
    """
    Send an email to a segment of users (all / free / pro / team).
    Runs in background — returns immediately with recipient count.
    """
    if template_requires_body(req.template) and not req.body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "body is required for template='custom'")
    if req.template not in TEMPLATES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown template: {req.template!r}")

    db = get_db()

    # Build MongoDB filter by segment
    base_filter: dict = {"github_email": {"$ne": None, "$exists": True}}
    if req.segment != "all":
        base_filter["tier"] = req.segment

    cursor = db.users.find(base_filter, {"github_email": 1, "github_login": 1})
    docs = await cursor.to_list(length=None)
    recipients = [(d["github_email"], d.get("github_login")) for d in docs if d.get("github_email")]

    if req.dry_run:
        return {
            "dry_run": True,
            "segment": req.segment,
            "template": req.template,
            "recipient_count": len(recipients),
        }

    if not recipients:
        return {"message": "No users with email in this segment.", "sent": 0}

    subject = _resolve_subject(req.template, req.subject)
    redis = get_redis()

    background_tasks.add_task(
        _broadcast_task,
        recipients,
        req.template,
        subject,
        req.body or "",
        redis,
    )

    logger.info(
        f"Broadcast queued: segment={req.segment!r} template={req.template!r} "
        f"recipients={len(recipients)}"
    )

    return {
        "message": "Broadcast queued in background.",
        "segment": req.segment,
        "template": req.template,
        "recipient_count": len(recipients),
        "started_at": datetime.utcnow().isoformat(),
    }


@router.get("/stats")
async def email_stats(_=Depends(require_admin)):
    """Return cumulative sent/failed counts from Redis."""
    redis = get_redis()
    sent   = int(await redis.get(f"{_STAT_KEY}:sent")   or 0)
    failed = int(await redis.get(f"{_STAT_KEY}:failed") or 0)
    return {
        "sent_total":   sent,
        "failed_total": failed,
        "success_rate": round(sent / (sent + failed) * 100, 1) if (sent + failed) else None,
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def template_requires_body(template: str) -> bool:
    return template == "custom"


def _resolve_subject(template: str, override: str | None) -> str:
    if override:
        return override
    defaults = {
        "welcome":         "Welcome to OpsHero",
        "quota_warning":   "OpsHero — quota warning",
        "quota_exhausted": "OpsHero — monthly limit reached",
        "custom":          "Message from OpsHero",
        "admin_alert":     "OpsHero alert",
    }
    return defaults.get(template, "OpsHero notification")
