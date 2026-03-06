"""Team model."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from uuid import uuid4


class Team(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))

    name: str
    slug: str                           # URL-friendly identifier
    owner_id: str                       # User.id of owner

    # Members: list of {user_id, role, joined_at}
    members: list[dict] = []            # [{user_id, role, joined_at}]

    # Pending invitations
    invitations: list[dict] = []        # [{email, role, token, created_at, expires_at}]

    # Settings
    default_repos: list[str] = []
    plan: str = "team"                  # always "team" for now

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TeamMemberPublic(BaseModel):
    user_id: str
    github_login: str
    github_avatar_url: Optional[str]
    github_name: Optional[str]
    role: str                           # owner | admin | member
    joined_at: datetime


class TeamPublic(BaseModel):
    id: str
    name: str
    slug: str
    owner_id: str
    members: list[TeamMemberPublic]
    plan: str
    created_at: datetime
    member_count: int
