"""Operational models: scrape runs, field-level events, incidents, and agent
traces. These back the Ops page and the eval suite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RunType = Literal[
    "census", "seed_deep_scrape", "on_demand_scrape", "reviews_sync", "robustness_test"
]
RunStatus = Literal["running", "completed", "failed", "cancelled"]
IncidentKind = Literal["missing_field", "drift", "source_disagreement", "endpoint_failure"]
IncidentSeverity = Literal["info", "warning", "critical"]


class ScrapeRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    run_type: RunType
    status: RunStatus = "running"
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    pages_fetched: int = 0
    products_touched: int = 0
    reviews_fetched: int = 0
    errors: int = 0
    notes: dict = Field(default_factory=dict)


class FieldEvent(BaseModel):
    """One extraction attempt for one field on one page. High-volume; this is
    what powers per-field completeness / drift and `diagnose_page`.
    """

    model_config = ConfigDict(extra="ignore")

    event_id: str
    run_id: str | None = None
    product_id: str
    field_name: str
    source: str
    success: bool
    fallback_used: bool = False
    error: str | None = None
    recorded_at: datetime = Field(default_factory=datetime.utcnow)


class Incident(BaseModel):
    model_config = ConfigDict(extra="ignore")

    incident_id: str
    kind: IncidentKind
    severity: IncidentSeverity
    product_id: str | None = None
    field_name: str | None = None
    detail: str
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None


class ToolCallTrace(BaseModel):
    """One node/tool/LLM step inside an agent turn. Written by
    `agent/tracing.py::TraceRecorder` (a LangChain callback handler).
    """

    model_config = ConfigDict(extra="ignore")

    trace_id: str
    session_id: str
    turn_id: str
    node: str  # guard | planner | catalog_agent | review_agent | scraper_agent | synthesizer | verifier
    kind: Literal["node", "tool_call", "llm_call", "error"]
    name: str  # tool name or llm role
    args_summary: str | None = None
    result_summary: str | None = None
    latency_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
