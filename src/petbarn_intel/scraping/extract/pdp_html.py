"""Secondary source: JSON-LD (`ProductGroup`/`hasVariant`) embedded in the
rendered PDP HTML. Used for (a) `brand`, which the public GraphQL endpoint
never populates (see `scraping/discovery/graphql_census.py::infer_brand` for
the category-based fallback used at census time -- JSON-LD is strictly
better when we have it), and (b) a cross-check on gtin/rating/offers against
the GraphQL response, feeding `disagreements` in the merger.
"""

from __future__ import annotations

import orjson
from bs4 import BeautifulSoup

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)


async def fetch_pdp_jsonld(fetcher: TieredFetcher, url: str) -> dict | None:
    result = await fetcher.fetch(url, resource_type="product_page", respect_robots=True)
    if not result.ok():
        logger.warning("pdp_html_fetch_failed", url=url, reason=result.block_reason)
        return None
    return parse_jsonld(result.text)


def parse_jsonld(html_text: str) -> dict:
    """Returns {"brand": str|None, "variants": {sku: {gtin, rating_value,
    review_count, price, availability}}}. Missing/malformed blocks degrade
    to an empty result rather than raising -- this is a secondary source.
    """
    soup = BeautifulSoup(html_text, "lxml")
    out: dict = {"brand": None, "variants": {}}
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = orjson.loads(script.string)
        except orjson.JSONDecodeError:
            continue
        if data.get("@type") != "ProductGroup":
            continue
        brand = data.get("brand") or {}
        if brand.get("name"):
            out["brand"] = brand["name"]
        for variant in data.get("hasVariant", []):
            sku = variant.get("sku")
            if not sku:
                continue
            rating = variant.get("aggregateRating") or {}
            offers = variant.get("offers") or {}
            out["variants"][sku] = {
                "gtin": variant.get("gtin"),
                "rating_value": rating.get("ratingValue"),
                "review_count": rating.get("reviewCount"),
                "price": offers.get("price"),
                "availability": _availability_from_schema(offers.get("availability")),
            }
    return out


def _availability_from_schema(value: str | None) -> str | None:
    if not value:
        return None
    # e.g. "https://schema.org/InStock" -> "InStock"
    return value.rsplit("/", 1)[-1]
