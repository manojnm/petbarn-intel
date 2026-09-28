"""Catalog browse page -- shows the full scraped product inventory.

Lets a reviewer verify the dataset claims without running CLI commands or
opening SQLite. Uses the same repos as the chat tools; no live scraping.

Navigate to it via the Streamlit page selector (sidebar).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from petbarn_intel.app._secrets_bridge import sync_secrets_to_env

sync_secrets_to_env()

from petbarn_intel.app.charts import render_aspect_breakdown, render_rating_distribution  # noqa: E402
from petbarn_intel.app.styles import inject_css  # noqa: E402
from petbarn_intel.config import get_settings  # noqa: E402
from petbarn_intel.logging import ensure_configured  # noqa: E402
from petbarn_intel.store import products_repo, reviews_repo  # noqa: E402
from petbarn_intel.store.db import get_connection, init_db  # noqa: E402

ensure_configured()
st.set_page_config(page_title="PetInsight — Catalog", page_icon="📦", layout="wide")
inject_css()


@st.cache_resource
def _bootstrap() -> bool:
    init_db(get_connection())
    return True


_bootstrap()
conn = get_connection()
settings = get_settings()

# ── Page header ────────────────────────────────────────────────────────────

st.title("📦 Catalog")
st.caption(
    "Browse all scraped Petbarn products and their reviews. "
    "**Tier** shows how much data we have: *census* = name+price only; "
    "*seed* = full details + reviews; *on_demand* = deep-scraped mid-chat."
)

# ── Dataset summary banner ─────────────────────────────────────────────────

counts = products_repo.count_products(conn=conn)
total = sum(counts.values())
n_reviews = conn.execute("SELECT COUNT(*) as n FROM reviews").fetchone()["n"]
n_aspects = conn.execute("SELECT COUNT(*) as n FROM review_aspects").fetchone()["n"]

b1, b2, b3, b4, b5 = st.columns(5)
b1.metric("Total products", f"{total:,}")
b2.metric("Census only", f"{counts.get('census', 0):,}", help="Name + price, no reviews")
b3.metric("Deep-scraped (seed)", f"{counts.get('seed', 0):,}", help="Full detail + reviews")
b4.metric("On-demand scraped", f"{counts.get('on_demand', 0):,}", help="Scraped live mid-chat")
b5.metric("Reviews stored", f"{n_reviews:,}")
st.caption(f"Aspect-sentiment rows: {n_aspects:,}  ·  Hybrid search index: FTS5 + sqlite-vec")

st.divider()

# ── Filters ────────────────────────────────────────────────────────────────

col_q, col_tier, col_brand = st.columns([3, 2, 2])

with col_q:
    query = st.text_input("🔍 Search by name, brand, or product ID", placeholder="e.g. Royal Canin, grain-free…")

with col_tier:
    tier_options = ["All tiers", "seed", "on_demand", "census"]
    selected_tier = st.selectbox("Tier", tier_options)

with col_brand:
    # Quick brand list from a lightweight query
    all_brands = conn.execute(
        "SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL ORDER BY brand LIMIT 200"
    ).fetchall()
    brand_list = ["All brands"] + [r["brand"] for r in all_brands]
    selected_brand = st.selectbox("Brand", brand_list)

# ── Load and filter products ───────────────────────────────────────────────

tier_arg = None if selected_tier == "All tiers" else selected_tier
all_rows = products_repo.list_products(tier=tier_arg, conn=conn)

# Apply text and brand filters
needle = query.strip().lower()
filtered = []
for r in all_rows:
    r = dict(r)
    if selected_brand != "All brands" and r.get("brand") != selected_brand:
        continue
    if needle and not any(
        needle in (r.get(col) or "").lower()
        for col in ("product_id", "name", "brand", "description")
    ):
        continue
    filtered.append(r)

st.caption(f"Showing {len(filtered):,} of {len(all_rows):,} products")

# ── Products table ─────────────────────────────────────────────────────────

if not filtered:
    st.info("No products match the current filters.")
    st.stop()

table_rows = []
for r in filtered[:2000]:  # cap at 2k for rendering performance
    price_min = r.get("price_min")
    price_max = r.get("price_max")
    if price_min and price_max and float(price_min) != float(price_max):
        price_str = f"A${float(price_min):.2f}–${float(price_max):.2f}"
    elif price_min:
        price_str = f"A${float(price_min):.2f}"
    else:
        price_str = "—"
    table_rows.append({
        "Name": (r.get("name") or "")[:60],
        "Brand": r.get("brand") or "—",
        "Product ID": r.get("product_id") or "—",
        "Price": price_str,
        "Rating ★": r.get("rating_value"),
        "Reviews": r.get("review_count"),
        "Tier": r.get("tier") or "—",
        "Scraped": (r.get("scraped_at") or "")[:10] or "—",
    })

df = pd.DataFrame(table_rows)
st.dataframe(
    df,
    hide_index=True,
    use_container_width=True,
    column_config={
        "Rating ★": st.column_config.NumberColumn(format="%.1f"),
        "Reviews": st.column_config.NumberColumn(format="%d"),
    },
)

st.divider()

# ── Per-product drill-down ─────────────────────────────────────────────────

st.markdown("### Product drill-down")
product_options = {
    f"{r.get('name', '')[:55]} ({r.get('tier', '—')})": r.get("product_id")
    for r in filtered[:500]
}
selected_label = st.selectbox("Select a product to inspect", list(product_options.keys()))
selected_pid = product_options.get(selected_label)

if selected_pid:
    product = products_repo.get_product(selected_pid, conn=conn)

    if product:
        c1, c2, c3 = st.columns(3)
        c1.metric("Brand", product.brand or "—")
        price_min = product.price_min
        price_max = product.price_max
        if price_min and price_max and price_min != price_max:
            c2.metric("Price", f"A${price_min:.2f}–${price_max:.2f}")
        elif price_min:
            c2.metric("Price", f"A${price_min:.2f}")
        else:
            c2.metric("Price", "—")

        official_rating = product.rating_value
        official_count = product.review_count
        if official_rating:
            c3.metric(
                "Rating (official)",
                f"{official_rating:.1f} ★",
                help=f"Complete Bazaarvoice stats — {official_count:,} total reviews" if official_count else None,
            )

        if product.description:
            with st.expander("Description", expanded=False):
                st.markdown(product.description[:1000])

    # ── Reviews and charts ─────────────────────────────────────────────

    tab_reviews, tab_stats, tab_aspects = st.tabs(["Reviews", "Rating stats", "Aspect sentiment"])

    with tab_reviews:
        reviews = reviews_repo.get_reviews(selected_pid, limit=20, conn=conn)
        if not reviews:
            st.info("No stored review text for this product. Run a deep scrape to fetch reviews.")
        else:
            st.caption(f"Showing {len(reviews)} stored review excerpts (stratified sample).")
            for rv in reviews:
                rating = rv.get("rating")
                stars = "★" * int(rating) + "☆" * (5 - int(rating)) if rating else "—"
                date = (rv.get("submitted_at") or "")[:10]
                title = rv.get("title") or ""
                text = rv.get("text") or ""
                label = rv.get("vader_label") or ""
                st.markdown(
                    f"**{stars}** · {date}"
                    + (f" · *{label}*" if label else "")
                    + (f"\n\n**{title}**\n\n{text}" if title else f"\n\n{text}")
                )
                st.divider()

    with tab_stats:
        stats = reviews_repo.review_stats(selected_pid, conn=conn)
        if product:
            stats["official_rating_value"] = product.rating_value
            stats["official_review_count"] = product.review_count
        if stats.get("total_reviews", 0) > 0:
            render_rating_distribution(stats)
            if stats.get("distribution"):
                dist_data = {
                    f"{s}★": int(stats["distribution"].get(str(s), 0))
                    for s in range(1, 6)
                }
                st.dataframe(
                    pd.DataFrame({"Stars": list(dist_data.keys()), "Count": list(dist_data.values())}),
                    hide_index=True,
                )
        else:
            st.info("No rating statistics available for this product yet.")

    with tab_aspects:
        aspect_rows = reviews_repo.aspect_summary(selected_pid, conn=conn)
        if not aspect_rows:
            st.info("No aspect-sentiment data yet. Run enrichment to extract aspects from reviews.")
        else:
            by_aspect: dict[str, dict] = {}
            for r in aspect_rows:
                r = dict(r)
                entry = by_aspect.setdefault(
                    r["aspect"], {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
                )
                entry[r["sentiment"]] = r["n"]
            render_aspect_breakdown({"aspects": by_aspect})
            # Show summary table
            table = [
                {
                    "Aspect": a.replace("_", " ").title(),
                    "Positive": c.get("positive", 0),
                    "Neutral": c.get("neutral", 0),
                    "Negative": c.get("negative", 0),
                    "Mixed": c.get("mixed", 0),
                    "Total": sum(c.get(k, 0) for k in ("positive", "neutral", "negative", "mixed")),
                }
                for a, c in by_aspect.items()
            ]
            st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)
