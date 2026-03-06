"""
Admin LLM / Groq cost tracking and config endpoints.
GET  /admin/groq/costs
GET  /admin/groq/config
PUT  /admin/groq/config
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from config import settings
from database import get_db, get_redis
from deps.admin_auth import CurrentAdmin, SuperAdmin

router = APIRouter(prefix="/admin/groq", tags=["admin-groq"])


PERIOD_MAP = {
    "today": timedelta(hours=24),
    "7d":    timedelta(days=7),
    "30d":   timedelta(days=30),
}


# Token pricing (USD per 1M tokens) — matches engine/groq_client.py BudgetTracker
_PRICING: dict[str, tuple[float, float]] = {
    "llama-3.1-8b-instant":    (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "mixtral-8x7b-32768":      (0.24, 0.24),
}

def _compute_cost(model: str, input_tok: int, output_tok: int) -> float:
    in_r, out_r = _PRICING.get(model, (0.59, 0.79))
    return (input_tok * in_r + output_tok * out_r) / 1_000_000


# MongoDB $addFields cost expression using $switch on llm_model
_COST_EXPR = {
    "$divide": [
        {"$add": [
            {"$multiply": [
                {"$ifNull": ["$llm_input_tokens", 0]},
                {"$switch": {
                    "branches": [
                        {"case": {"$eq": ["$llm_model", "llama-3.1-8b-instant"]},    "then": 0.05},
                        {"case": {"$eq": ["$llm_model", "llama-3.3-70b-versatile"]}, "then": 0.59},
                        {"case": {"$eq": ["$llm_model", "mixtral-8x7b-32768"]},      "then": 0.24},
                    ],
                    "default": 0.59,
                }},
            ]},
            {"$multiply": [
                {"$ifNull": ["$llm_output_tokens", 0]},
                {"$switch": {
                    "branches": [
                        {"case": {"$eq": ["$llm_model", "llama-3.1-8b-instant"]},    "then": 0.08},
                        {"case": {"$eq": ["$llm_model", "llama-3.3-70b-versatile"]}, "then": 0.79},
                        {"case": {"$eq": ["$llm_model", "mixtral-8x7b-32768"]},      "then": 0.24},
                    ],
                    "default": 0.79,
                }},
            ]},
        ]},
        1_000_000,
    ]
}


@router.get("/costs")
async def get_llm_costs(
    admin: CurrentAdmin,
    period: str = Query("30d"),
):
    """Aggregate LLM call costs from the analyses collection."""
    db = get_db()
    since = datetime.utcnow() - PERIOD_MAP.get(period, timedelta(days=30))

    _llm_match = {"created_at": {"$gte": since}, "match_method": "groq_llm"}

    # Total cost + call count + avg latency — compute cost from token counts
    summary_pipeline = [
        {"$match": _llm_match},
        {"$addFields": {"_cost": _COST_EXPR}},
        {"$group": {
            "_id": None,
            "calls":       {"$sum": 1},
            "total_cost":  {"$sum": "$_cost"},
            "avg_latency": {"$avg": "$total_latency_ms"},
        }},
    ]
    summary = await db.analyses.aggregate(summary_pipeline).to_list(1)
    s = summary[0] if summary else {"calls": 0, "total_cost": 0.0, "avg_latency": 0.0}

    # Cost breakdown by model
    model_pipeline = [
        {"$match": _llm_match},
        {"$addFields": {"_cost": _COST_EXPR}},
        {"$group": {
            "_id":  "$llm_model",
            "cost": {"$sum": "$_cost"},
            "calls": {"$sum": 1},
        }},
    ]
    model_docs = await db.analyses.aggregate(model_pipeline).to_list(None)
    by_model = {
        (d["_id"] or "unknown"): {"cost": round(d["cost"], 6), "calls": d["calls"]}
        for d in model_docs
    }

    # Daily cost trend
    trend_days = 30 if period == "30d" else 7 if period == "7d" else 1
    trend_since = datetime.utcnow() - timedelta(days=trend_days)
    trend_pipeline = [
        {"$match": {"created_at": {"$gte": trend_since}, "match_method": "groq_llm"}},
        {"$addFields": {"_cost": _COST_EXPR}},
        {"$group": {
            "_id":  {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
            "cost": {"$sum": "$_cost"},
        }},
        {"$sort": {"_id": 1}},
    ]
    trend_docs = await db.analyses.aggregate(trend_pipeline).to_list(None)
    cost_trend = [{"date": d["_id"], "cost": round(d["cost"], 6)} for d in trend_docs]

    # Budget — read real-time Redis spend for today / this month
    daily_budget   = settings.llm_daily_budget_usd
    monthly_budget = settings.llm_monthly_budget_usd
    try:
        from database import get_redis
        redis = get_redis()
        today_cost   = float(await redis.get("llm:budget:daily:spent")   or 0)
        month_cost   = float(await redis.get("llm:budget:monthly:spent") or 0)
    except Exception:
        today_cost = month_cost = 0.0

    return {
        "total_cost_usd":     round(s["total_cost"], 6),
        "calls_total":        s["calls"],
        "avg_latency_ms":     round(s.get("avg_latency") or 0.0, 1),
        "daily_budget_usd":   daily_budget,
        "monthly_budget_usd": monthly_budget,
        "daily_used_pct":     round(today_cost / daily_budget * 100, 1) if daily_budget else 0.0,
        "monthly_used_pct":   round(month_cost / monthly_budget * 100, 1) if monthly_budget else 0.0,
        "by_model":           by_model,
        "cost_trend":         cost_trend,
    }


class GroqConfigUpdate(BaseModel):
    llm_enabled:              Optional[bool]  = None
    llm_primary_model:        Optional[str]   = None
    llm_fast_model:           Optional[str]   = None
    llm_long_context_model:   Optional[str]   = None
    llm_confidence_threshold: Optional[float] = None
    llm_daily_budget_usd:     Optional[float] = None
    llm_monthly_budget_usd:   Optional[float] = None
    llm_enabled_for_free:     Optional[bool]  = None
    llm_enabled_for_pro:      Optional[bool]  = None
    llm_enabled_for_team:     Optional[bool]  = None
    llm_calls_per_day_pro:    Optional[int]   = None
    llm_calls_per_day_team:   Optional[int]   = None


class ApiKeyUpdate(BaseModel):
    api_key: str


@router.get("/config")
async def get_groq_config(admin: CurrentAdmin):
    """Return current LLM/Groq runtime configuration."""
    db = get_db()
    # Check if a custom API key has been stored in platform_config
    key_doc = await db.platform_config.find_one({"key": "groq.api_key"})
    api_key_set = bool(key_doc and key_doc.get("value"))

    return {
        "llm_enabled":              settings.llm_enabled,
        "llm_primary_model":        settings.llm_primary_model,
        "llm_fast_model":           settings.llm_fast_model,
        "llm_long_context_model":   settings.llm_long_context_model,
        "llm_confidence_threshold": settings.llm_confidence_threshold,
        "llm_daily_budget_usd":     settings.llm_daily_budget_usd,
        "llm_monthly_budget_usd":   settings.llm_monthly_budget_usd,
        "llm_enabled_for_free":     settings.llm_enabled_for_free,
        "llm_enabled_for_pro":      settings.llm_enabled_for_pro,
        "llm_enabled_for_team":     settings.llm_enabled_for_team,
        "llm_calls_per_day_pro":    settings.llm_calls_per_day_pro,
        "llm_calls_per_day_team":   settings.llm_calls_per_day_team,
        "groq_api_key_set":         api_key_set,
    }


@router.put("/config")
async def update_groq_config(body: GroqConfigUpdate, admin: SuperAdmin):
    """
    Persist LLM config overrides to platform_config collection
    and publish a Redis reload event.
    """
    db = get_db()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}

    for key, value in updates.items():
        config_key = f"llm.{key.removeprefix('llm_')}"
        await db.platform_config.update_one(
            {"key": config_key},
            {"$set": {
                "value":      value,
                "updated_at": datetime.utcnow(),
                "updated_by": admin.email,
            }},
            upsert=True,
        )

    try:
        redis = get_redis()
        await redis.publish("config:changed", "llm.*")
    except Exception:
        pass

    return {"message": "LLM config updated", "updated": list(updates.keys())}


@router.put("/api-key")
async def update_api_key(body: ApiKeyUpdate, admin: SuperAdmin):
    """
    Update the Groq API key (super_admin only).
    Stored in platform_config — takes effect on next server restart
    or when the engine reloads config.
    """
    if not body.api_key.strip():
        from fastapi import HTTPException
        raise HTTPException(400, "API key cannot be empty")

    db = get_db()
    await db.platform_config.update_one(
        {"key": "groq.api_key"},
        {"$set": {
            "key":        "groq.api_key",
            "value":      body.api_key.strip(),
            "category":   "llm",
            "description": "Groq API key (runtime override)",
            "updated_at": datetime.utcnow(),
            "updated_by": admin.email,
        }},
        upsert=True,
    )

    try:
        redis = get_redis()
        await redis.publish("config:changed", "groq.api_key")
    except Exception:
        pass

    return {"message": "API key updated — effective on next restart"}
