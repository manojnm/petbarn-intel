"""Sitemap discovery: cross-checks the GraphQL census (are we missing any
product?) and provides `lastmod` timestamps for incremental re-crawls.
"""

from __future__ import annotations

import re

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

SITEMAP_INDEX_URL = "https://www.petbarn.com.au/media/sitemap/au/sitemap.xml"

_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_URL_ENTRY_RE = re.compile(r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", re.DOTALL)


async def _get_xml(fetcher: TieredFetcher, url: str) -> str:
    result = await fetcher.fetch(url, resource_type="sitemap", respect_robots=True)
    if not result.ok():
        raise RuntimeError(f"sitemap fetch failed for {url}: {result.block_reason}")
    return result.text


async def fetch_product_urls(fetcher: TieredFetcher) -> dict[str, str]:
    """Returns {product_url: lastmod_iso} for every `/p/...` URL in the
    sitemap (product sitemaps only, category/advice/store-location sitemaps
    are skipped).
    """
    index_xml = await _get_xml(fetcher, SITEMAP_INDEX_URL)
    sub_sitemaps = [
        loc
        for loc in _LOC_RE.findall(index_xml)
        if "/sitemap-" in loc and "advice" not in loc and "store-locations" not in loc
    ]

    product_urls: dict[str, str] = {}
    for sub_url in sub_sitemaps:
        xml = await _get_xml(fetcher, sub_url)
        for loc, lastmod in _URL_ENTRY_RE.findall(xml):
            if "/p/" in loc:
                product_urls[loc] = lastmod
    logger.info("sitemap_product_urls", count=len(product_urls), sub_sitemaps=len(sub_sitemaps))
    return product_urls


def url_key_from_url(url: str) -> str:
    """`https://www.petbarn.com.au/p/some-slug/123456` -> `some-slug`
    `https://www.petbarn.com.au/p/some-slug` -> `some-slug`
    """
    tail = url.split("/p/", 1)[-1].strip("/")
    parts = tail.split("/")
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[0]
    return parts[0]
