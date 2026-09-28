# Petbarn Product & Review Assistant

A Streamlit chatbot that answers questions about Petbarn (petbarn.com.au) products and
customer reviews by dynamically calling tools over a real, scraped dataset -- not
canned answers. A [LangGraph](https://github.com/langchain-ai/langgraph) agent (guard
-> planner -> specialists -> synthesizer -> verifier) resolves each question, decides
which specialist(s) to consult, and those specialists call typed tools against a SQLite
knowledge store built by this repo's own scraper.

```
User -> Guard (rules) -> Planner (LLM) -> Catalog / Review / Scraper specialists (ReAct, tool-calling)
                                        -> Synthesizer (LLM) -> Verifier (LLM, complex questions only) -> Answer
```

## What's in the dataset

Already scraped and shipped in `data/db/petbarn_intel.sqlite3` (committed to this repo
so the deployed app needs zero setup):

| | |
|---|---|
| Catalog census | 5,288 products (GraphQL + Bazaarvoice bulk stats + sitemap cross-check) |
| Deep-scraped ("seed") products | 200 (stratified by pet type/category, incl. low-rated ones) -- well beyond the 8-10 the brief asks for |
| Customer reviews | 10,363 (full text, rating, helpfulness, recommend flag) |
| Aspect-sentiment rows | 11,976 (LLM-extracted: price, quality, palatability, health/digestion, packaging, delivery, service) |
| Review embeddings | 10,363 (Azure `text-embedding-3-large`, 512-dim, for hybrid keyword+semantic search) |

Any other product in the census (5,000+) can be scraped live, on demand, by the agent's
`scrape_product` tool mid-conversation.

## Tools the agent can call

- **Catalog** -- `search_products`, `get_product_details`, `compare_products`,
  `get_price_history`, `query_catalog` (read-only SQL, `sqlglot`-validated).
- **Review Analyst** -- `get_review_stats` (complete Bazaarvoice statistics, never
  sample-biased), `analyze_aspects`, `search_reviews` (hybrid BM25 + vector, with
  quotes/citations), `get_vendor_summary` (Petbarn's own AI summary, clearly labelled
  as a secondary signal), `get_product_qa`.
- **Scraper** -- `scrape_product` (live, on-demand deep scrape), `diagnose_page`,
  `get_scraper_health`.

Every tool call and LLM step is written to a `traces` table and shown in the chat UI's
"Agent trace" expander and on the **Ops** page (Streamlit multipage: Catalog / Data
quality / Incidents / Agent traces & cost).

All 10 read-only catalog/review tools sit behind a short-TTL (5 min) in-process cache
(`agent/tools/cache.py`), so repeat lookups -- two specialists resolving the same
product in parallel, a verifier retry, a follow-up question -- cost nothing. Cleared
automatically after any on-demand scrape writes fresh data. Hit rate is visible on the
Ops page's "Agent traces & cost" tab.

## Quickstart (local dev)

```bash
cd poc
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # fill in AZURE_OPENAI_* (local dev uses Azure)
petbarn-intel db init
```

Run the chatbot:

```bash
streamlit run src/petbarn_intel/app/Home.py
```

Or talk to the same agent from the terminal:

```bash
petbarn-intel agent chat
```

## CLI reference

All commands: `petbarn-intel <group> <command>` (equivalently
`python -m petbarn_intel.cli.main <group> <command>`).

```bash
petbarn-intel config                        # effective settings (no secrets)
petbarn-intel db init                       # create/upgrade schema (idempotent)
petbarn-intel db status                     # row counts per table

petbarn-intel scrape census                 # catalog discovery (~5,300 products)
petbarn-intel scrape seed --n 200           # stratified seed selection
petbarn-intel scrape deep                   # deep-scrape the seed set (detail + reviews + Q&A)

petbarn-intel enrich aspects                # LLM aspect-sentiment extraction
petbarn-intel enrich embeddings --concurrency 25   # review embeddings

petbarn-intel agent ping --role mini        # smoke-test the configured LLM deployment
petbarn-intel agent chat                    # REPL against the full agent graph

petbarn-intel report coverage               # per-field extraction completeness
petbarn-intel report incidents [--all]      # open (or all) data-quality incidents
petbarn-intel report cost --limit 20        # per-turn agent latency/cost/errors
```

The dataset above is already built; you only need `scrape`/`enrich` commands to rebuild
it from scratch or extend it.

## Deployment

Deployed on **Streamlit Community Cloud**, main file `src/petbarn_intel/app/Home.py`.

**Credential isolation, on purpose:** local development runs on an Azure OpenAI
deployment whose credentials never leave the developer's machine (not committed, not
put in any Cloud secret). The deployed/public app runs on a **separate OpenAI API
key** instead -- `config.py`'s `LLM_PROVIDER` switch makes this a config-only change,
never a code change (see `Settings.chat_model_id`/`embedding_model_id`). Streamlit
Cloud's Secrets box is set to:

```toml
LLM_PROVIDER = "openai"
OPENAI_API_KEY = "sk-..."
OPENAI_CHAT_MODEL_MINI = "gpt-4o-mini"
OPENAI_CHAT_MODEL_MAIN = "gpt-4o-mini"
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
```

(template at `.streamlit/secrets.toml.example`; `app/_secrets_bridge.py` bridges
Streamlit's `st.secrets` into `os.environ` so the plain `pydantic-settings` `Settings`
class -- shared with the CLI/scraper, no Streamlit dependency -- picks it up).

One consequence: the shipped review embeddings were computed with Azure
`text-embedding-3-large`. Since the deployed app never calls Azure, `search_reviews`
detects that mismatch (`embeddings.query_embeddings_compatible()`) and automatically
falls back to keyword-only (BM25) search rather than comparing incompatible vector
spaces -- semantic search over reviews is a local-dev-only feature today unless the
review set is re-embedded with an OpenAI model (`petbarn-intel enrich embeddings` with
`LLM_PROVIDER=openai`, a one-time offline job).

## Design notes / scope trade-offs

This started from a larger internal plan (multi-tier scraping, full ADR-style
`docs/`, a bulk-crawl human-in-the-loop approval flow, a generic multi-retailer
adapter). What actually shipped, and why:

- **Verifier** does one groundedness/coverage check and retries the plan at most once
  -- not a full multi-round critique loop, which wasn't worth the added latency for a
  POC-scale eval.
- **Conversational memory** is the last few `(question, answer)` pairs folded into the
  prompt, not a dedicated focus-entity tracker -- simpler, and sufficient for "compare
  it to the other one" follow-ups.
- **On-demand scraping** (`scrape_product`) reuses the same deep-scrape path as the
  seed set, for any product already in the catalog census; a generic (non-Petbarn)
  retailer adapter was scoped out.
- **Cost tracking** in `report cost`/the Ops page estimates USD per turn even for
  custom Azure deployment names, which LiteLLM's price registry doesn't recognize by
  name -- `estimate_cost()` falls back to pricing a configurable public reference model
  (`cost_reference_model`, default `gpt-5.4-mini`) with the same underlying model
  family. Latency, step counts, and errors are always exact regardless.
- **Evals** are 10 golden agent questions (`tests/test_agent_evals.py`), not the full
  plan's ~40-question suite plus a separate 40-page extraction golden set and a
  DOM-mutation robustness test -- those need a larger annotated fixture set than a
  POC warrants; the 10 cover every scenario type (lookup, fan-out/comparison, aspect
  sentiment, catalog SQL, refusal, injection, honesty) and all pass.

## Tests

```bash
pytest                              # everything below
pytest tests/test_tool_cache.py -v  # fast, no credentials needed
pytest -m llm -v -s                 # agent eval suite only, needs LLM credentials
```

- `tests/fixtures/` -- recon fixtures for the scraper's extraction chain.
- `tests/test_tool_cache.py` -- unit tests for the tool-result cache (hit/miss, TTL
  expiry, invalidation, LangChain signature preservation). Pure logic, no DB/LLM.
- `tests/test_agent_evals.py` -- 10 golden-question agent evals run against the real,
  configured LLM (simple lookups, comparison/fan-out, aspect sentiment, catalog SQL,
  out-of-scope refusal, prompt injection, greeting, honesty on unknown products).
  Property-based assertions (ground-truth numbers present, right specialist(s)
  touched) rather than exact-string matching, since LLM phrasing varies run to run.
  Marked `@pytest.mark.llm` and auto-skips if no LLM credentials are configured.
