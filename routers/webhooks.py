"""
GitHub Webhook endpoint.

POST /webhooks/github

Handles:
  - pull_request (action=closed, merged=true) → auto-seed pattern files
  - push (to main/master) → auto-seed changed pattern files

Pipeline on merge:
  1. Verify HMAC-SHA256 signature (X-Hub-Signature-256 header)
  2. Find changed files under shared/patterns/*.json (excluding index.json)
  3. Fetch each file from GitHub API
  4. Validate against schema v2.0.0 (pattern_validator)
  5. Upsert valid patterns into MongoDB
  6. Publish to Redis pubsub → PatternIndex hot-reload (zero downtime)
  7. Record community_contribution audit entry

Environment variables required:
  GITHUB_WEBHOOK_SECRET     — secret configured in GitHub repo webhook settings
  GITHUB_PATTERNS_TOKEN     — GitHub PAT with read access to the patterns repo
                              (falls back to GITHUB_CLIENT_SECRET if not set)
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from uuid import uuid4

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from config import settings
from database import get_db, get_redis
from engine.pattern_validator import validate_pattern_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PATTERN_PATH_PREFIX = "shared/patterns/"
REDIS_HOT_RELOAD_CHANNEL = "pattern_updates"


# ── HMAC signature verification ───────────────────────────────────────────────

def _verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub's HMAC-SHA256 webhook signature."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _gh_token() -> str:
    """Return the GitHub token to use for API calls."""
    return settings.github_patterns_token or settings.github_client_secret or ""


async def _gh_get(url: str, params: dict | None = None) -> dict | list | None:
    """Perform a GET request to the GitHub API."""
    token = _gh_token()
    headers = {
        "Accept": "application/vnd.github.v3+json",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            logger.warning("GitHub API %s → %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        logger.warning("GitHub API request failed (%s): %s", url, exc)
        return None


async def _fetch_file_raw(owner: str, repo: str, path: str, ref: str) -> dict | None:
    """Fetch raw JSON content of a file from a GitHub repo."""
    token = _gh_token()
    headers = {
        "Accept": "application/vnd.github.v3.raw",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params={"ref": ref})
        if resp.status_code != 200:
            logger.warning("Fetch %s@%s → %s", path, ref, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", path, exc)
        return None


async def _get_pr_pattern_files(owner: str, repo: str, pr_number: int) -> list[str]:
    """Return list of pattern file paths changed in a PR (excluding index.json)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    data = await _gh_get(url)
    if not isinstance(data, list):
        return []
    return [
        f["filename"]
        for f in data
        if (
            isinstance(f, dict)
            and f.get("filename", "").startswith(PATTERN_PATH_PREFIX)
            and f.get("filename", "").endswith(".json")
            and not f.get("filename", "").endswith("index.json")
            and f.get("status") in ("added", "modified")
        )
    ]


# ── Core: upsert + hot-reload ─────────────────────────────────────────────────

async def _upsert_and_hot_reload(patterns: list[dict]) -> int:
    """
    Upsert patterns into MongoDB and publish Redis events for hot-reload.
    Returns the number of upserted/modified patterns.
    """
    db = get_db()
    redis = get_redis()
    count = 0

    for pattern in patterns:
        pid = pattern["pattern_id"]
        result = await db.patterns.update_one(
            {"pattern_id": pid},
            {"$set": {**pattern, "source": "community_pr", "status": "active"}},
            upsert=True,
        )
        if result.upserted_id or result.modified_count:
            count += 1
            try:
                await redis.publish(
                    REDIS_HOT_RELOAD_CHANNEL,
                    json.dumps({"action": "upsert", "pattern_id": pid}),
                )
            except Exception as exc:
                logger.warning("Redis publish failed for %s: %s", pid, exc)

    return count


# ── Main webhook handler ───────────────────────────────────────────────────────

@router.post("/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    """
    Receive and process GitHub webhook events.
    Validates HMAC signature, then routes by event type.
    """
    if not settings.github_webhook_secret:
        logger.warning("Webhook received but GITHUB_WEBHOOK_SECRET not configured")
        raise HTTPException(503, "Webhook not configured on this server")

    payload_bytes = await request.body()

    # ── Signature verification ─────────────────────────────────────────────
    if not _verify_signature(payload_bytes, x_hub_signature_256, settings.github_webhook_secret):
        logger.warning("Webhook: invalid HMAC signature from %s", request.client)
        raise HTTPException(401, "Invalid webhook signature")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(400, "Malformed JSON payload")

    # ── Route by event ─────────────────────────────────────────────────────
    if x_github_event == "pull_request":
        await _handle_pr_event(payload)
    elif x_github_event == "push":
        await _handle_push_event(payload)
    elif x_github_event == "ping":
        logger.info("GitHub webhook ping received — webhook configured successfully")
    else:
        logger.debug("Unhandled GitHub event: %s", x_github_event)

    return {"status": "ok", "event": x_github_event}


# ── Event handlers ────────────────────────────────────────────────────────────

async def _handle_pr_event(payload: dict) -> None:
    """Process pull_request events — only merged PRs that touch pattern files."""
    action = payload.get("action")
    pr = payload.get("pull_request", {})

    if action != "closed" or not pr.get("merged"):
        return  # Ignore opened, synchronize, closed-but-not-merged

    repo_data = payload.get("repository", {})
    owner, _, repo = repo_data.get("full_name", "/").partition("/")
    pr_number: int = pr.get("number", 0)
    merge_sha: str = pr.get("merge_commit_sha") or pr.get("head", {}).get("sha", "HEAD")
    pr_url: str = pr.get("html_url", "")
    author: str = pr.get("user", {}).get("login", "unknown")

    changed_files = await _get_pr_pattern_files(owner, repo, pr_number)
    if not changed_files:
        logger.info("PR #%s merged — no pattern files changed", pr_number)
        return

    logger.info("PR #%s merged — processing %d pattern file(s)", pr_number, len(changed_files))

    total_upserted = 0
    all_errors: list[str] = []

    for file_path in changed_files:
        file_data = await _fetch_file_raw(owner, repo, file_path, merge_sha)
        if file_data is None:
            all_errors.append(f"Could not fetch {file_path}")
            continue

        valid_patterns, errors = validate_pattern_file(file_data)
        all_errors.extend([f"{file_path}: {e}" for e in errors])

        if valid_patterns:
            n = await _upsert_and_hot_reload(valid_patterns)
            total_upserted += n
            logger.info("  %s → %d patterns upserted", file_path, n)

    # ── Audit trail ────────────────────────────────────────────────────────
    db = get_db()
    await db.community_contributions.insert_one({
        "id": str(uuid4()),
        "type": "github_pr",
        "status": "approved",
        "author_github": author,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "files_changed": changed_files,
        "patterns_upserted": total_upserted,
        "validation_errors": all_errors,
        "ci_passed": True,
        "reviewed_by": "auto_webhook",
        "review_notes": f"Auto-merged via GitHub PR #{pr_number}",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    })

    logger.info(
        "PR #%s: %d patterns upserted, %d validation error(s)",
        pr_number, total_upserted, len(all_errors),
    )


async def _handle_push_event(payload: dict) -> None:
    """Process push events to main/master that change pattern files."""
    ref = payload.get("ref", "")
    if ref not in ("refs/heads/main", "refs/heads/master"):
        return

    sha = payload.get("after", "HEAD")
    repo_data = payload.get("repository", {})
    owner, _, repo = repo_data.get("full_name", "/").partition("/")
    pusher = payload.get("pusher", {}).get("name", "unknown")

    # Collect all added/modified files across commits
    touched: set[str] = set()
    for commit in payload.get("commits", []):
        for f in commit.get("added", []) + commit.get("modified", []):
            if (
                isinstance(f, str)
                and f.startswith(PATTERN_PATH_PREFIX)
                and f.endswith(".json")
                and not f.endswith("index.json")
            ):
                touched.add(f)

    if not touched:
        return

    logger.info("Push to %s by %s — processing %d pattern file(s)", ref, pusher, len(touched))

    for file_path in touched:
        file_data = await _fetch_file_raw(owner, repo, file_path, sha)
        if file_data is None:
            continue

        valid_patterns, errors = validate_pattern_file(file_data)
        if errors:
            logger.warning("  %s: %d validation error(s): %s", file_path, len(errors), errors[:3])
        if valid_patterns:
            n = await _upsert_and_hot_reload(valid_patterns)
            logger.info("  %s → %d patterns upserted", file_path, n)
