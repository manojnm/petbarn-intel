"""Ops page — Langfuse-inspired tracing dashboard.

Reads live from SQLite: agent traces with token/cost/latency breakdowns,
scraper health, data-quality metrics, and open incidents.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from petbarn_intel.app._secrets_bridge import sync_secrets_to_env

sync_secrets_to_env()

from petbarn_intel.agent.tools.cache import cache_stats  # noqa: E402
from petbarn_intel.app.styles import inject_css  # noqa: E402
from petbarn_intel.config import get_settings  # noqa: E402
from petbarn_intel.logging import ensure_configured  # noqa: E402
from petbarn_intel.store import ops_repo, products_repo  # noqa: E402
from petbarn_intel.store.db import get_connection, init_db  # noqa: E402

ensure_configured()
st.set_page_config(page_title="PetInsight — Ops", page_icon="📡", layout="wide")
inject_css()


@st.cache_resource
def _bootstrap() -> bool:
    init_db(get_connection())
    return True


_bootstrap()
conn = get_connection()
settings = get_settings()

# ── Page header ────────────────────────────────────────────────────────────

st.title("📡 Ops & Tracing")
st.caption("Live observability: agent traces, token usage, cost, latency, and data-quality health — all from SQLite.")

# ── Time range filter ──────────────────────────────────────────────────────

_HOUR_OPTIONS = {"Last 1 h": 1, "Last 6 h": 6, "Last 24 h": 24, "Last 7 d": 168, "All time": None}

col_range, col_search, _ = st.columns([2, 3, 5])
with col_range:
    range_label = st.selectbox("Time range", list(_HOUR_OPTIONS.keys()), index=2)
hours_filter = _HOUR_OPTIONS[range_label]

with col_search:
    session_filter = st.text_input("Filter by session ID", placeholder="partial match…")

st.divider()

# ── Aggregate metrics banner ───────────────────────────────────────────────

metrics = ops_repo.aggregate_metrics(hours=hours_filter)

st.markdown('<div class="ops-section-title">Overview</div>', unsafe_allow_html=True)
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Turns", f"{metrics.get('total_turns', 0):,}")
m2.metric("LLM calls", f"{metrics.get('total_llm_calls', 0):,}")
m3.metric("Tokens in", f"{int(metrics.get('tokens_in', 0)):,}")
m4.metric("Tokens out", f"{int(metrics.get('tokens_out', 0)):,}")
m5.metric("Total cost", f"${metrics.get('total_cost', 0):.4f}")
m6.metric("Error rate", f"{metrics.get('error_rate', 0):.1f}%")

# ── Tool-result cache ──────────────────────────────────────────────────────

st.markdown('<div class="ops-section-title">Tool-result cache</div>', unsafe_allow_html=True)
stats = cache_stats()
ca, cb, cc, cd = st.columns(4)
ca.metric("Hit rate", f"{stats['hit_rate'] * 100:.0f}%")
cb.metric("Hits", f"{stats['hits']:,}")
cc.metric("Misses", f"{stats['misses']:,}")
cd.metric("Cached entries", f"{stats['size']} / {stats['maxsize']}")

st.divider()

# ── Trace list ─────────────────────────────────────────────────────────────

st.markdown('<div class="ops-section-title">Agent traces</div>', unsafe_allow_html=True)

turns = ops_repo.turn_cost_summary_filtered(
    hours=hours_filter,
    session_search=session_filter or None,
    limit=100,
)

if not turns:
    st.info("No agent turns recorded yet — ask the chatbot something first.")
    st.stop()

df_turns = pd.DataFrame([dict(t) for t in turns])
df_turns["session_short"] = df_turns["session_id"].str[:8] + "…"
df_turns["latency_s"] = (df_turns["latency_ms"] / 1000).round(2)
df_turns["cost_fmt"] = df_turns["cost_usd"].apply(lambda v: f"${v:.5f}")

display_df = df_turns[
    ["started_at", "session_short", "steps", "latency_s", "tokens_in", "tokens_out", "cost_fmt", "errors"]
].rename(
    columns={
        "started_at": "Time",
        "session_short": "Session",
        "steps": "Steps",
        "latency_s": "Latency (s)",
        "tokens_in": "Tokens ↑",
        "tokens_out": "Tokens ↓",
        "cost_fmt": "Cost",
        "errors": "Errors",
    }
)

st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Errors": st.column_config.NumberColumn(format="%d", help="Steps that raised an error"),
        "Cost": st.column_config.TextColumn(),
        "Latency (s)": st.column_config.NumberColumn(format="%.2f s"),
    },
)

st.divider()

# ── Turn detail — waterfall ────────────────────────────────────────────────

st.markdown('<div class="ops-section-title">Turn detail — waterfall</div>', unsafe_allow_html=True)

turn_options = {
    f"{t['session_id'][:8]}… · {t['started_at'][:19].replace('T', ' ')}": dict(t)
    for t in turns
}
selected_label = st.selectbox("Select turn", list(turn_options.keys()))

if selected_label:
    selected = turn_options[selected_label]
    sid = selected["session_id"]
    tid = selected["turn_id"]

    # Header summary
    hc1, hc2, hc3, hc4 = st.columns(4)
    hc1.metric("Steps", selected["steps"])
    hc2.metric("Latency", f"{selected['latency_ms'] / 1000:.2f}s")
    hc3.metric("Tokens", f"{int(selected.get('tokens_in', 0) + selected.get('tokens_out', 0)):,}")
    hc4.metric("Cost", f"${selected['cost_usd']:.5f}")

    # Fetch per-step traces
    step_rows = conn.execute(
        "SELECT * FROM traces WHERE session_id = ? AND turn_id = ? ORDER BY created_at",
        (sid, tid),
    ).fetchall()

    if step_rows:
        steps = [dict(r) for r in step_rows]
        max_lat = max((s.get("latency_ms") or 0) for s in steps) or 1

        # ── Waterfall HTML ────────────────────────────────────────────────
        _KIND_CLASS = {
            "llm_call": "wf-bar-llm",
            "tool_call": "wf-bar-tool",
            "error": "wf-bar-error",
        }
        _KIND_LABEL = {
            "llm_call": "LLM",
            "tool_call": "TOOL",
            "error": "ERR",
            "node": "NODE",
        }

        rows_html: list[str] = []
        for s in steps:
            kind = s.get("kind", "node")
            lat = s.get("latency_ms") or 0
            bar_pct = max(2, round(lat / max_lat * 100))
            bar_cls = _KIND_CLASS.get(kind, "wf-bar-node")
            err_cls = "wf-error-row" if s.get("error") else ""
            node_name = f"{s.get('node', '?')}"
            step_name = s.get("name", "")
            label = f"{node_name} · {step_name}" if step_name and step_name != node_name else node_name
            lat_str = f"{lat:.0f}ms" if lat else "—"
            tok_in = s.get("tokens_in") or 0
            tok_out = s.get("tokens_out") or 0
            tok_str = f"{tok_in} / {tok_out}" if (tok_in or tok_out) else "—"
            cost_str = f"${s['cost_usd']:.5f}" if s.get("cost_usd") else "—"
            kind_str = _KIND_LABEL.get(kind, kind.upper()[:4])

            rows_html.append(
                f'<div class="wf-row {err_cls}">'
                f'  <div class="wf-node" title="{label}">{label}</div>'
                f'  <div class="wf-bar-bg"><div class="wf-bar {bar_cls}" style="width:{bar_pct}%"></div></div>'
                f'  <div class="wf-latency">{lat_str}</div>'
                f'  <div class="wf-tokens">{kind_str} · {tok_str}</div>'
                f'  <div class="wf-cost">{cost_str}</div>'
                f'</div>'
            )

        waterfall_html = (
            '<div class="wf-container">'
            + "".join(rows_html)
            + "</div>"
        )
        st.markdown(waterfall_html, unsafe_allow_html=True)

        # Legend
        st.markdown(
            '<div style="display:flex;gap:20px;margin-top:6px;font-size:11px;color:#475569;">'
            '<span><span style="display:inline-block;width:12px;height:10px;background:#4F46E5;border-radius:2px;margin-right:4px;"></span>LLM call</span>'
            '<span><span style="display:inline-block;width:12px;height:10px;background:#10B981;border-radius:2px;margin-right:4px;"></span>Tool call</span>'
            '<span><span style="display:inline-block;width:12px;height:10px;background:#EF4444;border-radius:2px;margin-right:4px;"></span>Error</span>'
            '<span><span style="display:inline-block;width:12px;height:10px;background:#64748B;border-radius:2px;margin-right:4px;"></span>Node</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        # ── Step detail table ─────────────────────────────────────────────
        with st.expander("Step details (args & results)", expanded=False):
            for s in steps:
                kind = s.get("kind", "?")
                node = s.get("node", "?")
                name = s.get("name", "?")
                lat = f"{s['latency_ms']:.0f}ms" if s.get("latency_ms") else "—"
                err = s.get("error")
                header = f"**[{node}]** `{kind}:{name}` — {lat}"
                if err:
                    header += f" — ⚠️ `{err}`"
                tok_info = ""
                if s.get("tokens_in") or s.get("tokens_out"):
                    tok_info = f"  \n**Tokens:** {s.get('tokens_in', 0)} in / {s.get('tokens_out', 0)} out"
                cost_info = f"  \n**Cost:** ${s['cost_usd']:.5f}" if s.get("cost_usd") else ""
                st.markdown(header + tok_info + cost_info)
                if s.get("args_summary"):
                    st.code(s["args_summary"], language="text")
                if s.get("result_summary"):
                    with st.container():
                        st.caption("result →")
                        st.code(s["result_summary"][:800], language="text")
                st.divider()

st.divider()

# ── Scraper / data quality (collapsible) ───────────────────────────────────

with st.expander("📦 Catalog & scrape runs", expanded=False):
    counts = products_repo.count_products()
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Total products", f"{sum(counts.values()):,}")
    sc2.metric("Seed (deep-scraped)", f"{counts.get('seed', 0):,}")
    sc3.metric("On-demand scraped", f"{counts.get('on_demand', 0):,}")
    sc4.metric("Reviews stored", f"{conn.execute('SELECT COUNT(*) as n FROM reviews').fetchone()['n']:,}")

    st.subheader("Recent scrape runs")
    runs = ops_repo.list_runs(limit=20)
    if runs:
        rdf = pd.DataFrame([dict(r) for r in runs])[
            ["run_id", "run_type", "status", "started_at", "finished_at",
             "pages_fetched", "products_touched", "reviews_fetched", "errors"]
        ]
        st.dataframe(rdf, use_container_width=True, hide_index=True)
    else:
        st.info("No scrape runs recorded yet.")

with st.expander("🔬 Data quality — field completeness", expanded=False):
    rows = ops_repo.field_completeness()
    if rows:
        qdf = pd.DataFrame([dict(r) for r in rows])
        qdf["completeness_%"] = (qdf["completeness"] * 100).round(1)
        st.dataframe(
            qdf[["field_name", "completeness_%", "fallback_count", "attempts"]],
            use_container_width=True,
            hide_index=True,
        )
        st.bar_chart(qdf.set_index("field_name")["completeness_%"])
    else:
        st.info("No field-level events recorded yet.")

with st.expander("🚨 Open incidents", expanded=False):
    open_incidents = ops_repo.open_incidents()
    if open_incidents:
        idf = pd.DataFrame([dict(i) for i in open_incidents])[
            ["opened_at", "kind", "severity", "product_id", "field_name", "detail"]
        ]
        sev_filter = st.multiselect(
            "Severity",
            options=sorted(idf["severity"].unique()),
            default=list(idf["severity"].unique()),
            key="sev_filter",
        )
        st.dataframe(idf[idf["severity"].isin(sev_filter)], use_container_width=True, hide_index=True)
    else:
        st.success("No open incidents.")
