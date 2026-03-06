"""
Database connections — MongoDB (Motor) + Redis.
Initialized once at application startup via lifespan.
"""

import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from redis.asyncio import Redis, ConnectionPool

from config import settings

logger = logging.getLogger(__name__)

# ── Globals (populated at startup) ───────────────────────────────────────────
_mongo_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None
_redis: Redis | None = None


async def connect_mongo() -> AsyncIOMotorDatabase:
    global _mongo_client, _db
    _mongo_client = AsyncIOMotorClient(
        settings.mongodb_url,
        serverSelectionTimeoutMS=5000,
        maxPoolSize=50,
    )
    _db = _mongo_client[settings.mongodb_db]
    # Ping to verify connection
    await _db.command("ping")
    logger.info(f"MongoDB connected → {settings.mongodb_db}")
    return _db


async def connect_redis() -> Redis:
    global _redis
    pool = ConnectionPool.from_url(
        settings.redis_url,
        max_connections=20,
        decode_responses=True,
    )
    _redis = Redis(connection_pool=pool)
    await _redis.ping()
    logger.info("Redis connected")
    return _redis


async def disconnect_mongo():
    if _mongo_client:
        _mongo_client.close()
        logger.info("MongoDB disconnected")


async def disconnect_redis():
    if _redis:
        await _redis.aclose()
        logger.info("Redis disconnected")


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialized. Call connect_mongo() first.")
    return _db


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call connect_redis() first.")
    return _redis


# ── MongoDB Index Setup ───────────────────────────────────────────────────────
async def create_indexes(db: AsyncIOMotorDatabase):
    """Create all MongoDB indexes at startup. Idempotent."""

    # Users - drop old obsolete indexes if they exist
    obsolete_indexes = ["username_1", "email_1"]
    for idx_name in obsolete_indexes:
        try:
            await db.users.drop_index(idx_name)
            logger.info(f"Dropped obsolete {idx_name} index")
        except Exception:
            pass  # Index doesn't exist, that's fine

    # Users
    await db.users.create_index("github_id", unique=True)
    await db.users.create_index("github_login", unique=True)
    await db.users.create_index("created_at")

    # Analyses
    await db.analyses.create_index([("user_id", 1), ("created_at", -1)])
    await db.analyses.create_index("client_id", unique=True, sparse=True)
    await db.analyses.create_index("pattern_id")
    # TTL: auto-delete log content after 90 days (keeps metadata)
    await db.analyses.create_index(
        "logs_expires_at",
        expireAfterSeconds=0,
        sparse=True,
    )

    # Patterns
    await db.patterns.create_index("pattern_id", unique=True)
    await db.patterns.create_index("category")
    await db.patterns.create_index("metadata.source")
    await db.patterns.create_index("status")

    # Pattern feedback
    await db.pattern_feedback.create_index([("pattern_id", 1), ("created_at", -1)])
    await db.pattern_feedback.create_index("analysis_id")

    # Community contributions — drop old unique pr_number index (form submissions have null pr_number)
    try:
        await db.community_contributions.drop_index("pr_number_1")
        logger.info("Dropped obsolete unique pr_number_1 index on community_contributions")
    except Exception:
        pass  # Index doesn't exist or already dropped
    await db.community_contributions.create_index("pr_number", sparse=True)
    await db.community_contributions.create_index("status")
    await db.community_contributions.create_index("author_user_id")

    # Pattern candidates (AI learning pipeline)
    await db.pattern_candidates.create_index("id", unique=True)
    await db.pattern_candidates.create_index("llm_pattern_id")
    await db.pattern_candidates.create_index(
        [("status", 1), ("unmatched_count", -1)]
    )
    await db.pattern_candidates.create_index(
        [("status", 1), ("llm_confidence", -1)]
    )
    await db.pattern_candidates.create_index("last_seen_at")

    # Learning jobs audit log
    await db.learning_jobs.create_index("type")
    await db.learning_jobs.create_index([("updated_at", -1)])
    await db.learning_jobs.create_index("candidate_id", sparse=True)

    # Teams
    await db.teams.create_index("slug", unique=True)
    await db.teams.create_index("owner_id")

    # Team invitations — TTL 7 days
    await db.team_invitations.create_index("token", unique=True)
    await db.team_invitations.create_index(
        "expires_at",
        expireAfterSeconds=0,
    )

    # Admin users
    await db.admin_users.create_index("email", unique=True)

    # Audit log
    await db.audit_log.create_index([("admin_id", 1), ("timestamp", -1)])
    await db.audit_log.create_index("action")

    logger.info("MongoDB indexes created/verified")
