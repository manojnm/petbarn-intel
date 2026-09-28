"""Enrichment services (plan section 1.5/1.6, "EnrichService" in the
architecture diagram): batch LLM aspect-sentiment extraction for seed
reviews, and the embedding + hybrid-search index build. Both are driven by
the work-queue functions already in `store/reviews_repo.py`
(`aspects_missing_for_product`, `reviews_without_embeddings`), so re-running
either module after a prompt/model version bump only recomputes what's
actually stale.
"""
