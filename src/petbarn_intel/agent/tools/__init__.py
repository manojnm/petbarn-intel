"""Agent tool registries, grouped by specialist (plan section 2b): each
specialist sees only its own toolset, which is what keeps tool selection
accurate on a small model.
"""

from __future__ import annotations

from petbarn_intel.agent.tools import catalog, reviews, scraper

CATALOG_TOOLS = catalog.TOOLS
# The Review and Scraper specialists run as independent parallel branches
# (plan section 2's `Send` fan-out) -- each needs to resolve a product name
# to an id on its own rather than depend on the Catalog specialist having
# run first, so both get `search_products` too.
REVIEW_TOOLS = [catalog.search_products, *reviews.TOOLS]
SCRAPER_TOOLS = [catalog.search_products, *scraper.TOOLS]
ALL_TOOLS = [*CATALOG_TOOLS, *reviews.TOOLS, *scraper.TOOLS]

TOOLS_BY_SPECIALIST = {
    "catalog": CATALOG_TOOLS,
    "review": REVIEW_TOOLS,
    "scraper": SCRAPER_TOOLS,
}

__all__ = [
    "CATALOG_TOOLS",
    "REVIEW_TOOLS",
    "SCRAPER_TOOLS",
    "ALL_TOOLS",
    "TOOLS_BY_SPECIALIST",
]
