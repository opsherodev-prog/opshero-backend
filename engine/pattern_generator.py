"""
Pattern Generator — the core AI learning engine.

This module is distinct from the analysis LLM call (which diagnoses a single log).
Its purpose: given N real-world examples of the SAME unknown error, generate a
complete, production-quality regex pattern for the permanent library.

Flow:
  pattern_candidates collection
    → candidate has 10+ sightings  (auto-promote threshold)
        ↓
  PatternGenerator.generate(candidate)
        ↓
  Dedicated Groq LLM call (pattern_generation prompt)
        ↓
  Structured PatternGenerationResult (schema v2 pattern dict)
        ↓
  PatternValidator validates strictly
        ↓
  If valid → auto-promoted to live library (hot-reload)
  If invalid → escalated to "ready_for_review" with the draft
        ↓
  Admin sees it in /admin/learning/ with pre-filled form

The LLM is given:
  - 1-5 real log examples from different users
  - The original auto-analysis from the analysis LLM
  - The current best-guess regex (from pattern_extractor)
  - Instructions to produce a complete schema v2 pattern

Result quality is far higher than single-pass extraction because:
  1. Multiple real examples → more robust regex
  2. Dedicated prompt → LLM focuses 100% on pattern quality, not diagnosis
  3. Strict validation loop → only correct patterns go live
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, RateLimitError

from engine.pattern_validator import validate_pattern

logger = logging.getLogger(__name__)


# ── System prompt ──────────────────────────────────────────────────────────────

_PATTERN_GEN_SYSTEM = """\
You are an expert pattern engineer for OpsHero, a CI/CD log analysis platform.

Your role: analyze real-world CI/CD error log examples and produce a PRECISE,
PRODUCTION-QUALITY detection pattern that will be permanently added to the
OpsHero pattern library.

CRITICAL RULES:
1. Return ONLY valid JSON — no markdown, no explanation outside the JSON
2. The regex MUST match ALL provided log examples
3. The regex must NOT be so broad it matches unrelated logs — be specific
4. keywords_required must be exact strings that appear verbatim in ALL examples
5. Solutions must include REAL, WORKING shell commands that actually fix the problem
6. severity scale: critical=production down/data loss, high=blocking, medium=warning, low=info
7. pattern_id: lowercase snake_case, 3-80 chars, descriptive (e.g. k8s_crash_loop_backoff)
8. Never invent non-existent commands

REQUIRED OUTPUT — OpsHero Pattern Schema v2.0.0:
{
  "pattern_id": "unique_snake_case_id",
  "version": "1.0.0",
  "name": "Human readable name (< 80 chars)",
  "category": "docker|npm|python|git|tests|kubernetes|terraform|security|ci|other",
  "subcategory": "specific subcategory",
  "severity": "critical|high|medium|low",
  "tags": ["tag1", "tag2", "category"],
  "detection": {
    "regex": "regex_pattern (re.IGNORECASE | re.MULTILINE compatible)",
    "keywords_required": ["must_present_verbatim"],
    "keywords_optional": ["helpful_if_present"],
    "exclude_if": ["if_present_not_this_error"],
    "file_patterns": [],
    "variables": {}
  },
  "solutions": [
    {
      "rank": 1,
      "title": "Short action (< 60 chars)",
      "explanation": "Why this fixes it. Step-by-step. 2-4 sentences.",
      "command_template": "actual shell command or empty string",
      "confidence": 0.0-1.0,
      "risk": "none|low|medium|high",
      "reversible": true,
      "affects_files": false,
      "requires_confirmation": false
    }
  ],
  "causal_chain": {
    "often_caused_by": [],
    "often_causes": []
  },
  "metadata": {
    "source": "ai_generated",
    "author": "ai-engine",
    "stats": {"match_count": 0, "helpful_count": 0, "not_helpful_count": 0}
  }
}"""


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PatternGenerationResult:
    success: bool
    pattern_data: Optional[dict] = None
    validation_errors: list[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    error: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return self.success and not self.validation_errors


# ── Generator ─────────────────────────────────────────────────────────────────

class PatternGenerator:
    """
    Uses a dedicated Groq LLM call to generate a complete schema v2 pattern
    from accumulated real-world error log examples.
    """

    # Use the most capable model for generation quality (not fast-path)
    GENERATION_MODEL = "llama-3.3-70b-versatile"
    MAX_EXAMPLES = 5       # send up to 5 distinct examples to the LLM
    MAX_LOG_CHARS = 1500   # truncate each example to keep prompt size manageable
    TIMEOUT = 30.0

    def __init__(self, api_key: str, base_url: str):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def generate(
        self,
        candidate: dict,
        extra_examples: Optional[list[str]] = None,
    ) -> PatternGenerationResult:
        """
        Generate a schema v2 pattern from a pattern candidate and its accumulated
        example logs.

        Args:
            candidate:      A pattern_candidates document from MongoDB
            extra_examples: Additional log snippets from related sightings
        """
        try:
            prompt = self._build_prompt(candidate, extra_examples or [])
        except Exception as e:
            return PatternGenerationResult(success=False, error=f"Prompt build failed: {e}")

        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=self.GENERATION_MODEL,
                messages=[
                    {"role": "system", "content": _PATTERN_GEN_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.05,   # very low temperature for deterministic output
                max_tokens=2048,    # patterns can be verbose
                response_format={"type": "json_object"},
                timeout=self.TIMEOUT,
            )
        except APITimeoutError:
            return PatternGenerationResult(success=False, error="LLM timeout (30s)")
        except RateLimitError:
            return PatternGenerationResult(success=False, error="LLM rate limit")
        except APIConnectionError as e:
            return PatternGenerationResult(success=False, error=f"LLM connection error: {e}")
        except Exception as e:
            return PatternGenerationResult(success=False, error=f"LLM error: {type(e).__name__}: {e}")

        latency_ms = int((time.monotonic() - start) * 1000)
        usage = response.usage

        raw = response.choices[0].message.content
        pattern_data = self._parse_json(raw)
        if not pattern_data:
            return PatternGenerationResult(
                success=False,
                error="LLM returned non-parseable JSON",
                model=self.GENERATION_MODEL,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                latency_ms=latency_ms,
            )

        # Inject metadata source
        if "metadata" not in pattern_data:
            pattern_data["metadata"] = {}
        pattern_data["metadata"]["source"] = "ai_generated"
        pattern_data["metadata"]["author"] = "ai-engine"
        pattern_data["metadata"].setdefault("stats", {
            "match_count": 0, "helpful_count": 0, "not_helpful_count": 0
        })
        pattern_data["metadata"]["generated_from_candidate"] = candidate.get("id")
        pattern_data["metadata"]["generation_model"] = self.GENERATION_MODEL

        # Validate against schema v2
        validation_errors = validate_pattern(pattern_data)

        return PatternGenerationResult(
            success=True,
            pattern_data=pattern_data,
            validation_errors=validation_errors,
            model=self.GENERATION_MODEL,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
        )

    def _build_prompt(self, candidate: dict, extra_examples: list[str]) -> str:
        """
        Build the pattern generation prompt from a candidate and its examples.
        """
        parts: list[str] = []

        # ── Context block ────────────────────────────────────────────────────
        parts.append("## Error Overview\n")

        llm_pid = candidate.get("llm_pattern_id") or candidate.get("pattern_id", "unknown")
        llm_type = candidate.get("llm_error_type") or candidate.get("llm_category", "")
        category = candidate.get("llm_category") or candidate.get("category", "other")
        sightings = candidate.get("unmatched_count", 1)
        confidence = candidate.get("llm_confidence", 0.0)

        parts.append(
            f"- Pattern ID (suggested): `{llm_pid}`\n"
            f"- Error type: {llm_type}\n"
            f"- Category: {category}\n"
            f"- Times seen: {sightings} (across {sightings} real user logs)\n"
            f"- Average LLM confidence: {confidence:.0%}\n"
        )

        if candidate.get("llm_causal_hint"):
            parts.append(f"- Causal hint: {candidate['llm_causal_hint']}\n")

        # ── Existing regex hint ───────────────────────────────────────────────
        if candidate.get("suggested_regex"):
            parts.append(
                f"\n## Existing Regex (auto-extracted, may be imprecise)\n"
                f"```\n{candidate['suggested_regex']}\n```\n"
                f"Improve this regex to be more precise and robust.\n"
            )

        if candidate.get("extracted_keywords"):
            kw = ", ".join(f'`{k}`' for k in candidate["extracted_keywords"][:8])
            parts.append(f"\n## Extracted Keywords\n{kw}\n")

        # ── Example logs ─────────────────────────────────────────────────────
        examples: list[str] = []

        # Primary example from the candidate
        primary = candidate.get("example_log_snippet") or candidate.get("example_log", "")
        if primary:
            examples.append(primary[:self.MAX_LOG_CHARS])

        # Additional examples from related sightings
        for ex in extra_examples[: self.MAX_EXAMPLES - 1]:
            if ex and ex not in examples:
                examples.append(ex[:self.MAX_LOG_CHARS])

        if examples:
            parts.append(f"\n## Real-World Log Examples ({len(examples)} example(s))\n")
            for i, ex in enumerate(examples, 1):
                parts.append(f"### Example {i}\n```\n{ex}\n```\n")

        # ── Existing solutions from LLM analysis ────────────────────────────
        existing_solutions = candidate.get("pattern_data", {}).get("solutions") or []
        if existing_solutions:
            parts.append("\n## Existing Solutions (refine and improve these)\n")
            for s in existing_solutions[:3]:
                title = s.get("title", "")
                cmd = s.get("command_template") or s.get("command") or ""
                expl = s.get("explanation", "")[:200]
                parts.append(f"- **{title}**: {expl}" + (f"\n  Command: `{cmd}`" if cmd else "") + "\n")

        # ── Task ─────────────────────────────────────────────────────────────
        parts.append(
            "\n## Your Task\n"
            "Based on the examples above, generate a complete OpsHero pattern schema v2.0.0 JSON.\n\n"
            "Requirements:\n"
            "1. The `detection.regex` MUST match all provided log examples\n"
            "2. The regex must be specific enough to avoid false positives\n"
            "3. `keywords_required` must appear verbatim in ALL examples\n"
            "4. Solutions must have real, working shell commands\n"
            "5. The `pattern_id` must be unique, lowercase snake_case\n\n"
            "Return ONLY the JSON pattern object, nothing else."
        )

        return "\n".join(parts)

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        """Multi-strategy JSON parser (same approach as GroqClient)."""
        if not raw or not raw.strip():
            return None

        # Strategy 1: Direct parse
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Strategy 2: Markdown code block
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 3: Outermost braces
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # Strategy 4: Fix trailing commas
        try:
            fixed = re.sub(r",(\s*[}\]])", r"\1", raw)
            m2 = re.search(r"\{.*\}", fixed, re.DOTALL)
            if m2:
                return json.loads(m2.group(0))
        except json.JSONDecodeError:
            pass

        logger.warning("Pattern generator: failed to parse LLM JSON response")
        return None


    def _validate_regex_against_examples(
        self,
        regex_str: str,
        examples: list[str],
    ) -> tuple[bool, str]:
        """
        Test whether the generated regex matches all provided example logs.
        Returns (all_matched, error_message).
        """
        try:
            compiled = re.compile(regex_str, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            return False, f"Regex compile error: {e}"

        unmatched = []
        for i, example in enumerate(examples):
            if not compiled.search(example):
                unmatched.append(i + 1)

        if unmatched:
            return False, f"Regex did not match examples: {unmatched}"

        return True, ""

    async def generate_and_validate(
        self,
        candidate: dict,
        extra_examples: Optional[list[str]] = None,
        retry_on_regex_mismatch: bool = True,
    ) -> PatternGenerationResult:
        """
        Generate a pattern and validate the regex against example logs.
        If the regex doesn't match the examples, retry once with a correction hint.
        """
        examples = []
        if candidate.get("example_log_snippet"):
            examples.append(candidate["example_log_snippet"])
        if extra_examples:
            examples.extend(extra_examples[:self.MAX_EXAMPLES - 1])

        result = await self.generate(candidate, extra_examples)

        if not result.success or not result.pattern_data:
            return result

        # Test regex against examples
        regex_str = result.pattern_data.get("detection", {}).get("regex", "")
        if regex_str and examples:
            matched, error_msg = self._validate_regex_against_examples(regex_str, examples)

            if not matched and retry_on_regex_mismatch:
                logger.info(
                    "Generated regex did not match examples (%s), retrying with correction hint",
                    error_msg,
                )
                # Add correction hint to candidate and retry
                augmented_candidate = {
                    **candidate,
                    "_regex_correction_hint": (
                        f"IMPORTANT: Your previous regex `{regex_str}` did NOT match the examples. "
                        f"Error: {error_msg}. Please look more carefully at the exact error text "
                        f"in the examples and write a more precise regex."
                    ),
                }
                result = await self.generate(augmented_candidate, extra_examples)

        return result


# ── Factory (created in main.py lifespan, injected where needed) ──────────────

_generator: Optional[PatternGenerator] = None


def set_pattern_generator(generator: PatternGenerator) -> None:
    global _generator
    _generator = generator


def get_pattern_generator() -> Optional[PatternGenerator]:
    return _generator
