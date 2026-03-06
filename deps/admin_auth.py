"""
Admin authentication dependencies.
Uses a SEPARATE JWT secret (admin_jwt_secret) and scope "admin".
Regular user tokens are rejected.
"""

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from config import settings
from database import get_db, get_redis
from models.admin import AdminUser, AuditLogEntry

logger = logging.getLogger(__name__)

_admin_bearer = HTTPBearer(auto_error=False)

LOCKOUT_MINUTES = 15
MAX_FAILED_ATTEMPTS = 5


# ── JWT helpers ────────────────────────────────────────────────────────────

def create_admin_token(admin: AdminUser) -> str:
    """Create an 8-hour admin JWT with scope: admin."""
    now = datetime.utcnow()
    payload = {
        "sub": admin.id,
        "id": admin.id,                          # explicit id for frontend
        "email": admin.email,
        "full_name": admin.full_name,
        "role": admin.role,
        "permissions": admin.permissions.model_dump(),
        "scope": "admin",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(hours=settings.admin_jwt_expire_hours),
    }
    return jwt.encode(
        payload,
        settings.admin_jwt_secret,
        algorithm="HS256",
    )


def create_totp_pending_token(admin_id: str, email: str) -> str:
    """
    Short-lived token (5 min) issued after email+password verification.
    No scope → cannot be used to call admin APIs.
    The TOTP step exchanges this for a full admin token.
    """
    now = datetime.utcnow()
    payload = {
        "sub": admin_id,
        "email": email,
        "scope": "totp_pending",
        "jti": str(uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm="HS256")


def decode_totp_pending_token(token: str) -> dict:
    """Decode and validate a TOTP pending token."""
    try:
        payload = jwt.decode(token, settings.admin_jwt_secret, algorithms=["HS256"])
    except JWTError as e:
        raise HTTPException(status_code=401, detail="Invalid or expired pending token") from e

    if payload.get("scope") != "totp_pending":
        raise HTTPException(status_code=401, detail="Not a TOTP pending token")

    return payload


# ── Core dependency ────────────────────────────────────────────────────────

async def _get_admin_from_token(token: str, request: Request) -> AdminUser:
    """Internal: validate admin JWT, check JTI blacklist, return AdminUser."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired admin token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.admin_jwt_secret, algorithms=["HS256"])
    except JWTError:
        raise credentials_error

    if payload.get("scope") != "admin":
        raise credentials_error

    admin_id = payload.get("sub")
    if not admin_id:
        raise credentials_error

    # JTI blacklist check (logout / revocation)
    jti = payload.get("jti", "")
    redis = get_redis()
    if await redis.exists(f"admin:jti:blacklist:{jti}"):
        raise credentials_error

    db = get_db()
    admin_doc = await db.admin_users.find_one({"id": admin_id})
    if not admin_doc:
        raise credentials_error

    admin = AdminUser(**admin_doc)
    if not admin.is_active:
        raise HTTPException(status_code=403, detail="Admin account deactivated")

    # Passive audit: log API access
    await _record_audit(
        admin=admin,
        action="api_access",
        category="auth",
        request=request,
        details={"endpoint": str(request.url.path)},
    )

    return admin


async def require_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_admin_bearer)],
    admin_token: Annotated[str | None, Cookie()] = None,
) -> AdminUser:
    """
    FastAPI dependency: extract admin token from Authorization header OR
    from admin_token cookie (set by the frontend after TOTP verification).
    """
    token: str | None = None

    if credentials and credentials.credentials:
        token = credentials.credentials
    elif admin_token:
        token = admin_token

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return await _get_admin_from_token(token, request)


async def require_super_admin(
    admin: Annotated[AdminUser, Depends(require_admin)],
) -> AdminUser:
    """Stricter dependency: only super_admin role passes."""
    if admin.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires super_admin role",
        )
    return admin


def require_permission(perm: str):
    """
    Factory: require a specific permission.
    super_admin bypasses all checks.

    Usage:
        @router.delete("/{id}", dependencies=[Depends(require_permission("can_delete_users"))])
    """
    async def _check(admin: Annotated[AdminUser, Depends(require_admin)]) -> AdminUser:
        if not admin.has_permission(perm):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {perm}",
            )
        return admin
    return _check


# ── Audit helpers ──────────────────────────────────────────────────────────

async def _record_audit(
    admin: AdminUser,
    action: str,
    category: str,
    request: Request,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
    result: str = "success",
    error_message: str | None = None,
) -> None:
    """Insert an immutable audit log entry."""
    try:
        entry = AuditLogEntry(
            admin_id=admin.id,
            admin_email=admin.email,
            admin_ip=request.client.host if request.client else "unknown",
            admin_user_agent=request.headers.get("user-agent", ""),
            action=action,
            category=category,
            target_type=target_type,
            target_id=target_id,
            details=details or {},
            result=result,
            error_message=error_message,
        )
        # Integrity hash: SHA-256(id + timestamp.isoformat + admin_id + action)
        raw = f"{entry.id}{entry.timestamp.isoformat()}{admin.id}{action}"
        entry.integrity_hash = hashlib.sha256(raw.encode()).hexdigest()

        db = get_db()
        await db.admin_audit_log.insert_one(entry.model_dump())
    except Exception as exc:  # pragma: no cover
        logger.error(f"Failed to write audit log: {exc}")


# ── Lockout helpers ────────────────────────────────────────────────────────

async def record_failed_login(admin: AdminUser) -> None:
    """Increment failed attempts; lock account after MAX_FAILED_ATTEMPTS."""
    db = get_db()
    new_attempts = admin.failed_attempts + 1
    update: dict = {"$set": {"failed_attempts": new_attempts}}

    if new_attempts >= MAX_FAILED_ATTEMPTS:
        locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        update["$set"]["locked_until"] = locked_until
        logger.warning(
            f"Admin {admin.email} locked until {locked_until} after {new_attempts} failed attempts"
        )

    await db.admin_users.update_one({"id": admin.id}, update)


async def clear_failed_login(admin: AdminUser) -> None:
    """Reset failed attempts after successful login."""
    db = get_db()
    await db.admin_users.update_one(
        {"id": admin.id},
        {"$set": {"failed_attempts": 0, "locked_until": None, "last_login_at": datetime.utcnow()}},
    )


# ── Type aliases ───────────────────────────────────────────────────────────

CurrentAdmin = Annotated[AdminUser, Depends(require_admin)]
SuperAdmin = Annotated[AdminUser, Depends(require_super_admin)]
