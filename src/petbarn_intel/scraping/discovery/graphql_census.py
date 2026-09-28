"""Full-catalog enumeration via Petbarn's public Magento GraphQL endpoint.

One query with `pageSize=300` returns `total_count` (6,818 at time of recon)
and `page_info.total_pages` (23), so the whole catalog is ~23 requests. This
is dramatically cheaper than crawling category pages, and gives us
structured price/category/stock data with no HTML parsing at all.
"""

from __future__ import annotations

import contextlib

import orjson

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

GRAPHQL_URL = "https://www.petbarn.com.au/graphql"

_QUERY_TMPL = """
{{
  products(filter: {{price: {{from: "0"}}}}, pageSize: {page_size}, currentPage: {page}) {{
    total_count
    page_info {{ current_page total_pages page_size }}
    items {{
      __typename
      sku
      name
      url_key
      stock_status
      price_range {{
        minimum_price {{
          regular_price {{ value currency }}
          final_price {{ value }}
        }}
      }}
      categories {{ name url_path }}
      short_description {{ html }}
      ... on ConfigurableProduct {{
        variants {{ product {{ sku }} }}
      }}
    }}
  }}
}}
"""

# Skip composite/donation SKUs -- they are not standalone reviewable products.
_SKIP_SKU_PREFIXES = ("bundle-", "custom-")

_PET_TYPE_SLUGS = {
    # Verified against live category url_paths (docs/recon.md) -- Petbarn uses
    # singular slugs for some pet types, which is easy to get wrong.
    "dogs": "Dog",
    "cats": "Cat",
    "fish": "Fish",
    "bird": "Bird",
    "small-animal": "Small Pet",
    "reptile": "Reptile",
    "farm-animals": "Farm Animal",
    "horses": "Horse",
}


def infer_pet_types(categories: list[dict]) -> list[str]:
    found = set()
    for cat in categories:
        top = (cat.get("url_path") or "").split("/")[0]
        if top in _PET_TYPE_SLUGS:
            found.add(_PET_TYPE_SLUGS[top])
    return sorted(found)


def infer_brand(categories: list[dict]) -> str | None:
    """Petbarn's GraphQL `manufacturer` attribute is unpopulated store-wide, but
    every branded product carries a top-level `brand/<slug>` category (e.g.
    `brand/black-hawk` -> "Black Hawk") -- recovered here with zero LLM cost.
    """
    for cat in categories:
        path = cat.get("url_path") or ""
        if path.startswith("brand/") and path.count("/") == 1 and path != "brand/":
            return cat.get("name")
    return None


async def fetch_page(fetcher: TieredFetcher, page: int, page_size: int = 300) -> dict:
    query = _QUERY_TMPL.format(page_size=page_size, page=page)
    result = await fetcher.fetch(
        GRAPHQL_URL,
        resource_type="graphql_census",
        params={"query": query},
        expected_markers=None,
    )
    if not result.ok():
        raise RuntimeError(f"GraphQL census page {page} failed: {result.block_reason}")
    payload = orjson.loads(result.content)
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors on page {page}: {payload['errors']}")
    return payload["data"]["products"]


async def enumerate_catalog(fetcher: TieredFetcher, page_size: int = 300) -> list[dict]:
    """Returns a flat list of raw census item dicts (still Petbarn's shape,
    lightly normalized). Deep-scrape and seed-selection consume this.
    """
    first = await fetch_page(fetcher, 1, page_size)
    total_pages = first["page_info"]["total_pages"]
    total_count = first["total_count"]
    logger.info("census_graphql_start", total_count=total_count, total_pages=total_pages)

    logger.info("census_graphql_page", page=1, total_pages=total_pages, items=len(first["items"]))
    all_items: list[dict] = list(first["items"])
    for page in range(2, total_pages + 1):
        data = await fetch_page(fetcher, page, page_size)
        all_items.extend(data["items"])
        logger.info(
            "census_graphql_page", page=page, total_pages=total_pages, items=len(data["items"]),
            running_total=len(all_items),
        )

    normalized = []
    for item in all_items:
        sku = item.get("sku") or ""
        if any(sku.startswith(p) for p in _SKIP_SKU_PREFIXES):
            continue
        price = None
        with contextlib.suppress(KeyError, TypeError):
            price = item["price_range"]["minimum_price"]["final_price"]["value"]
        categories = item.get("categories") or []
        # Bazaarvoice reviews are syndicated against the numeric *variant*
        # SKU (e.g. "127958" for one dog-food size), never the configurable
        # parent's own SKU -- querying BV stats with the parent SKU silently
        # matches an unrelated near-empty phantom record (verified live: 1
        # review / rating 5.0) instead of the real ~800 reviews living on
        # each size variant. `variant_skus` lets the census aggregate BV
        # stats correctly (see `bv_statistics.py`).
        variants = item.get("variants") or []
        variant_skus = [v["product"]["sku"] for v in variants if v.get("product", {}).get("sku")]
        if not variant_skus:
            variant_skus = [sku]
        normalized.append(
            {
                "sku": sku,
                "name": item.get("name") or "",
                "url_key": item.get("url_key") or "",
                "stock_status": item.get("stock_status"),
                "price": price,
                "brand": infer_brand(categories),
                "categories": [c["name"] for c in categories],
                "category_paths": [c["url_path"] for c in categories],
                "pet_types": infer_pet_types(categories),
                "description": (item.get("short_description") or {}).get("html"),
                "typename": item.get("__typename"),
                "variant_skus": variant_skus,
            }
        )
    logger.info("census_graphql_done", raw_items=len(all_items), normalized=len(normalized))
    return normalized
