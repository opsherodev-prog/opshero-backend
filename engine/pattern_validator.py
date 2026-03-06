"""
Pattern Schema Validator — validates a pattern dict against schema v2.0.0.

Used by:
  - webhooks.py (GitHub PR merge → auto-seed)
  - admin_contributions.py (promote form submission → live pattern)
  - admin_learning.py (promote AI candidate → live pattern)
"""

import re
from typing import Any

REQUIRED_TOP_LEVEL = {
    "pattern_id", "version", "name", "category", "severity", "detection", "solutions"
}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_RISKS = {"none", "low", "medium", "high"}
REQUIRED_DETECTION_FIELDS = {"regex", "keywords_required"}
REQUIRED_SOLUTION_FIELDS = {"rank", "title", "explanation", "confidence"}


def validate_pattern(pattern: Any) -> list[str]:
    """
    Validate a single pattern dict against schema v2.0.0.
    Returns a list of error strings — empty list means valid.
    """
    errors: list[str] = []

    if not isinstance(pattern, dict):
        return ["Pattern must be a JSON object"]

    # ── Required top-level fields ─────────────────────────────────────────────
    for f in REQUIRED_TOP_LEVEL:
        if f not in pattern:
            errors.append(f"Missing required field: '{f}'")

    if errors:
        return errors  # Can't validate further without core fields

    # ── pattern_id ────────────────────────────────────────────────────────────
    pid = pattern["pattern_id"]
    if not isinstance(pid, str) or not re.match(r"^[a-z][a-z0-9_]{2,79}$", pid):
        errors.append(
            "pattern_id must be lowercase letters/digits/underscores, 3–80 chars, "
            "starting with a letter (e.g. 'k8s_crash_loop_backoff')"
        )

    # ── version ───────────────────────────────────────────────────────────────
    if not isinstance(pattern["version"], str):
        errors.append("version must be a string (e.g. '1.0.0')")

    # ── name ──────────────────────────────────────────────────────────────────
    if not isinstance(pattern["name"], str) or not pattern["name"].strip():
        errors.append("name must be a non-empty string")

    # ── severity ─────────────────────────────────────────────────────────────
    if pattern["severity"] not in VALID_SEVERITIES:
        errors.append(
            f"severity must be one of: {', '.join(sorted(VALID_SEVERITIES))} "
            f"(got '{pattern['severity']}')"
        )

    # ── detection block ───────────────────────────────────────────────────────
    detection = pattern.get("detection", {})
    if not isinstance(detection, dict):
        errors.append("detection must be an object")
    else:
        for f in REQUIRED_DETECTION_FIELDS:
            if f not in detection:
                errors.append(f"detection.{f} is required")

        regex_str = detection.get("regex")
        if regex_str is not None:
            if not isinstance(regex_str, str):
                errors.append("detection.regex must be a string")
            elif regex_str.strip():
                try:
                    re.compile(regex_str, re.IGNORECASE | re.MULTILINE)
                except re.error as e:
                    errors.append(f"detection.regex is invalid: {e}")

        kw_req = detection.get("keywords_required")
        if kw_req is not None and not isinstance(kw_req, list):
            errors.append("detection.keywords_required must be an array")

        kw_opt = detection.get("keywords_optional")
        if kw_opt is not None and not isinstance(kw_opt, list):
            errors.append("detection.keywords_optional must be an array")

        exc = detection.get("exclude_if")
        if exc is not None and not isinstance(exc, list):
            errors.append("detection.exclude_if must be an array")

    # ── solutions array ───────────────────────────────────────────────────────
    solutions = pattern.get("solutions", [])
    if not isinstance(solutions, list) or len(solutions) == 0:
        errors.append("solutions must be a non-empty array")
    else:
        ranks_seen: set[int] = set()
        for i, sol in enumerate(solutions):
            if not isinstance(sol, dict):
                errors.append(f"solutions[{i}] must be an object")
                continue
            for f in REQUIRED_SOLUTION_FIELDS:
                if f not in sol:
                    errors.append(f"solutions[{i}].{f} is required")

            rank = sol.get("rank")
            if rank is not None:
                if not isinstance(rank, int):
                    errors.append(f"solutions[{i}].rank must be an integer")
                elif rank in ranks_seen:
                    errors.append(f"solutions[{i}].rank={rank} is duplicated")
                else:
                    ranks_seen.add(rank)

            conf = sol.get("confidence")
            if conf is not None:
                try:
                    if not (0.0 <= float(conf) <= 1.0):
                        errors.append(f"solutions[{i}].confidence must be 0.0–1.0")
                except (TypeError, ValueError):
                    errors.append(f"solutions[{i}].confidence must be a number")

            risk = sol.get("risk")
            if risk is not None and risk not in VALID_RISKS:
                errors.append(
                    f"solutions[{i}].risk must be one of: {', '.join(sorted(VALID_RISKS))}"
                )

    # ── optional tags ─────────────────────────────────────────────────────────
    tags = pattern.get("tags")
    if tags is not None and not isinstance(tags, list):
        errors.append("tags must be an array of strings")

    return errors


def validate_pattern_file(data: Any) -> tuple[list[dict], list[str]]:
    """
    Validate a full pattern file dict (e.g. kubernetes.json).
    Returns (valid_patterns, all_errors).
    Valid patterns are those that passed all checks.
    """
    if not isinstance(data, dict):
        return [], ["File root must be a JSON object"]

    errors: list[str] = []

    if "schema_version" not in data:
        errors.append("Missing 'schema_version' field in file root")

    patterns = data.get("patterns")
    if not isinstance(patterns, list):
        return [], [*errors, "'patterns' must be an array"]

    if len(patterns) == 0:
        errors.append("'patterns' array is empty")
        return [], errors

    valid: list[dict] = []
    seen_ids: set[str] = set()

    for i, p in enumerate(patterns):
        if not isinstance(p, dict):
            errors.append(f"patterns[{i}]: must be a JSON object")
            continue

        pid = p.get("pattern_id", f"<index {i}>")
        pid_str = str(pid)

        if pid_str in seen_ids:
            errors.append(f"patterns[{i}]: duplicate pattern_id '{pid_str}'")
            continue
        seen_ids.add(pid_str)

        p_errors = validate_pattern(p)
        if p_errors:
            for e in p_errors:
                errors.append(f"patterns[{i}] ({pid_str}): {e}")
        else:
            valid.append(p)

    return valid, errors


def validate_pattern_strict(pattern: Any) -> None:
    """
    Validate a pattern and raise ValueError with all errors if invalid.
    Convenience wrapper for places that want to raise on failure.
    """
    errors = validate_pattern(pattern)
    if errors:
        raise ValueError("Pattern validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
