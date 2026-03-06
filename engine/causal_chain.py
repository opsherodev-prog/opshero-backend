"""
Causal chain detector.
Identifies cascading errors in a single log output.
"""

from dataclasses import dataclass
from typing import Optional

from engine.index import PatternIndex


@dataclass
class CausalChain:
    current_pattern_id: str
    current_pattern_name: str
    root_causes: list[dict]       # patterns that likely caused this
    downstream_effects: list[dict]  # patterns this likely causes
    recommendation: Optional[str]


def detect_causal_chain(
    pattern_id: str,
    log: str,
    index: PatternIndex,
) -> Optional[CausalChain]:
    """
    Check if the matched pattern is part of a known error cascade.

    Example cascade:
      docker_no_space_left → docker_layer_cache_failure
      docker_credential_helper_error → docker_invalid_base_image

    Returns None if no causal relationships found.
    """
    pattern = index.get_pattern(pattern_id)
    if not pattern:
        return None

    causal = pattern.get("causal_chain", {})
    caused_by_ids: list[str] = causal.get("often_caused_by", [])
    often_causes_ids: list[str] = causal.get("often_causes", [])

    if not caused_by_ids and not often_causes_ids:
        return None

    log_lower = log.lower()
    root_causes: list[dict] = []
    downstream_effects: list[dict] = []

    for cause_id in caused_by_ids:
        cause_pattern = index.get_pattern(cause_id)
        if cause_pattern and _quick_match(cause_pattern, log_lower):
            root_causes.append({
                "pattern_id": cause_id,
                "name": cause_pattern.get("name", cause_id),
                "role": "root_cause",
            })

    for effect_id in often_causes_ids:
        effect_pattern = index.get_pattern(effect_id)
        if effect_pattern and _quick_match(effect_pattern, log_lower):
            downstream_effects.append({
                "pattern_id": effect_id,
                "name": effect_pattern.get("name", effect_id),
                "role": "downstream_effect",
            })

    if not root_causes and not downstream_effects:
        return None

    if root_causes:
        recommendation = (
            f"Fix '{root_causes[0]['name']}' first — "
            f"it likely triggered this error."
        )
    else:
        recommendation = (
            f"Resolving this may also fix '{downstream_effects[0]['name']}' downstream."
        )

    return CausalChain(
        current_pattern_id=pattern_id,
        current_pattern_name=pattern.get("name", pattern_id),
        root_causes=root_causes,
        downstream_effects=downstream_effects,
        recommendation=recommendation,
    )


def _quick_match(pattern: dict, log_lower: str) -> bool:
    """Check if a pattern's required keywords all appear in the log."""
    keywords = pattern.get("detection", {}).get("keywords_required", [])
    if not keywords:
        return False
    return all(kw.lower() in log_lower for kw in keywords)
