"""Scraper agent tools -- on-demand ("Tier C") deep scraping of any
product already known to the catalog census but not yet deep-scraped, plus
diagnostics (plan section 2b, examples 3 and 5).

Live scraping is network- and LLM-bound, so tools here are synchronous
wrappers around the async scraping engine (`scraping/deep_scrape.py`) --
`run_async()` below lets the same wrapper work whether it's called from a
plain sync context (Streamlit) or from inside an already-running event
loop (an async CLI/agent invocation), which `asyncio.run()` alone can't do.
"""

from __future__ import annotations

import asyncio
import concurrent.futures

from langchain_core.tools import tool

from petbarn_intel.agent.tools.cache import clear_cache
from petbarn_intel.agent.turn_context import check_scrape_budget, increment_scrape_count
from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger
from petbarn_intel.store import ops_repo, products_repo
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)

# Maximum reviews fetched per on-demand scrape (per product, not per turn).
_MAX_REVIEWS_ON_DEMAND = 150


def run_async(coro):
    """Run an async coroutine to completion from sync code, safely, whether
    or not an event loop is already running on this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@tool
def scrape_product(product_id: str, reviews: int = 100) -> dict:
    """Live-scrape one product right now: product detail page, price,
    specification/ingredients, Petbarn's AI review summary, a stratified
    review sample, and Q&A -- then store it, so it's available to every
    other tool immediately after. Use this when `get_product_details` or
    `get_review_stats` comes back thin/missing for a product the user is
    asking about (it exists in our 5,000+ product census but hasn't had a
    full deep scrape yet). Takes several seconds; tell the user you're
    fetching live data. Not needed for products already deep-scraped
    (tier "seed" or "on_demand")."""
    from petbarn_intel.scraping.deep_scrape import deep_scrape_product
    from petbarn_intel.scraping.fetch import TieredFetcher

    settings = get_settings()
    if not check_scrape_budget(settings.agent_max_live_scrapes_per_turn):
        return {
            "product_id": product_id,
            "ok": False,
            "error": "scrape_budget_exhausted",
            "message": (
                f"This turn already used {settings.agent_max_live_scrapes_per_turn} live scrape(s) "
                f"(agent_max_live_scrapes_per_turn={settings.agent_max_live_scrapes_per_turn}). "
                "Answer using data already gathered."
            ),
        }
    increment_scrape_count()

    conn = get_connection()
    pid = products_repo.resolve_product_id(product_id, conn=conn)
    if pid is None:
        return {
            "error": "not_found",
            "message": (
                f"'{product_id}' isn't in our catalog census either. Call "
                "search_products first to confirm the product exists."
            ),
        }
    existing = products_repo.get_product(pid, conn=conn)
    if existing and existing.tier in ("seed", "on_demand") and existing.completeness >= 0.6:
        return {
            "product_id": pid,
            "skipped": True,
            "reason": "already_deep_scraped",
            "tier": existing.tier,
            "completeness": existing.completeness,
            "scraped_at": existing.scraped_at.isoformat() if existing.scraped_at else None,
        }

    reviews = min(reviews, _MAX_REVIEWS_ON_DEMAND)

    async def _run():
        run = ops_repo.start_run("on_demand_scrape", conn=conn)
        async with TieredFetcher() as fetcher:
            try:
                result = await deep_scrape_product(fetcher, pid, run.run_id, reviews, conn)
                ops_repo.finish_run(
                    run.run_id,
                    status="completed" if result.get("ok") else "failed",
                    pages_fetched=1,
                    products_touched=1 if result.get("ok") else 0,
                    reviews_fetched=result.get("reviews", 0),
                    errors=0 if result.get("ok") else 1,
                    conn=conn,
                )
                return result
            except Exception as exc:  # noqa: BLE001 - report, don't crash the agent turn
                ops_repo.finish_run(run.run_id, status="failed", errors=1, notes={"error": str(exc)}, conn=conn)
                logger.warning("on_demand_scrape_failed", product_id=pid, error=str(exc))
                return {"product_id": pid, "ok": False, "error": str(exc)}

    result = run_async(_run())
    result["freshly_scraped"] = bool(result.get("ok"))
    if result["freshly_scraped"]:
        # A real (non-skipped) scrape just wrote fresh data for this
        # product -- drop every cached tool result so a follow-up question
        # this session can't be served a stale pre-scrape entry for the
        # rest of the TTL window. Cache is small and cheap to rebuild.
        clear_cache()
    return result


@tool
def diagnose_page(product_id: str) -> dict:
    """Explain why a field might be missing/wrong for a product: shows the
    last extraction attempts per field, which source (GraphQL/JSON-LD/HTML
    anchor/LLM fallback) succeeded or failed, and any open data-quality
    incidents. Use for "why don't you have X for this product" questions."""
    conn = get_connection()
    pid = products_repo.resolve_product_id(product_id, conn=conn) or product_id
    events = ops_repo.diagnose_product_fields(pid, conn=conn)
    incidents = [
        dict(i) for i in ops_repo.open_incidents(conn=conn) if i["product_id"] == pid
    ]
    return {"product_id": pid, "field_events": [dict(e) for e in events], "open_incidents": incidents}


@tool
def get_scraper_health() -> dict:
    """Overall scraping/data-quality health: recent scrape runs, per-field
    completeness across all runs, and currently open incidents. Use for
    "how fresh/complete is your data" meta-questions."""
    conn = get_connection()
    settings = get_settings()
    return {
        "product_counts_by_tier": products_repo.count_products(conn=conn),
        "recent_runs": [dict(r) for r in ops_repo.list_runs(limit=5, conn=conn)],
        "field_completeness": [dict(r) for r in ops_repo.field_completeness(conn=conn)],
        "open_incidents": len(ops_repo.open_incidents(conn=conn)),
        "agent_max_live_scrapes_per_turn": settings.agent_max_live_scrapes_per_turn,
    }


TOOLS = [scrape_product, diagnose_page, get_scraper_health]
