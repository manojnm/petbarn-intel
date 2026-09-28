"""Verifier node: groundedness + coverage check, complex mode only (plan
section 2). A cheap ("mini") LLM call that checks the synthesized answer's
concrete claims trace back to the evidence ledger, and every planned
subtask actually got addressed. Retries the plan at most once on failure
-- a second failure is returned as-is rather than looping forever.
"""

from __future__ import annotations

import json
import time

from petbarn_intel.agent.llm import achat_full, estimate_cost
from petbarn_intel.agent.state import AgentState
from petbarn_intel.agent.synthesizer import _format_evidence
from petbarn_intel.agent.tracing import log_step
from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 1

_SYSTEM_PROMPT = """\
You are a strict verifier for a Petbarn shopping assistant's draft answer. Given the \
original question, the evidence the specialists collected, and the draft answer, check:
1. Groundedness: every concrete number, price, rating, percentage, or quote in the \
draft actually appears in (or is a fair restatement of) the evidence. Flag anything \
that looks invented.
2. Coverage: the draft actually addresses what the user asked (e.g. a comparison \
question shouldn't only cover one product).

Respond with strict JSON only: {"passed": true|false, "notes": "short explanation, \
empty string if passed"}. Be pragmatic: minor wording differences are fine; only fail \
on a genuine ungrounded claim or a clearly unaddressed part of the question."""


async def verifier_node(state: AgentState) -> dict:
    session_id, turn_id = state["session_id"], state["turn_id"]
    prompt = (
        f"Original question: {state['question']}\n\n"
        f"Evidence:\n{_format_evidence(state.get('evidence', []))}\n\n"
        f"Draft answer:\n{state.get('answer', '')}"
    )

    started = time.monotonic()
    tokens_in = tokens_out = None
    cost_usd = None
    error: str | None = None
    try:
        response = await achat_full(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            role="mini",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content or "")
        passed = bool(parsed.get("passed", True))
        notes = str(parsed.get("notes", ""))[:400]
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)
        cost_usd = estimate_cost(get_settings().chat_model_id("mini"), tokens_in, tokens_out)
    except Exception as exc:  # noqa: BLE001 - a broken verifier must not block an answer
        logger.warning("verifier_call_failed", error=str(exc))
        passed, notes = True, ""
        error = str(exc)
    log_step(
        session_id, turn_id, "verifier", "llm_call", "verifier",
        latency_ms=(time.monotonic() - started) * 1000, result_summary=f"passed={passed} {notes}",
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, error=error,
    )

    return {"verifier_passed": passed, "verifier_notes": notes}


def should_retry(state: AgentState) -> bool:
    return not state.get("verifier_passed", True) and state.get("retry_count", 0) < _MAX_RETRIES
