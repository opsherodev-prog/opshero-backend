"""
Inverted keyword index for O(1) pattern candidate lookup.

Replaces O(N) full scan with a lookup table:
  keyword → set of pattern_ids that contain it.

Hot-reloaded from MongoDB on admin updates via Redis pub/sub.
"""

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class PatternIndex:
    """Thread-safe (asyncio) inverted keyword index."""

    def __init__(self):
        # keyword (lowercase) → set of pattern_ids
        self._kw_index: dict[str, set[str]] = defaultdict(set)
        # pattern_id → full pattern document
        self._patterns: dict[str, dict] = {}
        # pattern_id → category (for category boost)
        self._categories: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # ── Build ────────────────────────────────────────────────────────────────

    async def build(self, patterns: list[dict]) -> None:
        """
        Build the full index from a list of pattern documents.
        Called once at application startup.
        """
        async with self._lock:
            self._kw_index.clear()
            self._patterns.clear()
            self._categories.clear()

            for pattern in patterns:
                self._index_pattern(pattern)

            self._loaded = True

        logger.info(
            f"Pattern index built — {len(self._patterns)} patterns, "
            f"{len(self._kw_index)} unique tokens"
        )

    def _index_pattern(self, pattern: dict) -> None:
        """Add a single pattern to the index structures. Call inside lock."""
        pid = pattern.get("pattern_id")
        if not pid:
            return

        self._patterns[pid] = pattern
        self._categories[pid] = pattern.get("category", "other")

        detection = pattern.get("detection", {})

        # Index required keywords — each contributes to candidate selection
        for kw in detection.get("keywords_required", []):
            for token in self._tokenize(kw):
                self._kw_index[token].add(pid)

        # Index optional keywords — also indexed (same lookup, weight differs in scorer)
        for kw in detection.get("keywords_optional", []):
            for token in self._tokenize(kw):
                self._kw_index[token].add(pid)

    # ── Lookup ───────────────────────────────────────────────────────────────

    def lookup(
        self,
        log_tokens: set[str],
        category_hint: str | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """
        Find candidate patterns for a set of log tokens.

        Returns up to top_k patterns sorted by keyword hit count (desc).
        Category-matching patterns get a weighted boost.
        """
        if not self._loaded:
            logger.error("Pattern index not built yet!")
            return []

        hit_counts: dict[str, int] = defaultdict(int)

        for token in log_tokens:
            for pid in self._kw_index.get(token, set()):
                # Category boost: +3 if detected category matches pattern category
                if category_hint and self._categories.get(pid) == category_hint:
                    hit_counts[pid] += 3
                else:
                    hit_counts[pid] += 1

        if not hit_counts:
            return []

        top_pids = sorted(hit_counts, key=hit_counts.__getitem__, reverse=True)[:top_k]
        return [self._patterns[pid] for pid in top_pids if pid in self._patterns]

    def get_pattern(self, pattern_id: str) -> dict | None:
        return self._patterns.get(pattern_id)

    def all_patterns(self) -> list[dict]:
        return list(self._patterns.values())

    def pattern_count(self) -> int:
        return len(self._patterns)

    # ── Hot Updates ──────────────────────────────────────────────────────────

    async def update_pattern(self, pattern: dict) -> None:
        """
        Hot-update a single pattern without rebuilding the whole index.
        Called when admin publishes a pattern change (Redis pub/sub).
        """
        pid = pattern.get("pattern_id")
        if not pid:
            return

        async with self._lock:
            # Remove old index entries for this pattern
            for token_set in self._kw_index.values():
                token_set.discard(pid)

            if pattern.get("status") == "disabled":
                self._patterns.pop(pid, None)
                self._categories.pop(pid, None)
            else:
                self._index_pattern(pattern)

        logger.debug(f"Pattern index hot-updated: {pid}")

    async def remove_pattern(self, pattern_id: str) -> None:
        async with self._lock:
            for token_set in self._kw_index.values():
                token_set.discard(pattern_id)
            self._patterns.pop(pattern_id, None)
            self._categories.pop(pattern_id, None)

    # ── Redis Pub/Sub Listener ────────────────────────────────────────────────

    async def listen_for_updates(self, redis: "Redis", db) -> None:
        """
        Background task: subscribe to pattern:invalidate channel.
        When an admin updates a pattern, reloads it from MongoDB.
        """
        pubsub = redis.pubsub()
        await pubsub.subscribe("pattern:invalidate")
        logger.info("Pattern index listening for updates on Redis channel")

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            pattern_id = message["data"]
            try:
                pattern = await db.patterns.find_one({"pattern_id": pattern_id})
                if pattern:
                    await self.update_pattern(pattern)
                else:
                    await self.remove_pattern(pattern_id)
            except Exception as e:
                logger.error(f"Failed to reload pattern {pattern_id}: {e}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _tokenize(keyword: str) -> list[str]:
        """Normalize a keyword string to index tokens."""
        return [keyword.lower().strip()]
