"""Shared state for the agent graph (plan section 2, the guard -> planner
-> specialists -> synthesizer -> verifier flow).

`evidence` uses the `operator.add` reducer because the planner fans out to
multiple specialists in parallel (`langgraph.types.Send`, complex mode) --
each branch appends its own entry and LangGraph concatenates them back
together once every branch finishes, before the synthesizer runs.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

Mode = Literal["fast", "complex"]
SpecialistName = Literal["catalog", "review", "scraper"]


class ToolCallRecord(TypedDict, total=False):
    name: str
    args: str
    result: str


class Evidence(TypedDict, total=False):
    specialist: SpecialistName
    subtask: str
    answer: str
    tool_calls: list[ToolCallRecord]


class AgentState(TypedDict, total=False):
    # --- turn identity -----------------------------------------------------
    question: str
    session_id: str
    turn_id: str

    # --- guard ---------------------------------------------------------------
    blocked: bool
    block_reason: str | None

    # --- planner ------------------------------------------------------------
    in_scope: bool
    mode: Mode
    specialists: list[SpecialistName]
    subtasks: dict[str, str]
    plan_reason: str

    # --- specialists / evidence ledger --------------------------------------
    evidence: Annotated[list[Evidence], operator.add]

    # --- synthesis + verification --------------------------------------------
    answer: str
    citations: list[str]
    verifier_passed: bool
    verifier_notes: str
    retry_count: int

    # --- final -----------------------------------------------------------------
    final_answer: str | None
