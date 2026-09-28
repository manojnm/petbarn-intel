"""Census orchestrator: GraphQL catalog enumeration + Bazaarvoice bulk
statistics + a sitemap cross-check, producing a lightweight `Product` row
(tier="census") for every SKU in the catalog (~6,800 at time of recon).

This is Tier A from the plan (section 1.2): cheap, ~95 requests total,
1-2 minutes, and makes the *whole* catalog searchable/filterable even before
any deep scrape happens. Tier B (deep scrape of the seed set) and Tier C
(on-demand deep scrape) both start from a census row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from petbarn_intel.logging import get_logger
from petbarn_intel.models.product import Product, ProductVariant
from petbarn_intel.models.trace import Incident
from petbarn_intel.scraping.discovery import bv_statistics, sitemap
from petbarn_intel.scraping.discovery.graphql_census import _SKIP_SKU_PREFIXES, enumerate_catalog
from petbarn_intel.scraping.fetch import TieredFetcher
from petbarn_intel.store import ops_repo, products_repo
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)

BASE_URL = "https://www.petbarn.com.au"


def _product_url(url_key: str) -> str:
    return f"{BASE_URL}/p/{url_key}"


async def run_census(
    fetcher: TieredFetcher | None = None,
    fetch_statistics: bool = True,
    cross_check_sitemap: bool = True,
    conn=None,
) -> dict:
    conn = conn or get_connection()
    own_fetcher = fetcher is None
    fetcher = fetcher or TieredFetcher()
    run = ops_repo.start_run("census", conn=conn)

    try:
        items = await enumerate_catalog(fetcher)
        logger.info("census_upsert_start", total_items=len(items))
        products_touched = 0
        now = datetime.now(UTC)

        for item in items:
            sku = item["sku"]
            if not sku or not item["url_key"]:
                continue
            sources = {"name": "graphql", "price_min": "graphql", "categories": "graphql"}
            confidence = {"name": 0.95, "price_min": 0.9}
            if item.get("brand"):
                sources["brand"] = "graphql_category_inference"
                confidence["brand"] = 0.85
            product = Product(
                product_id=sku,
                name=item["name"],
                brand=item.get("brand"),
                url=_product_url(item["url_key"]),
                url_key=item["url_key"],
                categories=item["categories"],
                pet_types=item["pet_types"],
                description=item.get("description"),
                variants=[
                    ProductVariant(
                        sku=sku,
                        name=item["name"],
                        price=item.get("price"),
                    )
                ],
                price_min=item.get("price"),
                price_max=item.get("price"),
                stock_status=item.get("stock_status"),
                tier="census",
                sources=sources,
                confidence=confidence,
                completeness=0.3,  # census rows are intentionally shallow
                scraped_at=now,
                updated_at=now,
            )
            products_repo.upsert_product(product, conn=conn)
            products_touched += 1
            if products_touched % 500 == 0:
                logger.info("census_upsert_progress", done=products_touched, total=len(items))

        logger.info("census_upsert_done", products_touched=products_touched)
        ops_repo.bump_run(run.run_id, pages=1, products=products_touched, conn=conn)

        stats_matched = 0
        if fetch_statistics:
            # Query BV with *variant* SKUs (see bv_statistics.py docstring),
            # then aggregate per parent product: total review count summed
            # across sizes, rating weighted by each variant's review count.
            variant_to_parent: dict[str, str] = {}
            for item in items:
                for vsku in item.get("variant_skus") or [item["sku"]]:
                    variant_to_parent[vsku] = item["sku"]
            all_variant_skus = list(variant_to_parent.keys())
            variant_stats = await bv_statistics.bulk_statistics(fetcher, all_variant_skus)

            per_parent: dict[str, list[tuple[float, int]]] = {}
            for vsku, s in variant_stats.items():
                parent = variant_to_parent.get(vsku)
                rating = s.get("average_rating")
                count = s.get("review_count") or 0
                if parent and rating is not None and count > 0:
                    per_parent.setdefault(parent, []).append((rating, count))

            for parent_sku, pairs in per_parent.items():
                product = products_repo.get_product(parent_sku, conn=conn)
                if product is None:
                    continue
                total_count = sum(c for _, c in pairs)
                weighted_rating = (
                    round(sum(r * c for r, c in pairs) / total_count, 4) if total_count else None
                )
                product.review_count = total_count
                product.rating_value = weighted_rating
                product.sources["review_count"] = "bazaarvoice"
                product.sources["rating_value"] = "bazaarvoice"
                product.updated_at = datetime.now(UTC)
                products_repo.upsert_product(product, conn=conn)
            stats_matched = len(per_parent)
            ops_repo.bump_run(run.run_id, pages=(len(all_variant_skus) // 100 + 1), conn=conn)

        sitemap_coverage = None
        if cross_check_sitemap:
            try:
                sitemap_urls = await sitemap.fetch_product_urls(fetcher)
                known_url_keys = {item["url_key"] for item in items}
                sitemap_url_keys = {sitemap.url_key_from_url(u) for u in sitemap_urls}
                raw_missing = sitemap_url_keys - known_url_keys
                # Bundles/custom-donation SKUs are deliberately excluded from the
                # census (see graphql_census._SKIP_SKU_PREFIXES) -- they are not
                # standalone reviewable products, so they should not count as
                # coverage drift even though the sitemap lists them.
                excluded_bundles = {
                    k for k in raw_missing if any(k.startswith(p) for p in _SKIP_SKU_PREFIXES)
                }
                missing_from_census = raw_missing - excluded_bundles
                sitemap_coverage = {
                    "sitemap_products": len(sitemap_url_keys),
                    "census_products": len(known_url_keys),
                    "excluded_bundles": len(excluded_bundles),
                    "missing_from_census": len(missing_from_census),
                    "sample_missing": list(missing_from_census)[:10],
                }
                real_sitemap_products = len(sitemap_url_keys) - len(excluded_bundles)
                if real_sitemap_products and len(missing_from_census) > 0.05 * real_sitemap_products:
                    ops_repo.open_incident(
                        Incident(
                            incident_id=f"census-drift-{run.run_id[:8]}",
                            kind="drift",
                            severity="warning",
                            detail=(
                                f"{len(missing_from_census)} non-bundle product(s) in the "
                                "sitemap are missing from the GraphQL census (>5%): possible "
                                "GraphQL catalog filter drift."
                            ),
                        ),
                        conn=conn,
                    )
                logger.info("census_sitemap_cross_check", **sitemap_coverage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sitemap_cross_check_failed", error=str(exc))

        ops_repo.finish_run(
            run.run_id,
            status="completed",
            pages_fetched=products_touched,
            products_touched=products_touched,
            notes={"stats_matched": stats_matched, "sitemap_coverage": sitemap_coverage},
            conn=conn,
        )
        return {
            "run_id": run.run_id,
            "products": products_touched,
            "stats_matched": stats_matched,
            "sitemap_coverage": sitemap_coverage,
        }
    except Exception as exc:  # noqa: BLE001
        ops_repo.finish_run(run.run_id, status="failed", errors=1, notes={"error": str(exc)}, conn=conn)
        raise
    finally:
        if own_fetcher:
            await fetcher.aclose()
