"""Per-turn scrape-budget tracking using a ContextVar.

`run_specialist_node` sets the current turn_id via `set_current_turn()`.
`scrape_product` calls `increment_scrape_count()` / `check_scrape_budget()`
to enforce `agent_max_live_scrapes_per_turn` with a real hard stop rather
than just reporting the setting in get_scraper_health().
"""

from __future__ import annotations

from contextvars import ContextVar

_current_turn_id: ContextVar[str] = ContextVar("current_turn_id", default="")
_scrape_counts: dict[str, int] = {}


def set_current_turn(turn_id: str) -> None:
    _current_turn_id.set(turn_id)


def increment_scrape_count() -> int:
    """Increment and return the scrape count for the current turn."""
    tid = _current_turn_id.get()
    if not tid:
        return 1
    _scrape_counts[tid] = _scrape_counts.get(tid, 0) + 1
    return _scrape_counts[tid]


def check_scrape_budget(limit: int) -> bool:
    """Return True if another scrape is allowed, False if the budget is
    already exhausted (count == limit -- pre-increment check)."""
    tid = _current_turn_id.get()
    if not tid:
        return True
    return _scrape_counts.get(tid, 0) < limit


def clear_turn(turn_id: str) -> None:
    """Remove the counter once a turn completes (called by run_turn)."""
    _scrape_counts.pop(turn_id, None)
