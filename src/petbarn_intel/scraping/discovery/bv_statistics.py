"""Bulk Bazaarvoice review statistics for the catalog census.

One call covers up to `batch_size` SKUs (default 100). Callers must pass
*variant*-level SKUs, not configurable-parent SKUs -- Bazaarvoice syndicates
reviews against the numeric variant SKU (verified live: querying a parent
SKU matches an unrelated near-empty phantom record). `census.py` builds the
parent->variants map from `graphql_census.enumerate_catalog`'s
`variant_skus` field and aggregates the per-variant results returned here
back up to one number per product.
"""

from __future__ import annotations

import orjson

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.discovery.bazaarvoice_config import (
    BazaarvoiceConfigError,
    get_display_code,
)
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

STATS_URL = (
    "https://apps.bazaarvoice.com/bfd/v1/clients/petbarn-au/api-products/cv2/"
    "resources/data/statistics.json"
)


def _headers(display_code: str) -> dict:
    return {
        "Origin": "https://www.petbarn.com.au",
        "Referer": "https://www.petbarn.com.au/",
        "bv-bfd-token": f"{display_code},main_site,en_AU",
        "Accept": "application/json; version=1",
    }


async def _fetch_batch(
    fetcher: TieredFetcher, skus: list[str], display_code: str, retried: bool = False
) -> dict:
    params = {
        "apiversion": "5.5",
        "displaycode": f"{display_code}-en_au",
        "filter": f"productid:eq:{','.join(skus)}",
        "stats": "reviews",
        "filter_reviews": "contentlocale:eq:en_AU",
    }
    result = await fetcher.fetch(
        STATS_URL,
        resource_type="bv_statistics",
        params=params,
        headers=_headers(display_code),
        respect_robots=False,
    )
    if result.status_code in (401, 403) and not retried:
        logger.warning("bv_statistics_auth_failed_rediscovering_config")
        new_code = await get_display_code(fetcher, force_refresh=True)
        return await _fetch_batch(fetcher, skus, new_code, retried=True)
    if not result.ok():
        logger.warning("bv_statistics_batch_failed", reason=result.block_reason, n=len(skus))
        return {}
    payload = orjson.loads(result.content)
    response = payload.get("response", payload)
    if response.get("HasErrors"):
        logger.warning("bv_statistics_api_errors", errors=response.get("Errors"))
    out: dict[str, dict] = {}
    for item in response.get("Results", []):
        stats = item.get("ProductStatistics", {})
        pid = stats.get("ProductId")
        review_stats = stats.get("ReviewStatistics", {}) or {}
        if pid:
            out[pid] = {
                "review_count": review_stats.get("TotalReviewCount"),
                "average_rating": review_stats.get("AverageOverallRating"),
            }
    return out


async def bulk_statistics(
    fetcher: TieredFetcher, skus: list[str], batch_size: int = 100
) -> dict[str, dict]:
    try:
        display_code = await get_display_code(fetcher)
    except BazaarvoiceConfigError as exc:
        logger.error("bv_config_unavailable", error=str(exc))
        return {}

    total_batches = (len(skus) + batch_size - 1) // batch_size
    out: dict[str, dict] = {}
    for i in range(0, len(skus), batch_size):
        batch = skus[i : i + batch_size]
        batch_result = await _fetch_batch(fetcher, batch, display_code)
        out.update(batch_result)
        logger.info(
            "bv_statistics_batch", batch=(i // batch_size) + 1, total_batches=total_batches,
            matched_so_far=len(out),
        )
    logger.info("bv_bulk_statistics_done", requested=len(skus), matched=len(out))
    return out
