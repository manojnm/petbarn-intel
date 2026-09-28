"""Repository functions for products, variants, price history, vendor
summaries. Pure SQL in, Pydantic models out -- callers never see raw rows.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import orjson

from petbarn_intel.models.product import Product, ProductVariant, VendorSummary
from petbarn_intel.store.db import get_connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dumps(obj) -> str:
    return orjson.dumps(obj).decode()


def upsert_product(product: Product, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    conn.execute(
        """
        INSERT INTO products (
            product_id, name, brand, url, url_key, categories_json, pet_types_json,
            description, specification_json, ingredients, feeding_guide, features_json,
            price_min, price_max, currency, stock_status, rating_value, review_count,
            written_review_count, tier, sources_json, confidence_json, completeness,
            disagreements_json, scraped_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(product_id) DO UPDATE SET
            name=excluded.name, brand=excluded.brand, url=excluded.url,
            url_key=excluded.url_key, categories_json=excluded.categories_json,
            pet_types_json=excluded.pet_types_json, description=excluded.description,
            specification_json=excluded.specification_json, ingredients=excluded.ingredients,
            feeding_guide=excluded.feeding_guide, features_json=excluded.features_json,
            price_min=excluded.price_min, price_max=excluded.price_max,
            currency=excluded.currency, stock_status=excluded.stock_status,
            rating_value=excluded.rating_value, review_count=excluded.review_count,
            written_review_count=excluded.written_review_count,
            -- Tier priority is seed > on_demand > census. A routine refresh
            -- of a cheaper tier (e.g. re-running the census, or an
            -- on-demand scrape touching a product that's already
            -- deep-scraped) must never *downgrade* an existing row.
            tier=CASE
                WHEN products.tier = 'seed' THEN 'seed'
                WHEN products.tier = 'on_demand' AND excluded.tier = 'census'
                    THEN 'on_demand'
                ELSE excluded.tier
            END,
            sources_json=excluded.sources_json, confidence_json=excluded.confidence_json,
            completeness=excluded.completeness, disagreements_json=excluded.disagreements_json,
            scraped_at=excluded.scraped_at, updated_at=excluded.updated_at
        """,
        (
            product.product_id,
            product.name,
            product.brand,
            product.url,
            product.url_key,
            _dumps(product.categories),
            _dumps(product.pet_types),
            product.description,
            _dumps(product.specification),
            product.ingredients,
            product.feeding_guide,
            _dumps(product.features),
            product.price_min,
            product.price_max,
            product.currency,
            product.stock_status,
            product.rating_value,
            product.review_count,
            product.written_review_count,
            product.tier,
            _dumps(product.sources),
            _dumps(product.confidence),
            product.completeness,
            _dumps(product.disagreements),
            product.scraped_at.isoformat() if product.scraped_at else None,
            product.updated_at.isoformat(),
        ),
    )
    for variant in product.variants:
        upsert_variant(product.product_id, variant, conn=conn)
    if product.vendor_summary:
        upsert_vendor_summary(product.vendor_summary, conn=conn)
    conn.commit()


def upsert_variant(
    product_id: str, variant: ProductVariant, conn: sqlite3.Connection | None = None
) -> None:
    conn = conn or get_connection()
    conn.execute(
        """
        INSERT INTO variants (sku, product_id, name, size, gtin, price, currency,
                               member_price, availability, image_url, url)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(sku) DO UPDATE SET
            product_id=excluded.product_id, name=excluded.name, size=excluded.size,
            gtin=excluded.gtin, price=excluded.price, currency=excluded.currency,
            member_price=excluded.member_price, availability=excluded.availability,
            image_url=excluded.image_url, url=excluded.url
        """,
        (
            variant.sku,
            product_id,
            variant.name,
            variant.size,
            variant.gtin,
            variant.price,
            variant.currency,
            variant.member_price,
            variant.availability,
            variant.image_url,
            variant.url,
        ),
    )
    conn.execute(
        """
        INSERT INTO price_history (sku, product_id, price, member_price, availability, recorded_at)
        SELECT ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM price_history
            WHERE sku = ? ORDER BY recorded_at DESC LIMIT 1
        ) OR (
            SELECT price FROM price_history WHERE sku = ? ORDER BY recorded_at DESC LIMIT 1
        ) IS NOT ?
        """,
        (
            variant.sku,
            product_id,
            variant.price,
            variant.member_price,
            variant.availability,
            _now(),
            variant.sku,
            variant.sku,
            variant.price,
        ),
    )


def upsert_vendor_summary(summary: VendorSummary, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    conn.execute(
        """
        INSERT INTO vendor_summaries (product_id, paragraph, bullets, disclaimer,
                                       vendor_created_at, fetched_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(product_id) DO UPDATE SET
            paragraph=excluded.paragraph, bullets=excluded.bullets,
            disclaimer=excluded.disclaimer, vendor_created_at=excluded.vendor_created_at,
            fetched_at=excluded.fetched_at
        """,
        (
            summary.product_id,
            summary.paragraph,
            summary.bullets,
            summary.disclaimer,
            summary.vendor_created_at.isoformat() if summary.vendor_created_at else None,
            summary.fetched_at.isoformat(),
        ),
    )
    conn.commit()


def _row_to_product(row: dict, variants: list[dict], vendor_row: dict | None) -> Product:
    return Product(
        product_id=row["product_id"],
        name=row["name"],
        brand=row["brand"],
        url=row["url"],
        url_key=row["url_key"],
        categories=orjson.loads(row["categories_json"]),
        pet_types=orjson.loads(row["pet_types_json"]),
        description=row["description"],
        specification=orjson.loads(row["specification_json"]),
        ingredients=row["ingredients"],
        feeding_guide=row["feeding_guide"],
        features=orjson.loads(row["features_json"]),
        variants=[
            ProductVariant(
                sku=v["sku"],
                name=v["name"],
                size=v["size"],
                gtin=v["gtin"],
                price=v["price"],
                currency=v["currency"],
                member_price=v["member_price"],
                availability=v["availability"],
                image_url=v["image_url"],
                url=v["url"],
            )
            for v in variants
        ],
        price_min=row["price_min"],
        price_max=row["price_max"],
        currency=row["currency"],
        stock_status=row["stock_status"],
        rating_value=row["rating_value"],
        review_count=row["review_count"],
        written_review_count=row["written_review_count"],
        vendor_summary=(
            VendorSummary(
                product_id=row["product_id"],
                paragraph=vendor_row["paragraph"],
                bullets=vendor_row["bullets"],
                disclaimer=vendor_row["disclaimer"],
                vendor_created_at=vendor_row["vendor_created_at"],
                fetched_at=vendor_row["fetched_at"],
            )
            if vendor_row
            else None
        ),
        tier=row["tier"],
        sources=orjson.loads(row["sources_json"]),
        confidence=orjson.loads(row["confidence_json"]),
        completeness=row["completeness"],
        disagreements=orjson.loads(row["disagreements_json"]),
        scraped_at=row["scraped_at"],
        updated_at=row["updated_at"],
    )


def get_product(product_id: str, conn: sqlite3.Connection | None = None) -> Product | None:
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    if not row:
        return None
    variants = conn.execute(
        "SELECT * FROM variants WHERE product_id = ?", (product_id,)
    ).fetchall()
    vendor_row = conn.execute(
        "SELECT * FROM vendor_summaries WHERE product_id = ?", (product_id,)
    ).fetchone()
    return _row_to_product(row, variants, vendor_row)


def get_product_by_sku(sku: str, conn: sqlite3.Connection | None = None) -> Product | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT product_id FROM variants WHERE sku = ?", (sku,)
    ).fetchone()
    if not row:
        return None
    return get_product(row["product_id"], conn=conn)


def search_products(
    query: str = "",
    brand: str | None = None,
    category: str | None = None,
    pet_type: str | None = None,
    max_price: float | None = None,
    min_rating: float | None = None,
    limit: int = 20,
    conn: sqlite3.Connection | None = None,
) -> list[Product]:
    """Simple SQL-level pre-filter; callers layer RapidFuzz ranking for
    free-text `query` on top of this (see agent/tools/catalog.py)."""
    conn = conn or get_connection()
    clauses: list[str] = []
    params: list = []
    if query:
        clauses.append("(name LIKE ? OR brand LIKE ? OR description LIKE ?)")
        like = f"%{query}%"
        params += [like, like, like]
    if brand:
        clauses.append("brand LIKE ?")
        params.append(f"%{brand}%")
    if category:
        clauses.append("categories_json LIKE ?")
        params.append(f"%{category}%")
    if pet_type:
        clauses.append("pet_types_json LIKE ?")
        params.append(f"%{pet_type}%")
    if max_price is not None:
        clauses.append("price_min <= ?")
        params.append(max_price)
    if min_rating is not None:
        clauses.append("rating_value >= ?")
        params.append(min_rating)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM products {where} ORDER BY review_count DESC NULLS LAST LIMIT ?",
        (*params, limit),
    ).fetchall()
    out = []
    for row in rows:
        variants = conn.execute(
            "SELECT * FROM variants WHERE product_id = ?", (row["product_id"],)
        ).fetchall()
        out.append(_row_to_product(row, variants, None))
    return out


def list_products(
    tier: str | None = None, limit: int = 10_000, conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Lightweight listing (no variants join) for census reports / seed selection."""
    conn = conn or get_connection()
    if tier:
        rows = conn.execute(
            "SELECT * FROM products WHERE tier = ? LIMIT ?", (tier, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products LIMIT ?", (limit,)).fetchall()
    return rows


def count_products(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    conn = conn or get_connection()
    rows = conn.execute("SELECT tier, COUNT(*) as n FROM products GROUP BY tier").fetchall()
    return {r["tier"]: r["n"] for r in rows}


def get_price_history(sku: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT * FROM price_history WHERE sku = ? ORDER BY recorded_at", (sku,)
    ).fetchall()


def get_vendor_summary(product_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn or get_connection()
    row = conn.execute(
        "SELECT * FROM vendor_summaries WHERE product_id = ?", (product_id,)
    ).fetchone()
    return dict(row) if row else None


def resolve_product_id(product_id_or_sku: str, conn: sqlite3.Connection | None = None) -> str | None:
    """Best-effort resolution of either a canonical `product_id` or a
    variant `sku` to a canonical `product_id`. Used by tools that accept
    either, so the agent doesn't have to know which kind of id it has."""
    conn = conn or get_connection()
    row = conn.execute("SELECT product_id FROM products WHERE product_id = ?", (product_id_or_sku,)).fetchone()
    if row:
        return row["product_id"]
    row = conn.execute("SELECT product_id FROM variants WHERE sku = ?", (product_id_or_sku,)).fetchone()
    return row["product_id"] if row else None


def get_product_display_cards(
    product_ids: list[str], conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Return display-card data (name, price, rating, image_url, url) for a
    list of product_ids. Image URL comes from the first variant with a
    non-null image_url; falls back to None if no variants have images."""
    if not product_ids:
        return []
    conn = conn or get_connection()
    placeholders = ",".join("?" * len(product_ids))
    products = conn.execute(
        f"SELECT product_id, name, price_min, rating_value, url FROM products "
        f"WHERE product_id IN ({placeholders})",
        product_ids,
    ).fetchall()
    # Build a lookup for image_url from variants
    images = conn.execute(
        f"SELECT product_id, image_url FROM variants "
        f"WHERE product_id IN ({placeholders}) AND image_url IS NOT NULL "
        f"GROUP BY product_id",
        product_ids,
    ).fetchall()
    image_map = {r["product_id"]: r["image_url"] for r in images}
    # Preserve caller order
    order = {pid: i for i, pid in enumerate(product_ids)}
    cards = []
    for row in products:
        pid = row["product_id"]
        cards.append(
            {
                "product_id": pid,
                "name": row["name"],
                "price_min": row["price_min"],
                "rating_value": row["rating_value"],
                "url": row["url"],
                "image_url": image_map.get(pid),
            }
        )
    cards.sort(key=lambda c: order.get(c["product_id"], 999))
    return cards
