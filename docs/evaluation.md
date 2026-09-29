# Evaluation

> **How correctness is verified — from fast unit checks to end-to-end
> golden-question agent evals.**

---

## Two-tier eval strategy

```
Tier 1: Fast (no LLM)          Tier 2: Agent evals (real LLM)
─────────────────────          ───────────────────────────────
pytest tests/                  pytest -m llm
• tool cache logic             • 10 golden questions
• scraper extraction           • real graph, real model
• no credentials needed        • ~1-3 minutes, needs .env
• runs in CI (<5 seconds)
```

Run everything:

```bash
pytest                           # tier 1 + tier 2 (skips tier 2 if no .env)
pytest tests/test_tool_cache.py  # tier 1 only, always fast
pytest -m llm -v -s              # tier 2 only, verbose
```

The `addopts = "-m 'not llm'"` in `pyproject.toml` means plain `pytest` skips
LLM-dependent tests by default — CI passes without credentials.

---

## Tier 1 — Unit tests (no LLM)

### `tests/test_tool_cache.py`

Tests the `agent/tools/cache.py` in-process cache layer:

| Test | What it checks |
|---|---|
| Cache hit | Second call with same args returns memoised result |
| Cache miss | Different args → fresh call |
| TTL expiry | Result expires after `ttl_seconds` |
| Cache invalidation | `clear_cache()` forces a fresh call on next access |
| LangChain signature | Decorated function passes LangChain's `@tool` introspection |

No database, no network, no LLM — deterministic and fast (<1 second).

### `tests/fixtures/`

Saved HTML pages from Petbarn product URLs used to test the scraper's
extraction chain offline. Tests assert that specific fields (`price`, `brand`,
`rating`, `review_count`) are extracted correctly from the saved HTML — so a
change to the extractor can be verified without a live network call.

---

## Tier 2 — Golden-question agent evals

### Overview

`tests/test_agent_evals.py` runs **10 parametrised golden cases** against the
real agent graph and the real configured LLM. Each case:

1. Calls `run_turn(question, session_id=..., history=...)`.
2. Runs a **property-based check** — assertions about which specialists were
   called, whether known ground-truth numbers appear in the answer, whether
   certain topics are addressed.
3. Never does exact-string matching (LLM phrasing varies run-to-run).

Ground-truth figures are pulled _live_ from the shipped DB rather than being
hardcoded, so the evals stay correct if the dataset is ever re-scraped.

### Golden cases

| Case ID | Question type | What is asserted |
|---|---|---|
| `simple_price_lookup` | Single-product catalog | Catalog specialist called; price range appears in answer |
| `simple_rating_lookup` | Single-product rating | Review specialist called; official BV rating appears |
| `price_quality_sentiment` | Aspect sentiment | Review specialist called; price and quality terms in answer |
| `pros_and_cons` | Pros / cons synthesis | Review specialist called; positive and negative content present |
| `comparison` | Cross-product comparison | `mode == "complex"`; both product names in answer |
| `catalog_wide_sql` | Catalog-wide SQL query | Catalog specialist called; non-empty answer |
| `out_of_scope` | Off-topic question | No specialists called; answer references Petbarn/products |
| `greeting` | Social/greeting | Non-empty answer; answer is concise (< 500 chars) |
| `prompt_injection_blocked` | Security / injection | Guard blocks turn before any specialist; no tool calls recorded |
| `unknown_product_honesty` | Honesty on missing data | Turn completes cleanly; no fabricated numbers |

### Skipping in CI

The entire file is marked:

```python
pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(not _has_credentials(), reason="No LLM credentials configured"),
]
```

`_has_credentials()` calls `get_settings().has_llm_credentials` — a
`Settings` property that checks whether at least one of
`AZURE_OPENAI_API_KEY` or `OPENAI_API_KEY` is set. If the `.env` file is
absent or empty, all 10 cases are automatically skipped.

The `pyproject.toml` `addopts`:

```toml
[tool.pytest.ini_options]
addopts = "-m 'not llm'"
markers = ["llm: ...", "release: ..."]
```

means `pytest` (no flags) is equivalent to `pytest -m 'not llm'` — CI
machines without credentials get a clean green run without needing
conditional logic in the CI YAML.

### Extending the eval suite

To add a new golden case:

1. Define a `_check_<name>` function that accepts `result: dict` and returns
   a `list[str]` of failure reasons (empty = pass).
2. Add a `Case(id, question, check)` entry to the `CASES` list.
3. Run `pytest -m llm -k <id> -v -s` to verify it locally.

The `result` dict shape returned by `run_turn`:

```python
{
    "answer": str,         # final synthesizer answer
    "mode": str,           # "simple" | "complex"
    "specialists": list,   # ["catalog"] | ["review", "catalog"] | []
    "evidence": dict,      # raw specialist outputs
    "turn_id": str,
}
```

---

## Evaluation coverage matrix

| Scenario | Tier 1 | Tier 2 |
|---|---|---|
| Tool result cache (hit, miss, TTL, invalidation) | ✅ | — |
| Scraper HTML extraction | ✅ (fixtures) | — |
| Simple catalog lookup | — | ✅ |
| Simple review lookup | — | ✅ |
| Aspect sentiment aggregation | — | ✅ |
| Multi-product comparison (fan-out) | — | ✅ |
| Catalog-wide SQL query | — | ✅ |
| Out-of-scope refusal | — | ✅ |
| Social greeting | — | ✅ |
| Prompt injection blocking | — | ✅ |
| Honesty on unknown products | — | ✅ |
| Scrape budget enforcement | — | *(manual test)* |
| Output guard / leak scan | — | *(unit test, not yet written)* |
