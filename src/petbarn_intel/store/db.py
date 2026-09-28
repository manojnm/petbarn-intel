"""SQLite connection management.

One file, WAL mode, `sqlite_vec` loaded as an extension when available so the
same connection can serve relational queries, FTS5, and vector search. All
timestamps are stored as ISO-8601 UTC strings for portability and readability
when inspecting the DB directly.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from petbarn_intel.config import get_settings
from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

_local = threading.local()

EMBEDDING_DIM = 512  # text-embedding-3-small, truncated via `dimensions=` param


def _load_vec_extension(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("sqlite_vec_unavailable", error=str(exc))
        return False


def _dict_row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    fields = [c[0] for c in cursor.description]
    return dict(zip(fields, row, strict=True))


def new_connection(db_path: Path | None = None) -> sqlite3.Connection:
    settings = get_settings()
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = _dict_row_factory
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    _load_vec_extension(conn)
    return conn


def get_connection() -> sqlite3.Connection:
    """Thread-local singleton connection, suitable for CLI scripts and the
    Streamlit app (each Streamlit script-run thread gets its own).
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = new_connection()
        _local.conn = conn
    return conn


def _schema_sql() -> str:
    return resources.files("petbarn_intel.store").joinpath("schema.sql").read_text()


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Idempotent: safe to call on every startup."""
    own = conn is None
    conn = conn or get_connection()
    conn.executescript(_schema_sql())
    ensure_vec_table(conn)
    if own:
        conn.commit()
    else:
        conn.commit()


def ensure_vec_table(conn: sqlite3.Connection, dim: int = EMBEDDING_DIM) -> None:
    try:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_reviews USING vec0("
            f"review_rowid INTEGER PRIMARY KEY, embedding float[{dim}])"
        )
    except sqlite3.OperationalError as exc:
        logger.warning("vec_table_unavailable", error=str(exc))


@contextmanager
def transaction(conn: sqlite3.Connection | None = None):
    conn = conn or get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
