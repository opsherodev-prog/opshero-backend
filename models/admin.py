"""
Admin user model — completely separate from the regular User model.
Stored in the `admin_users` MongoDB collection.
"""

from datetime import datetime
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class AdminPermissions(BaseModel):
    can_manage_patterns: bool = True
    can_review_contributions: bool = True
    can_manage_users: bool = False       # super_admin only by default
    can_view_billing: bool = False       # super_admin only by default
    can_manage_config: bool = False      # super_admin only by default
    can_delete_users: bool = False       # super_admin only


class AdminUser(BaseModel):
    """Admin user — completely separate from regular User model."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    email: str
    password_hash: str            # bcrypt, cost factor 14
    totp_secret: str              # base32, Fernet-encrypted at rest
    totp_enabled: bool = True     # mandatory, cannot be disabled
    full_name: str
    role: Literal["super_admin", "admin", "analyst"] = "admin"

    permissions: AdminPermissions = Field(default_factory=AdminPermissions)

    # Security tracking
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    failed_attempts: int = 0
    locked_until: Optional[datetime] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None    # admin_id who created this admin
    is_active: bool = True

    def is_locked(self) -> bool:
        """Returns True if account is currently locked due to failed attempts."""
        if self.locked_until is None:
            return False
        return datetime.utcnow() < self.locked_until

    def has_permission(self, perm: str) -> bool:
        """Check a named permission. Super admins bypass all checks."""
        if self.role == "super_admin":
            return True
        return getattr(self.permissions, perm, False)


class AuditLogEntry(BaseModel):
    """Immutable audit log entry for every admin action."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Who
    admin_id: str
    admin_email: str
    admin_ip: str
    admin_user_agent: str = ""

    # What
    action: str          # e.g. "pattern_updated", "user_suspended"
    category: str        # "patterns" | "users" | "config" | "billing" | "auth"

    # Target
    target_type: Optional[str] = None   # "pattern" | "user" | "config_key"
    target_id: Optional[str] = None     # ID of affected resource

    # Details
    details: dict = Field(default_factory=dict)   # before/after state for mutations
    result: Literal["success", "failure"] = "success"
    error_message: Optional[str] = None

    # Integrity — SHA-256 of (id + timestamp + admin_id + action)
    # Computed on write; if hash doesn't match on read → alert security team
    integrity_hash: Optional[str] = None


class PlatformConfigEntry(BaseModel):
    """A single platform configuration key/value pair."""
    key: str
    value: object
    category: str
    description: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None   # admin_email


class Announcement(BaseModel):
    """Platform announcement (banner / maintenance notice)."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: Literal["info", "warning", "maintenance", "feature", "billing"] = "info"
    title: str
    message: str               # Markdown supported
    target_tiers: list[str] = Field(default_factory=lambda: ["free", "pro", "team"])
    target_countries: Optional[list[str]] = None   # None = all
    dismissible: bool = True
    show_from: datetime = Field(default_factory=datetime.utcnow)
    show_until: Optional[datetime] = None
    cta_text: Optional[str] = None
    cta_url: Optional[str] = None
    created_by: str            # admin_id
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
