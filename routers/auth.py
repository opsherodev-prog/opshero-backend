"""
Authentication router — GitHub OAuth (web flow + device flow) + JWT management.
"""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, status, BackgroundTasks
from pydantic import BaseModel

from config import settings
from database import get_db, get_redis
from models.user import User, UserPublic, TokenPair

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# ── JWT helpers ───────────────────────────────────────────────────────────────

from jose import jwt as jose_jwt


def _create_tokens(user_id: str) -> TokenPair:
    """Create an access + refresh token pair for a user."""
    jti = str(uuid4())
    now = datetime.utcnow()

    access_payload = {
        "sub": user_id,
        "jti": jti,
        "scope": "user",
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    access_token = jose_jwt.encode(
        access_payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    refresh_jti = str(uuid4())
    refresh_payload = {
        "sub": user_id,
        "jti": refresh_jti,
        "scope": "refresh",
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_refresh_expire_days),
    }
    refresh_token = jose_jwt.encode(
        refresh_payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_expire_hours * 3600,
    )


async def _get_or_create_user(
    github_data: dict, 
    github_token: Optional[str] = None,
    background_tasks: Optional[BackgroundTasks] = None
) -> User:
    """Upsert a user from GitHub profile data."""
    db = get_db()
    github_id = int(github_data["id"])

    existing = await db.users.find_one({"github_id": github_id})
    now = datetime.utcnow()

    if existing:
        # Update profile fields that may have changed
        update_fields: dict = {
            "github_login": github_data.get("login", existing.get("github_login")),
            "github_avatar_url": github_data.get("avatar_url"),
            "github_name": github_data.get("name"),
            "last_login_at": now,
        }
        if github_token:
            update_fields["github_token"] = github_token
        
        # Update email if we fetched it and it's different from stored value
        new_email = github_data.get("email")
        if new_email and new_email != existing.get("github_email"):
            update_fields["github_email"] = new_email
            logger.info(f"Updated email for {github_data.get('login')}: {new_email}")
        
        await db.users.update_one(
            {"github_id": github_id},
            {"$set": update_fields},
        )
        updated_user = User(**{**existing, **update_fields})
        
        # Send welcome email if this is first time we got their email
        if new_email and not existing.get("github_email") and background_tasks:
            from services.email import send_welcome_email
            logger.info(f"Sending delayed welcome email to {new_email}")
            background_tasks.add_task(
                send_welcome_email,
                to=new_email,
                username=updated_user.github_login,
            )
        
        return updated_user

    # Create new user
    user = User(
        github_id=github_id,
        github_login=github_data["login"],
        github_email=github_data.get("email"),
        github_avatar_url=github_data.get("avatar_url"),
        github_name=github_data.get("name"),
        github_token=github_token,
        created_at=now,
        last_login_at=now,
    )
    await db.users.insert_one(user.model_dump())
    logger.info(f"New user created: {user.github_login} ({user.id})")

    # Send welcome email via background task
    if user.github_email and background_tasks:
        from services.email import send_welcome_email
        background_tasks.add_task(
            send_welcome_email,
            to=user.github_email,
            username=user.github_login,
        )

    return user


# ── Web OAuth flow ────────────────────────────────────────────────────────────

class OAuthCallbackRequest(BaseModel):
    code: str
    state: Optional[str] = None


@router.post("/github/callback", response_model=TokenPair)
async def github_oauth_callback(body: OAuthCallbackRequest, background_tasks: BackgroundTasks):
    """
    Exchange GitHub OAuth code for OpsHero JWT tokens.
    Called by the dashboard after redirect from GitHub.
    """
    # 1. Exchange code for GitHub access token
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": body.code,
                "redirect_uri": settings.github_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )

    if token_resp.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "GitHub token exchange failed")

    token_data = token_resp.json()
    if "error" in token_data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, token_data.get("error_description", "OAuth error"))

    gh_access_token = token_data["access_token"]

    # 2. Fetch GitHub user profile
    async with httpx.AsyncClient(timeout=10.0) as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {gh_access_token}",
                "Accept": "application/vnd.github+json",
            },
        )

    if user_resp.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Failed to fetch GitHub profile")

    github_data = user_resp.json()

    # 2b. Fetch user emails if not present (GitHub doesn't always return email in /user)
    if not github_data.get("email"):
        logger.info(f"No email in /user response for {github_data.get('login')}, fetching from /user/emails")
        async with httpx.AsyncClient(timeout=10.0) as client:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={
                    "Authorization": f"Bearer {gh_access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if emails_resp.status_code == 200:
                emails = emails_resp.json()
                logger.info(f"Fetched {len(emails)} emails from GitHub for {github_data.get('login')}")
                # Find primary verified email
                primary_email = next(
                    (e["email"] for e in emails if e.get("primary") and e.get("verified")),
                    None
                )
                if primary_email:
                    github_data["email"] = primary_email
                    logger.info(f"✅ Set primary email for {github_data.get('login')}: {primary_email}")
                else:
                    logger.warning(f"No primary verified email found for {github_data.get('login')}")
            else:
                logger.error(f"Failed to fetch emails from GitHub: {emails_resp.status_code}")

    # 3. Get or create user
    user = await _get_or_create_user(github_data, github_token=gh_access_token, background_tasks=background_tasks)

    # 4. Return OpsHero tokens
    return _create_tokens(user.id)


# ── Device flow (CLI) ─────────────────────────────────────────────────────────

class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@router.post("/github/device/code", response_model=DeviceCodeResponse)
async def github_device_code():
    """
    Step 1 of GitHub Device Flow — request a device + user code.
    CLI displays user_code and opens verification_uri in the browser.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://github.com/login/device/code",
            json={
                "client_id": settings.github_client_id,
                "scope": "read:user user:email repo",
            },
            headers={"Accept": "application/json"},
        )

    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "error" in data:
        gh_error = data.get("error", "")
        gh_desc  = data.get("error_description", resp.text)
        logger.error(
            "GitHub device/code failed: status=%s error=%r desc=%r",
            resp.status_code, gh_error, gh_desc,
        )
        if gh_error in ("not_supported", "device_flow_disabled"):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Device Flow is not enabled on this GitHub OAuth App. "
                "Go to GitHub → Settings → Developer settings → OAuth Apps → [your app] → "
                "check 'Enable Device Flow', then save.",
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"GitHub device code request failed: {gh_error or resp.status_code} — {gh_desc}",
        )

    return DeviceCodeResponse(**data)


class DevicePollRequest(BaseModel):
    device_code: str


@router.post("/github/device/poll", response_model=TokenPair)
async def github_device_poll(body: DevicePollRequest):
    """
    Step 2 of GitHub Device Flow — poll for token after user authorizes.
    CLI polls this endpoint every `interval` seconds.
    Returns 202 if still pending, 200 with tokens on success.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": settings.github_client_id,
                "device_code": body.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )

    data = resp.json()
    error = data.get("error")

    if error == "authorization_pending":
        raise HTTPException(status.HTTP_202_ACCEPTED, "Authorization pending")
    if error == "slow_down":
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Polling too fast")
    if error == "expired_token":
        raise HTTPException(status.HTTP_410_GONE, "Device code expired — start again")
    if error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, data.get("error_description", error))

    gh_access_token = data.get("access_token")
    if not gh_access_token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No access token in response")

    async with httpx.AsyncClient(timeout=10.0) as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {gh_access_token}",
                "Accept": "application/vnd.github+json",
            },
        )

    github_data = user_resp.json()
    user = await _get_or_create_user(github_data, github_token=gh_access_token)
    return _create_tokens(user.id)


# ── Token management ──────────────────────────────────────────────────────────

class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenPair)
async def refresh_token(body: RefreshRequest):
    """Exchange a refresh token for a new token pair."""
    try:
        payload = jose_jwt.decode(
            body.refresh_token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    if payload.get("scope") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not a refresh token")

    # Blacklist the used refresh token (one-time use)
    redis = get_redis()
    jti = payload.get("jti")
    if jti:
        if await redis.exists(f"jti:blacklist:{jti}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token already used")
        exp_ttl = max(0, int(payload.get("exp", 0) - datetime.utcnow().timestamp()))
        await redis.setex(f"jti:blacklist:{jti}", exp_ttl, "1")

    user_id = payload["sub"]
    db = get_db()
    user_doc = await db.users.find_one({"id": user_id})
    if not user_doc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")

    return _create_tokens(user_id)


class LogoutRequest(BaseModel):
    access_token: str


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest):
    """Invalidate the access token by adding its JTI to the blacklist."""
    try:
        payload = jose_jwt.decode(
            body.access_token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        jti = payload.get("jti")
        if jti:
            redis = get_redis()
            exp_ttl = max(0, int(payload.get("exp", 0) - datetime.utcnow().timestamp()))
            await redis.setex(f"jti:blacklist:{jti}", exp_ttl + 60, "1")
    except Exception:
        pass  # Ignore invalid tokens on logout — always succeed


from deps.auth import CurrentUser


@router.get("/me", response_model=UserPublic)
async def get_me(user: CurrentUser):
    """Return the current user's public profile."""
    return UserPublic(
        id=user.id,
        github_login=user.github_login,
        github_avatar_url=user.github_avatar_url,
        github_name=user.github_name,
        tier=user.tier,
        team_id=user.team_id,
        analyses_this_month=user.analyses_this_month,
        created_at=user.created_at,
        is_suspended=user.is_suspended,
        suspended_reason=user.suspended_reason,
    )
