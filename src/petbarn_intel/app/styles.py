"""Shared CSS injected into every Streamlit page via inject_css().

Provides:
- Product cards (image + name + price)
- Follow-up suggestion pills
- Langfuse-style trace waterfall (used in Ops page)
- General UI polish (metric cards, chat bubbles, sidebar)
"""

from __future__ import annotations

import streamlit as st

_CSS = """
<style>

/* ── General layout ────────────────────────────────────────────────────── */
[data-testid="stAppViewContainer"] {
    background-color: #0F172A;
}
[data-testid="stSidebar"] {
    background-color: #1E293B;
    border-right: 1px solid rgba(79,70,229,0.2);
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
    font-size: 0.85rem !important;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #94A3B8 !important;
    margin-bottom: 4px !important;
}
[data-testid="metric-container"] {
    background: #1E293B;
    border: 1px solid rgba(79,70,229,0.25);
    border-radius: 10px;
    padding: 12px 16px;
}

/* ── Chat messages ─────────────────────────────────────────────────────── */
[data-testid="stChatMessage"] {
    border-radius: 12px;
    padding: 4px 0;
}

/* ── Product cards ─────────────────────────────────────────────────────── */
.product-cards-row {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin: 12px 0 8px 0;
}
.product-card {
    background: #1E293B;
    border: 1px solid rgba(79,70,229,0.3);
    border-radius: 12px;
    padding: 10px;
    width: 140px;
    text-align: center;
    transition: border-color 0.2s, transform 0.15s;
    flex-shrink: 0;
}
.product-card:hover {
    border-color: #4F46E5;
    transform: translateY(-2px);
}
.product-card img {
    width: 110px;
    height: 110px;
    object-fit: contain;
    border-radius: 8px;
    background: #0F172A;
}
.product-card .pc-name {
    font-size: 11px;
    color: #CBD5E1;
    margin-top: 6px;
    line-height: 1.3;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}
.product-card .pc-price {
    font-size: 12px;
    font-weight: 600;
    color: #818CF8;
    margin-top: 4px;
}
.product-card .pc-rating {
    font-size: 11px;
    color: #FBBF24;
    margin-top: 2px;
}

/* ── Follow-up pill buttons ────────────────────────────────────────────── */
.followup-section {
    margin: 8px 0 4px 0;
    padding: 6px 0;
}
.followup-label {
    font-size: 11px;
    color: #64748B;
    margin-bottom: 6px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}
div[data-testid="stHorizontalBlock"] button[kind="secondary"] {
    background: #1E293B !important;
    border: 1px solid rgba(79,70,229,0.4) !important;
    border-radius: 999px !important;
    color: #A5B4FC !important;
    font-size: 12px !important;
    padding: 4px 14px !important;
    transition: all 0.15s !important;
}
div[data-testid="stHorizontalBlock"] button[kind="secondary"]:hover {
    background: rgba(79,70,229,0.15) !important;
    border-color: #4F46E5 !important;
    color: #C7D2FE !important;
}

/* ── Trace waterfall (used in Ops page) ────────────────────────────────── */
.wf-container {
    font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', monospace;
    background: #0F172A;
    border: 1px solid rgba(79,70,229,0.2);
    border-radius: 10px;
    padding: 16px;
    margin: 8px 0;
}
.wf-row {
    display: grid;
    grid-template-columns: 150px 1fr 75px 110px 100px;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
    min-height: 24px;
}
.wf-node {
    font-size: 11px;
    color: #94A3B8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.wf-bar-bg {
    background: #1E293B;
    border-radius: 4px;
    height: 14px;
    position: relative;
    overflow: hidden;
}
.wf-bar {
    height: 100%;
    border-radius: 4px;
    min-width: 3px;
}
.wf-bar-llm   { background: linear-gradient(90deg, #4F46E5, #6366F1); }
.wf-bar-tool  { background: linear-gradient(90deg, #059669, #10B981); }
.wf-bar-error { background: linear-gradient(90deg, #DC2626, #EF4444); }
.wf-bar-node  { background: linear-gradient(90deg, #475569, #64748B); }
.wf-latency {
    font-size: 11px;
    color: #64748B;
    text-align: right;
}
.wf-tokens {
    font-size: 10px;
    color: #475569;
    text-align: center;
}
.wf-cost {
    font-size: 10px;
    color: #475569;
    text-align: right;
}
.wf-error-row .wf-node { color: #EF4444; }
.wf-error-row .wf-latency { color: #EF4444; }

/* ── Session history in sidebar ────────────────────────────────────────── */
.session-btn {
    font-size: 12px;
    color: #94A3B8;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 6px;
    margin-bottom: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.session-btn:hover {
    background: rgba(79,70,229,0.15);
    color: #C7D2FE;
}

/* ── Ops page ──────────────────────────────────────────────────────────── */
.ops-section-title {
    font-size: 13px;
    font-weight: 600;
    color: #CBD5E1;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin: 18px 0 8px 0;
    padding-bottom: 4px;
    border-bottom: 1px solid rgba(79,70,229,0.2);
}

</style>
"""


def inject_css() -> None:
    """Call once at the top of every Streamlit page (after set_page_config)."""
    st.markdown(_CSS, unsafe_allow_html=True)
