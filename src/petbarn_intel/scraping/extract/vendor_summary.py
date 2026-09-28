"""Petbarn's "Summary of Reviews" panel.

Recon (2026-09-28, via Playwright network capture -- see
docs/decisions/003-vendor-summary-endpoint.md): clicking the panel's arrow
fires a POST to Petbarn's own `mesh.petbarn.com.au` GraphQL gateway, not a
direct Bazaarvoice REST call. The gateway wraps Bazaarvoice's OAIQ
(OpenAI-powered) review-summarization product:

    query BvReviewSummary($sku: String!) {
      bv_review_summary(sku: $sku) {
        ... on ReviewSummary { summary disclaimer }
      }
    }

Always presented to users as *Petbarn/Bazaarvoice's own AI summary*, never
as our own analysis (`VendorSummary` docstring, `models/product.py`).
"""

from __future__ import annotations

import orjson

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

MESH_GRAPHQL_URL = "https://mesh.petbarn.com.au/api/graphql"

_QUERY = """
  query BvReviewSummary($sku: String!) {
    bv_review_summary(sku: $sku) {
      ... on ReviewSummary {
        summary
        disclaimer
      }
    }
  }
"""


async def fetch_vendor_summary(fetcher: TieredFetcher, sku: str) -> dict | None:
    """Returns {"paragraph": str, "disclaimer": str} or None if unavailable
    (no reviews yet, endpoint error, etc. -- always optional/best-effort).
    """
    result = await fetcher.fetch(
        MESH_GRAPHQL_URL,
        resource_type="bv_summary",
        json_body={"query": _QUERY, "variables": {"sku": sku}},
        respect_robots=False,
    )
    if not result.ok():
        logger.info("vendor_summary_fetch_failed", sku=sku, reason=result.block_reason)
        return None
    payload = orjson.loads(result.content)
    summary = (payload.get("data") or {}).get("bv_review_summary")
    if not summary or not summary.get("summary"):
        return None
    return {"paragraph": summary["summary"], "disclaimer": summary.get("disclaimer")}
