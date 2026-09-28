"""Repository functions for scrape runs, field-level events, incidents, and
agent traces -- everything the Ops page and evals read from.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

import orjson

from petbarn_intel.models.trace import FieldEvent, Incident, ScrapeRun, ToolCallTrace
from petbarn_intel.store.db import get_connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def start_run(run_type: str, conn: sqlite3.Connection | None = None) -> ScrapeRun:
    conn = conn or get_connection()
    run = ScrapeRun(run_id=str(uuid.uuid4()), run_type=run_type)
    conn.execute(
        "INSERT INTO scrape_runs (run_id, run_type, status, started_at, notes_json) "
        "VALUES (?,?,?,?,?)",
        (run.run_id, run.run_type, run.status, run.started_at.isoformat(), "{}"),
    )
    conn.commit()
    return run


def finish_run(
    run_id: str,
    status: str = "completed",
    pages_fetched: int = 0,
    products_touched: int = 0,
    reviews_fetched: int = 0,
    errors: int = 0,
    notes: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or get_connection()
    conn.execute(
        """
        UPDATE scrape_runs SET status=?, finished_at=?, pages_fetched=?,
               products_touched=?, reviews_fetched=?, errors=?, notes_json=?
        WHERE run_id = ?
        """,
        (
            status,
            _now(),
            pages_fetched,
            products_touched,
            reviews_fetched,
            errors,
            orjson.dumps(notes or {}).decode(),
            run_id,
        ),
    )
    conn.commit()


def bump_run(
    run_id: str,
    pages: int = 0,
    products: int = 0,
    reviews: int = 0,
    errors: int = 0,
    conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or get_connection()
    conn.execute(
        """
        UPDATE scrape_runs SET pages_fetched = pages_fetched + ?,
               products_touched = products_touched + ?,
               reviews_fetched = reviews_fetched + ?,
               errors = errors + ?
        WHERE run_id = ?
        """,
        (pages, products, reviews, errors, run_id),
    )


def list_runs(limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


def record_field_event(event: FieldEvent, conn: sqlite3.Connection | None = None) -> None:
    """Record one field-extraction attempt. `event_id` is a deterministic hash
    of (product_id, field, run_id), so a repeat call with identical inputs --
    e.g. a retried product within the same run -- recomputes the same id.
    `OR IGNORE` makes that a no-op instead of a UNIQUE-constraint crash that
    would otherwise abort the whole product (losing already-fetched reviews/
    product-merge work for it)."""
    conn = conn or get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO field_events (event_id, run_id, product_id, field_name, source,
                                   success, fallback_used, error, recorded_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            event.run_id,
            event.product_id,
            event.field_name,
            event.source,
            int(event.success),
            int(event.fallback_used),
            event.error,
            event.recorded_at.isoformat(),
        ),
    )


def field_completeness(run_id: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    clause = "WHERE run_id = ?" if run_id else ""
    params = (run_id,) if run_id else ()
    return conn.execute(
        f"""
        SELECT field_name,
               SUM(success) * 1.0 / COUNT(*) as completeness,
               SUM(fallback_used) as fallback_count,
               COUNT(*) as attempts
        FROM field_events {clause}
        GROUP BY field_name
        ORDER BY completeness ASC
        """,
        params,
    ).fetchall()


def diagnose_product_fields(product_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT * FROM field_events WHERE product_id = ? ORDER BY recorded_at DESC LIMIT 100",
        (product_id,),
    ).fetchall()


def open_incident(incident: Incident, conn: sqlite3.Connection | None = None) -> None:
    """Same idempotency reasoning as `record_field_event`: `incident_id` is a
    deterministic hash, so `OR IGNORE` avoids a UNIQUE-constraint crash on a
    repeat call for the same (kind, field, product_id, run_id)."""
    conn = conn or get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO incidents (incident_id, kind, severity, product_id, field_name,
                                detail, opened_at, resolved_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            incident.incident_id,
            incident.kind,
            incident.severity,
            incident.product_id,
            incident.field_name,
            incident.detail,
            incident.opened_at.isoformat(),
            None,
        ),
    )
    conn.commit()


def open_incidents(conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY opened_at DESC"
    ).fetchall()


def record_trace(trace: ToolCallTrace, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    conn.execute(
        """
        INSERT INTO traces (trace_id, session_id, turn_id, node, kind, name,
                             args_summary, result_summary, latency_ms, tokens_in,
                             tokens_out, cost_usd, error, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trace.trace_id,
            trace.session_id,
            trace.turn_id,
            trace.node,
            trace.kind,
            trace.name,
            trace.args_summary,
            trace.result_summary,
            trace.latency_ms,
            trace.tokens_in,
            trace.tokens_out,
            trace.cost_usd,
            trace.error,
            trace.created_at.isoformat(),
        ),
    )
    conn.commit()


def get_traces_for_turn(session_id: str, turn_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT * FROM traces WHERE session_id = ? AND turn_id = ? ORDER BY created_at",
        (session_id, turn_id),
    ).fetchall()


def turn_cost_summary(limit: int = 100, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        """
        SELECT session_id, turn_id,
               MIN(created_at) as started_at,
               SUM(COALESCE(cost_usd, 0)) as cost_usd,
               SUM(COALESCE(latency_ms, 0)) as latency_ms,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors,
               COUNT(*) as steps
        FROM traces
        GROUP BY session_id, turn_id
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Session & message persistence (chat history)
# ---------------------------------------------------------------------------


def save_session(session_id: str, title: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_connection()
    conn.execute(
        "INSERT INTO chat_sessions (session_id, created_at, title) VALUES (?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET title = COALESCE(excluded.title, chat_sessions.title)",
        (session_id, _now(), title),
    )
    conn.commit()


def save_message(
    session_id: str,
    role: str,
    content: str,
    turn_id: str | None = None,
    metadata: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or get_connection()
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, turn_id, metadata_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (session_id, role, content, turn_id, orjson.dumps(metadata or {}).decode(), _now()),
    )
    conn.commit()


def list_sessions(limit: int = 20, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    return conn.execute(
        "SELECT session_id, created_at, title FROM chat_sessions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def load_messages(session_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn or get_connection()
    rows = conn.execute(
        "SELECT role, content, turn_id, metadata_json FROM chat_messages "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    result = []
    for row in rows:
        msg: dict = {"role": row["role"], "content": row["content"]}
        if row["turn_id"]:
            msg["turn_id"] = row["turn_id"]
        try:
            meta = orjson.loads(row["metadata_json"] or "{}")
            msg.update(meta)
        except Exception:  # noqa: BLE001
            pass
        result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Aggregate metrics and filtered turn summaries (for Ops page)
# ---------------------------------------------------------------------------


def aggregate_metrics(hours: int | None = 24, conn: sqlite3.Connection | None = None) -> dict:
    """Aggregate token/cost/error metrics across all recorded traces.
    Pass hours=None for all-time stats."""
    conn = conn or get_connection()
    clause = "WHERE created_at >= datetime('now', ?)" if hours else ""
    params = (f"-{hours} hours",) if hours else ()
    row = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT turn_id)                                          AS total_turns,
            COUNT(CASE WHEN kind = 'llm_call' THEN 1 END)                   AS total_llm_calls,
            COALESCE(SUM(tokens_in), 0)                                      AS tokens_in,
            COALESCE(SUM(tokens_out), 0)                                     AS tokens_out,
            COALESCE(SUM(cost_usd), 0.0)                                     AS total_cost,
            COUNT(*)                                                          AS total_steps,
            COUNT(CASE WHEN error IS NOT NULL THEN 1 END)                    AS error_steps
        FROM traces {clause}
        """,
        params,
    ).fetchone()
    d = dict(row)
    steps = d.get("total_steps") or 1
    d["error_rate"] = (d.get("error_steps", 0) / steps) * 100
    return d


def turn_cost_summary_filtered(
    hours: int | None = None,
    session_search: str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Turn-level aggregation with optional time-range and session-id filters.
    Includes per-turn token totals in addition to cost/latency."""
    conn = conn or get_connection()
    clauses: list[str] = []
    params: list = []
    if hours:
        clauses.append("created_at >= datetime('now', ?)")
        params.append(f"-{hours} hours")
    if session_search:
        clauses.append("session_id LIKE ?")
        params.append(f"%{session_search}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return conn.execute(
        f"""
        SELECT session_id, turn_id,
               MIN(created_at)                                           AS started_at,
               COALESCE(SUM(cost_usd), 0)                               AS cost_usd,
               COALESCE(SUM(latency_ms), 0)                             AS latency_ms,
               COALESCE(SUM(tokens_in), 0)                              AS tokens_in,
               COALESCE(SUM(tokens_out), 0)                             AS tokens_out,
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)       AS errors,
               COUNT(*)                                                  AS steps
        FROM traces {where}
        GROUP BY session_id, turn_id
        ORDER BY started_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
