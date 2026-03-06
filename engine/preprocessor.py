"""
Log pre-processor.
Cleans raw CI/CD logs and extracts signals for pattern matching.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ── ANSI escape code removal ─────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# ── Timestamp patterns (various CI formats) ───────────────────────────────────
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)

# ── Category detection signals ────────────────────────────────────────────────
_CATEGORY_SIGNALS: dict[str, set[str]] = {
    "docker": {
        "dockerfile", "docker", "container", "image", "layer", "buildkit",
        "apt-get", "dockerignore", "docker-compose", "runc", "oci",
        "registry", "manifest", "buildx", "entrypoint",
    },
    "npm": {
        "npm err!", "node_modules", "package-lock", "package.json",
        "registry.npmjs", "eresolve", "ebadengine", "node-gyp",
        "yarn", "pnpm", "enospc", "npx",
        # Node.js / JavaScript runtime errors
        "typeerror:", "referenceerror:", "rangeerror:", "syntaxerror:",
        "uncaughtexception", "unhandledpromiserejection",
        "cannot read properties", "is not a function", "is not defined",
        "require(", ".js:", "node:", "at new ", "at async ",
        "express", "nestjs", "webpack", "esbuild", "vite",
    },
    "python": {
        "traceback", "modulenotfounderror", "importerror",
        "pip", "requirements.txt", "virtualenv", "setuptools", "pypi",
        "python", "venv", "pytest", "poetry",
        "indentationerror", "attributeerror", "valueerror",
        ".py:", "def ", "class ", "pip install",
    },
    "git": {
        "fatal:", "authentication failed", "merge conflict", "push",
        "remote:", "submodule", "branch", "refs/heads", "rebase",
        "gitmodules", "git lfs",
    },
    "tests": {
        "assertionerror", "failed", "passed", "pytest", "jest",
        "coverage", "timeout", "testcase", "beforeall", "afterall",
        "describe", "it(", "expect(",
    },
    "auth": {
        "unauthorized", "forbidden", "jwt", "token", "bearer",
        "oauth", "authentication", "authorization", "invalid signature",
        "algorithm mismatch", "access denied", "permission denied",
        "invalid credentials", "api key", "secret key",
        "rs256", "hs256", "hs512", "totp", "2fa",
    },
}

# ── Stacktrace detection ───────────────────────────────────────────────────────
_STACKTRACE_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|at .*\(.*:\d+:\d+\)"     # JS stacktrace
    r"|at .+\..+\(.+\)"         # Java stacktrace
    r"|^\s+File \".+\", line \d+",  # Python
    re.MULTILINE,
)


@dataclass
class ProcessedLog:
    cleaned_log: str
    keyword_tokens: set[str]
    probable_category: Optional[str]
    log_size_chars: int
    line_count: int
    has_stacktrace: bool = False
    is_truncated: bool = False


def preprocess_log(raw_log: str, max_chars: int = 8000) -> ProcessedLog:
    """
    Clean and tokenize a raw CI/CD log for the matching engine.

    Truncation strategy: keep the TAIL of the log.
    Errors are always at the end, preamble is rarely useful.
    """
    if not raw_log:
        return ProcessedLog(
            cleaned_log="",
            keyword_tokens=set(),
            probable_category=None,
            log_size_chars=0,
            line_count=0,
        )

    # 1. Remove ANSI color/control codes
    clean = _ANSI_RE.sub("", raw_log)

    # 2. Normalize line endings
    clean = clean.replace("\r\n", "\n").replace("\r", "\n")

    # 3. Strip leading/trailing whitespace per line (preserve structure)
    lines = [line.rstrip() for line in clean.split("\n")]
    clean = "\n".join(lines)

    # 4. Truncate — keep the last max_chars (errors are at the bottom)
    is_truncated = False
    if len(clean) > max_chars:
        clean = "...[log truncated]\n" + clean[-max_chars:]
        is_truncated = True

    # 5. Detect stacktrace
    has_stacktrace = bool(_STACKTRACE_RE.search(clean))

    # 6. Tokenize: extract meaningful words for index lookup
    # Include multi-word tokens like "npm err!" by lowercasing the full log
    clean_lower = clean.lower()
    # Single tokens
    word_tokens: set[str] = set(re.findall(r"[a-z][a-z0-9_\-\.!:]{2,}", clean_lower))
    # Multi-word signals (for compound keywords like "npm err!")
    compound_tokens: set[str] = set()
    for cat_signals in _CATEGORY_SIGNALS.values():
        for signal in cat_signals:
            if " " in signal and signal in clean_lower:
                compound_tokens.add(signal)

    all_tokens = word_tokens | compound_tokens

    # 7. Detect probable category
    category = _detect_category(all_tokens, clean_lower)

    return ProcessedLog(
        cleaned_log=clean,
        keyword_tokens=all_tokens,
        probable_category=category,
        log_size_chars=len(clean),
        line_count=clean.count("\n") + 1,
        has_stacktrace=has_stacktrace,
        is_truncated=is_truncated,
    )


def _detect_category(tokens: set[str], log_lower: str) -> Optional[str]:
    """
    Score each category by how many of its signal keywords appear.
    Returns the best category if score >= 2, else None.
    'auth' is a valid detection category but maps downstream to 'other'
    since no dedicated auth pattern file exists yet.
    """
    scores: dict[str, int] = {}

    for cat, signals in _CATEGORY_SIGNALS.items():
        score = 0
        for signal in signals:
            if " " in signal:
                # Compound keyword — check in full log string
                if signal in log_lower:
                    score += 2  # Compound match is stronger
            elif signal in tokens:
                score += 1
        scores[cat] = score

    best_cat = max(scores, key=scores.__getitem__)
    if scores[best_cat] < 2:
        return None
    # 'auth' has no dedicated pattern file — keep it as the category label
    # so the LLM knows the context, but the index won't find auth-specific patterns.
    return best_cat
