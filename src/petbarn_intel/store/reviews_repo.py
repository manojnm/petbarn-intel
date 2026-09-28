"""Repository functions for reviews, aspects, Q&A, and the hybrid search
index (FTS5 + sqlite-vec).
"""

from __future__ import annotations

import sqlite3

import orjson

from petbarn_intel.models.qa import Question
from petbarn_intel.models.review import Review, ReviewAspect
from petbarn_intel.store.db import get_connection


def _dumps(obj) -> str:
    return orjson.dumps(obj).decode()


def upsert_reviews(reviews: list[Review], conn: sqlite3.Connection | None = None) -> int:
    conn = conn or get_connection()
    n = 0
    for r in reviews:
        conn.execute(
            """
            INSERT INTO reviews (
                review_id, product_id, rating, title, text, author, submitted_at,
                is_recommended, helpful_votes, not_helpful_votes, verified_purchaser,
                incentivized, syndicated, vader_compound, vader_label,
                sample_reasons_json, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(review_id) DO UPDATE SET
                rating=excluded.rating, title=excluded.title, text=excluded.text,
                helpful_votes=excluded.helpful_votes,
                not_helpful_votes=excluded.not_helpful_votes,
                vader_compound=excluded.vader_compound, vader_label=excluded.vader_label,
                sample_reasons_json=(
                    SELECT json_group_array(v) FROM (
                        SELECT value AS v FROM json_each(reviews.sample_reasons_json)
                        UNION
                        SELECT value AS v FROM json_each(excluded.sample_reasons_json)
                    )
                )
            """,
            (
                r.review_id,
                r.product_id,
                r.rating,
                r.title,
                r.text,
                r.author,
                r.submitted_at.isoformat() if r.submitted_at else None,
                int(r.is_recommended) if r.is_recommended is not None else None,
                r.helpful_votes,
                r.not_helpful_votes,
                int(r.verified_purchaser) if r.verified_purchaser is not None else None,
                int(r.incentivized) if r.incentivized is not None else None,
                int(r.syndicated) if r.syndicated is not None else None,
                r.vader_compound,
                r.vader_label,
                _dumps(r.sample_reasons),
                r.fetched_at.isoformat(),
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO fts_reviews (rowid, review_id, product_id, text) "
            "VALUES ((SELECT rowid FROM reviews WHERE review_id = ?), ?, ?, ?)",
            (r.review_id, r.review_id, r.product_id, f"{r.title or ''} {r.text}"),
        )
        n += 1
    conn.commit()
    return n


def get_reviews(
    product_id: str,
    rating_max: int | None = None,
    rating_min: int | None = None,
    sort: str = "recent",
    limit: int = 200,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    conn = conn or get_connection()
    clauses = ["product_id = ?"]
    params: list = [product_id]
    if rating_max is not None:
        clauses.append("rating <= ?")
        params.append(rating_max)
    if rating_min is not None:
        clauses.append("rating >= ?")
        params.append(rating_min)
    order = {
        "recent": "submitted_at DESC",
        "helpful": "helpful_votes DESC",
        "lowest": "rating ASC, submitted_at DESC",
        "highest": "rating DESC, submitted_at DESC",
    }.get(sort, "submitted_at DESC")
    rows = conn.execute(
        f"SELECT * FROM reviews WHERE {' AND '.join(clauses)} ORDER BY {order} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return rows


def review_stats(product_id: str, conn: sqlite3.Connection | None = None) -> dict:
    """Deterministic statistics over every *stored* review row for a product.
    NOTE: the `reviews` table itself only ever holds a stratified sample (plan
    section 1.3 -- deliberately over-representing 1-2 star reviews), so
    `average_rating`/`distribution`/`pct_recommended` here are sample-derived
    and can be skewed, not the true population statistics. `get_review_stats`
    (the tool wrapper) adds `official_rating_value`/`official_review_count`
    from the census-time Bazaarvoice bulk statistics for that -- callers
    needing the *real* rating should use those, not these.
    """
    conn = conn or get_connection()
    dist_rows = conn.execute(
        "SELECT rating, COUNT(*) as n FROM reviews WHERE product_id = ? GROUP BY rating",
        (product_id,),
    ).fetchall()
    distribution = {r["rating"]: r["n"] for r in dist_rows}
    total = sum(distribution.values())
    avg_row = conn.execute(
        "SELECT AVG(rating) as avg, "
        "SUM(CASE WHEN is_recommended = 1 THEN 1 ELSE 0 END) as rec, "
        "SUM(CASE WHEN is_recommended IS NOT NULL THEN 1 ELSE 0 END) as rec_total "
        "FROM reviews WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    recent_avg_row = conn.execute(
        "SELECT AVG(rating) as avg, COUNT(*) as n FROM reviews "
        "WHERE product_id = ? AND submitted_at >= datetime('now', '-90 days')",
        (product_id,),
    ).fetchone()
    return {
        "total_reviews": total,
        "distribution": distribution,
        "average_rating": avg_row["avg"],
        "pct_recommended": (
            round(100 * avg_row["rec"] / avg_row["rec_total"], 1) if avg_row["rec_total"] else None
        ),
        "recent_90d_average": recent_avg_row["avg"],
        "recent_90d_count": recent_avg_row["n"],
    }


def upsert_review_aspects(aspects: list[ReviewAspect], conn: sqlite3.Connection | None = None) -> int:
    conn = conn or get_connection()
    for a in aspects:
        conn.execute(
            """
            INSERT INTO review_aspects (review_id, aspect, sentiment, evidence,
                                         prompt_version, model, extracted_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(review_id, aspect, prompt_version) DO UPDATE SET
                sentiment=excluded.sentiment, evidence=excluded.evidence,
                model=excluded.model, extracted_at=excluded.extracted_at
            """,
            (
                a.review_id,
                a.aspect,
                a.sentiment,
                a.evidence,
                a.prompt_version,
                a.model,
                a.extracted_at.isoformat(),
            ),
        )
    conn.commit()
    return len(aspects)


def aspects_missing_for_product(
    product_id: str, prompt_version: str, conn: sqlite3.Connection | None = None
) -> list[dict]:
    """Reviews for this product with no aspect rows at the current prompt
    version -- the enrichment service's incremental work queue."""
    conn = conn or get_connection()
    return conn.execute(
        """
        SELECT r.* FROM reviews r
        WHERE r.product_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM review_aspects ra
              WHERE ra.review_id = r.review_id AND ra.prompt_version = ?
          )
        """,
        (product_id, prompt_version),
    ).fetchall()


def aspect_summary(
    product_id: str,
    aspects: list[str] | None = None,
    window: str = "all",
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Aggregate aspect-sentiment counts for a product. `window="recent"`
    restricts to reviews submitted in the last 90 days (plan section 2,
    example 2's "recent vs overall" comparison); `aspects` restricts to a
    subset of the taxonomy (see `models.review.ASPECT_TAXONOMY`)."""
    conn = conn or get_connection()
    clauses = ["r.product_id = ?"]
    params: list = [product_id]
    if window == "recent":
        clauses.append("r.submitted_at >= datetime('now', '-90 days')")
    if aspects:
        clauses.append(f"ra.aspect IN ({','.join('?' for _ in aspects)})")
        params.extend(aspects)
    return conn.execute(
        f"""
        SELECT aspect, sentiment, COUNT(*) as n
        FROM review_aspects ra JOIN reviews r ON r.review_id = ra.review_id
        WHERE {' AND '.join(clauses)}
        GROUP BY aspect, sentiment
        ORDER BY aspect
        """,
        params,
    ).fetchall()


def aspect_evidence(
    product_id: str,
    aspect: str,
    sentiment: str | None = None,
    limit: int = 5,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """A few concrete quotes backing one aspect (for citations), newest first."""
    conn = conn or get_connection()
    clauses = ["r.product_id = ?", "ra.aspect = ?"]
    params: list = [product_id, aspect]
    if sentiment:
        clauses.append("ra.sentiment = ?")
        params.append(sentiment)
    return conn.execute(
        f"""
        SELECT ra.aspect, ra.sentiment, ra.evidence, r.review_id, r.rating,
               r.submitted_at, r.helpful_votes
        FROM review_aspects ra JOIN reviews r ON r.review_id = ra.review_id
        WHERE {' AND '.join(clauses)}
        ORDER BY r.submitted_at DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def upsert_questions(questions: list[Question], conn: sqlite3.Connection | None = None) -> int:
    conn = conn or get_connection()
    for q in questions:
        conn.execute(
            """
            INSERT INTO questions (question_id, product_id, question_text,
                                    answer_texts_json, submitted_at, fetched_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(question_id) DO UPDATE SET
                answer_texts_json=excluded.answer_texts_json, fetched_at=excluded.fetched_at
            """,
            (
                q.question_id,
                q.product_id,
                q.question_text,
                _dumps(q.answer_texts),
                q.submitted_at.isoformat() if q.submitted_at else None,
                q.fetched_at.isoformat(),
            ),
        )
    conn.commit()
    return len(questions)


def get_questions(product_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT * FROM questions WHERE product_id = ? ORDER BY submitted_at DESC", (product_id,)
    ).fetchall()


def reviews_without_embeddings(
    product_id: str | None = None, limit: int = 1000, conn: sqlite3.Connection | None = None
) -> list[dict]:
    conn = conn or get_connection()
    clause = "AND r.product_id = ?" if product_id else ""
    params = (product_id, limit) if product_id else (limit,)
    return conn.execute(
        f"""
        SELECT r.rowid as rowid, r.review_id, r.product_id, r.title, r.text
        FROM reviews r
        LEFT JOIN review_embeddings e ON e.review_id = r.review_id
        WHERE e.review_id IS NULL {clause}
        LIMIT ?
        """,
        params,
    ).fetchall()


def mark_embedded(review_id: str, rowid: int, model: str, dim: int, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    from datetime import UTC, datetime

    conn.execute(
        "INSERT OR REPLACE INTO review_embeddings (review_id, model, dim, created_at) VALUES (?,?,?,?)",
        (review_id, model, dim, datetime.now(UTC).isoformat()),
    )
