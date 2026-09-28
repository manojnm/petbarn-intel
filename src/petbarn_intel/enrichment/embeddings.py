"""Review embeddings for hybrid search (plan section 1.5).

Primary path: Azure `text-embedding-3-large`, truncated to `EMBEDDING_DIM`
(512) via the API's own `dimensions` param -- the plan's stated model
choice, now that a deployment actually exists on the Azure resource.
Falls back automatically to local `fastembed` (`BAAI/bge-small-en-v1.5`,
zero-padded to the same 512 dims) if no embedding deployment is
configured -- the plan's own designed fallback for that case, and what
this ran on before the deployment existed.

Provider choice is transparent to storage either way: rows are keyed by
`(review_id, model)`, so switching providers just computes a fresh set
under the new model name -- nothing to clean up first.
"""

from __future__ import annotations

import asyncio

import litellm

from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger
from petbarn_intel.store import reviews_repo, search_repo
from petbarn_intel.store.db import EMBEDDING_DIM, ensure_vec_table, get_connection

logger = get_logger(__name__)

# Azure quota: 250,000 TPM / 1,500 RPM. At batch_size=300 this whole job is
# only ~35 requests (measured: 388,538 total tokens / 10,363 reviews) -- RPM
# is nowhere close to binding (35 << 1500), so concurrency is capped at the
# batch count, not throttled for it. TPM *is* binding in aggregate (389K
# tokens > one minute's 250K budget), but that's unavoidable regardless of
# concurrency -- it just means >=1 request will get a 429 no matter how
# this is paced, so real-time retry/backoff (below) does the pacing instead
# of a static guess.
_AZURE_BATCH_SIZE = 300
_AZURE_CONCURRENCY = 25
_MAX_RETRIES = 6
_BASE_BACKOFF_S = 2.0
# fastembed is CPU-bound, single-threaded (see below), and this whole
# module's calls run through one asyncio loop -- no concurrency benefit,
# and no thread-safety guarantee across concurrent calls into one model
# instance, so it stays sequential.
_LOCAL_BATCH_SIZE = 64
_FASTEMBED_NATIVE_DIM = 384

_fastembed_model = None


def _has_azure_embedding_deployment() -> bool:
    settings = get_settings()
    return (
        settings.llm_provider == "azure"
        and settings.has_azure_credentials
        and bool(settings.azure_embedding_deployment)
    )


def _azure_kwargs() -> dict:
    settings = get_settings()
    return {
        "api_key": settings.azure_openai_api_key,
        "api_base": settings.azure_openai_endpoint,
        "api_version": settings.azure_embedding_api_version,
    }


def _retry_after_seconds(exc: Exception) -> float | None:
    headers = getattr(exc, "headers", None) or {}
    val = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


async def _embed_azure(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await litellm.aembedding(
                model=settings.embedding_model_id(),
                input=texts,
                dimensions=EMBEDDING_DIM,
                **_azure_kwargs(),
            )
            return [d["embedding"] for d in resp.data]
        except litellm.RateLimitError as exc:
            last_exc = exc
            # TPM quota is exceeded in aggregate for this job regardless of
            # concurrency (see constants above) -- honor Azure's own
            # Retry-After when given, else back off exponentially.
            wait = _retry_after_seconds(exc) or (_BASE_BACKOFF_S * (2**attempt))
            logger.info("embedding_rate_limited", attempt=attempt + 1, wait_s=round(wait, 1))
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


def _embed_azure_sync(texts: list[str]) -> list[list[float]]:
    settings = get_settings()
    resp = litellm.embedding(
        model=settings.embedding_model_id(),
        input=texts,
        dimensions=EMBEDDING_DIM,
        **_azure_kwargs(),
    )
    return [d["embedding"] for d in resp.data]


def _get_fastembed_model():
    global _fastembed_model
    if _fastembed_model is None:
        from fastembed import TextEmbedding

        # `threads=1`: the multi-threaded onnxruntime session aborts with a
        # `recursive_mutex lock failed` error on macOS -- reproduced twice
        # live, once mid-run (not just at exit as first assumed). Slower,
        # but the version that actually completes without crashing.
        _fastembed_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5", threads=1)
    return _fastembed_model


def _embed_fastembed(texts: list[str]) -> list[list[float]]:
    model = _get_fastembed_model()
    out = []
    for vec in model.embed(texts):
        vec = list(vec)[:_FASTEMBED_NATIVE_DIM]
        if len(vec) < EMBEDDING_DIM:
            vec = vec + [0.0] * (EMBEDDING_DIM - len(vec))
        out.append(vec)
    return out


def model_name() -> str:
    settings = get_settings()
    if _has_azure_embedding_deployment():
        return settings.embedding_model_id()
    return "fastembed/BAAI/bge-small-en-v1.5"


def stored_embedding_models(conn=None) -> set[str]:
    conn = conn or get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT model FROM review_embeddings").fetchall()
    except Exception:  # noqa: BLE001 - table may not exist yet on a brand-new DB
        return set()
    return {r["model"] for r in rows}


def query_embeddings_compatible(conn=None) -> bool:
    """Whether `embed_query()`'s current model would land in the same
    vector space as what's actually stored in `vec_reviews`. Comparing a
    query vector from a *different* embedding model against those stored
    vectors isn't just lower quality, it's meaningless (different
    coordinate spaces) -- e.g. the deployed app running `LLM_PROVIDER=openai`
    (so no Azure credentials ever reach it) against a DB whose reviews were
    embedded via Azure `text-embedding-3-large`. Callers should skip vector
    search entirely (keyword/FTS still works fine) rather than silently
    return near-random rankings. True on a fresh DB with nothing stored
    yet, since there's nothing to mismatch against."""
    stored = stored_embedding_models(conn=conn)
    return not stored or model_name() in stored


def embed_query(text: str) -> list[float]:
    """Sync -- `search_repo.hybrid_search_reviews(embed_fn=...)` calls this
    synchronously at query time (one embedding per search call, not a
    batch job, so a blocking round-trip is fine here)."""
    if _has_azure_embedding_deployment():
        return _embed_azure_sync([text])[0]
    return _embed_fastembed([text])[0]


async def run_embedding_batch(
    product_id: str | None = None, concurrency: int = _AZURE_CONCURRENCY, conn=None,
) -> dict:
    """Embed every review missing one (optionally scoped to `product_id`).
    Safe to re-run -- `reviews_without_embeddings()` only returns what's
    not already covered under the *current* model name."""
    conn = conn or get_connection()
    ensure_vec_table(conn)
    use_azure = _has_azure_embedding_deployment()
    name = model_name()
    batch_size = _AZURE_BATCH_SIZE if use_azure else _LOCAL_BATCH_SIZE

    missing = reviews_repo.reviews_without_embeddings(
        product_id=product_id, limit=100_000, conn=conn,
    )
    if not missing:
        logger.info("embedding_nothing_to_do", model=name)
        return {"embedded": 0, "model": name, "batches": 0}

    batches = [missing[i : i + batch_size] for i in range(0, len(missing), batch_size)]
    sem = asyncio.Semaphore(concurrency if use_azure else 1)
    total = 0
    done_batches = 0

    async def process(batch: list[dict]) -> None:
        nonlocal total, done_batches
        texts = [f"{(r['title'] or '')} {r['text']}".strip() or " " for r in batch]
        async with sem:
            try:
                if use_azure:
                    vectors = await _embed_azure(texts)
                else:
                    vectors = await asyncio.to_thread(_embed_fastembed, texts)
            except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
                logger.warning("embedding_batch_failed", error=str(exc), n=len(batch))
                return
        for row, vec in zip(batch, vectors, strict=True):
            search_repo.upsert_vec_embedding(row["rowid"], vec, conn=conn)
            reviews_repo.mark_embedded(row["review_id"], row["rowid"], name, EMBEDDING_DIM, conn=conn)
        conn.commit()
        total += len(batch)
        done_batches += 1
        logger.info(
            "embedding_batch_done",
            done=done_batches, total_batches=len(batches), embedded=total, model=name,
        )

    await asyncio.gather(*(process(b) for b in batches))
    return {"embedded": total, "model": name, "batches": len(batches)}
