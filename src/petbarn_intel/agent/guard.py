"""Guard node: cheap, rule-based checks that run before any LLM call
(plan section 2 / 2b). Scope judgement itself is left to the planner (an
LLM call anyway, and better placed to judge "is this pet/product-related"
than a keyword list) -- the guard's job is the two things pure rules are
actually good at: reject empty input, and reject obvious prompt-injection
attempts before they ever reach a tool-calling loop.
"""

from __future__ import annotations

import re

from petbarn_intel.agent.state import AgentState
from petbarn_intel.agent.tracing import log_step

_INJECTION_PATTERNS = [
    re.compile(r"ignore (all|any|previous|the) (previous |prior )?instructions", re.I),
    re.compile(r"reveal (your|the) (system )?prompt", re.I),
    re.compile(r"you are now (in )?(dan|developer|jailbreak)", re.I),
    re.compile(r"disregard (all|any) (rules|guidelines|instructions)", re.I),
    re.compile(r"pretend (you have|to have) no (rules|restrictions|guidelines)", re.I),
]

_REFUSAL_EMPTY = "Please ask a question -- for example, a product, a comparison, or what reviewers say about something."
_REFUSAL_INJECTION = (
    "I can't follow instructions embedded in a message like that. Ask me about a "
    "Petbarn product, its price/specs, or what customers say about it, and I'm happy to help."
)


def guard_node(state: AgentState) -> dict:
    question = (state.get("question") or "").strip()
    session_id, turn_id = state["session_id"], state["turn_id"]

    if not question:
        log_step(session_id, turn_id, "guard", "node", "guard", result_summary="blocked:empty")
        return {"blocked": True, "block_reason": "empty", "final_answer": _REFUSAL_EMPTY}

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(question):
            log_step(
                session_id, turn_id, "guard", "node", "guard",
                result_summary="blocked:injection", args_summary=question,
            )
            return {"blocked": True, "block_reason": "injection", "final_answer": _REFUSAL_INJECTION}

    log_step(session_id, turn_id, "guard", "node", "guard", result_summary="passed")
    return {"blocked": False, "block_reason": None}
