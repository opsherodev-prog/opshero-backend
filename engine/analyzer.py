"""
OpsHero Hybrid Analysis Engine — main entry point.

Pipeline:
  1. Pre-process log (clean, tokenize, detect category)
  2. Lookup candidates via inverted index (O(1))
  3. Test each candidate (exclude_if, keywords_required, regex)
  4. Score confidence (composite: historical + regex + keywords)
  5. Decision: regex result OR Groq LLM fallback
  6. Generate solutions with interpolated variables
  7. Detect causal chain (cascading errors)
"""

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from engine.preprocessor import preprocess_log, ProcessedLog
from engine.index import PatternIndex
from engine.scorer import compute_confidence
from engine.groq_client import GroqClient, LLMResult
from engine.solution_generator import generate_solutions, Solution
from engine.causal_chain import detect_causal_chain, CausalChain

logger = logging.getLogger(__name__)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    # Identity
    user_id: str
    client_id: str
    log_hash: str = ""

    # Match result
    pattern_id: Optional[str] = None
    confidence: float = 0.0
    match_method: str = "no_match"  # regex | groq_llm | regex_low_confidence | generic_fallback
    detected_category: Optional[str] = None
    extracted_vars: dict = field(default_factory=dict)

    # Solutions
    solutions: list[Solution] = field(default_factory=list)
    causal_chain: Optional[CausalChain] = None

    # LLM metadata (when used)
    llm_model: Optional[str] = None
    llm_latency_ms: Optional[int] = None
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0

    # Timings
    total_latency_ms: int = 0
    log_size_chars: int = 0

    # Error (if something went wrong — engine never raises)
    error: Optional[str] = None


@dataclass
class _MatchedCandidate:
    pattern_id: str
    pattern: dict
    confidence: float
    variables: dict
    regex_matched: bool


# ── Main analyzer ─────────────────────────────────────────────────────────────

class HybridAnalyzer:
    """
    Hybrid regex + Groq LLM analyzer.
    Always returns an AnalysisResult, never raises.
    """

    MAX_CANDIDATES = 10

    def __init__(
        self,
        index: PatternIndex,
        groq: Optional[GroqClient],
        confidence_threshold: float = 0.65,
    ):
        self.index = index
        self.groq = groq
        self.confidence_threshold = confidence_threshold

    async def analyze(
        self,
        raw_log: str,
        user_id: str,
        user_tier: str,
        client_id: str,
        context: dict | None = None,
    ) -> AnalysisResult:
        """
        Analyze a raw CI/CD log.
        Returns AnalysisResult always — degraded gracefully on any error.
        """
        start = time.monotonic()
        result = AnalysisResult(
            user_id=user_id,
            client_id=client_id,
            log_hash=hashlib.sha256(raw_log.encode()).hexdigest()[:16],
        )

        try:
            # ─ Step 1: Pre-processing ────────────────────────────────────
            processed = preprocess_log(raw_log)
            result.detected_category = processed.probable_category
            result.log_size_chars = processed.log_size_chars

            # ─ Step 2: Candidate lookup (inverted index) ─────────────────
            candidates = self.index.lookup(
                log_tokens=processed.keyword_tokens,
                category_hint=processed.probable_category,
                top_k=self.MAX_CANDIDATES,
            )

            # ─ Step 3 & 4: Test candidates + score ───────────────────────
            matched: list[_MatchedCandidate] = []
            for pattern in candidates:
                m = self._test_pattern(pattern, processed)
                if m:
                    matched.append(m)

            # ─ Step 5: Decision ───────────────────────────────────────────
            best: Optional[_MatchedCandidate] = None
            if matched:
                best = max(matched, key=lambda m: m.confidence)
                result.pattern_id = best.pattern_id
                result.confidence = best.confidence
                result.extracted_vars = best.variables
                result.match_method = "regex"

            use_llm = (
                self.groq is not None
                and self._llm_allowed_for_tier(user_tier)
                and (best is None or best.confidence < self.confidence_threshold)
            )

            if use_llm:
                assert self.groq is not None
                llm_result = await self.groq.analyze(
                    log=processed.cleaned_log,
                    category_hint=processed.probable_category,
                    regex_candidate_id=best.pattern_id if best else None,
                )
                if llm_result:
                    result.llm_model = llm_result.model
                    result.llm_latency_ms = llm_result.latency_ms
                    result.llm_input_tokens = llm_result.input_tokens
                    result.llm_output_tokens = llm_result.output_tokens

                    if llm_result.confidence > (best.confidence if best else 0):
                        result.pattern_id = llm_result.pattern_id
                        result.confidence = llm_result.confidence
                        result.extracted_vars = llm_result.variables
                        result.match_method = "groq_llm"
                        result.detected_category = (
                            llm_result.error_category or result.detected_category
                        )
                        # Convert LLM solutions to Solution objects
                        result.solutions = [
                            Solution(
                                rank=s.rank,
                                title=s.title,
                                explanation=s.explanation,
                                confidence=s.confidence,
                                risk=s.risk,
                                reversible=s.reversible,
                                affects_files=False,
                                requires_confirmation=s.risk in ("high",),
                                command=s.command,
                            )
                            for s in llm_result.solutions
                        ]
                    elif best:
                        result.match_method = "regex_low_confidence"
                    else:
                        result.match_method = "generic_fallback"
                elif best:
                    result.match_method = "regex_low_confidence"
                else:
                    result.match_method = "generic_fallback"

            # ─ Step 6: Generate solutions (regex path) ───────────────────
            if result.pattern_id and not result.solutions:
                pattern = self.index.get_pattern(result.pattern_id)
                if pattern:
                    result.solutions = generate_solutions(
                        pattern=pattern,
                        variables=result.extracted_vars,
                        user_tier=user_tier,
                    )

            # ─ Step 7: Causal chain ───────────────────────────────────────
            if result.pattern_id:
                result.causal_chain = detect_causal_chain(
                    pattern_id=result.pattern_id,
                    log=processed.cleaned_log,
                    index=self.index,
                )

        except Exception as e:
            logger.exception(f"Analyzer error for user {user_id}: {e}")
            result.match_method = "error_fallback"
            result.error = str(e)

        finally:
            result.total_latency_ms = int((time.monotonic() - start) * 1000)

        return result

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _test_pattern(
        self,
        pattern: dict,
        processed: ProcessedLog,
    ) -> Optional[_MatchedCandidate]:
        """
        Test a single pattern against a preprocessed log.
        Returns None if the pattern doesn't match.
        """
        detection = pattern.get("detection", {})
        log_lower = processed.cleaned_log.lower()

        # 1. Fast exclude_if check (anti-false-positive)
        for exc in detection.get("exclude_if", []):
            if exc.lower() in log_lower:
                return None

        # 2. All required keywords must be present
        for kw in detection.get("keywords_required", []):
            if kw.lower() not in log_lower:
                return None

        # 3. Regex match + variable extraction
        variables: dict = {}
        regex_matched = False
        regex_str = detection.get("regex")

        if regex_str:
            try:
                compiled = re.compile(regex_str, re.IGNORECASE | re.MULTILINE)
                m = compiled.search(processed.cleaned_log)
                if m:
                    regex_matched = True
                    variables = self._extract_variables(
                        detection.get("variables", {}),
                        m,
                    )
            except re.error as e:
                logger.warning(
                    f"Regex error in pattern {pattern.get('pattern_id')}: {e}"
                )
                return None

        # 4. Compute confidence
        confidence = compute_confidence(
            pattern=pattern,
            processed=processed,
            regex_matched=regex_matched,
            variables_extracted=bool(variables),
        )

        if confidence < 0.25:
            return None  # Too weak — not worth considering

        return _MatchedCandidate(
            pattern_id=pattern["pattern_id"],
            pattern=pattern,
            confidence=confidence,
            variables=variables,
            regex_matched=regex_matched,
        )

    def _extract_variables(
        self,
        var_defs: dict,
        match: re.Match,
    ) -> dict:
        """Extract and transform variables from regex capture groups."""
        result: dict = {}
        groups = match.groups()

        for var_name, var_def in var_defs.items():
            source: str = var_def.get("from", "regex_group_1")
            default: str = var_def.get("default", "")
            transform: str = var_def.get("transform", "strip")

            try:
                if source.startswith("regex_group_"):
                    idx = int(source.split("_")[-1]) - 1
                    raw_value = groups[idx] if idx < len(groups) else default
                elif source == "named_group":
                    raw_value = match.group(var_name) or default
                else:
                    raw_value = default

                value = raw_value or default
                result[var_name] = _apply_transform(value, transform)

            except (IndexError, AttributeError, IndexError):
                result[var_name] = default

        return result

    def _llm_allowed_for_tier(self, tier: str) -> bool:
        """Check if LLM fallback is enabled for this user tier (from config)."""
        from config import settings
        if not settings.llm_enabled:
            return False
        return getattr(settings, f"llm_enabled_for_{tier}", False)


# ── Transform helpers ─────────────────────────────────────────────────────────

def _apply_transform(value: str, transform: str) -> str:
    match transform:
        case "strip":     return value.strip()
        case "lowercase": return value.strip().lower()
        case "uppercase": return value.strip().upper()
        case "basename":
            v = value.strip()
            return v.split("/")[-1].split("\\")[-1]
        case _:           return value
