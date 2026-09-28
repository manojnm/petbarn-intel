"""Hybrid review search: BM25 (FTS5) fused with vector similarity
(sqlite-vec) via reciprocal rank fusion.

Exact terms matter for this domain (brand names, "grain free", SKUs), so we
never rely on embeddings alone -- see docs/decisions/004-embeddings-choice.md.
"""

from __future__ import annotations

import sqlite3

from petbarn_intel.store.db import EMBEDDING_DIM, get_connection


def _fts_search(
    conn: sqlite3.Connection, query: str, product_id: str | None, limit: int
) -> list[tuple[int, float]]:
    """Returns [(rowid, bm25_rank)] ordered best-first. Lower bm25() is better."""
    clause = "fts_reviews MATCH ?"
    match = query.replace('"', '""')
    match = f'"{match}"' if " " in match else match
    params: list = [match]
    if product_id:
        clause += " AND product_id = ?"
        params.append(product_id)
    try:
        rows = conn.execute(
            f"SELECT rowid, bm25(fts_reviews) as score FROM fts_reviews "
            f"WHERE {clause} ORDER BY score LIMIT ?",
            (*params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["rowid"], r["score"]) for r in rows]


def _vec_search(
    conn: sqlite3.Connection, embedding: list[float], product_id: str | None, limit: int
) -> list[tuple[int, float]]:
    import sqlite_vec

    blob = sqlite_vec.serialize_float32(embedding[:EMBEDDING_DIM])
    try:
        if product_id:
            rows = conn.execute(
                """
                SELECT v.review_rowid as rowid, v.distance as distance
                FROM vec_reviews v
                JOIN reviews r ON r.rowid = v.review_rowid
                WHERE v.embedding MATCH ? AND k = ? AND r.product_id = ?
                ORDER BY v.distance
                """,
                (blob, limit, product_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT review_rowid as rowid, distance FROM vec_reviews "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (blob, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["rowid"], r["distance"]) for r in rows]


def hybrid_search_reviews(
    query: str,
    product_id: str | None = None,
    limit: int = 15,
    embed_fn=None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Reciprocal rank fusion over BM25 and (optional) vector results.

    `embed_fn` is a callable `str -> list[float]`; if None, falls back to
    keyword-only search (still useful, and always available offline).
    """
    conn = conn or get_connection()
    candidate_pool = max(limit * 4, 40)
    fts_hits = _fts_search(conn, query, product_id, candidate_pool)

    vec_hits: list[tuple[int, float]] = []
    if embed_fn is not None:
        try:
            embedding = embed_fn(query)
            vec_hits = _vec_search(conn, embedding, product_id, candidate_pool)
        except Exception:
            vec_hits = []

    k_rrf = 60.0
    scores: dict[int, float] = {}
    for rank, (rowid, _) in enumerate(fts_hits):
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k_rrf + rank)
    for rank, (rowid, _) in enumerate(vec_hits):
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k_rrf + rank)

    if not scores:
        return []

    ranked_rowids = sorted(scores, key=lambda r: scores[r], reverse=True)[:limit]
    placeholders = ",".join("?" for _ in ranked_rowids)
    rows = conn.execute(
        f"SELECT * FROM reviews WHERE rowid IN ({placeholders})", ranked_rowids
    ).fetchall()
    by_rowid = {}
    for row in rows:
        rowid = conn.execute(
            "SELECT rowid FROM reviews WHERE review_id = ?", (row["review_id"],)
        ).fetchone()["rowid"]
        by_rowid[rowid] = row
    return [by_rowid[r] for r in ranked_rowids if r in by_rowid]


def upsert_vec_embedding(
    rowid: int, embedding: list[float], conn: sqlite3.Connection | None = None
) -> None:
    import sqlite_vec

    conn = conn or get_connection()
    blob = sqlite_vec.serialize_float32(embedding[:EMBEDDING_DIM])
    conn.execute(
        "INSERT OR REPLACE INTO vec_reviews (review_rowid, embedding) VALUES (?, ?)",
        (rowid, blob),
    )
