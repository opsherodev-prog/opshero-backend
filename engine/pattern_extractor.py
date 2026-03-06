"""
Pattern Candidate Extractor.

When the LLM fallback successfully analyzes a log that has no matching regex pattern,
this module extracts a draft pattern candidate that admins can review and promote
to the live regex library.

The goal: turn every LLM hit on an unknown error into a training example that
gradually shrinks the set of logs that need LLM processing (faster + cheaper).

Flow:
  LLM handles unknown log
        ↓
  PatternExtractor.extract_candidate(raw_log, llm_result)
        ↓
  Draft stored in `pattern_candidates` collection
        ↓
  Admin reviews in /admin/learning/
        ↓
  Admin promotes → live regex pattern (hot-reloaded)
"""

import hashlib
import logging
import re
from datetime import datetime
from typing import Optional
from uuid import uuid4

from engine.groq_client import LLMResult

logger = logging.getLogger(__name__)

# Lines likely to contain the error signal
_ERROR_SIGNAL_RE = re.compile(
    r"(?i)(error|fail(?:ure|ed)?|exception|fatal|panic|crash|abort|killed|"
    r"denied|timeout|refused|conflict|invalid|not found|missing|cannot|could not)",
)

# Tokens to skip when building keywords (too generic)
_SKIP_TOKENS = {
    "error", "failed", "failure", "exception", "the", "and", "for",
    "with", "from", "into", "that", "this", "have", "has", "was",
    "not", "can", "could", "would", "should", "will", "may",
    "true", "false", "null", "none", "undefined",
}

# Minimum LLM confidence to create a candidate
MIN_CONFIDENCE = 0.70


def extract_candidate(
    raw_log: str,
    llm_result: LLMResult,
    detected_category: Optional[str] = None,
    analysis_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Build a pattern candidate dict from a successful LLM result.

    Returns None if:
      - LLM confidence is below threshold
      - No solutions were returned
      - Extraction fails for any reason
    """
    if llm_result.confidence < MIN_CONFIDENCE:
        return None
    if not llm_result.solutions:
        return None

    try:
        error_lines = _extract_error_lines(raw_log)
        suggested_regex = _suggest_regex(error_lines, llm_result)
        keywords = _extract_keywords(error_lines, llm_result)
        category = detected_category or llm_result.error_category or "other"

        # Auto-generate a stable pattern_id from the regex fingerprint
        pattern_id_base = llm_result.pattern_id or f"auto_{hashlib.sha256(suggested_regex.encode()).hexdigest()[:10]}"
        # Sanitize: lowercase, replace non-word chars with underscores
        pattern_id_base = re.sub(r"[^a-z0-9_]", "_", pattern_id_base.lower())
        pattern_id_base = re.sub(r"_+", "_", pattern_id_base).strip("_")[:60]
        draft_pattern_id = f"auto_{pattern_id_base}"

        # Build schema v2 pattern_data block
        pattern_data = {
            "pattern_id": draft_pattern_id,
            "version": "0.1.0",
            "name": f"[AI Draft] {_humanize(llm_result.pattern_id)}",
            "category": category,
            "subcategory": "auto_extracted",
            "severity": _infer_severity(llm_result),
            "tags": ["auto-extracted", "ai-candidate", category],
            "detection": {
                "regex": suggested_regex,
                "keywords_required": keywords[:3],
                "keywords_optional": keywords[3:8],
                "exclude_if": [],
                "file_patterns": [],
                "variables": {},
            },
            "solutions": [
                {
                    "rank": s.rank,
                    "title": s.title,
                    "explanation": s.explanation,
                    "command_template": s.command or "",
                    "confidence": s.confidence,
                    "risk": s.risk or "none",
                    "reversible": s.reversible,
                    "affects_files": False,
                    "requires_confirmation": s.risk == "high",
                }
                for s in llm_result.solutions
            ],
            "causal_chain": {
                "often_caused_by": [llm_result.causal_hint] if llm_result.causal_hint else [],
                "often_causes": [],
            },
            "metadata": {
                "source": "ai_extracted",
                "author": "ai-engine",
                "docs_url": None,
                "stats": {
                    "match_count": 0,
                    "helpful_count": 0,
                    "not_helpful_count": 0,
                },
            },
        }

        candidate = {
            "id": str(uuid4()),
            "status": "pending",
            "origin": "llm_fallback",

            # AI-extracted draft pattern (schema v2 format, ready to promote)
            "pattern_data": pattern_data,

            # LLM context for admin review
            "llm_pattern_id": llm_result.pattern_id,
            "llm_confidence": llm_result.confidence,
            "llm_model": llm_result.model,
            "llm_error_type": llm_result.error_type,
            "llm_category": llm_result.error_category,
            "llm_causal_hint": llm_result.causal_hint,

            # Extracted signals for admin to edit
            "suggested_regex": suggested_regex,
            "extracted_keywords": keywords,
            "example_log_snippet": raw_log[:2000],  # truncated for storage

            # Traceability
            "source_analysis_id": analysis_id,

            # Review state
            "reviewed_by": None,
            "reviewed_at": None,
            "unmatched_count": 1,  # how many times this unknown pattern was seen

            "created_at": datetime.utcnow(),
            "last_seen_at": datetime.utcnow(),
        }

        return candidate

    except Exception as e:
        logger.warning(f"PatternExtractor.extract_candidate failed: {e}")
        return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract_error_lines(log: str) -> list[str]:
    """Return lines that contain error signals, capped at 20."""
    lines = log.splitlines()
    result = [
        line.strip()
        for line in lines
        if _ERROR_SIGNAL_RE.search(line) and len(line.strip()) > 10
    ]
    return result[:20]


def _suggest_regex(error_lines: list[str], llm_result: LLMResult) -> str:
    """
    Suggest a regex pattern from the most distinctive error lines.

    Strategy:
    1. Find the most signal-rich, shortest error line
    2. Extract 3-4 distinctive tokens
    3. Join with .*  to form a flexible cross-token pattern
    """
    if not error_lines:
        # Fallback: use the LLM pattern_id words as a keyword basis
        fallback_words = [w for w in llm_result.pattern_id.replace("_", " ").split() if len(w) > 3]
        return ".*".join(re.escape(w) for w in fallback_words[:3]) if fallback_words else "error"

    # Pick the best line: prefer shorter (more specific) with more unique words
    best_line = min(
        error_lines,
        key=lambda ln: (len(ln), -len(set(ln.lower().split()))),
    )

    # Extract distinctive tokens: skip timestamps, hex hashes, generic words
    raw_tokens = re.split(r"[\s,;:=|()\[\]{}\"'`]+", best_line)
    distinctive = []
    for t in raw_tokens:
        t = t.strip(".-/\\")
        if not t:
            continue
        # Skip pure numbers, short tokens, hex strings, generic words
        if re.match(r"^[\d.]+$", t):
            continue
        if len(t) < 4:
            continue
        if re.match(r"^[0-9a-f]{8,}$", t, re.I):
            continue
        if t.lower() in _SKIP_TOKENS:
            continue
        distinctive.append(t)
        if len(distinctive) >= 4:
            break

    if len(distinctive) >= 2:
        return ".*".join(re.escape(t) for t in distinctive)

    # Last resort: escape first 80 chars of the best line
    return re.escape(best_line[:80])


def _extract_keywords(error_lines: list[str], llm_result: LLMResult) -> list[str]:
    """
    Extract distinctive keywords from error lines.

    Priority:
    1. CamelCase identifiers (CrashLoopBackOff, OOMKilled)
    2. Error codes (ERESOLVE, SIGKILL, CVE-2024-xxxx, E137)
    3. Uppercase acronyms (RBAC, OOM, LFS)
    4. Fall back to LLM pattern_id tokens
    """
    keywords: list[str] = []
    seen: set[str] = set()

    for line in error_lines:
        # CamelCase identifiers (e.g. CrashLoopBackOff, ImagePullBackOff)
        for w in re.findall(r"\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b", line):
            key = w.lower()
            if key not in seen and key not in _SKIP_TOKENS:
                keywords.append(w)
                seen.add(key)

        # CVE identifiers
        for w in re.findall(r"CVE-\d{4}-\d{4,}", line, re.I):
            key = w.lower()
            if key not in seen:
                keywords.append(w.upper())
                seen.add(key)

        # Error codes (uppercase, 3+ chars, may include digits)
        for w in re.findall(r"\b[A-Z]{2,}(?:[_-]?[A-Z0-9]{1,})*\b", line):
            key = w.lower()
            if (
                key not in seen
                and key not in _SKIP_TOKENS
                and len(w) >= 3
            ):
                keywords.append(w)
                seen.add(key)

        if len(keywords) >= 10:
            break

    # Supplement with LLM pattern_id tokens if we don't have enough
    if len(keywords) < 3:
        for token in llm_result.pattern_id.split("_"):
            key = token.lower()
            if len(token) >= 4 and key not in seen and key not in _SKIP_TOKENS:
                keywords.append(token)
                seen.add(key)

    return keywords[:10]


def _infer_severity(llm_result: LLMResult) -> str:
    """Infer pattern severity from the LLM solution risk levels and confidence."""
    risks = [s.risk for s in llm_result.solutions if s.risk]
    if "high" in risks or llm_result.confidence >= 0.95:
        return "critical"
    if "medium" in risks or llm_result.confidence >= 0.85:
        return "high"
    return "medium"


def _humanize(snake_case_id: str) -> str:
    """Convert 'k8s_crash_loop_backoff' → 'K8s Crash Loop Backoff'."""
    return " ".join(w.capitalize() for w in snake_case_id.replace("-", "_").split("_"))
