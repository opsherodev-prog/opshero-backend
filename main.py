"""
OpsHero Backend — FastAPI application entry point.

Startup sequence:
  1. Connect to MongoDB + Redis
  2. Create indexes
  3. Load patterns into PatternIndex
  4. Initialise HybridAnalyzer (with or without Groq depending on config)
  5. Start Redis pub/sub listener for hot pattern updates (background task)
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from database import connect_mongo, connect_redis, create_indexes, get_db, get_redis
from engine.analyzer import HybridAnalyzer
from engine.groq_client import GroqClient, ModelSelector, BudgetTracker
from engine.index import PatternIndex
from engine.pattern_generator import PatternGenerator, set_pattern_generator
from engine.learning_jobs import run_learning_loop
from routers import analyses as analyses_router
from routers import announcements as announcements_router
from routers import auth as auth_router
from routers import contributions as contributions_router
from routers import feedback as feedback_router
from routers import feedback_hub as feedback_hub_router
from routers import patterns as patterns_router
from routers import sync as sync_router
from routers import billing as billing_router
from routers import teams as teams_router
from routers.analyses import set_analyzer
from routers import admin_auth as admin_auth_router
from routers import admin_dashboard as admin_dashboard_router
from routers import admin_patterns as admin_patterns_router
from routers import admin_users as admin_users_router
from routers import admin_audit as admin_audit_router
from routers import admin_config as admin_config_router
from routers import admin_email as admin_email_router
from routers import admin_announcements as admin_announcements_router
from routers import admin_contributions as admin_contributions_router
from routers import admin_billing as admin_billing_router
from routers import admin_groq as admin_groq_router
from routers import admin_feedback_hub as admin_feedback_hub_router
from routers import admin_learning as admin_learning_router
from routers import github as github_router
from routers import webhooks as webhooks_router
from routers import integrations as integrations_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Log CORS origins at startup
logger.info(f"CORS allowed origins: {settings.allowed_origins}")

# ── Pattern loading ────────────────────────────────────────────────────────────

PATTERNS_DIR = (
    Path(__file__).parent / "shared" / "patterns"
    if (Path(__file__).parent / "shared" / "patterns").exists()
    else Path(__file__).parent.parent / "shared" / "patterns"
)


def _load_all_patterns() -> list[dict]:
    """Load all pattern JSON files from shared/patterns/ directory."""
    index_path = PATTERNS_DIR / "index.json"
    if not index_path.exists():
        logger.warning(f"Pattern index not found at {index_path}")
        return []

    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)

    patterns: list[dict] = []
    for file_entry in index.get("files", []):
        file_path = PATTERNS_DIR / file_entry["file"]
        if not file_path.exists():
            logger.warning(f"Pattern file not found: {file_path}")
            continue
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
        patterns.extend(data.get("patterns", []))

    logger.info(f"Loaded {len(patterns)} patterns from {PATTERNS_DIR}")
    return patterns


async def _seed_patterns_to_mongo(patterns: list[dict]) -> None:
    """
    Upsert patterns into MongoDB so the PatternIndex can hot-reload
    and the admin panel can CRUD them.
    Uses pattern_id as the upsert key.
    """
    db = get_db()
    for p in patterns:
        await db.patterns.update_one(
            {"pattern_id": p["pattern_id"]},
            {"$setOnInsert": p},
            upsert=True,
        )
    logger.info(f"Seeded {len(patterns)} patterns to MongoDB (upsert, no overwrite)")


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("OpsHero backend starting up…")

    # 1. Connections
    await connect_mongo()
    await connect_redis()
    await create_indexes(get_db())

    # 2. Load patterns from disk
    raw_patterns = _load_all_patterns()
    await _seed_patterns_to_mongo(raw_patterns)

    # 3. Build inverted index
    pattern_index = PatternIndex()
    await pattern_index.build(raw_patterns)

    # 4. Groq client (optional — disabled if no API key or llm_enabled=false)
    groq_client: GroqClient | None = None
    if settings.llm_enabled and settings.groq_api_key:
        selector = ModelSelector(settings)
        budget = BudgetTracker(get_redis(), settings)
        groq_client = GroqClient(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            selector=selector,
            budget=budget,
        )
        logger.info(
            f"Groq LLM enabled — primary model: {settings.llm_primary_model}"
        )
    else:
        logger.info("Groq LLM disabled (llm_enabled=false or no API key)")

    # 5. Build analyzer
    analyzer = HybridAnalyzer(
        index=pattern_index,
        groq=groq_client,
        confidence_threshold=settings.llm_confidence_threshold,
    )
    set_analyzer(analyzer)

    # 6. Pattern Generator (dedicated LLM for auto-learning)
    pattern_generator: PatternGenerator | None = None
    if settings.learning_enabled and settings.groq_api_key:
        pattern_generator = PatternGenerator(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
        )
        set_pattern_generator(pattern_generator)
        logger.info("Pattern Generator (auto-learning LLM) initialized")
    else:
        logger.info("Auto-learning disabled (learning_enabled=false or no Groq API key)")

    # 7. Hot-reload listener (background)
    redis = get_redis()
    hot_reload_task = asyncio.create_task(
        pattern_index.listen_for_updates(redis=redis, db=get_db()),
        name="pattern-hot-reload",
    )

    # 8. Auto-learning background loop
    learning_task: asyncio.Task | None = None
    if settings.learning_enabled:
        learning_task = asyncio.create_task(
            run_learning_loop(
                db=get_db(),
                redis=get_redis(),
                generator=pattern_generator,
                interval_seconds=settings.learning_job_interval_seconds,
                min_sightings=settings.learning_auto_promote_min_sightings,
                min_confidence=settings.learning_auto_promote_min_confidence,
            ),
            name="auto-learning-loop",
        )
        logger.info(
            "Auto-learning loop started (interval=%ds, min_sightings=%d, min_confidence=%.0f%%)",
            settings.learning_job_interval_seconds,
            settings.learning_auto_promote_min_sightings,
            settings.learning_auto_promote_min_confidence * 100,
        )

    logger.info("OpsHero backend ready.")
    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("OpsHero backend shutting down…")
    if learning_task:
        learning_task.cancel()
        try:
            await learning_task
        except asyncio.CancelledError:
            pass
    hot_reload_task.cancel()
    try:
        await hot_reload_task
    except asyncio.CancelledError:
        pass


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="OpsHero API",
    version="1.0.0",
    description="Hybrid CI/CD log analysis engine — regex + Groq LLM",
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url="/redoc" if settings.app_env != "production" else None,
    lifespan=lifespan,
    redirect_slashes=False,  # Disable automatic trailing slash redirects (307)
)

# ── CORS ───────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(auth_router.router)
app.include_router(github_router.router)
app.include_router(webhooks_router.router)
app.include_router(analyses_router.router)
app.include_router(announcements_router.router)
app.include_router(contributions_router.router)
app.include_router(feedback_router.router)
app.include_router(feedback_hub_router.router)
app.include_router(patterns_router.router)
app.include_router(sync_router.router)
app.include_router(billing_router.router)
app.include_router(teams_router.router)
app.include_router(integrations_router.router)

# ── Admin routes (/admin/*) — completely separate from user routes ──────────
app.include_router(admin_auth_router.router)
app.include_router(admin_dashboard_router.router)
app.include_router(admin_patterns_router.router)
app.include_router(admin_users_router.router)
app.include_router(admin_audit_router.router)
app.include_router(admin_config_router.router)
app.include_router(admin_email_router.router)
app.include_router(admin_announcements_router.router)
app.include_router(admin_contributions_router.router)
app.include_router(admin_billing_router.router)
app.include_router(admin_groq_router.router)
app.include_router(admin_feedback_hub_router.router)
app.include_router(admin_learning_router.router)


# ── Health / liveness probes ───────────────────────────────────────────────────

@app.get("/health", tags=["infra"])
async def health():
    """Kubernetes liveness probe."""
    return {"status": "ok"}


@app.get("/ready", tags=["infra"])
async def ready():
    """
    Kubernetes readiness probe.
    Checks MongoDB and Redis connectivity.
    """
    errors: list[str] = []
    try:
        db = get_db()
        await db.command("ping")
    except Exception as e:
        errors.append(f"mongo: {e}")

    try:
        redis = get_redis()
        await redis.ping()
    except Exception as e:
        errors.append(f"redis: {e}")

    if errors:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "errors": errors},
        )
    return {"status": "ready"}


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "opshero-backend", "version": "1.0.0"}
