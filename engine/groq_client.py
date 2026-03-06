"""
Groq LLM client — OpenAI-compatible endpoint.
Used as fallback when regex engine confidence < threshold.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, RateLimitError

logger = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are OpsHero, an expert DevOps/SRE engineer specializing in CI/CD pipeline error diagnosis.

Your task: analyze CI/CD error logs and return PRECISE, ACTIONABLE solutions.

RULES (non-negotiable):
1. Return ONLY valid JSON — no markdown, no explanation outside the JSON
2. Provide REAL solutions with actual shell commands that exist
3. Extract actual values from the log (package names, file paths, version numbers)
4. Rank solutions by probability of fixing the issue (rank 1 = most likely)
5. confidence must reflect certainty — never give 0.99 unless the match is obvious
6. If you cannot determine a solution, still return the JSON with a generic helpful explanation
7. NEVER invent non-existent commands

REQUIRED JSON SCHEMA:
{
  "pattern_id": "descriptive_snake_case_id",
  "confidence": 0.0-1.0,
  "error_category": "docker|npm|python|git|tests|auth|other",
  "error_type": "short description of the root cause (< 80 chars)",
  "extracted_variables": {"var_name": "extracted_value"},
  "solutions": [
    {
      "rank": 1,
      "title": "Short action title (< 60 chars)",
      "command": "exact shell command or null",
      "explanation": "Why this fixes the problem. What to do step by step. (2-4 sentences)",
      "confidence": 0.0-1.0,
      "risk": "none|low|medium|high",
      "reversible": true
    }
  ],
  "causal_hint": "optional: what likely triggered this error"
}

CATEGORY GUIDE (pick the most specific match):
- docker: Dockerfile, container build/run, image pull, registry, Docker daemon
- npm: Node.js, JavaScript, TypeScript, npm/yarn/pnpm, webpack, esbuild, node_modules
- python: Python runtime, pip, virtualenv, pytest, ImportError, traceback
- git: git clone/push/pull, merge conflicts, submodules, LFS, branch protection
- tests: unit/integration/e2e test failures, coverage thresholds, flaky tests
- auth: JWT, OAuth, API keys, TOTP, token validation, algorithm mismatch, SSL/TLS certs
- other: Java/Maven, Rust/Cargo, Go/mod, Terraform, Kubernetes, or unclassified"""


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class LLMSolution:
    rank: int
    title: str
    explanation: str
    confidence: float
    risk: str = "low"
    reversible: bool = True
    command: Optional[str] = None


@dataclass
class LLMResult:
    pattern_id: str
    confidence: float
    error_type: str
    error_category: str
    variables: dict
    solutions: list[LLMSolution]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    causal_hint: Optional[str] = None


# ── Model selector ────────────────────────────────────────────────────────────

class ModelSelector:
    def __init__(self, cfg):
        self.cfg = cfg

    def select(self, log_len: int) -> str:
        if log_len < self.cfg.llm_short_log_threshold:
            return self.cfg.llm_fast_model          # llama-3.1-8b-instant
        if log_len > self.cfg.llm_long_log_threshold:
            return self.cfg.llm_long_context_model   # mixtral-8x7b-32768
        return self.cfg.llm_primary_model            # llama-3.3-70b-versatile


# ── Budget tracker ────────────────────────────────────────────────────────────

class BudgetTracker:
    _DAILY_KEY = "llm:budget:daily:spent"
    _MONTHLY_KEY = "llm:budget:monthly:spent"

    # Pricing (USD per 1M tokens) — approximate Groq rates
    _PRICING: dict[str, tuple[float, float]] = {
        "llama-3.1-8b-instant":      (0.05, 0.08),
        "llama-3.3-70b-versatile":   (0.59, 0.79),
        "mixtral-8x7b-32768":        (0.24, 0.24),
    }

    def __init__(self, redis, cfg):
        self.redis = redis
        self.cfg = cfg

    async def can_spend(self) -> bool:
        daily = float(await self.redis.get(self._DAILY_KEY) or 0)
        return daily < self.cfg.llm_daily_budget_usd

    async def record(self, model: str, input_tok: int, output_tok: int) -> float:
        in_rate, out_rate = self._PRICING.get(model, (0.59, 0.79))
        cost = (input_tok * in_rate + output_tok * out_rate) / 1_000_000

        pipe = self.redis.pipeline()
        pipe.incrbyfloat(self._DAILY_KEY, cost)
        pipe.expire(self._DAILY_KEY, 86_400)
        pipe.incrbyfloat(self._MONTHLY_KEY, cost)
        pipe.expire(self._MONTHLY_KEY, 2_592_000)
        await pipe.execute()

        # Alert check (fire-and-forget)
        daily_spent = float(await self.redis.get(self._DAILY_KEY) or 0)
        if daily_spent >= self.cfg.llm_daily_budget_usd:
            logger.warning(f"Daily LLM budget EXHAUSTED: ${daily_spent:.2f}")

        return cost


# ── Groq client ───────────────────────────────────────────────────────────────

class GroqClient:
    """
    Calls Groq API via the OpenAI-compatible endpoint.
    base_url = https://api.groq.com/openai/v1
    """

    def __init__(self, api_key: str, base_url: str, selector: ModelSelector, budget: BudgetTracker):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._selector = selector
        self._budget = budget

    async def analyze(
        self,
        log: str,
        category_hint: Optional[str] = None,
        regex_candidate_id: Optional[str] = None,
    ) -> Optional[LLMResult]:
        """
        Analyze a log with Groq LLM.
        Returns None on failure or budget exhaustion — caller degrades gracefully.
        """
        if not await self._budget.can_spend():
            logger.warning("LLM budget exhausted — skipping Groq call")
            return None

        model = self._selector.select(len(log))
        prompt = self._build_prompt(log, category_hint, regex_candidate_id)

        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1024,
                response_format={"type": "json_object"},
                timeout=15.0,
            )
        except APITimeoutError:
            logger.warning("Groq API timeout (15s)")
            return None
        except RateLimitError:
            logger.warning("Groq rate limit hit")
            return None
        except APIConnectionError as e:
            logger.error(f"Groq connection error: {e}")
            return None
        except Exception as e:
            logger.error(f"Groq unexpected error: {type(e).__name__}: {e}")
            return None

        latency_ms = int((time.monotonic() - start) * 1000)
        usage = response.usage

        await self._budget.record(
            model=model,
            input_tok=usage.prompt_tokens,
            output_tok=usage.completion_tokens,
        )

        raw = response.choices[0].message.content
        parsed = self._parse_json(raw)
        if not parsed:
            return None

        solutions = [
            LLMSolution(
                rank=s.get("rank", i + 1),
                title=s.get("title", "Solution"),
                explanation=s.get("explanation", ""),
                confidence=float(s.get("confidence", 0.5)),
                risk=s.get("risk", "low"),
                reversible=s.get("reversible", True),
                command=s.get("command") or None,
            )
            for i, s in enumerate(parsed.get("solutions", []))
        ]

        return LLMResult(
            pattern_id=parsed.get("pattern_id", "llm_unclassified"),
            confidence=float(parsed.get("confidence", 0.5)),
            error_type=parsed.get("error_type", "Unknown"),
            error_category=parsed.get("error_category", category_hint or "other"),
            variables=parsed.get("extracted_variables", {}),
            solutions=solutions,
            model=model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
            causal_hint=parsed.get("causal_hint"),
        )

    def _build_prompt(
        self,
        log: str,
        category_hint: Optional[str],
        regex_candidate_id: Optional[str],
    ) -> str:
        parts: list[str] = []

        if category_hint:
            parts.append(f"Context: This appears to be a **{category_hint}** error.")

        if regex_candidate_id:
            parts.append(
                f"Hint: The regex engine partially matched pattern `{regex_candidate_id}` "
                f"but with low confidence. Use as context only."
            )

        # Truncate log to 6000 chars for the prompt (keep the tail)
        log_excerpt = log[-6000:] if len(log) > 6000 else log
        if len(log) > 6000:
            parts.append(f"(Log truncated — showing last 6000 of {len(log)} chars)")

        parts.append(f"\nCI/CD Error Log:\n```\n{log_excerpt}\n```")
        parts.append("\nAnalyze this error. Return precise JSON with actionable solutions.")

        return "\n".join(parts)

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        """
        5-strategy JSON parser — handles common LLM output quirks.
        """
        if not raw or not raw.strip():
            return None

        # Strategy 1: Direct parse (happy path)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Strategy 2: Extract from markdown code block
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Strategy 3: Find outermost braces
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # Strategy 4: Fix trailing commas + single quotes
        fixed = re.sub(r",(\s*[}\]])", r"\1", raw)
        fixed = fixed.replace("'", '"')
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # Strategy 5: Minimal fallback
        logger.warning(f"Could not parse LLM JSON: {raw[:150]!r}")
        return {
            "pattern_id": "llm_parse_failed",
            "confidence": 0.2,
            "error_type": "Parse failed",
            "error_category": "other",
            "extracted_variables": {},
            "solutions": [{
                "rank": 1,
                "title": "Manual review required",
                "command": None,
                "explanation": (
                    "The error could not be automatically parsed. "
                    "Review the log output manually and search for the error message online."
                ),
                "confidence": 0.2,
                "risk": "none",
                "reversible": True,
            }],
        }
