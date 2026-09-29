# ADR 002 — GraphQL attributes over HTML scraping for product data

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2024-Q4 (initial design) |
| **Deciders** | Engineering |

---

## Context

Petbarn's product pages are React SPAs. The product data visible in the
browser (price, brand, variants, availability, rating) is loaded by two
mechanisms:

1. **A GraphQL API** — the React app fetches structured JSON from an internal
   GraphQL endpoint during page load.
2. **Rendered HTML** — once the JS executes, the DOM contains the same data
   serialised into `data-*` attributes, JSON-LD structured data, and visible
   text nodes.

We need to extract product attributes for 5,000+ catalog entries and 200+
deep-scraped products reliably and efficiently.

## Options considered

| Option | Pro | Con |
|---|---|---|
| **GraphQL API calls** | Structured JSON, no HTML parsing, 10-100× faster than rendering, survives CSS/layout changes | API is undocumented; passKey/client config could change |
| **Plain HTML scraping (requests)** | Simpler code; no API reverse-engineering | Many fields only appear after JS executes; often returns skeleton HTML |
| **Headless browser rendering** | Sees the same page as a human browser | 5-10× slower; heavier infra; still requires CSS-selector maintenance |
| **LLM-only extraction** | No CSS selectors to maintain | Expensive; slow; hallucination risk on structured fields like prices |

## Decision

**Primary source: GraphQL API** (direct HTTP call, JSON response).  
**Fallback 1: HTML structured data** (JSON-LD, microdata, `data-*` attrs from cached raw HTML).  
**Fallback 2: LLM extraction** (`scraping/quality/llm_fallback.py`) for
fields that remain `None` after both structured sources.

The extraction chain tries each source in order and stops at the first
non-null result. The `source` column in the relevant table records which
path produced each value (`graphql | html | llm_fallback`).

### Why not just use the headless renderer everywhere?

The TieredFetcher (`scraping/fetch/tiered.py`) does support Playwright as a
fallback for pages that are completely JS-gated. But using it as the _primary_
path for 5,000+ census products would:

- Take hours instead of minutes for a full census run.
- Require a Playwright install in the deployment environment.
- Still require CSS-selector maintenance when the page layout changes.

GraphQL fields are stable — they are typed, named, and versioned by the
frontend team for their own internal use — making them more robust to
cosmetic page redesigns than HTML selectors.

### LLM fallback scope

The LLM fallback (`llm_fallback.py`) is intentionally narrow:

- It is only triggered when _both_ GraphQL and HTML return `None`.
- It receives a short HTML excerpt, not the full page.
- The extracted value is stored with `source = "llm_fallback"` so it can be
  filtered out in coverage reports and flagged for human review.
- It is **never** used for numeric fields like price or rating — only for
  text fields like product description where a small inaccuracy is tolerable.

## Consequences

✅ Census scrape completes in minutes (GraphQL pagination, ~5,300 products).  
✅ Resilient to CSS/layout changes on the product page.  
✅ LLM fallback provides a safety net for rare extraction failures.  
⚠️ GraphQL endpoint is undocumented; Petbarn could change its schema or auth.
   Mitigated by the HTML fallback path.  
⚠️ LLM fallback adds latency and cost; rate of use is monitored via
   `petbarn-intel report coverage` (which shows `source` distribution per field).
