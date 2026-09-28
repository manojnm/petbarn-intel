"""Stratified seed selection: picks the ~200 (configurable) products that get
a full deep scrape (Tier B). Every other census product is deep-scraped on
demand the first time a user asks about it (Tier C).

Selection goals (plan section 1.2):
    - cover every pet type present in the catalog, proportionally to how many
      reviewed products exist for it
    - within a pet type, prefer products with the most review material
    - deliberately include some low-rated and "polarizing" (middling-rating,
      high-volume) products so pros/cons questions have real substance --
      an all-five-star seed set would make the review analyst useless.
"""

from __future__ import annotations

import orjson
from pydantic import BaseModel

from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)

MIN_REVIEWS_ELIGIBLE = 5
POLARIZING_RATING_RANGE = (3.0, 4.2)
POLARIZING_MIN_REVIEWS = 15
LOW_RATED_MAX = 3.0
LOW_RATED_MIN_REVIEWS = 5

# Bucket proportions within each pet-type's allocation.
BUCKET_SHARES = {"top": 0.70, "polarizing": 0.20, "low_rated": 0.10}


class SeedSelection(BaseModel):
    product_id: str
    pet_type: str
    bucket: str
    review_count: int
    rating_value: float | None


def _eligible_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT product_id, name, brand, pet_types_json, rating_value, review_count
        FROM products
        WHERE review_count >= ?
        """,
        (MIN_REVIEWS_ELIGIBLE,),
    ).fetchall()
    out = []
    for r in rows:
        pet_types = orjson.loads(r["pet_types_json"]) or ["Other"]
        out.append({**r, "pet_type": pet_types[0]})
    return out


def _allocate_counts(groups: dict[str, list[dict]], total: int) -> dict[str, int]:
    sizes = {k: len(v) for k, v in groups.items()}
    grand_total = sum(sizes.values()) or 1
    allocation = {}
    for pet_type, n in sizes.items():
        share = max(3, round(total * n / grand_total)) if n > 0 else 0
        allocation[pet_type] = min(share, n)
    # Trim/grow to hit `total` as closely as possible without starving groups.
    diff = total - sum(allocation.values())
    pet_types_sorted = sorted(groups, key=lambda k: len(groups[k]), reverse=True)
    i = 0
    while diff != 0 and pet_types_sorted:
        pt = pet_types_sorted[i % len(pet_types_sorted)]
        if diff > 0 and allocation[pt] < sizes[pt]:
            allocation[pt] += 1
            diff -= 1
        elif diff < 0 and allocation[pt] > 3:
            allocation[pt] -= 1
            diff += 1
        i += 1
        if i > 10_000:  # pragma: no cover - safety valve
            break
    return allocation


def select_seed_products(
    target_size: int | None = None, conn=None
) -> list[SeedSelection]:
    settings = get_settings()
    target = target_size or settings.seed_size
    conn = conn or get_connection()

    rows = _eligible_rows(conn)
    if not rows:
        logger.warning("seed_selection_no_eligible_rows")
        return []

    by_pet_type: dict[str, list[dict]] = {}
    for row in rows:
        by_pet_type.setdefault(row["pet_type"], []).append(row)

    allocation = _allocate_counts(by_pet_type, target)
    logger.info("seed_allocation", allocation=allocation, eligible_total=len(rows))

    selected: list[SeedSelection] = []
    seen: set[str] = set()

    for pet_type, quota in allocation.items():
        if quota <= 0:
            continue
        candidates = by_pet_type[pet_type]
        top_n = max(1, round(quota * BUCKET_SHARES["top"]))
        polarizing_n = max(0, round(quota * BUCKET_SHARES["polarizing"]))
        low_n = max(0, quota - top_n - polarizing_n)

        by_reviews = sorted(candidates, key=lambda r: r["review_count"], reverse=True)
        top_bucket = [r for r in by_reviews if r["product_id"] not in seen][:top_n]
        for r in top_bucket:
            seen.add(r["product_id"])

        polarizing_pool = sorted(
            (
                r
                for r in candidates
                if r["product_id"] not in seen
                and r["rating_value"] is not None
                and POLARIZING_RATING_RANGE[0] <= r["rating_value"] <= POLARIZING_RATING_RANGE[1]
                and r["review_count"] >= POLARIZING_MIN_REVIEWS
            ),
            key=lambda r: r["review_count"],
            reverse=True,
        )
        polarizing_bucket = polarizing_pool[:polarizing_n]
        for r in polarizing_bucket:
            seen.add(r["product_id"])

        low_pool = sorted(
            (
                r
                for r in candidates
                if r["product_id"] not in seen
                and r["rating_value"] is not None
                and r["rating_value"] < LOW_RATED_MAX
                and r["review_count"] >= LOW_RATED_MIN_REVIEWS
            ),
            key=lambda r: r["review_count"],
            reverse=True,
        )
        low_bucket = low_pool[:low_n]
        for r in low_bucket:
            seen.add(r["product_id"])

        # Backfill any shortfall (e.g. no polarizing/low-rated candidates)
        # with more top-by-reviews so the pet type still gets its full quota.
        chosen_so_far = len(top_bucket) + len(polarizing_bucket) + len(low_bucket)
        shortfall = quota - chosen_so_far
        backfill: list[dict] = []
        if shortfall > 0:
            backfill = [r for r in by_reviews if r["product_id"] not in seen][:shortfall]
            for r in backfill:
                seen.add(r["product_id"])

        for bucket_name, bucket_rows in (
            ("top", top_bucket),
            ("polarizing", polarizing_bucket),
            ("low_rated", low_bucket),
            ("backfill", backfill),
        ):
            for r in bucket_rows:
                selected.append(
                    SeedSelection(
                        product_id=r["product_id"],
                        pet_type=pet_type,
                        bucket=bucket_name,
                        review_count=r["review_count"] or 0,
                        rating_value=r["rating_value"],
                    )
                )

    logger.info(
        "seed_selection_done",
        selected=len(selected),
        by_bucket={
            b: sum(1 for s in selected if s.bucket == b) for b in ("top", "polarizing", "low_rated", "backfill")
        },
    )
    return selected


def mark_as_seed(selections: list[SeedSelection], conn=None) -> int:
    """Flips `tier` to 'seed' for selected products (idempotent). The actual
    deep scrape (Tier B work) happens separately -- see `scraping/deep_scrape.py`.
    """
    conn = conn or get_connection()
    for s in selections:
        conn.execute("UPDATE products SET tier = 'seed' WHERE product_id = ?", (s.product_id,))
    conn.commit()
    return len(selections)
