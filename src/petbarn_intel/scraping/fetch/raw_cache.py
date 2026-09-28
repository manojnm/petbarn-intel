"""Content-addressed raw response cache: gzip files on disk + a SQLite
metadata row per resource. This is what lets us re-parse pages offline, build
test fixtures from real traffic, and run robustness tests, and it is the
backbone of the TTL / conditional-request cache layer described in
docs/caching-observability.md.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from petbarn_intel.config import get_settings
from petbarn_intel.store.db import get_connection


def compute_key(method: str, url: str, vary: str = "") -> str:
    raw = f"{method.upper()}:{url}:{vary}".encode()
    return hashlib.sha256(raw).hexdigest()


def _content_path(cache_key: str) -> Path:
    settings = get_settings()
    sub = settings.raw_cache_dir / cache_key[:2]
    sub.mkdir(parents=True, exist_ok=True)
    return sub / f"{cache_key}.gz"


@dataclass
class CacheEntry:
    cache_key: str
    url: str
    method: str
    status_code: int | None
    tier: str | None
    content_path: str
    content_type: str | None
    etag: str | None
    last_modified: str | None
    fetched_at: str
    expires_at: str | None
    latency_ms: float | None

    def read_bytes(self) -> bytes:
        with gzip.open(self.content_path, "rb") as fh:
            return fh.read()

    def is_fresh(self) -> bool:
        if not self.expires_at:
            return False
        return datetime.fromisoformat(self.expires_at) > datetime.now(UTC)


def get(cache_key: str, conn: sqlite3.Connection | None = None) -> CacheEntry | None:
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM raw_cache WHERE cache_key = ?", (cache_key,)).fetchone()
    if not row:
        return None
    return CacheEntry(
        cache_key=row["cache_key"],
        url=row["url"],
        method=row["method"],
        status_code=row["status_code"],
        tier=row["tier"],
        content_path=row["content_path"],
        content_type=row["content_type"],
        etag=row["etag"],
        last_modified=row["last_modified"],
        fetched_at=row["fetched_at"],
        expires_at=row["expires_at"],
        latency_ms=row["latency_ms"],
    )


def put(
    cache_key: str,
    url: str,
    method: str,
    status_code: int,
    tier: str,
    content: bytes,
    content_type: str | None,
    etag: str | None,
    last_modified: str | None,
    ttl_seconds: float,
    latency_ms: float | None,
    conn: sqlite3.Connection | None = None,
) -> CacheEntry:
    conn = conn or get_connection()
    path = _content_path(cache_key)
    with gzip.open(path, "wb", compresslevel=6) as fh:
        fh.write(content)
    now = datetime.now(UTC)
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute(
        """
        INSERT INTO raw_cache (cache_key, url, method, status_code, tier, content_path,
                                content_type, etag, last_modified, fetched_at, expires_at,
                                latency_ms)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(cache_key) DO UPDATE SET
            status_code=excluded.status_code, tier=excluded.tier,
            content_path=excluded.content_path, content_type=excluded.content_type,
            etag=excluded.etag, last_modified=excluded.last_modified,
            fetched_at=excluded.fetched_at, expires_at=excluded.expires_at,
            latency_ms=excluded.latency_ms
        """,
        (
            cache_key,
            url,
            method,
            status_code,
            tier,
            str(path),
            content_type,
            etag,
            last_modified,
            now.isoformat(),
            expires_at,
            latency_ms,
        ),
    )
    conn.commit()
    return CacheEntry(
        cache_key=cache_key,
        url=url,
        method=method,
        status_code=status_code,
        tier=tier,
        content_path=str(path),
        content_type=content_type,
        etag=etag,
        last_modified=last_modified,
        fetched_at=now.isoformat(),
        expires_at=expires_at,
        latency_ms=latency_ms,
    )


def touch_freshness(cache_key: str, ttl_seconds: float, conn: sqlite3.Connection | None = None) -> None:
    """Called after a 304 Not Modified: extends expiry without re-writing content."""
    conn = conn or get_connection()
    expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    conn.execute(
        "UPDATE raw_cache SET fetched_at = ?, expires_at = ? WHERE cache_key = ?",
        (datetime.now(UTC).isoformat(), expires_at, cache_key),
    )
    conn.commit()


# Default TTLs per resource type, in seconds (see docs/caching-observability.md).
DEFAULT_TTLS: dict[str, float] = {
    "graphql_census": 24 * 3600,
    "product_page": 24 * 3600,
    "reviews": 12 * 3600,
    "bv_statistics": 12 * 3600,
    "bv_summary": 7 * 24 * 3600,
    "bv_config": 24 * 3600,
    "sitemap": 24 * 3600,
    "generic": 6 * 3600,
}


def ttl_for(resource_type: str) -> float:
    return DEFAULT_TTLS.get(resource_type, DEFAULT_TTLS["generic"])
