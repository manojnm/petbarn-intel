"""Planner node: one structured-output LLM call that decides scope, mode
(fast/complex), which specialist(s) to fan out to, and a short subtask
instruction per specialist (plan section 2). Merges the "router" and
"planner" steps into one call, as the plan calls for.

Scope judgement lives here (an LLM call, not the guard's rules) because it
genuinely needs judgement: "what's Petbarn's return policy" is pet-retail
but not something our tools can answer; "compare X and Y" needs product
resolution the guard can't do cheaply.
"""

from __future__ import annotations

import json
import time

from petbarn_intel.agent.llm import achat_full, estimate_cost
from petbarn_intel.agent.state import AgentState
from petbarn_intel.agent.tracing import log_step
from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

_VALID_SPECIALISTS = ("catalog", "review", "scraper")

_SYSTEM_PROMPT = """\
You are the planner for a Petbarn (Australian pet retailer) shopping assistant. \
You do not answer the user directly -- you decide how the rest of the system should \
handle their message, by choosing which specialist teams to consult.

Specialists available:
- "catalog": product search, prices, specs, ingredients, brand, variants, comparisons, \
catalog-wide SQL queries (e.g. "cheapest grain-free food"). Covers ~5,000 products.
- "review": rating statistics, aspect-level sentiment (price, quality, palatability, \
health/digestion, packaging, delivery, customer service), review search/quotes, \
Q&A, and Petbarn's own AI review summary. Only has deep review data for products \
we've already scraped ("seed" tier, ~200 products) -- for anything else it will \
have thin or no review data.
- "scraper": live-scrapes a specific product right now if it's in our catalog but \
we don't have review/detail data for it yet, or if the user explicitly asks for \
fresh/live data. Slower (several seconds of network calls). Only include this if \
the question clearly needs data we likely don't have yet.

Decide:
- "in_scope": false ONLY if the message has nothing to do with Petbarn products, \
pricing, or customer reviews (e.g. general chit-chat unrelated to shopping, general \
knowledge, coding help, or something no specialist above could ever help with). \
Simple greetings or "thank you" are in_scope=true with mode="fast" and no specialists needed.
- "mode": "fast" for a single direct lookup (one product, one fact, or a simple \
greeting/thanks) that needs at most one specialist. "complex" for comparisons, \
multi-aspect analysis, cross-product questions, or anything needing more than one \
specialist or a live scrape.
- "specialists": which of catalog/review/scraper to consult, in the order that makes \
sense. Usually 1-2. Empty list only if in_scope=false or it's a pure greeting.
- "subtasks": a short instruction per chosen specialist, written as if delegating to \
a colleague -- restate exactly what they need to find out, in their own terms. Each \
specialist resolves product names to IDs itself (via its own search tool), so pass \
product names/brands verbatim from the user's message, don't invent IDs.

Respond with strict JSON only, no markdown fences:
{"in_scope": true, "mode": "fast", "specialists": ["catalog"], \
"subtasks": {"catalog": "..."}, "reason": "one short sentence"}
"""

_DEFAULT_FALLBACK = {
    "in_scope": True,
    "mode": "fast",
    "specialists": ["catalog", "review"],
    "subtasks": {
        "catalog": "Look up the product(s) mentioned and get their details.",
        "review": "Look up review stats and relevant feedback for the product(s) mentioned.",
    },
    "reason": "planner call failed; defaulting to a safe broad lookup",
}


def _validate(parsed: dict) -> dict:
    specialists = [s for s in parsed.get("specialists", []) if s in _VALID_SPECIALISTS]
    subtasks_raw = parsed.get("subtasks", {})
    subtasks = {
        s: str(subtasks_raw[s]) for s in specialists if isinstance(subtasks_raw, dict) and s in subtasks_raw
    }
    return {
        "in_scope": bool(parsed.get("in_scope", True)),
        "mode": "complex" if parsed.get("mode") == "complex" else "fast",
        "specialists": specialists,
        "subtasks": subtasks,
        "reason": str(parsed.get("reason", ""))[:300],
    }


async def planner_node(state: AgentState) -> dict:
    question = state["question"]
    session_id, turn_id = state["session_id"], state["turn_id"]
    retry_count = state.get("retry_count", 0)

    prompt = question
    if retry_count > 0 and state.get("verifier_notes"):
        prompt = (
            f"{question}\n\n[Internal retry note: a verifier found the first attempt's answer "
            f"had gaps: {state['verifier_notes']}. Adjust specialists/subtasks to close them.]"
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
        parsed = _validate(json.loads(response.choices[0].message.content or ""))
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None)
        tokens_out = getattr(usage, "completion_tokens", None)
        cost_usd = estimate_cost(get_settings().chat_model_id("mini"), tokens_in, tokens_out)
    except Exception as exc:  # noqa: BLE001 - planner must never crash the turn
        logger.warning("planner_call_failed", error=str(exc))
        parsed = dict(_DEFAULT_FALLBACK)
        error = str(exc)
    log_step(
        session_id, turn_id, "planner", "llm_call", "planner",
        latency_ms=(time.monotonic() - started) * 1000, result_summary=str(parsed),
        tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd, error=error,
    )

    if not parsed["in_scope"]:
        return {
            **parsed,
            "final_answer": (
                "That's outside what I can help with -- I'm a Petbarn product & review "
                "assistant. Ask me about a product's price, specs, or what customers say "
                "about it, or compare a couple of products."
            ),
        }

    if not parsed["specialists"]:
        # In-scope but nothing to look up (e.g. "thanks!") -- straight to synthesis
        # with no evidence needed.
        return {**parsed, "mode": "fast"}

    return parsed
