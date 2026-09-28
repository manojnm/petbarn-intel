"""PetInsight — Streamlit chat page.

Run with: `streamlit run src/petbarn_intel/app/Home.py`
"""

from __future__ import annotations

import asyncio
import json
import uuid

import streamlit as st

from petbarn_intel.app._secrets_bridge import sync_secrets_to_env

sync_secrets_to_env()

from petbarn_intel.agent.graph import run_turn  # noqa: E402
from petbarn_intel.agent.llm import chat  # noqa: E402
from petbarn_intel.app.charts import (  # noqa: E402
    render_charts_from_evidence,
    render_source_line,
    wants_charts,
    wants_sources,
)
from petbarn_intel.app.styles import inject_css  # noqa: E402
from petbarn_intel.config import get_settings  # noqa: E402
from petbarn_intel.logging import ensure_configured  # noqa: E402
from petbarn_intel.store import ops_repo, products_repo  # noqa: E402
from petbarn_intel.store.db import get_connection, init_db  # noqa: E402

ensure_configured()

st.set_page_config(
    page_title="PetInsight",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_css()


# ── Bootstrap ──────────────────────────────────────────────────────────────

@st.cache_resource
def _bootstrap() -> bool:
    init_db(get_connection())
    return True


_bootstrap()
settings = get_settings()

# ── Session state init ─────────────────────────────────────────────────────

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "followups" not in st.session_state:
    st.session_state.followups = []
if "session_persisted" not in st.session_state:
    st.session_state.session_persisted = False


# ── Helpers ────────────────────────────────────────────────────────────────

def _history_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for m in st.session_state.messages:
        if m["role"] == "user":
            pending_user = m["content"]
        elif m["role"] == "assistant" and pending_user is not None:
            pairs.append((pending_user, m["content"]))
            pending_user = None
    return pairs


def _extract_product_ids(evidence: list[dict]) -> list[str]:
    """Pull product_ids from evidence tool-call args and results (max 4)."""
    ids: list[str] = []
    seen: set[str] = set()

    def _add(pid: str) -> None:
        if pid and pid not in seen:
            seen.add(pid)
            ids.append(pid)

    for item in evidence:
        for tc in item.get("tool_calls", []):
            # Parse args
            try:
                args = json.loads(tc.get("args", "{}"))
                if isinstance(args, dict):
                    if "product_id" in args:
                        _add(str(args["product_id"]))
                    for pid in args.get("product_ids", []):
                        _add(str(pid))
            except Exception:  # noqa: BLE001
                pass
            # Parse result for search_products cards
            if tc.get("name") in ("search_products", "get_product_details", "compare_products"):
                try:
                    res = json.loads(tc.get("result", "null"))
                    if isinstance(res, list):
                        for card in res:
                            if isinstance(card, dict) and "product_id" in card:
                                _add(str(card["product_id"]))
                    elif isinstance(res, dict) and "product_id" in res:
                        _add(str(res["product_id"]))
                except Exception:  # noqa: BLE001
                    pass
    return ids[:4]


def _render_product_cards(product_ids: list[str]) -> None:
    if not product_ids:
        return
    cards = products_repo.get_product_display_cards(product_ids)
    if not cards:
        return
    html_parts = ['<div class="product-cards-row">']
    for c in cards:
        name = (c["name"] or "")[:60]
        price = f"A${c['price_min']:.2f}" if c.get("price_min") else "—"
        rating = f"{'★' * round(c['rating_value'] or 0)}{'☆' * (5 - round(c['rating_value'] or 0))}" if c.get("rating_value") else ""
        link = c.get("url") or "#"
        if c.get("image_url"):
            img_tag = (
                f'<a href="{link}" target="_blank">'
                f'<img src="{c["image_url"]}" alt="{name}" '
                f'onerror="this.style.display=\'none\'">'
                f'</a>'
            )
        else:
            img_tag = '<div style="width:110px;height:110px;background:#0F172A;border-radius:8px;display:inline-block;"></div>'
        html_parts.append(
            f'<div class="product-card">'
            f'{img_tag}'
            f'<div class="pc-name"><a href="{link}" target="_blank" style="color:inherit;text-decoration:none;">{name}</a></div>'
            f'<div class="pc-price">{price}</div>'
            f'<div class="pc-rating">{rating}</div>'
            f'</div>'
        )
    html_parts.append("</div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)


def _generate_followups(answer: str, specialists: list[str], evidence: list[dict]) -> list[str]:
    """Call the mini model to generate 3 short follow-up question suggestions."""
    if not settings.has_llm_credentials:
        return []
    tool_names = []
    for item in evidence:
        for tc in item.get("tool_calls", []):
            if tc.get("name") and tc["name"] not in tool_names:
                tool_names.append(tc["name"])
    ctx = f"Specialists: {', '.join(specialists) or 'none'}. Tools used: {', '.join(tool_names) or 'none'}."
    prompt = (
        f"You are a shopping assistant. Based on the answer below, suggest 3 concise follow-up "
        f"questions a pet owner might ask next. Each question must be under 12 words. "
        f"Return exactly 3 lines, no numbering, no quotes, no punctuation at end.\n\n"
        f"{ctx}\n\nAnswer: {answer[:600]}"
    )
    try:
        text = chat([{"role": "user", "content": prompt}], role="mini")
        lines = [ln.strip().rstrip("?") + "?" for ln in text.strip().splitlines() if ln.strip()]
        return lines[:3]
    except Exception:  # noqa: BLE001
        return []


def _render_followup_pills(followups: list[str]) -> None:
    if not followups:
        return
    st.markdown('<div class="followup-label">Suggested follow-ups</div>', unsafe_allow_html=True)
    cols = st.columns(len(followups))
    for i, fq in enumerate(followups):
        if cols[i].button(fq, key=f"followup_{i}_{hash(fq)}", use_container_width=True):
            st.session_state.pending_question = fq
            st.rerun()


def _render_charts_and_sources(
    question: str,
    evidence: list[dict],
    key_suffix: str,
) -> None:
    """Render charts and/or source citation, gated on intent or button click."""
    show_charts = wants_charts(question)
    show_sources = wants_sources(question)

    col1, col2 = st.columns([1, 1])
    if not show_charts:
        with col1:
            if st.button("📊 Show charts", key=f"charts_{key_suffix}", use_container_width=True):
                show_charts = True
    if not show_sources:
        with col2:
            if st.button("📍 Show sources", key=f"sources_{key_suffix}", use_container_width=True):
                show_sources = True

    if show_charts:
        render_charts_from_evidence(evidence)
    if show_sources:
        render_source_line(evidence)


def _render_trace(mode: str, specialists: list[str], evidence: list[dict], turn_id: str) -> None:
    badge = "⚡ fast" if mode == "fast" else "🔬 complex"
    spec_str = ", ".join(specialists) or "none"
    label = f"Agent trace — {badge} · specialists: {spec_str}"
    with st.expander(label, expanded=False):
        for e in evidence:
            st.markdown(f"**{e['specialist'].title()} specialist** — *{e['subtask']}*")
            tool_names = ", ".join(t["name"] for t in e.get("tool_calls", [])) or "no tool calls"
            st.caption(f"Tools: {tool_names}")
            st.markdown(e["answer"])
            st.divider()

        traces = ops_repo.get_traces_for_turn(st.session_state.session_id, turn_id)
        if traces:
            st.caption("Step log (node · kind · name · latency · tokens · cost):")
            for t in traces:
                err = f"  ⚠️ {t['error']}" if t.get("error") else ""
                lat = f"{t['latency_ms']:.0f}ms" if t.get("latency_ms") else "—"
                tok = (
                    f"  {t['tokens_in']}→{t['tokens_out']} tok"
                    if (t.get("tokens_in") or t.get("tokens_out"))
                    else ""
                )
                cost = f"  ${t['cost_usd']:.5f}" if t.get("cost_usd") else ""
                st.text(f"[{t['node']}] {t['kind']}:{t['name']}  ({lat}){tok}{cost}{err}")


def _persist_turn(user_q: str, result: dict) -> None:
    """Write session + both messages to DB (idempotent on duplicate calls)."""
    sid = st.session_state.session_id
    title = user_q[:45] + ("…" if len(user_q) > 45 else "")
    if not st.session_state.session_persisted:
        ops_repo.save_session(sid, title=title)
        st.session_state.session_persisted = True
    ops_repo.save_message(sid, "user", user_q)
    ops_repo.save_message(
        sid,
        "assistant",
        result["answer"],
        turn_id=result["turn_id"],
        metadata={
            "mode": result["mode"],
            "specialists": result["specialists"],
            "evidence": result["evidence"],
            "product_ids": _extract_product_ids(result["evidence"]),
        },
    )


# ── Sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🐾 PetInsight")
    st.caption("Petbarn product & review intelligence")

    st.divider()

    # Dataset stats
    st.markdown("**Dataset**")
    counts = products_repo.count_products()
    total_products = sum(counts.values())
    c1, c2 = st.columns(2)
    c1.metric("Products", f"{total_products:,}")
    c2.metric("With reviews", f"{counts.get('seed', 0) + counts.get('on_demand', 0):,}")
    conn = get_connection()
    n_reviews = conn.execute("SELECT COUNT(*) as n FROM reviews").fetchone()["n"]
    st.metric("Customer reviews", f"{n_reviews:,}")

    st.divider()

    # Conversation history
    st.markdown("**Recent conversations**")
    past_sessions = ops_repo.list_sessions(limit=15)
    if past_sessions:
        for sess in past_sessions:
            label = sess["title"] or f"Session {sess['session_id'][:8]}"
            is_active = sess["session_id"] == st.session_state.session_id
            btn_label = f"{'▶ ' if is_active else ''}{label}"
            if st.button(btn_label, key=f"sess_{sess['session_id']}", use_container_width=True):
                if sess["session_id"] != st.session_state.session_id:
                    st.session_state.session_id = sess["session_id"]
                    st.session_state.messages = ops_repo.load_messages(sess["session_id"])
                    st.session_state.followups = []
                    st.session_state.session_persisted = True
                    st.rerun()
    else:
        st.caption("No past conversations yet.")

    st.divider()

    # Sample questions
    st.markdown("**Try asking**")
    st.markdown(
        "- *Price and rating of Black Hawk Chicken & Rice?*\n"
        "- *What do people say about Royal Canin?*\n"
        "- *Compare Hill's vs Advance dry dog food.*\n"
        "- *Grain-free cat food under \$50?*\n"
        "- *Pros and cons of [product] from reviews?*"
    )

    st.divider()

    if not settings.has_llm_credentials:
        st.error("No LLM credentials — set AZURE_OPENAI_* or OPENAI_API_KEY.")
    show_trace = st.toggle("Show agent trace", value=True)
    if st.button("＋ New conversation", use_container_width=True):
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.messages = []
        st.session_state.followups = []
        st.session_state.session_persisted = False
        st.rerun()
    st.caption(f"session `{st.session_state.session_id[:8]}`")


# ── Page header ────────────────────────────────────────────────────────────

st.title("🐾 PetInsight")
st.caption("Your AI assistant for Petbarn product specs, pricing, and customer reviews.")

# ── Message replay ─────────────────────────────────────────────────────────

for idx, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m["role"] == "assistant":
            _render_product_cards(m.get("product_ids", []))
            _render_charts_and_sources(
                m.get("question", ""),
                m.get("evidence", []),
                key_suffix=str(idx),
            )
            if show_trace and m.get("turn_id"):
                _render_trace(
                    m.get("mode", "fast"),
                    m.get("specialists", []),
                    m.get("evidence", []),
                    m["turn_id"],
                )

# Follow-up pills for the last assistant message
if st.session_state.messages and st.session_state.messages[-1]["role"] == "assistant":
    _render_followup_pills(st.session_state.followups)

# ── Chat input ─────────────────────────────────────────────────────────────

typed_question = st.chat_input("Ask about a Petbarn product…")
pending = st.session_state.pop("pending_question", None)
question = typed_question or pending

if question:
    # Render user bubble
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    history = _history_pairs()

    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            try:
                result = asyncio.run(
                    run_turn(question, session_id=st.session_state.session_id, history=history)
                )
            except Exception as exc:  # noqa: BLE001
                result = {
                    "answer": f"Something went wrong: {exc}",
                    "mode": "fast",
                    "specialists": [],
                    "evidence": [],
                    "turn_id": str(uuid.uuid4()),
                }

        st.markdown(result["answer"])

        # Product image cards
        product_ids = _extract_product_ids(result["evidence"])
        _render_product_cards(product_ids)

        # Charts and source citation (gated on intent or button)
        _render_charts_and_sources(question, result["evidence"], key_suffix="live")

        # Agent trace
        if show_trace:
            _render_trace(result["mode"], result["specialists"], result["evidence"], result["turn_id"])

    # Generate follow-ups (non-blocking, after answer is visible)
    followups = _generate_followups(result["answer"], result["specialists"], result["evidence"])
    st.session_state.followups = followups

    # Persist to session state
    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": result["answer"],
            "turn_id": result["turn_id"],
            "mode": result["mode"],
            "specialists": result["specialists"],
            "evidence": result["evidence"],
            "product_ids": product_ids,
            "question": question,
        }
    )

    # Persist to DB
    _persist_turn(question, result)

    # Render follow-up pills immediately
    _render_followup_pills(followups)
