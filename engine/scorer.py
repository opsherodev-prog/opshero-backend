"""
Confidence scoring for pattern candidates.

Computes a composite score [0.0, 1.0] based on:
- Historical success rate (from real user feedback)
- Regex match presence
- Optional keyword density
- Category match bonus
- Variable extraction bonus
"""

from engine.preprocessor import ProcessedLog


def compute_confidence(
    pattern: dict,
    processed: ProcessedLog,
    regex_matched: bool,
    variables_extracted: bool,
) -> float:
    """
    Compute composite confidence score for a pattern match.

    Returns a float in [0.0, 1.0].
    """
    detection = pattern.get("detection", {})
    stats = pattern.get("metadata", {}).get("stats", {})

    # ── Base score ───────────────────────────────────────────────────────────
    # Use real historical success rate when we have enough data (>= 20 matches).
    # Otherwise fall back to the pattern's best solution confidence.
    historical_rate = stats.get("success_rate")
    matched_count = stats.get("matched_count", 0)

    if historical_rate is not None and matched_count >= 20:
        # Bayesian-like weighting: blend historical rate with a neutral prior
        base = historical_rate * 0.8 + 0.1  # Always at least 0.1
    else:
        # Not enough data → use best solution's declared confidence
        solutions = pattern.get("solutions", [])
        if solutions:
            best_solution = min(solutions, key=lambda s: s.get("rank", 99))
            base = best_solution.get("confidence", 0.70)
        else:
            base = 0.50

    score = base

    # ── Regex match bonus ────────────────────────────────────────────────────
    if regex_matched:
        score += 0.15
    else:
        # Without a regex match, confidence should drop significantly.
        # A keyword-only match is much weaker.
        score -= 0.20

    # ── Optional keywords bonus ───────────────────────────────────────────────
    optional_kws = detection.get("keywords_optional", [])
    if optional_kws:
        hits = sum(
            1 for kw in optional_kws
            if kw.lower() in processed.cleaned_log.lower()
        )
        optional_ratio = hits / len(optional_kws)
        score += optional_ratio * 0.08  # Max +0.08

    # ── Category match bonus ─────────────────────────────────────────────────
    if (
        processed.probable_category
        and processed.probable_category == pattern.get("category")
    ):
        score += 0.05

    # ── Variable extraction bonus ────────────────────────────────────────────
    if variables_extracted:
        score += 0.04

    # ── Stacktrace bonus (more context = more confidence) ────────────────────
    if processed.has_stacktrace:
        score += 0.02

    return max(0.0, min(1.0, round(score, 4)))
