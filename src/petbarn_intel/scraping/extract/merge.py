"""Field merger: combines the census row (already in the DB) with the
GraphQL detail + JSON-LD cross-check fetched during deep scrape into one
`Product`, stamping per-field provenance/confidence and recording any
cross-source disagreements. This is the "quality layer" glue for deep-scrape
(see plan section 1.7); LLM field-level fallback is intentionally not
wired in here yet -- it triggers only when both structured sources miss a
field (tracked separately by the `quality` todo).
"""

from __future__ import annotations

from datetime import UTC, datetime

from petbarn_intel.models.product import Product, ProductVariant, VendorSummary
from petbarn_intel.models.provenance import SourceKind

_IMPORTANT_FIELDS = (
    "brand", "description", "ingredients", "feeding_guide", "features",
    "specification", "variants", "price_min",
)


def merge_product_detail(
    product: Product,
    graphql_detail: dict,
    jsonld: dict | None,
    vendor_summary: dict | None,
) -> Product:
    now = datetime.now(UTC)
    sources = dict(product.sources)
    confidence = dict(product.confidence)
    disagreements = dict(product.disagreements)

    def set_field(field: str, value, source: SourceKind, conf: float) -> None:
        setattr(product, field, value)
        sources[field] = source.value
        confidence[field] = conf

    if graphql_detail.get("description"):
        set_field("description", graphql_detail["description"], SourceKind.GRAPHQL, 0.95)
    if graphql_detail.get("ingredients"):
        set_field("ingredients", graphql_detail["ingredients"], SourceKind.GRAPHQL, 0.95)
    if graphql_detail.get("feeding_guide"):
        set_field("feeding_guide", graphql_detail["feeding_guide"], SourceKind.GRAPHQL, 0.95)
    if graphql_detail.get("features"):
        set_field("features", graphql_detail["features"], SourceKind.GRAPHQL, 0.9)
    if graphql_detail.get("specification"):
        merged_spec = {**product.specification, **graphql_detail["specification"]}
        set_field("specification", merged_spec, SourceKind.GRAPHQL, 0.9)
    if graphql_detail.get("stock_status"):
        set_field("stock_status", graphql_detail["stock_status"], SourceKind.GRAPHQL, 0.9)
    if graphql_detail.get("price_min") is not None:
        set_field("price_min", graphql_detail["price_min"], SourceKind.GRAPHQL, 0.9)
    if graphql_detail.get("price_max") is not None:
        set_field("price_max", graphql_detail["price_max"], SourceKind.GRAPHQL, 0.9)

    # Brand: JSON-LD is authoritative (explicit `brand.name`); the census-time
    # category-inference heuristic is only a fallback, so JSON-LD always wins
    # here rather than being treated as a disagreement.
    jsonld_variants: dict = (jsonld or {}).get("variants", {})
    if jsonld and jsonld.get("brand"):
        set_field("brand", jsonld["brand"], SourceKind.JSON_LD, 0.9)

    # Variants: GraphQL is the base; JSON-LD supplies gtin (GraphQL's own
    # `gtin` field is null store-wide) and is cross-checked against rating.
    variants: list[ProductVariant] = []
    for v in graphql_detail.get("variants", []):
        jv = jsonld_variants.get(v["sku"], {})
        gtin = v.get("gtin") or jv.get("gtin")
        variants.append(
            ProductVariant(
                sku=v["sku"],
                name=v["name"] or product.name,
                size=v.get("size"),
                gtin=gtin,
                price=v.get("price"),
                currency="AUD",
                member_price=v.get("member_price"),
                availability=v.get("availability") or jv.get("availability"),
                image_url=v.get("image_url"),
                url=v.get("url") or product.url,
            )
        )
    if variants:
        set_field("variants", variants, SourceKind.GRAPHQL, 0.9)
        sources["variants.gtin"] = SourceKind.JSON_LD.value

    # Cross-check rating/review_count against whatever census/BV-stats already
    # put there; a >15% relative gap on a reasonably-sized sample is worth a
    # disagreement note (both numbers are kept, higher-priority source wins).
    for sku, jv in jsonld_variants.items():
        jrc = jv.get("review_count")
        if jrc and product.review_count and product.review_count > 20:
            gap = abs(jrc - product.review_count) / product.review_count
            if gap > 0.15:
                disagreements.setdefault("review_count", []).append(
                    f"bazaarvoice={product.review_count} json_ld[{sku}]={jrc}"
                )

    if vendor_summary:
        product.vendor_summary = VendorSummary(
            product_id=product.product_id,
            paragraph=vendor_summary.get("paragraph"),
            disclaimer=vendor_summary.get("disclaimer"),
            fetched_at=now,
        )
        sources["vendor_summary"] = SourceKind.VENDOR_SUMMARY.value
        confidence["vendor_summary"] = 0.7

    product.sources = sources
    product.confidence = confidence
    product.disagreements = disagreements
    product.completeness = completeness_score(product)
    product.scraped_at = now
    product.updated_at = now
    return product


def completeness_score(product: Product) -> float:
    """Fraction of `_IMPORTANT_FIELDS` populated. Public so callers that
    patch fields onto an already-merged product (e.g. the LLM fallback in
    `scraping/quality/llm_fallback.py`) can recompute it without
    duplicating the field list."""
    present = 0
    for field in _IMPORTANT_FIELDS:
        value = getattr(product, field, None)
        if value:
            present += 1
    return round(present / len(_IMPORTANT_FIELDS), 2)
