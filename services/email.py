"""
OpsHero — Email service (Gmail SMTP).
Uses smtplib via asyncio.to_thread — no extra dependency required.
Never raises — logs errors so a failed email never breaks the request flow.
"""

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import settings

logger = logging.getLogger(__name__)

# ── Core send ─────────────────────────────────────────────────────────────────

def _send_sync(to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = settings.email_from
    msg["To"]      = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html",  "utf-8"))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.smtp_user, to, msg.as_string())


async def send_email(to: str, subject: str, html: str, text: str = "") -> bool:
    """Send an email. Returns True on success, False on failure. Never raises."""
    logger.info(f"[EMAIL] Attempting to send email to {to!r} with subject {subject!r}")
    
    if not settings.email_enabled:
        logger.debug(f"Email disabled — skipping send to {to!r}")
        return False
    if not settings.smtp_user or not settings.smtp_password:
        logger.warning("SMTP credentials not configured — skipping email")
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, html, text or _strip_html(html))
        logger.info(f"✅ Email sent successfully → {to!r}  subject={subject!r}")
        return True
    except Exception as exc:
        logger.error(f"❌ Email failed → {to!r}: {exc}")
        return False


# ── Public template functions ─────────────────────────────────────────────────

async def send_welcome_email(to: str, username: str) -> bool:
    return await send_email(
        to=to,
        subject="Welcome to OpsHero",
        html=render_template("welcome", username=username),
        text=(
            f"Hi {username},\n\nYour OpsHero account is active. "
            "You have 50 free analyses this month.\n\n"
            "Get started:\n  pip install opshero\n  opshero login\n"
            "  cat build.log | opshero analyze -\n\n"
            f"Dashboard: {_BASE_URL}/dashboard\n\n— OpsHero"
        ),
    )


async def send_quota_warning_email(
    to: str, username: str, analyses_used: int, limit: int
) -> bool:
    pct = int(analyses_used / limit * 100)
    return await send_email(
        to=to,
        subject=f"OpsHero — {pct}% of your monthly quota used",
        html=render_template("quota_warning", username=username,
                             analyses_used=analyses_used, limit=limit, pct=pct),
        text=(
            f"Hi {username},\n\nYou've used {analyses_used}/{limit} analyses "
            f"this month ({pct}%).\n\nUpgrade to Pro for unlimited analyses:\n"
            f"{_BASE_URL}/dashboard\n\n— OpsHero"
        ),
    )


async def send_quota_exhausted_email(to: str, username: str, limit: int) -> bool:
    return await send_email(
        to=to,
        subject="OpsHero — monthly analysis limit reached",
        html=render_template("quota_exhausted", username=username, limit=limit),
        text=(
            f"Hi {username},\n\nYou've used all {limit} free analyses for this month.\n\n"
            f"Upgrade to Pro: {_BASE_URL}/dashboard\n\n"
            "Your free quota resets on the 1st of next month.\n\n— OpsHero"
        ),
    )


async def send_custom_email(to: str, subject: str, body_text: str) -> bool:
    """Admin-composed custom email."""
    return await send_email(
        to=to,
        subject=subject,
        html=render_template("custom", subject=subject, body=body_text),
        text=body_text,
    )


async def send_admin_alert_email(to: str, subject: str, body: str) -> bool:
    return await send_email(
        to=to,
        subject=subject,
        html=render_template("admin_alert", subject=subject, body=body),
        text=body,
    )


async def send_suspension_notification(to: str, username: str, reason: str) -> bool:
    """Notify user that their account has been suspended."""
    logger.info(f"[EMAIL] Preparing suspension notification for {username} ({to})")
    return await send_email(
        to=to,
        subject="OpsHero — Account Suspended",
        html=render_template("suspension", username=username, reason=reason),
        text=(
            f"Hi {username},\n\n"
            f"Your OpsHero account has been suspended.\n\n"
            f"Reason: {reason}\n\n"
            f"If you believe this is a mistake, please contact support.\n\n"
            f"— OpsHero Team"
        ),
    )


async def send_reactivation_notification(to: str, username: str) -> bool:
    """Notify user that their account has been reactivated."""
    logger.info(f"[EMAIL] Preparing reactivation notification for {username} ({to})")
    return await send_email(
        to=to,
        subject="OpsHero — Account Reactivated",
        html=render_template("reactivation", username=username),
        text=(
            f"Hi {username},\n\n"
            f"Good news! Your OpsHero account has been reactivated.\n\n"
            f"You can now log in and use all features normally.\n\n"
            f"Dashboard: {_BASE_URL}/dashboard\n\n"
            f"— OpsHero Team"
        ),
    )


# ── Template registry ─────────────────────────────────────────────────────────

TEMPLATES: dict[str, dict] = {
    "welcome": {
        "label": "Welcome",
        "description": "Sent when a new user creates an account.",
        "vars": ["username"],
    },
    "quota_warning": {
        "label": "Quota Warning (80%)",
        "description": "Sent when a user reaches 80% of their monthly quota.",
        "vars": ["username", "analyses_used", "limit", "pct"],
    },
    "quota_exhausted": {
        "label": "Quota Exhausted (100%)",
        "description": "Sent when a user hits their monthly limit.",
        "vars": ["username", "limit"],
    },
    "suspension": {
        "label": "Account Suspended",
        "description": "Sent when an admin suspends a user account.",
        "vars": ["username", "reason"],
    },
    "reactivation": {
        "label": "Account Reactivated",
        "description": "Sent when an admin reactivates a suspended account.",
        "vars": ["username"],
    },
    "custom": {
        "label": "Custom Message",
        "description": "Compose a free-form email with a custom subject and body.",
        "vars": ["subject", "body"],
    },
}


def render_template(template: str, **kw) -> str:
    """Public — returns full HTML string. Used for preview + sending."""
    renderers = {
        "welcome":         _tpl_welcome,
        "quota_warning":   _tpl_quota_warning,
        "quota_exhausted": _tpl_quota_exhausted,
        "suspension":      _tpl_suspension,
        "reactivation":    _tpl_reactivation,
        "custom":          _tpl_custom,
        "admin_alert":     _tpl_admin_alert,
    }
    fn = renderers.get(template)
    if not fn:
        raise ValueError(f"Unknown email template: {template!r}")
    return fn(**kw)


# ── HTML templates ────────────────────────────────────────────────────────────

_LOGO_SVG = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
    'stroke="#00d4ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="4 17 10 11 4 5"/>'
    '<line x1="12" y1="19" x2="20" y2="19"/>'
    '</svg>'
)

_BASE_URL = "http://localhost:3000"

_CSS = """
    /* ─── Reset ─── */
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#040811;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
         color:#94a3b8;-webkit-font-smoothing:antialiased}
    a{color:inherit;text-decoration:none}
    img{border:0;display:block}

    /* ─── Layout ─── */
    .outer{background:#040811;padding:48px 16px 64px}
    .inner{max-width:560px;margin:0 auto}

    /* ─── Logo ─── */
    .logo-row{display:flex;align-items:center;gap:10px;margin-bottom:32px}
    .logo-icon{width:38px;height:38px;min-width:38px;
      background:rgba(0,212,255,.10);border:1px solid rgba(0,212,255,.22);
      border-radius:10px;display:inline-flex;align-items:center;justify-content:center}
    .logo-name{font-size:17px;font-weight:700;color:#f1f5f9;letter-spacing:-.3px}
    .logo-badge{font-size:10px;font-family:'Courier New',monospace;
      color:#00d4ff;background:rgba(0,212,255,.07);
      border:1px solid rgba(0,212,255,.16);border-radius:5px;
      padding:2px 6px;letter-spacing:.05em;margin-left:2px}

    /* ─── Card ─── */
    .card{background:#0a0f1c;border:1px solid #1a2235;border-radius:16px;overflow:hidden}
    .card-top{height:2px;background:linear-gradient(90deg,transparent,#00d4ff 40%,#00ff87 70%,transparent)}
    .card-body{padding:40px}

    /* ─── Typography ─── */
    h1{font-size:23px;font-weight:700;color:#f1f5f9;letter-spacing:-.4px;line-height:1.25;margin-bottom:14px}
    p{font-size:15px;line-height:1.7;color:#94a3b8;margin-bottom:14px}
    strong{color:#e2e8f0;font-weight:600}
    .muted{font-size:13px;color:#475569}

    /* ─── Code block ─── */
    .code{background:#02050d;border:1px solid #1a2235;border-radius:10px;
      padding:16px 20px;margin:20px 0;
      font-family:'Courier New',Courier,monospace;font-size:12.5px;line-height:1.85}
    .cp{color:#475569}
    .cc{color:#00ff87}
    .cd{color:#374151}
    .co{color:#00d4ff}

    /* ─── CTA ─── */
    .btn-wrap{margin:28px 0 20px}
    .btn{display:inline-block;
      background:linear-gradient(135deg,#00d4ff 0%,#00ff87 100%);
      color:#040811 !important;font-weight:700;font-size:14px;
      padding:13px 32px;border-radius:10px;letter-spacing:.01em}

    /* ─── Stats ─── */
    .stats{display:flex;gap:10px;margin:22px 0;flex-wrap:wrap}
    .stat{flex:1;min-width:140px;background:#0f172a;border:1px solid #1a2235;
      border-radius:10px;padding:14px 16px;text-align:center}
    .stat-val{font-size:20px;font-weight:700;color:#00d4ff;display:block;letter-spacing:-.5px}
    .stat-lbl{font-size:11px;color:#475569;display:block;margin-top:3px}

    /* ─── Notice ─── */
    .notice{background:rgba(255,176,32,.06);border:1px solid rgba(255,176,32,.18);
      border-radius:10px;padding:14px 18px;margin:20px 0}
    .notice p{color:#ffb020;margin:0;font-size:14px}
    .notice-danger{background:rgba(255,68,68,.05);border-color:rgba(255,68,68,.18)}
    .notice-danger p{color:#ff6b6b}

    /* ─── Divider ─── */
    .div{height:1px;background:#1a2235;margin:28px 0}

    /* ─── Feature list ─── */
    .flist{list-style:none;margin:16px 0}
    .flist li{font-size:14px;color:#94a3b8;padding:5px 0}
    .flist .ic{color:#00ff87;margin-right:8px}

    /* ─── Footer ─── */
    .footer{margin-top:32px;text-align:center;font-size:12px;color:#334155;line-height:1.7}
    .footer a{color:#475569}

    /* ─── Mobile ─── */
    @media screen and (max-width:600px){
      .outer{padding:24px 10px 48px !important}
      .card-body{padding:24px 20px !important}
      h1{font-size:20px !important}
      p{font-size:14px !important}
      .stats{flex-direction:column !important}
      .stat{width:100% !important;min-width:unset !important}
      .btn{display:block !important;text-align:center !important;
           padding:14px 20px !important;width:100% !important}
      .btn-wrap{text-align:center !important}
      .code{font-size:11.5px !important;padding:14px 14px !important}
      .logo-name{font-size:15px !important}
    }
"""


def _shell(body_html: str, preheader: str = "") -> str:
    pre = (
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">'
        f'{preheader}{"&nbsp;" * 60}</div>'
        if preheader else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>OpsHero</title>
<style>{_CSS}</style>
</head>
<body>
{pre}
<div class="outer">
  <div class="inner">

    <div class="logo-row">
      <span class="logo-icon">{_LOGO_SVG}</span>
      <span class="logo-name">OpsHero</span>
      <span class="logo-badge">beta</span>
    </div>

    <div class="card">
      <div class="card-top"></div>
      <div class="card-body">
        {body_html}
      </div>
    </div>

    <div class="footer">
      <p>© 2026 OpsHero &nbsp;·&nbsp;
         <a href="{_BASE_URL}/privacy">Privacy</a> &nbsp;·&nbsp;
         <a href="{_BASE_URL}/terms">Terms</a></p>
      <p style="margin-top:6px">You received this because you have an OpsHero account.</p>
    </div>

  </div>
</div>
</body>
</html>"""


# ── Individual templates ───────────────────────────────────────────────────────

def _tpl_welcome(username: str = "there", **_) -> str:
    return _shell(f"""
      <h1>Welcome, {username}.</h1>
      <p>Your OpsHero account is active. You have
         <strong>50 free analyses</strong> this month — no credit card required.</p>

      <div class="stats">
        <div class="stat">
          <span class="stat-val">50</span>
          <span class="stat-lbl">Free analyses / mo</span>
        </div>
        <div class="stat">
          <span class="stat-val">56+</span>
          <span class="stat-lbl">Error patterns</span>
        </div>
        <div class="stat">
          <span class="stat-val">47ms</span>
          <span class="stat-lbl">Avg latency</span>
        </div>
      </div>

      <div class="div"></div>

      <p><strong>Analyze your first broken pipeline in 30 seconds:</strong></p>
      <div class="code">
        <div><span class="cp">$ </span><span class="cc">pip install opshero</span></div>
        <div><span class="cp">$ </span><span class="cc">opshero login</span></div>
        <div class="cd">&nbsp; Opening github.com/login/oauth…</div>
        <div class="co">&nbsp; ✓ Authenticated as @{username}</div>
        <div style="margin-top:8px"><span class="cp">$ </span><span class="cc">cat build.log | opshero analyze -</span></div>
        <div class="co">&nbsp; ✓ docker_missing_package — 94% — 47ms</div>
      </div>

      <p class="muted">The CLI and web dashboard share the same account — same quota, same history.</p>

      <div class="btn-wrap">
        <a href="{_BASE_URL}/dashboard" class="btn">Open Dashboard</a>
      </div>
    """, preheader="Your OpsHero account is ready — 50 free analyses this month.")


def _tpl_quota_warning(username: str = "there", analyses_used: int = 0,
                        limit: int = 50, pct: int = 0, **_) -> str:
    return _shell(f"""
      <h1>Quota warning</h1>
      <p>Hi <strong>{username}</strong>, you've used
         <strong>{analyses_used} of {limit} analyses</strong> this month ({pct}%).</p>

      <div class="notice">
        <p>At this pace you may reach your free limit before the month ends.</p>
      </div>

      <p>Upgrade to <strong>Pro</strong> for unlimited analyses, AI engine fallback,
         and offline sync — $19/month.</p>

      <ul class="flist">
        <li><span class="ic">✓</span>Unlimited analyses</li>
        <li><span class="ic">✓</span>AI engine for errors no pattern covers</li>
        <li><span class="ic">✓</span>Offline pattern sync (CLI)</li>
        <li><span class="ic">✓</span>REST API access</li>
      </ul>

      <div class="btn-wrap">
        <a href="{_BASE_URL}/dashboard" class="btn">Upgrade to Pro — $19/mo</a>
      </div>

      <p class="muted">Your free quota resets on the 1st of next month.</p>
    """, preheader=f"You've used {pct}% of your monthly OpsHero quota.")


def _tpl_quota_exhausted(username: str = "there", limit: int = 50, **_) -> str:
    return _shell(f"""
      <h1>Monthly limit reached</h1>
      <p>Hi <strong>{username}</strong>, you've used all
         <strong>{limit} free analyses</strong> for this month.</p>

      <div class="notice notice-danger">
        <p>New analyses are blocked until your quota resets on the 1st,
           or until you upgrade.</p>
      </div>

      <p>Pro gives you unlimited analyses every month, AI engine fallback,
         and no quota to worry about.</p>

      <div class="btn-wrap">
        <a href="{_BASE_URL}/dashboard" class="btn">Upgrade to Pro — $19/mo</a>
      </div>

      <div class="div"></div>
      <p class="muted">Free quota resets automatically on the 1st of each month.</p>
    """, preheader="You've reached your free OpsHero quota for this month.")


def _tpl_custom(subject: str = "", body: str = "", **_) -> str:
    safe_body = body.replace("\n", "<br>")
    return _shell(f"""
      <h1>{subject}</h1>
      <p style="white-space:pre-wrap">{safe_body}</p>
    """)


def _tpl_suspension(username: str = "there", reason: str = "", **_) -> str:
    return _shell(f"""
      <h1>Account Suspended</h1>
      <p>Hi <strong>{username}</strong>,</p>
      
      <div class="notice notice-danger">
        <p>Your OpsHero account has been suspended and you can no longer access the service.</p>
      </div>

      <p><strong>Reason:</strong></p>
      <div class="code" style="white-space:pre-wrap;color:#94a3b8">{reason}</div>

      <div class="div"></div>

      <p>If you believe this is a mistake or would like to appeal this decision,
         please contact our support team.</p>

      <p class="muted">This action was taken by an OpsHero administrator.</p>
    """, preheader="Your OpsHero account has been suspended.")


def _tpl_reactivation(username: str = "there", **_) -> str:
    return _shell(f"""
      <h1>Account Reactivated</h1>
      <p>Hi <strong>{username}</strong>,</p>
      
      <div class="notice">
        <p>Good news! Your OpsHero account has been reactivated.</p>
      </div>

      <p>You can now log in and use all features normally. Your analyses history
         and settings have been preserved.</p>

      <div class="btn-wrap">
        <a href="{_BASE_URL}/dashboard" class="btn">Open Dashboard</a>
      </div>

      <p class="muted">Welcome back! If you have any questions, feel free to reach out to support.</p>
    """, preheader="Your OpsHero account has been reactivated.")


def _tpl_admin_alert(subject: str = "", body: str = "", **_) -> str:
    return _shell(f"""
      <h1>{subject}</h1>
      <div class="code" style="white-space:pre-wrap;color:#94a3b8">{body}</div>
    """)


# ── Utility ───────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
