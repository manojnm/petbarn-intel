"""Catalog tools -- the Catalog specialist's toolset (plan section 2b).

Every tool returns small, JSON-serializable dicts (never a raw Pydantic
`Product` dump), because that's what actually goes back into the LLM's
context window. Fields are picked for what a shopper question needs, not
for completeness -- `get_product_details` is the one exception, since it's
the tool that answers "tell me about X" directly.
"""

from __future__ import annotations

import sqlite3

import sqlglot
from langchain_core.tools import tool
from rapidfuzz import fuzz

from petbarn_intel.agent.tools.cache import cached_tool
from petbarn_intel.logging import get_logger
from petbarn_intel.store import products_repo
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)

_ALLOWED_TABLES = {"products", "variants", "price_history"}
_MAX_ROWS = 200


def _card(row: dict) -> dict:
    return {
        "product_id": row["product_id"],
        "name": row["name"],
        "brand": row.get("brand"),
        "price_min": row.get("price_min"),
        "price_max": row.get("price_max"),
        "rating_value": row.get("rating_value"),
        "review_count": row.get("review_count"),
        "tier": row.get("tier"),
        "url": row.get("url"),
    }


@tool
@cached_tool
def search_products(
    query: str = "",
    brand: str | None = None,
    category: str | None = None,
    pet_type: str | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search the product catalog (5,000+ Petbarn products). Use this first
    whenever the user names a product or brand, to resolve it to a
    `product_id` before calling `get_product_details`. `query` (matched
    against name/brand/description, then fuzzy-ranked) is the most
    reliable filter -- put the product/brand name there. `category` and
    `pet_type` are free-text substring matches against Petbarn's own
    (inconsistent) category tags, so they're unreliable for guessing --
    prefer leaving them unset and using `query` alone; only set them when
    the user's own words ("cat litter", "aquarium") are likely to appear
    verbatim in a category tag. If a filtered search returns nothing but
    `query` was given, this automatically retries with `query` alone.
    Returns compact product cards, not full details."""
    conn = get_connection()
    products = products_repo.search_products(
        query=query,
        brand=brand,
        category=category,
        pet_type=pet_type,
        max_price=max_price,
        min_rating=min_rating,
        limit=max(limit * 3, limit),
        conn=conn,
    )
    if not products and query and (category or pet_type):
        logger.info("search_products_filter_fallback", query=query, category=category, pet_type=pet_type)
        products = products_repo.search_products(
            query=query, brand=brand, max_price=max_price, min_rating=min_rating,
            limit=max(limit * 3, limit), conn=conn,
        )
    rows = [p.model_dump() for p in products]
    if query:
        rows.sort(
            key=lambda r: fuzz.WRatio(query.lower(), f"{r.get('brand') or ''} {r['name']}".lower()),
            reverse=True,
        )
    return [_card(r) for r in rows[:limit]]


@tool
@cached_tool
def get_product_details(product_id: str) -> dict:
    """Fetch full details for one product: name, brand, URL, categories,
    pet types, description, specification, ingredients, feeding guide,
    features, price range, per-variant (size) pricing and member pricing,
    stock, rating, review count, Petbarn's own AI review summary (a
    secondary, vendor-generated signal -- never present it as your own
    analysis), and data provenance/freshness. `product_id` should come from
    `search_products`. If the id doesn't resolve, this looks it up as a SKU
    too before giving up."""
    conn = get_connection()
    product = products_repo.get_product(product_id, conn=conn)
    if product is None:
        product = products_repo.get_product_by_sku(product_id, conn=conn)
    if product is None:
        return {
            "error": "not_found",
            "message": (
                f"No product found for id/sku '{product_id}'. "
                "Call search_products to resolve the name first."
            ),
        }
    data = product.model_dump()
    data["effective_price"] = product.effective_price()
    return data


@tool
@cached_tool
def compare_products(product_ids: list[str]) -> dict:
    """Side-by-side catalog comparison (price, brand, rating, variants,
    specification) for 2 or more products. Does not include review
    sentiment -- pair this with the Review analyst's tools for "compare what
    people say" questions. `product_ids` should come from
    `search_products`."""
    conn = get_connection()
    out = {}
    for pid in product_ids:
        product = products_repo.get_product(pid, conn=conn) or products_repo.get_product_by_sku(
            pid, conn=conn
        )
        if product is None:
            out[pid] = {"error": "not_found"}
            continue
        out[pid] = {
            "name": product.name,
            "brand": product.brand,
            "price_min": product.price_min,
            "price_max": product.price_max,
            "effective_price": product.effective_price(),
            "rating_value": product.rating_value,
            "review_count": product.review_count,
            "specification": product.specification,
            "variants": [v.model_dump() for v in product.variants],
            "tier": product.tier,
        }
    return out


@tool
@cached_tool
def get_price_history(sku: str) -> list[dict]:
    """Return recorded price/member-price snapshots over time for one
    variant SKU (only changes are recorded, so a flat price shows as one
    entry). Use for "has the price changed" questions."""
    conn = get_connection()
    rows = products_repo.get_price_history(sku, conn=conn)
    return [dict(r) for r in rows]


def _validate_readonly_sql(sql: str) -> str:
    """Raises ValueError with a human-readable reason on anything but a
    single read-only SELECT over the catalog tables. Never trust an LLM-
    generated SQL string directly against a real DB connection without
    this."""
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse SQL: {exc}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("only a single SELECT statement is allowed")
    stmt = statements[0]
    if not isinstance(stmt, sqlglot.exp.Select):
        raise ValueError("only SELECT statements are allowed")
    tables = {t.name.lower() for t in stmt.find_all(sqlglot.exp.Table)}
    disallowed = tables - _ALLOWED_TABLES
    if disallowed:
        raise ValueError(f"tables not allowed: {sorted(disallowed)} (allowed: {sorted(_ALLOWED_TABLES)})")
    if not stmt.args.get("limit"):
        stmt = stmt.limit(_MAX_ROWS)
    return stmt.sql(dialect="sqlite")


@tool
@cached_tool
def query_catalog(sql: str) -> dict:
    """Run a read-only SQL SELECT over the catalog for questions that need
    aggregation, ranking, or filtering beyond `search_products` (for
    example: "best-rated grain-free dry dog food under $100", "how many
    products does brand X have"). Only `products`, `variants`, and
    `price_history` tables are queryable; only SELECT is allowed (no
    INSERT/UPDATE/DELETE/PRAGMA/ATTACH). A missing LIMIT is capped at 200
    rows automatically. Useful columns on `products`: name, brand,
    price_min, price_max, rating_value, review_count, categories_json,
    pet_types_json, tier."""
    try:
        safe_sql = _validate_readonly_sql(sql)
    except ValueError as exc:
        return {"error": "invalid_sql", "message": str(exc)}
    conn = get_connection()
    try:
        rows = conn.execute(safe_sql).fetchall()
    except sqlite3.Error as exc:
        return {"error": "sql_error", "message": str(exc), "sql": safe_sql}
    return {"sql": safe_sql, "row_count": len(rows), "rows": [dict(r) for r in rows]}


TOOLS = [search_products, get_product_details, compare_products, get_price_history, query_catalog]
