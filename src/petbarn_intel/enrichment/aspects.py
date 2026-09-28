"""LLM aspect-sentiment extraction for seed reviews (plan section 1.6).

Batches ~50 reviews per structured-output call -- benchmarked live against
the configured deployment at ~5s/50-review batch, mostly fixed per-call
overhead, so bigger batches amortize it well. Results are cached by
`(review_id, aspect, prompt_version)` (`reviews_repo.upsert_review_aspects`
/ `aspects_missing_for_product`), so bumping `PROMPT_VERSION` after a
taxonomy change recomputes only what's stale, never a full re-run.

Concurrency is bounded (default 4) rather than firing all batches at once:
`kai-dev` looks like a low-quota dev deployment and its actual rate limit
is unknown, so this errs conservative and lets 429s be tuned via
`--concurrency` rather than discovered the hard way on a 10k-review run.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from petbarn_intel.agent.llm import achat
from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger
from petbarn_intel.models.review import ASPECT_TAXONOMY, ReviewAspect
from petbarn_intel.store import products_repo, reviews_repo
from petbarn_intel.store.db import get_connection

logger = get_logger(__name__)

PROMPT_VERSION = "v1"
_BATCH_SIZE = 50
_VALID_SENTIMENTS = ("positive", "negative", "neutral", "mixed")
_MAX_REVIEW_CHARS = 600
_MAX_EVIDENCE_CHARS = 200

_ASPECTS_LINE = ", ".join(ASPECT_TAXONOMY)


def _build_prompt(rows: list[dict]) -> str:
    lines = [
        f"[{r['review_id']}] ({r['rating']} stars) {(r['text'] or '')[:_MAX_REVIEW_CHARS]}"
        for r in rows
    ]
    return (
        f"Aspects: {_ASPECTS_LINE}.\n"
        "For each review below, decide which of these aspects it actually "
        "discusses and the sentiment for each (positive, negative, neutral, "
        "or mixed), plus a short verbatim quote (<20 words) as evidence. "
        "Only include aspects genuinely discussed in that specific review -- "
        "omit ones it doesn't mention, never invent one. Respond as strict "
        'JSON: {"review_id": {"aspect": {"sentiment": "...", "evidence": '
        '"..."}}}\n\n' + "\n".join(lines)
    )


async def _extract_batch(rows: list[dict]) -> dict:
    prompt = _build_prompt(rows)
    try:
        reply = await achat(
            [{"role": "user", "content": prompt}],
            role="mini",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(reply)
    except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
        logger.warning("aspect_batch_failed", error=str(exc), n=len(rows))
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_aspects(
    product_id: str, batch: list[dict], parsed: dict, model_id: str,
) -> list[ReviewAspect]:
    now = datetime.now(UTC)
    out: list[ReviewAspect] = []
    for row in batch:
        per_review = parsed.get(row["review_id"])
        if not isinstance(per_review, dict):
            continue
        for aspect, payload in per_review.items():
            if aspect not in ASPECT_TAXONOMY or not isinstance(payload, dict):
                continue
            sentiment = payload.get("sentiment")
            if sentiment not in _VALID_SENTIMENTS:
                continue
            evidence = payload.get("evidence")
            evidence = evidence[:_MAX_EVIDENCE_CHARS] if isinstance(evidence, str) else None
            out.append(
                ReviewAspect(
                    review_id=row["review_id"],
                    product_id=product_id,
                    aspect=aspect,
                    sentiment=sentiment,
                    evidence=evidence,
                    prompt_version=PROMPT_VERSION,
                    model=model_id,
                    extracted_at=now,
                )
            )
    return out


async def run_seed_aspect_enrichment(concurrency: int = 4, conn=None) -> dict:
    """Batch-extract aspects for every `tier='seed'` product's reviews that
    don't already have one at `PROMPT_VERSION`. Safe to re-run any time --
    it's a no-op pass over whatever's already covered."""
    conn = conn or get_connection()
    settings = get_settings()
    model_id = settings.chat_model_id("mini")

    seed_rows = products_repo.list_products(tier="seed", conn=conn)
    work: list[tuple[str, list[dict]]] = []
    for row in seed_rows:
        missing = reviews_repo.aspects_missing_for_product(
            row["product_id"], PROMPT_VERSION, conn=conn,
        )
        for i in range(0, len(missing), _BATCH_SIZE):
            work.append((row["product_id"], missing[i : i + _BATCH_SIZE]))

    if not work:
        logger.info("aspect_enrichment_nothing_to_do", products=len(seed_rows))
        return {"products": len(seed_rows), "batches": 0, "aspects_written": 0}

    sem = asyncio.Semaphore(concurrency)
    total_written = 0
    done = 0

    async def process(product_id: str, batch: list[dict]) -> None:
        nonlocal total_written, done
        async with sem:
            parsed = await _extract_batch(batch)
        aspects = _parse_aspects(product_id, batch, parsed, model_id)
        if aspects:
            total_written += reviews_repo.upsert_review_aspects(aspects, conn=conn)
        done += 1
        if done % 20 == 0 or done == len(work):
            logger.info(
                "aspect_enrichment_progress",
                done=done, total=len(work), aspects_written=total_written,
            )

    await asyncio.gather(*(process(pid, batch) for pid, batch in work))
    return {
        "products": len(seed_rows),
        "batches": len(work),
        "aspects_written": total_written,
    }
