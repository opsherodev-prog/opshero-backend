"""
Team management router.

Endpoints:
  GET  /teams/me                    current user's team
  POST /teams                       create a team (requires team tier)
  PUT  /teams/me                    update team settings
  POST /teams/invite                invite a member by GitHub login
  POST /teams/invite/accept         accept invitation
  DELETE /teams/members/{user_id}  remove a member (owner/admin only)
  PUT  /teams/members/{user_id}/role  change member role
  DELETE /teams/me                  dissolve team (owner only)
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional
import re

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from database import get_db
from deps.auth import CurrentUser
from models.team import Team
from models.user import TIER_LIMITS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/teams", tags=["teams"])


def _slug(name: str) -> str:
    """Convert team name to URL-safe slug."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:40]


async def _get_user_team(user_id: str, db) -> Optional[dict]:
    """Return the team document where user is a member."""
    return await db.teams.find_one(
        {"members.user_id": user_id},
        {"_id": 0},
    )


# ── GET /teams/me ─────────────────────────────────────────────────────────────

@router.get("/me")
async def get_my_team(user: CurrentUser):
    """Return the team the current user belongs to."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        return {"team": None}

    # Enrich members with GitHub profile
    enriched = []
    for m in team_doc.get("members", []):
        u = await db.users.find_one({"id": m["user_id"]}, {"_id": 0})
        if u:
            enriched.append({
                "user_id": m["user_id"],
                "github_login": u.get("github_login", ""),
                "github_avatar_url": u.get("github_avatar_url"),
                "github_name": u.get("github_name"),
                "role": m.get("role", "member"),
                "joined_at": m.get("joined_at"),
            })

    # Team-wide stats
    member_ids = [m["user_id"] for m in team_doc.get("members", [])]
    total_analyses = await db.analyses.count_documents({"user_id": {"$in": member_ids}})

    return {
        "team": {
            **team_doc,
            "members": enriched,
            "member_count": len(enriched),
            "total_analyses": total_analyses,
            "invitations_pending": len(team_doc.get("invitations", [])),
        }
    }


# ── POST /teams ───────────────────────────────────────────────────────────────

class CreateTeamRequest(BaseModel):
    name: str


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(body: CreateTeamRequest, user: CurrentUser):
    """Create a new team. Requires team tier."""
    if user.tier != "team":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Team features require the 'team' tier. Upgrade to create a team.",
        )

    db = get_db()

    # Check user not already in a team
    existing = await _get_user_team(user.id, db)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "You are already in a team.")

    name = body.name.strip()
    if not name or len(name) < 2:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Team name must be at least 2 characters.")

    slug = _slug(name)
    # Ensure unique slug
    if await db.teams.find_one({"slug": slug}):
        slug = f"{slug}-{secrets.token_hex(3)}"

    team = Team(
        name=name,
        slug=slug,
        owner_id=user.id,
        members=[{
            "user_id": user.id,
            "role": "owner",
            "joined_at": datetime.utcnow(),
        }],
    )

    await db.teams.insert_one(team.model_dump())

    # Update user's team_id
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"team_id": team.id, "team_role": "owner"}},
    )

    logger.info("Team '%s' created by user %s", name, user.github_login)
    return {"team_id": team.id, "slug": team.slug, "name": team.name}


# ── PUT /teams/me ─────────────────────────────────────────────────────────────

class UpdateTeamRequest(BaseModel):
    name: Optional[str] = None
    default_repos: Optional[list[str]] = None


@router.put("/me")
async def update_team(body: UpdateTeamRequest, user: CurrentUser):
    """Update team settings. Owner or admin only."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")

    my_role = next(
        (m["role"] for m in team_doc["members"] if m["user_id"] == user.id),
        None,
    )
    if my_role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only owner or admin can update team settings.")

    update: dict = {"updated_at": datetime.utcnow()}
    if body.name:
        update["name"] = body.name.strip()
    if body.default_repos is not None:
        update["default_repos"] = body.default_repos

    await db.teams.update_one({"id": team_doc["id"]}, {"$set": update})
    return {"ok": True}


# ── POST /teams/invite ────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    github_login: str
    role: str = "member"   # member | admin


@router.post("/invite")
async def invite_member(body: InviteRequest, user: CurrentUser):
    """Invite a GitHub user to the team."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")

    my_role = next(
        (m["role"] for m in team_doc["members"] if m["user_id"] == user.id),
        None,
    )
    if my_role not in ("owner", "admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only owner or admin can invite members.")

    if body.role not in ("member", "admin"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Role must be 'member' or 'admin'.")

    # Find invitee user
    invitee = await db.users.find_one({"github_login": body.github_login})
    if not invitee:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"User @{body.github_login} not found. They need to log in to OpsHero first.",
        )

    # Check not already a member
    already = any(m["user_id"] == invitee["id"] for m in team_doc.get("members", []))
    if already:
        raise HTTPException(status.HTTP_409_CONFLICT, f"@{body.github_login} is already in the team.")

    token = secrets.token_urlsafe(32)
    invitation = {
        "user_id": invitee["id"],
        "github_login": body.github_login,
        "role": body.role,
        "token": token,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(days=7),
    }

    await db.teams.update_one(
        {"id": team_doc["id"]},
        {"$push": {"invitations": invitation}},
    )

    logger.info("Invited @%s to team %s", body.github_login, team_doc["id"])
    return {"ok": True, "token": token, "expires_in_days": 7}


# ── POST /teams/invite/accept ─────────────────────────────────────────────────

class AcceptInviteRequest(BaseModel):
    token: str


@router.post("/invite/accept")
async def accept_invite(body: AcceptInviteRequest, user: CurrentUser):
    """Accept a team invitation using the invite token."""
    db = get_db()

    team_doc = await db.teams.find_one({"invitations.token": body.token}, {"_id": 0})
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found or already used.")

    invitation = next(
        (inv for inv in team_doc.get("invitations", []) if inv["token"] == body.token),
        None,
    )
    if not invitation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid invitation token.")

    if datetime.utcnow() > invitation["expires_at"]:
        raise HTTPException(status.HTTP_410_GONE, "Invitation has expired.")

    if invitation["user_id"] != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This invitation is for a different user.")

    # Check not already a member
    if any(m["user_id"] == user.id for m in team_doc.get("members", [])):
        raise HTTPException(status.HTTP_409_CONFLICT, "Already a member of this team.")

    now = datetime.utcnow()
    new_member = {
        "user_id": user.id,
        "role": invitation["role"],
        "joined_at": now,
    }

    # Add member, remove invitation
    await db.teams.update_one(
        {"id": team_doc["id"]},
        {
            "$push": {"members": new_member},
            "$pull": {"invitations": {"token": body.token}},
            "$set": {"updated_at": now},
        },
    )

    # Update user
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"team_id": team_doc["id"], "team_role": invitation["role"]}},
    )

    logger.info("User %s joined team %s", user.github_login, team_doc["id"])
    return {"ok": True, "team_id": team_doc["id"], "team_name": team_doc["name"]}


# ── DELETE /teams/members/{user_id} ──────────────────────────────────────────

@router.delete("/members/{target_user_id}")
async def remove_member(target_user_id: str, user: CurrentUser):
    """Remove a member from the team. Owner can remove anyone; admin can remove members only."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")

    my_role = next(
        (m["role"] for m in team_doc["members"] if m["user_id"] == user.id),
        None,
    )
    target = next(
        (m for m in team_doc["members"] if m["user_id"] == target_user_id),
        None,
    )
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found in team.")

    if target["role"] == "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cannot remove the team owner.")
    if my_role == "admin" and target["role"] == "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin cannot remove another admin.")
    if my_role not in ("owner", "admin") and target_user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only owner or admin can remove members.")

    await db.teams.update_one(
        {"id": team_doc["id"]},
        {
            "$pull": {"members": {"user_id": target_user_id}},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    await db.users.update_one(
        {"id": target_user_id},
        {"$set": {"team_id": None, "team_role": None}},
    )

    return {"ok": True}


# ── PUT /teams/members/{user_id}/role ─────────────────────────────────────────

class ChangeRoleRequest(BaseModel):
    role: str  # member | admin


@router.put("/members/{target_user_id}/role")
async def change_member_role(
    target_user_id: str,
    body: ChangeRoleRequest,
    user: CurrentUser,
):
    """Change a team member's role. Owner only."""
    if body.role not in ("member", "admin"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Role must be 'member' or 'admin'.")

    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")

    my_role = next(
        (m["role"] for m in team_doc["members"] if m["user_id"] == user.id),
        None,
    )
    if my_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the owner can change roles.")

    target = next(
        (m for m in team_doc["members"] if m["user_id"] == target_user_id),
        None,
    )
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found in team.")
    if target["role"] == "owner":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot change owner's role.")

    await db.teams.update_one(
        {"id": team_doc["id"], "members.user_id": target_user_id},
        {"$set": {"members.$.role": body.role, "updated_at": datetime.utcnow()}},
    )
    await db.users.update_one(
        {"id": target_user_id},
        {"$set": {"team_role": body.role}},
    )

    return {"ok": True}


# ── DELETE /teams/me (dissolve) ───────────────────────────────────────────────

@router.delete("/me")
async def dissolve_team(user: CurrentUser):
    """Dissolve the team. Owner only."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")

    my_role = next(
        (m["role"] for m in team_doc["members"] if m["user_id"] == user.id),
        None,
    )
    if my_role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the team owner can dissolve the team.")

    # Remove team_id from all members
    member_ids = [m["user_id"] for m in team_doc.get("members", [])]
    await db.users.update_many(
        {"id": {"$in": member_ids}},
        {"$set": {"team_id": None, "team_role": None}},
    )

    await db.teams.delete_one({"id": team_doc["id"]})
    logger.info("Team %s dissolved by owner %s", team_doc["id"], user.github_login)
    return {"ok": True}


# ── GET /teams/me/analyses ────────────────────────────────────────────────────

@router.get("/me/analyses")
async def get_team_analyses(
    user: CurrentUser,
    page: int = 1,
    per_page: int = 20,
):
    """Get shared analyses history for the entire team."""
    db = get_db()
    team_doc = await _get_user_team(user.id, db)
    if not team_doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "You are not in a team.")

    member_ids = [m["user_id"] for m in team_doc.get("members", [])]
    skip = (page - 1) * per_page

    cursor = db.analyses.find(
        {"user_id": {"$in": member_ids}},
        {"_id": 0, "raw_log": 0},
    ).sort("created_at", -1).skip(skip).limit(per_page)

    items = await cursor.to_list(per_page)
    total = await db.analyses.count_documents({"user_id": {"$in": member_ids}})

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }
