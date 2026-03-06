"""
Platform config CRUD.
GET/PUT /admin/config/*
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db, get_redis
from deps.admin_auth import CurrentAdmin, SuperAdmin

router = APIRouter(prefix="/admin/config", tags=["admin-config"])

# Keys that require super_admin to modify
SUPER_ADMIN_KEYS = {
    "security.admin_ip_allowlist",
    "security.require_2fa_admins",
}


class ConfigSetRequest(BaseModel):
    value: Any


@router.get("")
async def get_all_config(admin: CurrentAdmin):
    db = get_db()
    entries = await db.platform_config.find({}, {"_id": 0}).to_list(None)

    if not entries:
        # Return defaults if DB is empty
        return DEFAULT_CONFIG

    return entries


@router.get("/{key:path}")
async def get_config_value(key: str, admin: CurrentAdmin):
    db = get_db()
    entry = await db.platform_config.find_one({"key": key}, {"_id": 0})
    if not entry:
        raise HTTPException(404, f"Config key {key!r} not found")
    return entry


@router.put("/{key:path}")
async def set_config_value(key: str, body: ConfigSetRequest, admin: CurrentAdmin):
    # Some keys are super_admin only
    if key in SUPER_ADMIN_KEYS and admin.role != "super_admin":
        raise HTTPException(403, f"Modifying {key!r} requires super_admin role")

    db = get_db()
    await db.platform_config.update_one(
        {"key": key},
        {"$set": {
            "value": body.value,
            "updated_at": datetime.utcnow(),
            "updated_by": admin.email,
        }},
        upsert=True,
    )

    # Publish change for live reload
    try:
        redis = get_redis()
        await redis.publish("config:changed", key)
    except Exception:
        pass

    return {"key": key, "value": body.value, "updated_by": admin.email}


@router.post("/reload")
async def reload_config(admin: CurrentAdmin):
    """Trigger Redis pub/sub config reload across all backend instances."""
    try:
        redis = get_redis()
        await redis.publish("config:reload", "all")
    except Exception as exc:
        raise HTTPException(500, f"Redis publish failed: {exc}") from exc
    return {"message": "Config reload triggered"}


# ── Default config values (seeded to DB on first use) ─────────────────────

DEFAULT_CONFIG = [
    {"key": "llm.enabled",                    "value": True,              "category": "LLM",      "description": "Enable AI engine fallback"},
    {"key": "llm.confidence_threshold",        "value": 0.65,              "category": "LLM",      "description": "Below this → trigger LLM"},
    {"key": "llm.primary_model",              "value": "llama-3.3-70b-versatile", "category": "LLM", "description": "Primary LLM model"},
    {"key": "llm.daily_budget_usd",           "value": 10.00,             "category": "LLM",      "description": "Daily spend limit"},
    {"key": "llm.enabled_for_free",           "value": False,             "category": "LLM",      "description": "Free tier gets LLM"},
    {"key": "features.community_contributions","value": True,              "category": "FEATURES", "description": "Enable community PR submissions"},
    {"key": "features.mobile_money",          "value": True,              "category": "FEATURES", "description": "Enable mobile money payments"},
    {"key": "features.analytics",             "value": True,              "category": "FEATURES", "description": "Enable usage analytics"},
    {"key": "limits.free_analyses_per_day",   "value": 10,               "category": "LIMITS",   "description": "Free tier daily limit"},
    {"key": "limits.pro_analyses_per_day",    "value": 500,              "category": "LIMITS",   "description": "Pro tier daily limit"},
    {"key": "limits.log_max_size_bytes",      "value": 1_048_576,        "category": "LIMITS",   "description": "Max log size (1MB)"},
    {"key": "sync.retention_days",            "value": 90,               "category": "SYNC",     "description": "Days to retain analysis data"},
    {"key": "security.rate_limit_per_min",    "value": 60,               "category": "SECURITY", "description": "API rate limit per minute"},
    {"key": "security.require_2fa_admins",    "value": True,             "category": "SECURITY", "description": "Mandatory TOTP for admins"},
    {"key": "security.admin_ip_allowlist",    "value": [],               "category": "SECURITY", "description": "Empty = all IPs allowed"},
]
