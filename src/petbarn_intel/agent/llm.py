"""Thin LiteLLM wrapper around the configured chat deployment.

Every call site should go through `chat()` / `achat()` here rather than
importing `litellm` directly -- that's what keeps the provider swap
(Azure -> plain OpenAI) a one-line change in `config.py` (see
`Settings.chat_model_id`), never in call sites (docs/decisions/005).
"""

from __future__ import annotations

from typing import Any

import litellm

from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

# Azure deployment names (e.g. "gpt-5.4-mini-kai-dev") aren't in LiteLLM's
# model-cost registry, so its cost-calculation path prints a "Provider
# List" hint on every single call -- harmless, but drowns out real logs.
litellm.suppress_debug_info = True


def _provider_kwargs() -> dict[str, Any]:
    """Explicit credentials/endpoint for the active provider.

    Passed per-call (rather than relying on LiteLLM's implicit
    AZURE_API_KEY/AZURE_API_BASE/AZURE_API_VERSION env vars) so behaviour
    only ever depends on `Settings`, matching the rest of the codebase.
    """
    settings = get_settings()
    if settings.llm_provider == "azure":
        return {
            "api_key": settings.azure_openai_api_key,
            "api_base": settings.azure_openai_endpoint,
            "api_version": settings.azure_openai_api_version,
        }
    return {"api_key": settings.openai_api_key}


def chat(
    messages: list[dict[str, str]],
    role: str = "main",
    **kwargs: Any,
) -> str:
    """Sync chat completion. `role` is "mini" (cheap/fast) or "main"
    (default). Returns the assistant's text content."""
    settings = get_settings()
    if not settings.has_llm_credentials:
        raise RuntimeError(
            "No LLM credentials configured -- set AZURE_OPENAI_API_KEY (or "
            "OPENAI_API_KEY if LLM_PROVIDER=openai) in .env."
        )
    model = settings.chat_model_id(role)
    logger.info("llm_chat_request", model=model, role=role, n_messages=len(messages))
    response = litellm.completion(
        model=model, messages=messages, **_provider_kwargs(), **kwargs,
    )
    return response.choices[0].message.content or ""


async def achat_full(
    messages: list[dict[str, str]],
    role: str = "main",
    **kwargs: Any,
):
    """Like `achat()` but returns the raw LiteLLM `ModelResponse` (usage,
    token counts, etc.) instead of just the text -- use this at call sites
    that need to log cost/tokens (the agent graph's nodes; see
    `tracing.py`). Plain `achat()` below is just this plus `.content`."""
    settings = get_settings()
    if not settings.has_llm_credentials:
        raise RuntimeError(
            "No LLM credentials configured -- set AZURE_OPENAI_API_KEY (or "
            "OPENAI_API_KEY if LLM_PROVIDER=openai) in .env."
        )
    model = settings.chat_model_id(role)
    logger.info("llm_achat_request", model=model, role=role, n_messages=len(messages))
    return await litellm.acompletion(
        model=model, messages=messages, **_provider_kwargs(), **kwargs,
    )


async def achat(
    messages: list[dict[str, str]],
    role: str = "main",
    **kwargs: Any,
) -> str:
    """Async counterpart of `chat()` -- prefer this inside the scraping/
    agent event loop so an LLM call never blocks other in-flight work."""
    response = await achat_full(messages, role=role, **kwargs)
    return response.choices[0].message.content or ""


def _price_per_token(model_key: str) -> tuple[float, float] | None:
    entry = litellm.model_cost.get(model_key)
    if not entry:
        return None
    inp, out = entry.get("input_cost_per_token"), entry.get("output_cost_per_token")
    return (inp, out) if inp is not None and out is not None else None


def estimate_cost(model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float | None:
    """Best-effort USD cost for one completion from token counts. Tries
    `model` (the actual deployment string, e.g. "azure/gpt-5.4-mini-kai-dev")
    against LiteLLM's price registry first; a custom Azure deployment name
    won't be in there, so falls back to `Settings.cost_reference_model`
    (same underlying model, its public name) before giving up and
    returning None (which the Ops page renders as an honest "$0", not a
    silently wrong number)."""
    if prompt_tokens is None or completion_tokens is None:
        return None
    settings = get_settings()
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    ref = settings.cost_reference_model
    candidates += [ref, f"azure/{ref}", f"openai/{ref}"]
    for key in candidates:
        price = _price_per_token(key)
        if price:
            inp, out = price
            return prompt_tokens * inp + completion_tokens * out
    return None


def get_chat_model(role: str = "main", **kwargs: Any):
    """LangChain-compatible chat model for the agent graph (tool-calling
    specialists via `langgraph.prebuilt.create_react_agent`, which needs a
    `BaseChatModel` with `.bind_tools()` -- plain `chat()`/`achat()` above
    return raw strings and aren't enough for that).

    Still routed entirely through LiteLLM (`langchain_litellm.ChatLiteLLM`)
    so the Azure/OpenAI provider swap stays a one-line change in
    `Settings.chat_model_id`, never at call sites -- same contract as
    `chat()`/`achat()`.
    """
    from langchain_litellm import ChatLiteLLM

    settings = get_settings()
    if not settings.has_llm_credentials:
        raise RuntimeError(
            "No LLM credentials configured -- set AZURE_OPENAI_API_KEY (or "
            "OPENAI_API_KEY if LLM_PROVIDER=openai) in .env."
        )
    return ChatLiteLLM(
        model=settings.chat_model_id(role),
        **_provider_kwargs(),
        **kwargs,
    )
