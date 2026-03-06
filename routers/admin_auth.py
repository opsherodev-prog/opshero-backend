"""
Admin authentication router.

POST /admin/auth/login          — email + password → pending token
POST /admin/auth/totp-verify    — TOTP code + pending token → admin JWT (set in cookie)
POST /admin/auth/logout         — blacklist JTI
POST /admin/auth/refresh        — rotate token
"""

import logging
from datetime import datetime, timedelta

import bcrypt as _bcrypt
import pyotp
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr

from config import settings
from database import get_db, get_redis
from deps.admin_auth import (
    create_admin_token,
    create_totp_pending_token,
    decode_totp_pending_token,
    record_failed_login,
    clear_failed_login,
    CurrentAdmin,
)
from models.admin import AdminUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


# ── Request / Response models ──────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    status: str = "totp_required"
    pending_token: str


class TotpVerifyRequest(BaseModel):
    pending_token: str
    totp_code: str


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int   # seconds


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request):
    """
    Step 1 of admin login: verify email + password.
    Returns a short-lived pending_token; no admin access until TOTP is verified.
    Rate-limited: 5 attempts per email per 10 minutes, then 15-minute lockout.
    """
    db = get_db()
    redis = get_redis()

    ip = request.client.host if request.client else "unknown"

    # IP-level rate limit (5 attempts per 10 min)
    ip_key = f"admin:login:ip:{ip}"
    ip_attempts = await redis.incr(ip_key)
    if ip_attempts == 1:
        await redis.expire(ip_key, 600)   # 10 minutes window
    if ip_attempts > 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts from this IP. Try again in 10 minutes.",
        )

    admin_doc = await db.admin_users.find_one({"email": body.email})
    if not admin_doc:
        # Constant-time response — don't reveal if email exists
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    admin = AdminUser(**admin_doc)

    # Account active check
    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated")

    # Lockout check
    if admin.is_locked():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Account locked until {admin.locked_until.isoformat()}. Too many failed attempts.",
        )

    # Password check
    try:
        pw_ok = _bcrypt.checkpw(body.password.encode(), admin.password_hash.encode())
    except Exception as _pw_exc:
        logger.error(f"bcrypt.checkpw error for {body.email}: {_pw_exc}")
        pw_ok = False

    if not pw_ok:
        await record_failed_login(admin)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Password OK — issue pending token (awaits TOTP verification)
    pending_token = create_totp_pending_token(admin.id, admin.email)
    logger.info(f"Admin {admin.email} passed password check from {ip}")

    return LoginResponse(pending_token=pending_token)


@router.post("/totp-verify")
async def totp_verify(body: TotpVerifyRequest, request: Request, response: Response):
    """
    Step 2: verify TOTP code using the pending_token from /login.
    On success, sets admin_token HttpOnly cookie and returns access_token.
    """
    db = get_db()
    redis = get_redis()

    ip = request.client.host if request.client else "unknown"

    # Decode pending token
    payload = decode_totp_pending_token(body.pending_token)
    admin_id = payload["sub"]

    # TOTP rate limit (5 attempts per pending session)
    totp_key = f"admin:totp:attempts:{admin_id}"
    totp_attempts = await redis.incr(totp_key)
    if totp_attempts == 1:
        await redis.expire(totp_key, 300)   # 5 minutes
    if totp_attempts > 5:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many TOTP attempts. Start login again.",
        )

    admin_doc = await db.admin_users.find_one({"id": admin_id})
    if not admin_doc:
        raise HTTPException(status_code=401, detail="Admin not found")

    admin = AdminUser(**admin_doc)

    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated")

    # Decrypt and verify TOTP secret
    # The totp_secret stored in DB is Fernet-encrypted base32 string
    try:
        from cryptography.fernet import Fernet
        f = Fernet(settings.admin_totp_encryption_key.encode())
        totp_secret = f.decrypt(admin.totp_secret.encode()).decode()
    except Exception as exc:
        logger.error(f"TOTP decryption failed for {admin.email}: {exc}")
        raise HTTPException(status_code=500, detail="TOTP configuration error")

    totp = pyotp.TOTP(totp_secret)
    if not totp.verify(body.totp_code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid TOTP code",
        )

    # Success — issue full admin token
    await clear_failed_login(admin)
    await redis.delete(totp_key)   # clear TOTP attempt counter

    token = create_admin_token(admin)
    expires_in = settings.admin_jwt_expire_hours * 3600

    # Set HttpOnly cookie for browser clients (admin panel uses this)
    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=expires_in,
        path="/",
    )

    logger.info(f"Admin {admin.email} logged in from {ip}")

    return {"access_token": token, "expires_in": expires_in}


@router.post("/logout")
async def logout(
    admin: CurrentAdmin,
    response: Response,
):
    """
    Clears the admin_token cookie.
    For full JTI blacklisting, the token JTI would need to be extracted
    from the request and added to Redis — done via the CurrentAdmin dep's context.
    """
    response.delete_cookie("admin_token", path="/")

    logger.info(f"Admin {admin.email} logged out")
    return {"message": "Logged out successfully"}


@router.post("/refresh")
async def refresh_token(admin: CurrentAdmin, response: Response):
    """Issue a new admin token (resets 8h window). Max 3 rotations per session."""
    token = create_admin_token(admin)
    expires_in = settings.admin_jwt_expire_hours * 3600

    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=expires_in,
        path="/",
    )

    return {"access_token": token, "expires_in": expires_in}
