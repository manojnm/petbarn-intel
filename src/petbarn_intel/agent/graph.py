"""The agent graph (plan section 2):

    START -> guard -> planner -> [Send fan-out] -> run_specialist(s)
           -> synthesizer -> (complex mode) verifier -> (retry once) planner
           -> END

`thread_id` for the checkpointer is the per-turn `turn_id`, not the chat
`session_id` -- conversational memory across turns is handled explicitly
by the caller (Streamlit/CLI folding recent history into `question`
before calling `run_turn`, see its docstring), so the checkpointer here is
purely for within-turn resilience/inspection, not cross-turn state. An
in-memory checkpointer is enough for that; nothing needs to survive a
process restart.
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from petbarn_intel.agent.guard import guard_node
from petbarn_intel.agent.planner import planner_node
from petbarn_intel.agent.security import scan_output
from petbarn_intel.agent.specialists import run_specialist_node
from petbarn_intel.agent.state import AgentState
from petbarn_intel.agent.synthesizer import synthesizer_node
from petbarn_intel.agent.turn_context import clear_turn
from petbarn_intel.agent.verifier import should_retry, verifier_node
from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

_HISTORY_TURNS = 3  # how many prior (user, answer) pairs to fold into `question` for memory


def _route_after_guard(state: AgentState) -> str:
    return END if state.get("blocked") else "planner"


def _route_after_planner(state: AgentState):
    if state.get("final_answer"):
        return END
    specialists = state.get("specialists") or []
    if not specialists:
        return "synthesizer"
    subtasks = state.get("subtasks") or {}
    question = state["question"]
    return [
        Send(
            "run_specialist",
            {
                "specialist": name,
                "subtask": subtasks.get(name, question),
                "question": question,
                "session_id": state["session_id"],
                "turn_id": state["turn_id"],
            },
        )
        for name in specialists
    ]


def _route_after_synthesizer(state: AgentState) -> str:
    return "verifier" if state.get("mode") == "complex" else END


def _output_leak_node(state: AgentState) -> dict:
    """Runs after synthesizer on every path (fast and complex alike).
    If the answer contains a secret or a system-prompt echo, replace it
    with a safe refusal and log to the traces table."""
    answer = state.get("answer") or state.get("final_answer") or ""
    refusal = scan_output(answer)
    if refusal:
        logger.warning("output_leak_detected", session_id=state.get("session_id"), answer_prefix=answer[:80])
        from petbarn_intel.agent.tracing import log_step
        log_step(
            state["session_id"], state["turn_id"],
            "output_guard", "error", "leak_detected",
            result_summary="answer replaced with refusal",
        )
        return {"answer": refusal}
    return {}


def _route_after_verifier(state: AgentState) -> str:
    return "bump_retry" if should_retry(state) else END


def _bump_retry_node(state: AgentState) -> dict:
    return {"retry_count": state.get("retry_count", 0) + 1}


@lru_cache(maxsize=1)
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("guard", guard_node)
    graph.add_node("planner", planner_node)
    graph.add_node("run_specialist", run_specialist_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("output_guard", _output_leak_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("bump_retry", _bump_retry_node)

    graph.add_edge(START, "guard")
    graph.add_conditional_edges("guard", _route_after_guard, ["planner", END])
    graph.add_conditional_edges(
        "planner", _route_after_planner, ["run_specialist", "synthesizer", END]
    )
    graph.add_edge("run_specialist", "synthesizer")
    graph.add_edge("synthesizer", "output_guard")
    graph.add_conditional_edges("output_guard", _route_after_synthesizer, ["verifier", END])
    graph.add_conditional_edges("verifier", _route_after_verifier, ["bump_retry", END])
    graph.add_edge("bump_retry", "planner")

    return graph.compile(checkpointer=InMemorySaver())


def extract_answer(result: dict) -> str:
    # `guard`/out-of-scope paths set `final_answer` directly (they skip the
    # synthesizer entirely); every other path only sets `answer`.
    return result.get("final_answer") or result.get("answer") or "I couldn't come up with an answer this time."


def _fold_history(question: str, history: list[tuple[str, str]] | None) -> str:
    if not history:
        return question
    recent = history[-_HISTORY_TURNS:]
    transcript = "\n".join(f"User: {q}\nAssistant: {a}" for q, a in recent)
    return (
        f"[Recent conversation, for context/follow-ups like \"it\" or \"the other one\":\n"
        f"{transcript}\n]\n\nUser's new message: {question}"
    )


async def run_turn(
    question: str,
    session_id: str,
    history: list[tuple[str, str]] | None = None,
) -> dict:
    """Run one turn of the agent graph. `history` is a list of
    `(user_question, assistant_answer)` pairs from earlier in this chat
    session -- folded into the question text (see `_fold_history`) rather
    than relying on the checkpointer, which is per-turn here (see module
    docstring).

    Returns `{"answer", "mode", "specialists", "evidence", "turn_id"}`.
    """
    graph = build_graph()
    turn_id = str(uuid.uuid4())
    initial: AgentState = {
        "question": _fold_history(question, history),
        "session_id": session_id,
        "turn_id": turn_id,
        "retry_count": 0,
        "evidence": [],
    }
    result = await graph.ainvoke(
        initial,
        config={"configurable": {"thread_id": turn_id}, "recursion_limit": 40},
    )
    clear_turn(turn_id)  # release per-turn scrape counter
    return {
        "answer": extract_answer(result),
        "mode": result.get("mode", "fast"),
        "specialists": result.get("specialists", []),
        "evidence": result.get("evidence", []),
        "turn_id": turn_id,
        "verifier_passed": result.get("verifier_passed"),
        "verifier_notes": result.get("verifier_notes"),
    }
