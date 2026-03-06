"""User model."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from uuid import uuid4


class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))

    # GitHub identity
    github_id: int
    github_login: str
    github_email: Optional[str] = None
    github_avatar_url: Optional[str] = None
    github_name: Optional[str] = None
    github_token: Optional[str] = None  # OAuth token — used to call GitHub API on behalf of user

    # Subscription
    tier: str = "free"           # free | pro | team
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    subscription_expires_at: Optional[datetime] = None

    # Team membership
    team_id: Optional[str] = None
    team_role: Optional[str] = None  # owner | admin | member

    # Usage counters (reset monthly)
    analyses_this_month: int = 0
    llm_calls_today: int = 0
    llm_calls_reset_at: Optional[datetime] = None

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: Optional[datetime] = None
    last_active_at: Optional[datetime] = None

    # Status
    is_active: bool = True
    is_suspended: bool = False
    suspended_reason: Optional[str] = None


class UserPublic(BaseModel):
    """Safe subset of User for API responses."""
    id: str
    github_login: str
    github_avatar_url: Optional[str]
    github_name: Optional[str]
    tier: str
    team_id: Optional[str]
    analyses_this_month: int
    created_at: datetime
    is_suspended: bool = False
    suspended_reason: Optional[str] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


# ── Tier limits ───────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free": {
        "analyses_per_day": 10,
        "history_days": 7,
        "sync_enabled": False,
        "llm_enabled": False,
        "team_enabled": False,
    },
    "pro": {
        "analyses_per_day": 500,
        "history_days": 90,
        "sync_enabled": True,
        "llm_enabled": True,
        "team_enabled": False,
    },
    "team": {
        "analyses_per_day": -1,  # unlimited
        "history_days": 365,
        "sync_enabled": True,
        "llm_enabled": True,
        "team_enabled": True,
    },
}
