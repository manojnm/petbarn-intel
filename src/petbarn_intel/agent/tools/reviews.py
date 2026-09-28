"""Review Analyst tools -- statistics, aspect sentiment, hybrid search,
Q&A, and Petbarn's own vendor summary (plan section 2b).

Design rule carried over from plan section 1.3: `get_review_stats` always
reports Petbarn's *complete* Bazaarvoice statistics (never skewed by our
sample), while `search_reviews` explicitly returns a sample and says so --
the two must never be confused in an answer.
"""

from __future__ import annotations

from langchain_core.tools import tool

from petbarn_intel.agent.tools.cache import cached_tool, untrusted_tool
from petbarn_intel.logging import get_logger
from petbarn_intel.models.review import ASPECT_TAXONOMY
from petbarn_intel.store import products_repo, reviews_repo, search_repo
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)


def _embed_fn(text: str) -> list[float]:
    from petbarn_intel.enrichment.embeddings import embed_query

    return embed_query(text)


def _review_card(row: dict) -> dict:
    return {
        "review_id": row["review_id"],
        "rating": row["rating"],
        "title": row.get("title"),
        "text": row.get("text"),
        "submitted_at": row.get("submitted_at"),
        "helpful_votes": row.get("helpful_votes"),
        "is_recommended": bool(row["is_recommended"]) if row.get("is_recommended") is not None else None,
        "verified_purchaser": (
            bool(row["verified_purchaser"]) if row.get("verified_purchaser") is not None else None
        ),
    }


def _resolve(product_id: str, conn) -> str | None:
    return products_repo.resolve_product_id(product_id, conn=conn)


@tool
@cached_tool
def get_review_stats(product_id: str) -> dict:
    """Complete rating statistics for a product: full rating distribution
    (1-5 stars), average rating, percent recommended, and a last-90-day
    average/count for trend questions. These come from the *complete*
    Bazaarvoice statistics captured during the catalog census, not a
    sample, so counts and percentages here are exact -- use this (not
    `search_reviews`) whenever a question needs a number."""
    conn = get_connection()
    pid = _resolve(product_id, conn)
    if pid is None:
        return {"error": "not_found", "message": f"No product for id '{product_id}'."}
    stats = reviews_repo.review_stats(pid, conn=conn)
    product = products_repo.get_product(pid, conn=conn)
    stats["official_rating_value"] = product.rating_value if product else None
    stats["official_review_count"] = product.review_count if product else None
    stats["official_written_review_count"] = product.written_review_count if product else None
    stats["stored_review_text_count"] = stats["total_reviews"]
    if product and product.review_count and stats["total_reviews"] < product.review_count:
        stats["note"] = (
            f"IMPORTANT: `average_rating` and `distribution` above are computed from "
            f"only the {stats['total_reviews']} stored reviews (a stratified sample "
            "that deliberately over-represents 1-2 star reviews, per design -- "
            "negatives are rare but informative). They are NOT representative of the "
            f"product overall and can diverge sharply from the true rating. For the "
            "real, complete rating (from Bazaarvoice's full "
            f"{product.review_count:,}-review population), use `official_rating_value` "
            "and `official_review_count` instead -- always prefer those when answering "
            "'what's the rating' questions."
        )
    return stats


@tool
@cached_tool
@untrusted_tool
def analyze_aspects(
    product_id: str,
    aspects: list[str] | None = None,
    window: str = "all",
) -> dict:
    """Aspect-level sentiment breakdown (price/value, quality, palatability,
    health & digestion, packaging, delivery, customer service), extracted
    per-review by an LLM ahead of time. `window="recent"` restricts to the
    last 90 days (for "has opinion shifted lately" questions). Omit
    `aspects` for the full breakdown, or pass a subset of:
    price_value, quality, palatability, health_digestion, packaging,
    delivery_service, customer_service. Each row includes a short quote
    (`evidence`) so answers can cite specifics."""
    conn = get_connection()
    pid = _resolve(product_id, conn)
    if pid is None:
        return {"error": "not_found", "message": f"No product for id '{product_id}'."}
    requested = [a for a in (aspects or []) if a in ASPECT_TAXONOMY]
    rows = reviews_repo.aspect_summary(pid, aspects=requested or None, window=window, conn=conn)
    by_aspect: dict[str, dict] = {}
    for r in rows:
        entry = by_aspect.setdefault(r["aspect"], {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0})
        entry[r["sentiment"]] = r["n"]
    for aspect, counts in by_aspect.items():
        total = sum(counts.values())
        counts["total_mentions"] = total
        evidence = reviews_repo.aspect_evidence(pid, aspect, sentiment="negative", limit=2, conn=conn)
        evidence += reviews_repo.aspect_evidence(pid, aspect, sentiment="positive", limit=2, conn=conn)
        counts["sample_quotes"] = [
            {"review_id": e["review_id"], "sentiment": e["sentiment"], "quote": e["evidence"],
             "rating": e["rating"], "submitted_at": e["submitted_at"]}
            for e in evidence if e.get("evidence")
        ]
    return {"product_id": pid, "window": window, "aspects": by_aspect}


@tool
@cached_tool
@untrusted_tool
def search_reviews(
    product_id: str | None = None,
    query: str = "",
    sort: str = "relevance",
    rating_min: int | None = None,
    rating_max: int | None = None,
    limit: int = 8,
) -> dict:
    """Find specific review evidence to quote. With `query` set, does hybrid
    keyword+semantic search (so "upset tummy" also matches "runny poo",
    "loose stools") ranked by relevance, optionally scoped to one
    `product_id`; across ALL products when `product_id` is omitted (for
    "which products do people say cause X" questions). Without `query`,
    set `sort` to one of recent/helpful/lowest/highest to browse a specific
    product's stored sample. This returns a SAMPLE of stored reviews (not
    the complete set) -- use `get_review_stats` for exact counts."""
    conn = get_connection()
    pid = _resolve(product_id, conn) if product_id else None
    if product_id and pid is None:
        return {"error": "not_found", "message": f"No product for id '{product_id}'."}

    semantic = True
    if query:
        from petbarn_intel.enrichment.embeddings import query_embeddings_compatible

        use_vectors = query_embeddings_compatible(conn=conn)
        semantic = use_vectors
        try:
            rows = search_repo.hybrid_search_reviews(
                query, product_id=pid, limit=limit,
                embed_fn=_embed_fn if use_vectors else None, conn=conn,
            )
        except Exception as exc:  # noqa: BLE001 - embeddings may be unavailable
            logger.warning("hybrid_search_failed_falling_back", error=str(exc))
            semantic = False
            rows = search_repo.hybrid_search_reviews(query, product_id=pid, limit=limit, conn=conn)
    elif pid:
        rows = reviews_repo.get_reviews(
            pid, rating_min=rating_min, rating_max=rating_max, sort=sort, limit=limit, conn=conn
        )
    else:
        return {"error": "bad_request", "message": "Provide `query` and/or `product_id`."}

    mode = ("hybrid_search" if semantic else "keyword_search") if query else f"sorted:{sort}"
    result = {
        "product_id": pid,
        "mode": mode,
        "sample_size": len(rows),
        "reviews": [_review_card(r) for r in rows],
    }
    if query and not semantic:
        result["note"] = "Semantic re-ranking unavailable in this deployment; matched by keyword only."
    return result


@tool
@cached_tool
@untrusted_tool
def get_vendor_summary(product_id: str) -> dict:
    """Petbarn's own Bazaarvoice AI-generated "Summary of Reviews" panel --
    a secondary reference signal, not our own analysis. It can be weeks
    old, may include incentivized reviews, and has no citations. Use it
    only to contrast against your own evidence-backed analysis (e.g.
    "Petbarn's AI summary says X; a review of the actual quotes shows
    Y"), and always surface its `disclaimer` and `vendor_created_at` if
    quoting it."""
    conn = get_connection()
    pid = _resolve(product_id, conn)
    if pid is None:
        return {"error": "not_found", "message": f"No product for id '{product_id}'."}
    summary = products_repo.get_vendor_summary(pid, conn=conn)
    if summary is None:
        return {"product_id": pid, "available": False}
    summary["available"] = True
    summary["product_id"] = pid
    return summary


@tool
@cached_tool
@untrusted_tool
def get_product_qa(product_id: str) -> list[dict]:
    """Customer Q&A ("ask an owner") for a product -- questions and any
    answers shoppers/staff left. Useful for specific factual questions
    ("does this fit a size-large harness") that reviews may not cover."""
    conn = get_connection()
    pid = _resolve(product_id, conn)
    if pid is None:
        return [{"error": "not_found", "message": f"No product for id '{product_id}'."}]
    import orjson

    rows = reviews_repo.get_questions(pid, conn=conn)
    return [
        {
            "question": r["question_text"],
            "answers": orjson.loads(r["answer_texts_json"]) if r.get("answer_texts_json") else [],
            "submitted_at": r.get("submitted_at"),
        }
        for r in rows
    ]


TOOLS = [get_review_stats, analyze_aspects, search_reviews, get_vendor_summary, get_product_qa]
