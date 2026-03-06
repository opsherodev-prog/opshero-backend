"""
Solution generator.
Takes a matched pattern + extracted variables and produces
final, user-ready solutions with interpolated commands.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Solution:
    rank: int
    title: str
    explanation: str
    confidence: float
    risk: str
    reversible: bool
    affects_files: bool
    requires_confirmation: bool
    command: Optional[str] = None


def generate_solutions(
    pattern: dict,
    variables: dict,
    user_tier: str = "free",
) -> list[Solution]:
    """
    Generate ranked solutions from a matched pattern.

    Interpolates {variable} placeholders in command_template
    and explanation using extracted variables.
    """
    raw_solutions = pattern.get("solutions", [])
    result: list[Solution] = []

    for s in sorted(raw_solutions, key=lambda x: x.get("rank", 99)):
        command = _interpolate(s.get("command_template") or s.get("command"), variables)
        explanation = _interpolate(s.get("explanation", ""), variables)
        title = _interpolate(s.get("title", ""), variables)

        result.append(Solution(
            rank=s.get("rank", len(result) + 1),
            title=title,
            explanation=explanation,
            confidence=float(s.get("confidence", s.get("base_confidence", 0.7))),
            risk=s.get("risk", "low"),
            reversible=s.get("reversible", True),
            affects_files=s.get("affects_files", False),
            requires_confirmation=s.get("requires_confirmation", False),
            command=command,
        ))

    return result


def _interpolate(template: Optional[str], variables: dict) -> Optional[str]:
    """
    Replace {variable_name} placeholders with extracted values.
    Leaves unresolved placeholders as-is with a [?] marker.
    """
    if not template:
        return template

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        value = variables.get(key)
        if value is not None:
            return str(value)
        return f"[{key}?]"  # Unresolved placeholder — visible to user

    return re.sub(r"\{(\w+)\}", replacer, template)
