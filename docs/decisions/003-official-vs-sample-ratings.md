# ADR 003 — Official Bazaarvoice rating over computed-from-sample average

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2024-Q4 (initial design) |
| **Deciders** | Engineering |

---

## Context

The `get_review_stats` tool returns a product's star rating. There are two
plausible sources for this value:

1. **Official Bazaarvoice bulk statistics** — the aggregate `AverageOverallRating`
   returned by the BV Statistics API, computed by Bazaarvoice over the full
   population of reviews for a product.
2. **Computed average from stored sample** — `AVG(rating)` over the reviews
   we have scraped and stored in the `reviews` table.

## The sampling problem

We store a _stratified sample_ of reviews, not all of them:

| Bucket | Fraction |
|---|---|
| Most-recent | 50 % |
| Lowest-rating (1–2 ★) | 30 % |
| Most-helpful | 20 % |

This stratification is intentional — it ensures the agent can reason about
negative sentiment even for products with overwhelmingly positive reviews.
But it means the sample is **deliberately biased toward low ratings** relative
to the true population.

Computing `AVG(rating)` over this sample would systematically underestimate
the product's true rating. For a product with a true rating of 4.8 and 90 %
five-star reviews, our sample would heavily over-represent the 1–2 star
outliers, producing an apparent average of, say, 3.4.

## Decision

**Use the official Bazaarvoice bulk-stats rating** (`products.rating_value`
and `products.review_count`) as the authoritative figures for:

- `get_review_stats`
- Any answer that cites a numerical rating

The sample average (`AVG(reviews.rating WHERE product_id = ?)`) is **never
exposed to the agent or the user** as a rating figure.

The stored `reviews` sample remains valuable for:

- Aspect-sentiment extraction (we care about the _distribution_ of opinions,
  not a mean)
- Verbatim quote retrieval (`search_reviews`)
- Pros/cons synthesis

### Implementation

`products.rating_value` is populated during the census (Tier A) directly from
the BV Statistics API — before any reviews are scraped. This means the official
rating is available even for census-only products that have never been
deep-scraped.

The `get_review_stats` tool reads `products.rating_value` and
`products.review_count` from the `products` table, not from `reviews`.

### Labelling vendor-generated summaries

Bazaarvoice also provides an AI-generated review summary (`vendor_summaries`).
This is stored and returned by `get_vendor_summary`, but the agent's prompts
explicitly label it as a _secondary signal_ and instruct the Synthesizer not
to present it as a ground-truth sentiment score.

## Consequences

✅ Ratings reported to users are always the official, full-population figures.  
✅ No risk of systematic downward bias from stratified sampling.  
✅ Ratings are available for all 5,288 census products, not just the 200 seed.  
⚠️ Official BV rating could diverge from the stored sample's perceived quality
   if the product's review population has changed significantly since the last
   census run. Mitigated by re-running `petbarn-intel scrape census` periodically.
