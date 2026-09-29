# ADR 001 — SQLite over Postgres (or any client-server DB)

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2024-Q4 (initial design) |
| **Deciders** | Engineering |

---

## Context

The knowledge store needs to support:

1. **Hybrid search** — BM25 full-text + approximate nearest-neighbour (ANN)
   vector search over ~10 k reviews, fused with Reciprocal Rank Fusion.
2. **Relational queries** — price lookups, catalog census, product comparisons,
   aggregate review stats.
3. **Deployment simplicity** — the Streamlit app is deployed on Streamlit
   Community Cloud (a free tier). No managed database is available without
   an external paid plan.
4. **Zero-setup for reviewers** — the dataset is committed to the repo as a
   single `.sqlite3` file; a reviewer clones the repo and the app works
   immediately without running migrations or seeding a remote DB.

## Options considered

| Option | Pro | Con |
|---|---|---|
| **SQLite + sqlite-vec + FTS5** | Zero infra, single file, ships with the repo, both search modes built-in | Not horizontally scalable; single writer |
| **Postgres + pgvector + pg_trgm** | Production-grade, horizontally scalable | Requires a running Postgres server (not free on Community Cloud); no zero-setup path |
| **DuckDB** | Fast analytics, in-process | No vector ANN extension at decision time; weaker FTS support |
| **Elasticsearch / OpenSearch** | Excellent hybrid search | Heavy infra; not deployable on Community Cloud |
| **Pinecone / Weaviate** | Managed vector DB | No relational joins; extra paid service; data not bundled with repo |

## Decision

**Use SQLite** with:

- **sqlite-vec** (`vec0` virtual table) for ANN vector search.
- **FTS5** virtual table for BM25 full-text search.
- **WAL mode** for concurrent reads (Streamlit spawns multiple threads).
- **Single committed file** (`data/db/petbarn_intel.sqlite3`) so the
  deployed app needs no migrations or network access to start.

## Consequences

✅ Zero-infrastructure deployment on Streamlit Community Cloud.  
✅ Hybrid BM25 + vector search in a single `SELECT` with RRF post-processing.  
✅ Reviewers and CI jobs get the full dataset by running `git clone`.  
⚠️ Not suitable for write-heavy concurrent workloads (fine for this use case:
   writes only happen during offline scrape jobs, not during serving).  
⚠️ Dataset updates require a new git commit for the DB file; a production
   system would use a remote DB with a deployment pipeline.
