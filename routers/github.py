"""
GitHub proxy router — calls GitHub API on behalf of authenticated users
using their stored OAuth token.

Endpoints:
  GET /github/repos                             # list user repos with Actions enabled
  GET /github/repos/{owner}/{repo}/runs         # list workflow runs (failed only)
  GET /github/runs/{run_id}/logs                # download + return log text for a run
  GET /github/repos/{owner}/{repo}/runs/latest-failed  # shortcut: last failed run logs
"""

import asyncio
import io
import logging
import zipfile
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from database import get_db
from deps.auth import CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/github", tags=["github"])

_GH_API = "https://api.github.com"
_TIMEOUT = 20.0


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _gh_token(user: CurrentUser) -> str:
    """Return the user's stored GitHub token, or raise 401."""
    db = get_db()
    doc = await db.users.find_one({"id": user.id}, {"github_token": 1})
    token = (doc or {}).get("github_token")
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="No GitHub token stored. Re-authenticate with `opshero login`.",
        )
    return token


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _gh_get(token: str, url: str, params: Optional[dict] = None) -> dict | list:
    """Perform a GET against the GitHub API."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_gh_headers(token), params=params)
    if resp.status_code == 401:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "GitHub token expired or revoked. Re-authenticate.")
    if resp.status_code == 403:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "GitHub API rate limit or insufficient scope.")
    if resp.status_code == 404:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GitHub resource not found.")
    if resp.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"GitHub API error: {resp.status_code}")
    return resp.json()


async def _gh_get_safe(token: str, url: str, params: Optional[dict] = None) -> dict | list | None:
    """Like _gh_get but returns None instead of raising on error."""
    try:
        return await _gh_get(token, url, params)
    except Exception:
        return None


async def _build_synthetic_log(token: str, owner: str, repo: str, run_id: int) -> str:
    """
    Build a synthetic log from run metadata + job details + annotations.
    Used when the workflow failed before generating any runner logs
    (YAML syntax errors, missing secrets, invalid trigger, etc.)
    """
    lines: list[str] = []

    # ── Run details ───────────────────────────────────────────────────────────
    run = await _gh_get_safe(token, f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}")
    if isinstance(run, dict):
        lines += [
            "=== GitHub Actions Run Details ===",
            f"Workflow : {run.get('name', '?')}",
            f"Event    : {run.get('event', '?')}",
            f"Branch   : {run.get('head_branch', '?')}",
            f"Commit   : {str(run.get('head_sha', ''))[:8]}",
            f"Status   : {run.get('status', '?')}",
            f"Conclusion: {run.get('conclusion', '?')}",
            "",
        ]

    # ── Jobs + steps + annotations ────────────────────────────────────────────
    jobs_data = await _gh_get_safe(
        token, f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    )
    jobs = (jobs_data or {}).get("jobs", []) if isinstance(jobs_data, dict) else []

    for job in jobs:
        job_name       = job.get("name", "?")
        job_conclusion = job.get("conclusion") or job.get("status") or "?"
        lines.append(f"=== Job: {job_name}  [{job_conclusion.upper()}] ===")

        for step in job.get("steps", []):
            num        = step.get("number", "?")
            name       = step.get("name", "?")
            conclusion = step.get("conclusion") or step.get("status") or "—"
            prefix     = "FAILED" if conclusion == "failure" else conclusion
            lines.append(f"  step {num}: {prefix} — {name}")

        # Annotations (workflow YAML errors, lint errors, etc.)
        check_run_id = job.get("id")
        if check_run_id:
            annotations = await _gh_get_safe(
                token, f"{_GH_API}/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations"
            )
            if isinstance(annotations, list) and annotations:
                lines.append("  Annotations:")
                for ann in annotations:
                    level   = ann.get("annotation_level", "notice").upper()
                    message = ann.get("message", "")
                    path    = ann.get("path", "")
                    line_no = ann.get("start_line", "")
                    loc     = f"{path}:{line_no}" if path else ""
                    lines.append(f"    [{level}] {loc} {message}")
        lines.append("")

    if not lines:
        lines.append("No log data available for this run.")

    return "\n".join(lines)


# ── Response models ───────────────────────────────────────────────────────────

class RepoInfo(BaseModel):
    id: int
    full_name: str
    name: str
    owner: str
    private: bool
    default_branch: str
    has_actions: bool


class WorkflowRun(BaseModel):
    id: int
    name: Optional[str]
    workflow_name: Optional[str]
    head_branch: Optional[str]
    head_sha: str
    status: str
    conclusion: Optional[str]
    created_at: str
    updated_at: str
    html_url: str
    run_number: int


class RunLogsResponse(BaseModel):
    run_id: int
    repo: str
    logs: str  # concatenated plain-text logs
    truncated: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/repos", response_model=list[RepoInfo])
async def list_repos(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
):
    """
    List the user's GitHub repositories that have Actions workflows.
    Returns repos sorted by last push (GitHub default).
    """
    token = await _gh_token(user)
    data = await _gh_get(
        token,
        f"{_GH_API}/user/repos",
        params={
            "sort": "pushed",
            "per_page": per_page,
            "page": page,
            "type": "all",
        },
    )
    repos = []
    for r in (data if isinstance(data, list) else []):
        repos.append(RepoInfo(
            id=r["id"],
            full_name=r["full_name"],
            name=r["name"],
            owner=r["owner"]["login"],
            private=r.get("private", False),
            default_branch=r.get("default_branch", "main"),
            has_actions=not r.get("archived", False),  # approximation
        ))
    return repos


@router.get("/repos/{owner}/{repo}/runs", response_model=list[WorkflowRun])
async def list_workflow_runs(
    owner: str,
    repo: str,
    user: CurrentUser,
    status_filter: str = Query("failure", alias="status"),
    branch: Optional[str] = Query(None),
    per_page: int = Query(10, ge=1, le=50),
    page: int = Query(1, ge=1),
):
    """
    List workflow runs for a repo. Defaults to failed runs only.
    status: failure | success | in_progress | queued | waiting | all
    """
    token = await _gh_token(user)
    params: dict = {"per_page": per_page, "page": page}
    if status_filter != "all":
        # GitHub API uses 'status' for in_progress/queued/waiting,
        # and 'conclusion' doesn't exist as a filter — we filter client-side
        if status_filter in ("failure", "success", "cancelled", "skipped", "timed_out"):
            params["status"] = "completed"
        else:
            params["status"] = status_filter

    data = await _gh_get(token, f"{_GH_API}/repos/{owner}/{repo}/actions/runs", params=params)
    runs_raw = data.get("workflow_runs", []) if isinstance(data, dict) else []

    # Filter by conclusion client-side when needed
    if status_filter in ("failure", "success", "cancelled", "timed_out", "skipped"):
        runs_raw = [r for r in runs_raw if r.get("conclusion") == status_filter]

    if branch:
        runs_raw = [r for r in runs_raw if r.get("head_branch") == branch]

    runs = []
    for r in runs_raw:
        runs.append(WorkflowRun(
            id=r["id"],
            name=r.get("display_title") or r.get("name"),
            workflow_name=r.get("name"),
            head_branch=r.get("head_branch"),
            head_sha=r.get("head_sha", ""),
            status=r.get("status", ""),
            conclusion=r.get("conclusion"),
            created_at=r.get("created_at", ""),
            updated_at=r.get("updated_at", ""),
            html_url=r.get("html_url", ""),
            run_number=r.get("run_number", 0),
        ))
    return runs


@router.get("/runs/{run_id}/logs", response_model=RunLogsResponse)
async def get_run_logs(
    run_id: int,
    user: CurrentUser,
    owner: str = Query(...),
    repo: str = Query(...),
    max_bytes: int = Query(500_000, ge=1000, le=5_000_000),
):
    """
    Download and extract logs for a workflow run.
    GitHub returns a ZIP of text files — we concatenate them.
    max_bytes: truncate combined log at this size (default 500 KB).
    """
    token = await _gh_token(user)

    # GitHub returns a 302 redirect to a time-limited pre-signed URL (S3/CDN).
    # We fetch the redirect URL manually so we don't send the Bearer token to S3
    # (sending auth headers to a pre-signed URL can cause a 400/SignatureDoesNotMatch).
    # Also retry once after a short delay: logs may not be archived immediately
    # after a run completes (GitHub needs ~5-10s to package them).
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        resp = await client.get(
            f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            headers=_gh_headers(token),
        )

    last_status = resp.status_code

    # Retry once after 6s if logs aren't ready yet (404 on a freshly-completed run)
    if last_status == 404:
        await asyncio.sleep(6)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(
                f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
                headers=_gh_headers(token),
            )
        last_status = resp.status_code

    if last_status in (301, 302, 303, 307, 308):
        # Follow the redirect manually WITHOUT auth headers (pre-signed URL)
        redirect_url = resp.headers.get("location", "")
        if not redirect_url:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub returned redirect with no Location header.")
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(redirect_url)
        last_status = resp.status_code

    if last_status == 410:
        raise HTTPException(status.HTTP_410_GONE, "Logs have expired for this run.")
    if last_status == 401:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "GitHub token expired or revoked.")
    if last_status == 403:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient GitHub scope to read logs.")
    if last_status == 404:
        # No runner logs — workflow likely failed before any steps ran (YAML error, missing secrets…)
        # Fall back to synthetic log built from run metadata + job details + annotations.
        synthetic = await _build_synthetic_log(token, owner, repo, run_id)
        return RunLogsResponse(
            run_id=run_id,
            repo=f"{owner}/{repo}",
            logs=synthetic,
            truncated=False,
        )
    if last_status != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"GitHub logs error: {last_status}")

    # Extract ZIP in-memory
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile:
        # Sometimes GitHub returns plain text directly
        log_text = resp.text[:max_bytes]
        return RunLogsResponse(
            run_id=run_id,
            repo=f"{owner}/{repo}",
            logs=log_text,
            truncated=len(resp.text) > max_bytes,
        )

    # Sort files so the failed job logs come first
    names = sorted(zf.namelist())
    parts: list[str] = []
    total = 0
    truncated = False

    for name in names:
        if total >= max_bytes:
            truncated = True
            break
        try:
            raw = zf.read(name)
        except Exception:
            continue
        text = raw.decode("utf-8", errors="replace")
        remaining = max_bytes - total
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        parts.append(f"=== {name} ===\n{text}\n")
        total += len(text)

    combined = "\n".join(parts)
    return RunLogsResponse(
        run_id=run_id,
        repo=f"{owner}/{repo}",
        logs=combined,
        truncated=truncated,
    )


@router.post("/runs/{run_id}/rerun")
async def rerun_workflow(
    run_id: int,
    user: CurrentUser,
    owner: str = Query(...),
    repo: str = Query(...),
    failed_only: bool = Query(False, description="Re-run only failed jobs"),
):
    """
    Re-trigger a GitHub Actions workflow run.
    Uses the user's stored GitHub token.
    """
    token = await _gh_token(user)

    # Choose endpoint: rerun all jobs or only failed jobs
    if failed_only:
        url = f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs"
    else:
        url = f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/rerun"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_gh_headers(token))

    if resp.status_code == 403:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Insufficient GitHub permissions. Make sure your token has 'repo' scope.",
        )
    if resp.status_code == 404:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Run #{run_id} not found in {owner}/{repo}.",
        )
    if resp.status_code == 409:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This run is already in progress or cannot be re-run.",
        )
    if resp.status_code not in (201, 204):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"GitHub rerun failed: HTTP {resp.status_code}",
        )

    return {
        "ok": True,
        "run_id": run_id,
        "repo": f"{owner}/{repo}",
        "failed_only": failed_only,
        "message": f"Re-run triggered for run #{run_id}",
    }


@router.get("/repos/{owner}/{repo}/runs/latest-failed", response_model=RunLogsResponse)async def latest_failed_run_logs(
    owner: str,
    repo: str,
    user: CurrentUser,
    branch: Optional[str] = Query(None),
):
    """
    Shortcut: find the most recent failed run and return its logs.
    Used by the CLI when no run ID is specified.
    """
    token = await _gh_token(user)
    params: dict = {"status": "completed", "per_page": 20}
    if branch:
        params["branch"] = branch

    data = await _gh_get(token, f"{_GH_API}/repos/{owner}/{repo}/actions/runs", params=params)
    runs_raw = data.get("workflow_runs", []) if isinstance(data, dict) else []

    # Find first failed
    failed = next((r for r in runs_raw if r.get("conclusion") == "failure"), None)
    if not failed:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No failed runs found in {owner}/{repo}" + (f" on branch '{branch}'" if branch else "") + ".",
        )

    run_id = failed["id"]

    # Fetch log redirect URL from GitHub (don't follow — avoid sending auth header to S3)
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        resp = await client.get(
            f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
            headers=_gh_headers(token),
        )

    # Retry once after 6s if logs aren't ready yet
    if resp.status_code == 404:
        await asyncio.sleep(6)
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(
                f"{_GH_API}/repos/{owner}/{repo}/actions/runs/{run_id}/logs",
                headers=_gh_headers(token),
            )

    if resp.status_code in (301, 302, 303, 307, 308):
        redirect_url = resp.headers.get("location", "")
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(redirect_url)

    if resp.status_code == 404:
        synthetic = await _build_synthetic_log(token, owner, repo, run_id)
        return RunLogsResponse(
            run_id=run_id,
            repo=f"{owner}/{repo}",
            logs=synthetic,
            truncated=False,
        )
    if resp.status_code != 200:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Could not fetch logs for run #{run_id}: HTTP {resp.status_code}",
        )

    max_bytes = 500_000
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = sorted(zf.namelist())
        parts: list[str] = []
        total = 0
        truncated = False
        for name in names:
            if total >= max_bytes:
                truncated = True
                break
            try:
                raw = zf.read(name)
            except Exception:
                continue
            text = raw.decode("utf-8", errors="replace")
            remaining = max_bytes - total
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            parts.append(f"=== {name} ===\n{text}\n")
            total += len(text)
        combined = "\n".join(parts)
    except zipfile.BadZipFile:
        combined = resp.text[:max_bytes]
        truncated = len(resp.text) > max_bytes

    return RunLogsResponse(
        run_id=run_id,
        repo=f"{owner}/{repo}",
        logs=combined,
        truncated=truncated,
    )
