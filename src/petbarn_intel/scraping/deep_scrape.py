"""Deep-scrape orchestrator: for every `tier='seed'` product, pull the full
extraction chain (GraphQL detail -> JSON-LD cross-check -> field merge),
the vendor AI summary, a stratified review sample, and Q&A -- then persist
everything and bump the product to a rich, `completeness`-scored row.

This is Tier B from the plan (section 1.2). GraphQL + JSON-LD cover every
field the seed set needs on the happy path (verified live during recon,
docs/decisions/002, 003), so the LLM field-level fallback
(`scraping/quality/llm_fallback.py`, plan 1.7 layer 4) only fires for
whatever's still missing after both -- expected to be rare, which is the
point: pay the LLM cost only on genuine failure.
"""

from __future__ import annotations

import hashlib

from petbarn_intel.logging import get_logger
from petbarn_intel.models.provenance import SourceKind
from petbarn_intel.models.trace import FieldEvent, Incident
from petbarn_intel.scraping.extract import graphql_product, pdp_html, vendor_summary
from petbarn_intel.scraping.extract.merge import completeness_score, merge_product_detail
from petbarn_intel.scraping.extract.text_utils import parse_float
from petbarn_intel.scraping.fetch import TieredFetcher
from petbarn_intel.scraping.quality import llm_fallback
from petbarn_intel.scraping.reviews import bv_qa, bv_reviews
from petbarn_intel.store import ops_repo, products_repo, reviews_repo
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)

# Fields whose absence after the full chain is worth a `missing_field`
# incident -- i.e. things nearly every real product has.
_EXPECTED_FIELDS = ("description", "ingredients", "price_min")


def _stable_id(*parts: str) -> str:
    """Short, collision-resistant id from arbitrary parts. Naive
    `product_id[:24]` truncation collided across seed products sharing a
    long common slug prefix (e.g. two `advance-medium-breed-...` SKUs) --
    hashing the *whole* string avoids that regardless of slug length."""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


async def _try_llm_fallback(fetcher: TieredFetcher, product) -> None:
    """Mutate `product` in place: fill whatever of `_EXPECTED_FIELDS` is
    still empty after `merge_product_detail` via the LLM fallback, stamp
    provenance/confidence for anything recovered, and recompute
    completeness. No-op (fast) when GraphQL + JSON-LD already covered
    everything, which is the common case."""
    missing = [f for f in _EXPECTED_FIELDS if not getattr(product, f, None)]
    if not missing:
        return

    # Cache hit in practice: `pdp_html.fetch_pdp_jsonld` already fetched
    # this exact URL a few lines up in `deep_scrape_product`.
    html_result = await fetcher.fetch(product.url, resource_type="product_page", respect_robots=True)
    if not html_result.ok():
        return

    recovered = await llm_fallback.llm_extract_fields(html_result.text, product.name, missing)
    if not recovered:
        return

    sources = dict(product.sources)
    confidence = dict(product.confidence)
    for field, value in recovered.items():
        if field == "price_min":
            parsed = parse_float(value)
            if parsed is None:
                continue
            product.price_min = parsed
        else:
            setattr(product, field, value)
        sources[field] = SourceKind.LLM_FALLBACK.value
        confidence[field] = 0.6
    product.sources = sources
    product.confidence = confidence
    product.completeness = completeness_score(product)


async def deep_scrape_product(
    fetcher: TieredFetcher, product_id: str, run_id: str, reviews_per_product: int, conn,
) -> dict:
    product = products_repo.get_product(product_id, conn=conn)
    if product is None or not product.url_key:
        return {"product_id": product_id, "ok": False, "error": "not_found_or_no_url_key"}

    detail = await graphql_product.fetch_product_detail(fetcher, product.url_key)
    if detail is None:
        ops_repo.open_incident(
            Incident(
                incident_id=_stable_id("deepscrape-graphql", product_id, run_id),
                kind="endpoint_failure",
                severity="warning",
                product_id=product_id,
                detail="GraphQL product detail fetch returned nothing during deep scrape.",
            ),
            conn=conn,
        )
        return {"product_id": product_id, "ok": False, "error": "graphql_detail_missing"}

    jsonld = await pdp_html.fetch_pdp_jsonld(fetcher, product.url)

    # Vendor AI summary + reviews/QA are keyed by the numeric *variant* SKU,
    # not the parent product id (see docs/decisions/001-variant-sku-keying).
    # Sample across every variant's reviews (bounded per-variant so a
    # multi-size product doesn't multiply the total budget) and merge.
    variant_skus = [v["sku"] for v in detail.get("variants", []) if v.get("sku")]
    primary_sku = variant_skus[0] if variant_skus else product_id

    summary = await vendor_summary.fetch_vendor_summary(fetcher, primary_sku)

    per_variant_budget = max(20, reviews_per_product // max(1, len(variant_skus)))
    all_reviews: dict[str, object] = {}
    for vsku in variant_skus:
        sample = await bv_reviews.fetch_reviews_sample(
            fetcher, vsku, product_id, target_total=per_variant_budget,
        )
        for r in sample:
            all_reviews[r.review_id] = r
    reviews = list(all_reviews.values())[:reviews_per_product]

    questions: list = []
    for vsku in variant_skus[:1]:  # Q&A is sparse; one variant is representative.
        questions.extend(await bv_qa.fetch_questions(fetcher, vsku, product_id))

    product = merge_product_detail(product, detail, jsonld, summary)
    await _try_llm_fallback(fetcher, product)
    products_repo.upsert_product(product, conn=conn)
    n_reviews = reviews_repo.upsert_reviews(reviews, conn=conn) if reviews else 0
    if questions:
        reviews_repo.upsert_questions(questions, conn=conn)

    for field in _EXPECTED_FIELDS:
        ok = bool(getattr(product, field, None))
        ops_repo.record_field_event(
            FieldEvent(
                event_id=_stable_id("field_event", product_id, field, run_id),
                run_id=run_id,
                product_id=product_id,
                field_name=field,
                source=product.sources.get(field, "none"),
                success=ok,
            ),
            conn=conn,
        )
        if not ok:
            ops_repo.open_incident(
                Incident(
                    incident_id=_stable_id("deepscrape-missing", field, product_id, run_id),
                    kind="missing_field",
                    severity="info",
                    product_id=product_id,
                    field_name=field,
                    detail=f"'{field}' still empty after the full deep-scrape chain.",
                ),
                conn=conn,
            )
    conn.commit()
    return {
        "product_id": product_id,
        "ok": True,
        "reviews": n_reviews,
        "questions": len(questions),
        "completeness": product.completeness,
    }


async def run_deep_scrape(
    limit: int | None = None,
    reviews_per_product: int | None = None,
    fetcher: TieredFetcher | None = None,
    conn=None,
) -> dict:
    from petbarn_intel.config import get_settings

    conn = conn or get_connection()
    settings = get_settings()
    reviews_per_product = reviews_per_product or settings.reviews_per_product
    own_fetcher = fetcher is None
    fetcher = fetcher or TieredFetcher()
    run = ops_repo.start_run("seed_deep_scrape", conn=conn)

    try:
        seed_rows = products_repo.list_products(tier="seed", conn=conn)
        product_ids = [r["product_id"] for r in seed_rows]
        if limit:
            product_ids = product_ids[:limit]
        total = len(product_ids)
        logger.info("deep_scrape_start", total=total)

        ok_count = 0
        review_total = 0
        error_count = 0
        for idx, product_id in enumerate(product_ids, start=1):
            try:
                result = await deep_scrape_product(
                    fetcher, product_id, run.run_id, reviews_per_product, conn,
                )
            except Exception as exc:  # noqa: BLE001 - one bad product must not kill the run
                logger.warning("deep_scrape_product_failed", product_id=product_id, error=str(exc))
                result = {"product_id": product_id, "ok": False, "error": str(exc)}

            if result["ok"]:
                ok_count += 1
                review_total += result.get("reviews", 0)
            else:
                error_count += 1
            ops_repo.bump_run(
                run.run_id, pages=1, products=1 if result["ok"] else 0,
                reviews=result.get("reviews", 0), errors=0 if result["ok"] else 1, conn=conn,
            )
            if idx % 10 == 0 or idx == total:
                logger.info(
                    "deep_scrape_progress", done=idx, total=total, ok=ok_count,
                    errors=error_count, reviews_so_far=review_total,
                )

        ops_repo.finish_run(
            run.run_id, status="completed", pages_fetched=total, products_touched=ok_count,
            reviews_fetched=review_total, errors=error_count, conn=conn,
        )
        logger.info(
            "deep_scrape_done", total=total, ok=ok_count, errors=error_count,
            reviews=review_total,
        )
        return {
            "run_id": run.run_id,
            "total": total,
            "ok": ok_count,
            "errors": error_count,
            "reviews_fetched": review_total,
        }
    except Exception as exc:  # noqa: BLE001
        ops_repo.finish_run(run.run_id, status="failed", errors=1, notes={"error": str(exc)}, conn=conn)
        raise
    finally:
        if own_fetcher:
            await fetcher.aclose()
