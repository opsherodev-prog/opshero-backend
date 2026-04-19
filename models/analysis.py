"""Analysis model — represents one CI/CD log analysis."""

from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, Field
from uuid import uuid4


class SolutionOut(BaseModel):
    rank: int
    title: str
    explanation: str
    confidence: float
    risk: str
    reversible: bool
    affects_files: bool
    requires_confirmation: bool
    command: Optional[str] = None


class CausalChainOut(BaseModel):
    current_pattern_id: str
    current_pattern_name: str
    root_causes: list[dict]
    downstream_effects: list[dict]
    recommendation: Optional[str]


class Analysis(BaseModel):
    """MongoDB document for a single analysis."""
    id: str = Field(default_factory=lambda: str(uuid4()))

    # Identity / dedup
    user_id: str
    client_id: Optional[str] = None  # UUID from CLI — used for idempotent sync
    project_id: Optional[str] = None  # Project this analysis belongs to (team feature)

    # Log metadata (never store the raw log in prod beyond TTL)
    log_hash: str = ""
    log_size_chars: int = 0
    log_snippet: Optional[str] = None  # First 200 chars (safe preview)

    # Match result
    pattern_id: Optional[str] = None
    confidence: float = 0.0
    match_method: str = "no_match"
    detected_category: Optional[str] = None
    extracted_vars: dict = Field(default_factory=dict)

    # Solutions
    solutions: list[SolutionOut] = Field(default_factory=list)
    causal_chain: Optional[CausalChainOut] = None

    # LLM metadata
    llm_model: Optional[str] = None
    llm_latency_ms: Optional[int] = None
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0

    # Feedback
    user_feedback: Optional[bool] = None      # True = helpful, False = not helpful
    feedback_solution_rank: Optional[int] = None
    feedback_at: Optional[datetime] = None

    # Timings
    total_latency_ms: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # TTL — logs_expires_at triggers MongoDB TTL index cleanup
    logs_expires_at: Optional[datetime] = None

    # Sync metadata
    synced_from_client: bool = False  # True if pushed via sync/push (offline mode)

    # Error (if engine crashed)
    engine_error: Optional[str] = None

    def set_log_ttl(self, days: int = 90):
        self.logs_expires_at = datetime.utcnow() + timedelta(days=days)


# ── API request/response shapes ───────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    log: str = Field(..., min_length=10, max_length=500_000, description="Raw CI/CD log")
    client_id: Optional[str] = Field(None, description="UUID from CLI for dedup")
    project_id: Optional[str] = Field(None, description="Project ID for team context")
    context: Optional[dict] = Field(None, description="Extra context (OS, tool versions...)")
    metadata: Optional[dict] = Field(None, description="Additional metadata")


class AnalyzeResponse(BaseModel):
    id: Optional[str] = None
    pattern_id: Optional[str] = None
    confidence: float = 0.0
    match_method: str = "no_match"
    detected_category: Optional[str] = None
    extracted_vars: dict = Field(default_factory=dict)
    solutions: list[SolutionOut] = Field(default_factory=list)
    causal_chain: Optional[CausalChainOut] = None
    llm_model: Optional[str] = None
    llm_latency_ms: Optional[int] = None
    total_latency_ms: int = 0
    error: Optional[str] = None


class FeedbackRequest(BaseModel):
    helpful: bool
    comment: Optional[str] = None
    solution_rank: Optional[int] = Field(None, ge=1, le=5)


class AnalysisListResponse(BaseModel):
    items: list[dict]
    total: int
    page: int
    per_page: int
