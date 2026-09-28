"""Inline chat charts and source-citation rendering.

Called from Home.py after the assistant answer bubble, gated on user
intent (question mentions charts/compare/ratings/etc.) or an explicit
"📊 Show charts / 📍 Show sources" button.
"""

from __future__ import annotations

import json
import re
from typing import Any

import streamlit as st

# ── Intent detection ───────────────────────────────────────────────────────

_CHART_INTENT = re.compile(
    r"\b(chart|graph|plot|visuali[sz]|rating.?distribution|show.?table|"
    r"compare|comparison|vs\.?|versus|breakdown|aspect|sentiment|"
    r"price.?quality|pros.?and.?cons)\b",
    re.I,
)
_SOURCE_INTENT = re.compile(
    r"\b(source|citation|where.?did|snapshot|when.?scrape|how.?fresh|"
    r"how.?old|data.?from|last.?updated)\b",
    re.I,
)


def wants_charts(question: str) -> bool:
    return bool(_CHART_INTENT.search(question))


def wants_sources(question: str) -> bool:
    return bool(_SOURCE_INTENT.search(question))


# ── Evidence parsing ───────────────────────────────────────────────────────

def _tool_results(evidence: list[dict], tool_names: set[str]) -> list[dict]:
    """Extract all results from tool calls matching `tool_names`."""
    out: list[dict] = []
    for item in evidence:
        for tc in item.get("tool_calls", []):
            if tc.get("name") not in tool_names:
                continue
            raw = tc.get("result", "")
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    out.append(parsed)
                elif isinstance(parsed, list):
                    out.extend(p for p in parsed if isinstance(p, dict))
            except Exception:  # noqa: BLE001
                pass
    return out


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── Chart renderers ────────────────────────────────────────────────────────

def render_rating_distribution(stats: dict) -> None:
    """Bar chart of 1–5 star distribution from get_review_stats output."""
    dist = stats.get("distribution") or {}
    if not dist:
        return
    data = {f"{star}★": int(dist.get(str(star), 0) or dist.get(star, 0) or 0)
            for star in range(5, 0, -1)}
    if sum(data.values()) == 0:
        return
    official = _num(stats.get("official_rating_value"))
    official_n = stats.get("official_review_count")
    label = "Rating distribution"
    if official:
        label += f"  ·  Official avg: {official:.1f}★"
        if official_n:
            label += f" ({official_n:,} reviews)"
    st.caption(label)
    st.bar_chart(data, color="#4F46E5")


def render_aspect_breakdown(aspects_data: dict) -> None:
    """Stacked-like bar chart of aspect sentiment from analyze_aspects output."""
    aspects = aspects_data.get("aspects") or {}
    if not aspects:
        return
    rows: dict[str, dict[str, int]] = {}
    for aspect, counts in aspects.items():
        label = aspect.replace("_", " ").title()
        rows[label] = {
            "positive": counts.get("positive", 0),
            "neutral": counts.get("neutral", 0),
            "negative": counts.get("negative", 0),
        }
    if not rows:
        return
    import pandas as pd
    df = pd.DataFrame(rows).T
    st.caption("Aspect sentiment breakdown")
    st.bar_chart(df[["positive", "neutral", "negative"]])


def render_comparison_table(products: list[dict]) -> None:
    """Side-by-side dataframe + rating bar chart from compare_products output."""
    if not products:
        return
    import pandas as pd
    rows = []
    for p in products:
        price_min = _num(p.get("price_min"))
        price_max = _num(p.get("price_max"))
        if price_min and price_max and price_min != price_max:
            price_str = f"A${price_min:.2f}–${price_max:.2f}"
        elif price_min:
            price_str = f"A${price_min:.2f}"
        else:
            price_str = "—"
        rows.append({
            "Product": (p.get("name") or "")[:50],
            "Brand": p.get("brand") or "—",
            "Price": price_str,
            "Rating": _num(p.get("rating_value")),
            "Reviews": p.get("review_count"),
            "Tier": p.get("tier") or "—",
        })
    df = pd.DataFrame(rows)
    st.caption("Product comparison")
    st.dataframe(df, hide_index=True, use_container_width=True)
    # Rating bar chart
    ratings = {r["Product"]: r["Rating"] or 0 for r in rows if r.get("Rating")}
    if ratings:
        st.bar_chart(ratings, color="#10B981")


# ── Main entry points used by Home.py ─────────────────────────────────────

def render_charts_from_evidence(evidence: list[dict]) -> None:
    """Render all applicable charts from specialist evidence."""
    # Rating distribution from get_review_stats
    for stats in _tool_results(evidence, {"get_review_stats"}):
        if stats.get("distribution"):
            render_rating_distribution(stats)

    # Aspect breakdown from analyze_aspects
    for aspects_data in _tool_results(evidence, {"analyze_aspects"}):
        if aspects_data.get("aspects"):
            render_aspect_breakdown(aspects_data)

    # Comparison table from compare_products
    for comp in _tool_results(evidence, {"compare_products"}):
        products = comp.get("products") or []
        if products:
            render_comparison_table(products)


def render_source_line(evidence: list[dict]) -> None:
    """Render a shopper-facing source/citation block below the answer."""
    names: list[str] = []
    skus: list[str] = []
    scraped_at: str | None = None
    review_counts: list[int] = []

    for item in evidence:
        for tc in item.get("tool_calls", []):
            raw = tc.get("result", "")
            if not raw:
                continue
            try:
                res = json.loads(raw)
                if not isinstance(res, dict):
                    continue
                if res.get("name"):
                    name = str(res["name"])[:60]
                    if name not in names:
                        names.append(name)
                if res.get("sku") and res["sku"] not in skus:
                    skus.append(str(res["sku"]))
                if res.get("scraped_at") and not scraped_at:
                    scraped_at = str(res["scraped_at"])[:10]
                if res.get("official_review_count") is not None:
                    review_counts.append(int(res["official_review_count"]))
                # pull from search/compare product lists
                for p in (res.get("products") or []):
                    if isinstance(p, dict):
                        pname = (p.get("name") or "")[:60]
                        if pname and pname not in names:
                            names.append(pname)
            except Exception:  # noqa: BLE001
                pass

    lines: list[str] = ["📍 **Source:** Petbarn snapshot (petbarn.com.au)"]
    if names:
        lines.append("**Products:** " + ", ".join(names[:4]))
    if skus:
        lines.append("**SKUs:** " + ", ".join(skus[:4]))
    if scraped_at:
        lines.append(f"**Catalog snapshot date:** {scraped_at}")
    if review_counts:
        total = sum(review_counts)
        lines.append(f"**Review data:** {total:,} reviews across queried products")
    lines.append("*Prices and availability reflect the latest crawled snapshot, not the live site.*")
    st.info("\n\n".join(lines))
