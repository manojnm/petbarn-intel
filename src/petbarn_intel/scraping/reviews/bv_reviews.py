"""Bazaarvoice review fetching + stratified sampling for one product.

Endpoint verified live (2026-09-28) against the same public BFD host used
for bulk statistics (`bv_statistics.py`), reusing its display-code discovery
and 401/403 rediscovery pattern.
"""

from __future__ import annotations

from datetime import datetime

import orjson
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from petbarn_intel.logging import get_logger
from petbarn_intel.models.review import Review, SampleReason
from petbarn_intel.scraping.discovery.bazaarvoice_config import (
    BazaarvoiceConfigError,
    get_display_code,
)
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

REVIEWS_URL = (
    "https://apps.bazaarvoice.com/bfd/v1/clients/petbarn-au/api-products/cv2/"
    "resources/data/reviews.json"
)

_vader = SentimentIntensityAnalyzer()

# (sort key, sample_reason, share of the total budget)
_SAMPLE_STRATEGIES: list[tuple[str, SampleReason, float]] = [
    ("submissiontime:desc", "recent", 0.5),
    ("rating:asc", "low_rating", 0.3),
    ("helpfulness:desc", "most_helpful", 0.2),
]


def _headers(display_code: str) -> dict:
    return {
        "Origin": "https://www.petbarn.com.au",
        "Referer": "https://www.petbarn.com.au/",
        "bv-bfd-token": f"{display_code},main_site,en_AU",
        "Accept": "application/json; version=1",
    }


async def _fetch_page(
    fetcher: TieredFetcher, sku: str, display_code: str, sort: str, limit: int, offset: int = 0,
    retried: bool = False,
) -> list[dict]:
    params = {
        "apiversion": "5.5",
        "displaycode": f"{display_code}-en_au",
        "resource": "reviews",
        "action": "REVIEWS_N_STATS",
        "filter": f"productid:eq:{sku}",
        "filter_reviews": "contentlocale:eq:en_AU",
        "limit": str(limit),
        "offset": str(offset),
        "sort": sort,
    }
    result = await fetcher.fetch(
        REVIEWS_URL, resource_type="reviews", params=params, headers=_headers(display_code),
        respect_robots=False,
    )
    if result.status_code in (401, 403) and not retried:
        new_code = await get_display_code(fetcher, force_refresh=True)
        return await _fetch_page(fetcher, sku, new_code, sort, limit, offset, retried=True)
    if not result.ok():
        logger.warning("bv_reviews_fetch_failed", sku=sku, sort=sort, reason=result.block_reason)
        return []
    payload = orjson.loads(result.content)
    response = payload.get("response", payload)
    if response.get("HasErrors"):
        logger.warning("bv_reviews_api_errors", sku=sku, errors=response.get("Errors"))
        return []
    return response.get("Results", [])


def _parse_review(raw: dict, product_id: str, reason: SampleReason) -> Review:
    text = raw.get("ReviewText") or ""
    vader_scores = _vader.polarity_scores(text) if text else {"compound": 0.0}
    compound = vader_scores["compound"]
    label = "positive" if compound >= 0.05 else "negative" if compound <= -0.05 else "neutral"
    submitted_raw = raw.get("SubmissionTime")
    submitted_at = None
    if submitted_raw:
        try:
            submitted_at = datetime.fromisoformat(submitted_raw.replace("Z", "+00:00"))
        except ValueError:
            submitted_at = None
    return Review(
        review_id=str(raw.get("Id")),
        product_id=product_id,
        rating=int(raw.get("Rating") or 0),
        title=raw.get("Title"),
        text=text,
        author=raw.get("UserNickname"),
        submitted_at=submitted_at,
        is_recommended=raw.get("IsRecommended"),
        helpful_votes=int(raw.get("TotalPositiveFeedbackCount") or 0),
        not_helpful_votes=int(raw.get("TotalNegativeFeedbackCount") or 0),
        verified_purchaser="verifiedPurchaser" in (raw.get("Badges") or {}),
        incentivized=bool(raw.get("CampaignId") or ""),
        syndicated=raw.get("SourceClient") != "petbarn-au",
        vader_compound=compound,
        vader_label=label,
        sample_reasons=[reason],
    )


async def fetch_reviews_sample(
    fetcher: TieredFetcher, sku: str, product_id: str, target_total: int = 150,
) -> list[Review]:
    """Recent + low-rated + most-helpful sample, deduplicated by review id,
    each kept review tagged with every strategy that surfaced it.
    """
    try:
        display_code = await get_display_code(fetcher)
    except BazaarvoiceConfigError as exc:
        logger.error("bv_config_unavailable", sku=sku, error=str(exc))
        return []

    by_id: dict[str, Review] = {}
    for sort, reason, share in _SAMPLE_STRATEGIES:
        limit = max(10, round(target_total * share))
        raw_items = await _fetch_page(fetcher, sku, display_code, sort, limit)
        for raw in raw_items:
            review = _parse_review(raw, product_id, reason)
            existing = by_id.get(review.review_id)
            if existing is None:
                by_id[review.review_id] = review
            elif reason not in existing.sample_reasons:
                existing.sample_reasons.append(reason)

    reviews = list(by_id.values())
    logger.info(
        "bv_reviews_sampled", sku=sku, total=len(reviews),
        by_reason={r: sum(1 for rv in reviews if r in rv.sample_reasons) for _, r, _ in _SAMPLE_STRATEGIES},
    )
    return reviews


async def total_review_count(fetcher: TieredFetcher, sku: str) -> int:
    """Cheap way to know the true total without downloading everything --
    `TotalResults` on any single-page fetch."""
    try:
        display_code = await get_display_code(fetcher)
    except BazaarvoiceConfigError:
        return 0
    params = {
        "apiversion": "5.5",
        "displaycode": f"{display_code}-en_au",
        "resource": "reviews",
        "action": "REVIEWS_N_STATS",
        "filter": f"productid:eq:{sku}",
        "filter_reviews": "contentlocale:eq:en_AU",
        "limit": "1",
        "offset": "0",
    }
    result = await fetcher.fetch(
        REVIEWS_URL, resource_type="reviews", params=params, headers=_headers(display_code),
        respect_robots=False,
    )
    if not result.ok():
        return 0
    payload = orjson.loads(result.content)
    response = payload.get("response", payload)
    return int(response.get("TotalResults") or 0)
