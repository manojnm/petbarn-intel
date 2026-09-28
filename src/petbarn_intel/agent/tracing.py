"""Trace recording: every node, LLM call, and tool call in an agent turn is
written to the SQLite `traces` table (plan section 1.8). This is what
powers the in-app "show your work" trace view and the Ops page's cost/
latency/error metrics -- no external service required.

Two entry points, matching the two kinds of call sites in the graph:
- `TraceRecorder`: a LangChain callback handler, attached via
  `config={"callbacks": [...]}` when invoking a specialist's compiled
  ReAct subgraph, so every tool call and chat-model call it makes inside
  its own loop is captured automatically.
- `log_step()`: a plain function for the graph's own nodes (guard,
  planner, synthesizer, verifier), which call `agent.llm.achat()` directly
  rather than going through a LangChain-wrapped model.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from petbarn_intel.logging import get_logger
from petbarn_intel.models.trace import ToolCallTrace
from petbarn_intel.store import ops_repo

logger = get_logger(__name__)

_MAX_SUMMARY_CHARS = 500


def _summarize(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:_MAX_SUMMARY_CHARS]


def log_step(
    session_id: str,
    turn_id: str,
    node: str,
    kind: str,
    name: str,
    *,
    latency_ms: float | None = None,
    args_summary: str | None = None,
    result_summary: str | None = None,
    error: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> None:
    try:
        ops_repo.record_trace(
            ToolCallTrace(
                trace_id=str(uuid.uuid4()),
                session_id=session_id,
                turn_id=turn_id,
                node=node,
                kind=kind,
                name=name,
                args_summary=_summarize(args_summary) if args_summary else None,
                result_summary=_summarize(result_summary) if result_summary else None,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                error=error,
            )
        )
    except Exception as exc:  # noqa: BLE001 - tracing must never break the agent turn
        logger.warning("trace_write_failed", error=str(exc))


class TraceRecorder(BaseCallbackHandler):
    """Attach via `config={"callbacks": [TraceRecorder(session_id, turn_id, node)]}`
    on a specialist's `.invoke()`/`.ainvoke()` call."""

    def __init__(self, session_id: str, turn_id: str, node: str) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.node = node
        self._starts: dict[str, float] = {}

    def _start(self, run_id) -> float:
        started = time.monotonic()
        self._starts[str(run_id)] = started
        return started

    def _elapsed_ms(self, run_id) -> float | None:
        started = self._starts.pop(str(run_id), None)
        return (time.monotonic() - started) * 1000 if started else None

    # --- Tools ---------------------------------------------------------
    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs) -> None:  # noqa: D102
        self._start(run_id)
        name = (serialized or {}).get("name", "tool")
        log_step(self.session_id, self.turn_id, self.node, "tool_call", name, args_summary=input_str)

    def on_tool_end(self, output, *, run_id, **kwargs) -> None:  # noqa: D102
        latency_ms = self._elapsed_ms(run_id)
        log_step(
            self.session_id, self.turn_id, self.node, "tool_call", "tool_result",
            latency_ms=latency_ms, result_summary=str(output),
        )

    def on_tool_error(self, error, *, run_id, **kwargs) -> None:  # noqa: D102
        latency_ms = self._elapsed_ms(run_id)
        log_step(
            self.session_id, self.turn_id, self.node, "error", "tool_error",
            latency_ms=latency_ms, error=str(error),
        )

    # --- LLM calls -------------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs) -> None:  # noqa: D102
        self._start(run_id)

    def on_llm_end(self, response, *, run_id, **kwargs) -> None:  # noqa: D102
        from petbarn_intel.agent.llm import estimate_cost

        latency_ms = self._elapsed_ms(run_id)
        llm_output = getattr(response, "llm_output", None) or {}
        # `token_usage` is a LiteLLM `Usage` object (attribute access), not
        # a plain dict -- despite `llm_output` itself being one.
        token_usage = llm_output.get("token_usage") if isinstance(llm_output, dict) else None
        prompt_tokens = getattr(token_usage, "prompt_tokens", None)
        completion_tokens = getattr(token_usage, "completion_tokens", None)
        model = (llm_output.get("model") if isinstance(llm_output, dict) else None) or "unknown"
        try:
            text = response.generations[0][0].text
        except Exception:  # noqa: BLE001
            text = ""
        log_step(
            self.session_id, self.turn_id, self.node, "llm_call", "chat",
            latency_ms=latency_ms, result_summary=text,
            tokens_in=prompt_tokens, tokens_out=completion_tokens,
            cost_usd=estimate_cost(model, prompt_tokens, completion_tokens),
        )

    def on_llm_error(self, error, *, run_id, **kwargs) -> None:  # noqa: D102
        latency_ms = self._elapsed_ms(run_id)
        log_step(
            self.session_id, self.turn_id, self.node, "error", "llm_error",
            latency_ms=latency_ms, error=str(error),
        )
