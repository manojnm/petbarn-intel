"""Unit tests for the tool-result cache (plan section 1.9). Pure logic,
no DB/LLM/network -- fast and runs everywhere, unlike test_agent_evals.py.

Run with:
    pytest tests/test_tool_cache.py -v
"""

from __future__ import annotations

import time

from petbarn_intel.agent.tools import cache as cache_mod
from petbarn_intel.agent.tools.cache import cache_stats, cached_tool, clear_cache


def _reset() -> None:
    """Each test gets a clean slate: a fresh TTLCache and zeroed counters,
    since the module-level cache is a process-wide singleton."""
    cache_mod._cache = None
    cache_mod._hits = 0
    cache_mod._misses = 0


def test_cache_hit_on_repeat_call() -> None:
    _reset()
    calls = []

    @cached_tool
    def fn(x: int) -> int:
        calls.append(x)
        return x * 2

    assert fn(3) == 6
    assert fn(3) == 6
    assert calls == [3]  # second call was served from cache, not recomputed
    stats = cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_cache_miss_on_different_args() -> None:
    _reset()
    calls = []

    @cached_tool
    def fn(x: int, y: str = "a") -> str:
        calls.append((x, y))
        return f"{x}{y}"

    fn(1, y="a")
    fn(1, y="b")
    fn(2, y="a")
    assert len(calls) == 3  # every distinct arg combination is a miss


def test_cache_handles_unhashable_args() -> None:
    """`compare_products(product_ids: list[str])` passes a list -- must not
    raise on a normally-unhashable argument."""
    _reset()

    @cached_tool
    def fn(ids: list[str]) -> int:
        return len(ids)

    assert fn(["a", "b"]) == 2
    assert fn(["a", "b"]) == 2  # cache hit, no error
    stats = cache_stats()
    assert stats["hits"] == 1


def test_preserves_function_metadata_for_tool_introspection() -> None:
    """LangChain's `@tool` reads `__name__`/`__doc__`/signature -- must
    survive being wrapped by `@cached_tool` underneath it."""
    _reset()

    @cached_tool
    def my_tool(a: int, b: str = "x") -> str:
        """My docstring."""
        return f"{a}{b}"

    assert my_tool.__name__ == "my_tool"
    assert my_tool.__doc__ == "My docstring."
    import inspect

    sig = inspect.signature(my_tool)
    assert list(sig.parameters) == ["a", "b"]


def test_clear_cache_drops_entries_and_forces_recompute() -> None:
    _reset()
    calls = []

    @cached_tool
    def fn(x: int) -> int:
        calls.append(x)
        return x

    fn(1)
    fn(1)
    assert len(calls) == 1
    clear_cache()
    fn(1)
    assert len(calls) == 2  # cleared, so this was a fresh call again


def test_ttl_expiry() -> None:
    _reset()
    cache_mod._cache_instance().ttl  # noqa: B018 - force lazy init
    from cachetools import TTLCache

    cache_mod._cache = TTLCache(maxsize=8, ttl=0.05)
    calls = []

    @cached_tool
    def fn(x: int) -> int:
        calls.append(x)
        return x

    fn(1)
    time.sleep(0.1)
    fn(1)
    assert len(calls) == 2  # TTL expired, second call recomputed
