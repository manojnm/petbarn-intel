"""The tiered fetcher: the single entry point every scraper (Petbarn adapter,
generic adapter, census, discovery) uses to get a URL's content.

Escalation ladder, cheapest first:
    1. httpx        -- plain async HTTP/2, works for ~all Petbarn pages today.
    2. curl_cffi     -- impersonates a real Chrome TLS/JA3 fingerprint; used
                        when a host starts blocking plain HTTP clients.
    3. Playwright    -- full headless Chromium (optional install); used for
                        JS-challenge pages, client-side-only content on other
                        retailers, or to capture network requests during API
                        rediscovery.

The winning tier is remembered per host (`_domain_tier`) so later requests to
a host that needed escalation start there directly instead of re-discovering
it on every call. Every successful response is cached (raw_cache) with a
resource-type TTL and conditional-request support (ETag / If-Modified-Since).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

import httpx
import orjson
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from petbarn_intel.config import Settings, get_settings
from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.fetch import raw_cache
from petbarn_intel.scraping.fetch.block_detector import BlockDetector
from petbarn_intel.scraping.fetch.rate_limit import CircuitOpenError, HostThrottle
from petbarn_intel.scraping.fetch.robots import RobotsCache

logger = get_logger(__name__)

TIERS = ("httpx", "curl_cffi", "playwright")

_TRANSIENT_EXC = (httpx.TransportError, httpx.TimeoutException)


class RobotsDisallowedError(PermissionError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    text: str
    content: bytes
    headers: dict
    tier: str
    from_cache: bool
    latency_ms: float
    blocked: bool = False
    block_reason: str | None = None
    captured_requests: list[dict] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.blocked and 200 <= self.status_code < 300


class TieredFetcher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.throttle = HostThrottle(
            requests_per_second=self.settings.rate_limit_rps,
            max_concurrency_per_host=min(4, self.settings.max_concurrency),
        )
        self.robots = RobotsCache(self.settings.http_user_agent)
        self.detector = BlockDetector()
        self._domain_tier: dict[str, str] = {}
        self._httpx_client: httpx.AsyncClient | None = None
        self._curl_session = None

    async def __aenter__(self) -> TieredFetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._httpx_client is not None:
            await self._httpx_client.aclose()
        if self._curl_session is not None:
            await self._curl_session.close()

    async def _httpx(self) -> httpx.AsyncClient:
        if self._httpx_client is None:
            self._httpx_client = httpx.AsyncClient(
                http2=True,
                follow_redirects=True,
                timeout=self.settings.request_timeout_s,
                headers={"User-Agent": self.settings.http_user_agent},
            )
        return self._httpx_client

    async def _curl(self):
        if self._curl_session is None:
            from curl_cffi import requests as ccr

            self._curl_session = ccr.AsyncSession()
        return self._curl_session

    @staticmethod
    def _host(url: str) -> str:
        return urlparse(url).netloc

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        resource_type: str = "generic",
        ttl_seconds: float | None = None,
        expected_markers: list[str] | None = None,
        force_refresh: bool = False,
        respect_robots: bool = True,
        headers: dict | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        tier_preference: list[str] | None = None,
    ) -> FetchResult:
        full_url = f"{url}?{urlencode(params)}" if params else url
        if json_body is not None:
            method = "POST"
        ttl = ttl_seconds if ttl_seconds is not None else raw_cache.ttl_for(resource_type)
        cache_key = raw_cache.compute_key(method, full_url)
        if json_body is not None:
            body_hash = hashlib.sha256(orjson.dumps(json_body, option=orjson.OPT_SORT_KEYS)).hexdigest()
            cache_key = f"{cache_key}:{body_hash}"

        if not force_refresh:
            entry = raw_cache.get(cache_key)
            if entry is not None and entry.is_fresh():
                content = entry.read_bytes()
                return FetchResult(
                    url=full_url,
                    final_url=full_url,
                    status_code=entry.status_code or 200,
                    text=content.decode("utf-8", errors="replace"),
                    content=content,
                    headers={"content-type": entry.content_type or ""},
                    tier=entry.tier or "cache",
                    from_cache=True,
                    latency_ms=0.0,
                )
        else:
            entry = None

        if respect_robots:
            client = await self._httpx()
            host = self._host(full_url)
            if self.settings.allowlisted_domains_set and host not in self.settings.allowlisted_domains_set:
                raise ValueError(
                    f"Domain '{host}' is not in allowlisted_domains; refusing to fetch."
                )
            allowed = await self.robots.is_allowed(full_url, client)
            if not allowed:
                raise RobotsDisallowedError(f"robots.txt disallows fetching {full_url}")

        host = self._host(full_url)
        order = list(tier_preference) if tier_preference else self._tier_order(host)

        last_result: FetchResult | None = None
        for tier in order:
            try:
                async with self.throttle.slot(host):
                    result = await self._fetch_tier(
                        tier, full_url, method=method, headers=headers, entry=entry,
                        json_body=json_body,
                    )
            except CircuitOpenError as exc:
                logger.warning("circuit_open_skip_tier", host=host, tier=tier, error=str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - tier truly unavailable
                logger.warning("tier_failed_hard", tier=tier, url=full_url, error=str(exc))
                continue

            if result.status_code == 304 and entry is not None:
                raw_cache.touch_freshness(cache_key, ttl)
                content = entry.read_bytes()
                self.throttle.record_success(host)
                return FetchResult(
                    url=full_url,
                    final_url=full_url,
                    status_code=entry.status_code or 200,
                    text=content.decode("utf-8", errors="replace"),
                    content=content,
                    headers={"content-type": entry.content_type or ""},
                    tier=entry.tier or tier,
                    from_cache=True,
                    latency_ms=result.latency_ms,
                )

            verdict = self.detector.assess(result.status_code, result.text, expected_markers)
            if not verdict.blocked:
                self.throttle.record_success(host)
                self._domain_tier[host] = tier
                raw_cache.put(
                    cache_key,
                    full_url,
                    method,
                    result.status_code,
                    tier,
                    result.content,
                    result.headers.get("content-type"),
                    result.headers.get("etag"),
                    result.headers.get("last-modified"),
                    ttl,
                    result.latency_ms,
                )
                return result

            self.throttle.record_failure(host)
            result.blocked = True
            result.block_reason = verdict.reason
            last_result = result
            logger.info(
                "tier_blocked_escalating", host=host, tier=tier, reason=verdict.reason,
                url=full_url,
            )

        return last_result or FetchResult(
            url=full_url, final_url=full_url, status_code=0, text="", content=b"",
            headers={}, tier=order[-1] if order else "none", from_cache=False,
            latency_ms=0.0, blocked=True, block_reason="all_tiers_exhausted",
        )

    def _tier_order(self, host: str) -> list[str]:
        remembered = self._domain_tier.get(host)
        tiers = [t for t in TIERS if t != "playwright" or self.settings.enable_playwright]
        if remembered and remembered in tiers:
            return [remembered, *[t for t in tiers if t != remembered]]
        return tiers

    async def _fetch_tier(
        self, tier: str, url: str, *, method: str, headers: dict | None, entry,
        json_body: dict | None = None,
    ) -> FetchResult:
        if tier == "httpx":
            return await self._fetch_httpx(
                url, method=method, headers=headers, entry=entry, json_body=json_body
            )
        if tier == "curl_cffi":
            return await self._fetch_curl_cffi(
                url, method=method, headers=headers, json_body=json_body
            )
        if tier == "playwright":
            return await self._fetch_playwright(url, headers=headers)
        raise ValueError(f"unknown tier: {tier}")

    @retry(
        retry=retry_if_exception_type(_TRANSIENT_EXC),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    async def _fetch_httpx(
        self, url: str, *, method: str, headers: dict | None, entry,
        json_body: dict | None = None,
    ) -> FetchResult:
        client = await self._httpx()
        req_headers = dict(headers or {})
        if entry is not None:
            if entry.etag:
                req_headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                req_headers["If-Modified-Since"] = entry.last_modified
        started = time.monotonic()
        resp = await client.request(method, url, headers=req_headers, json=json_body)
        latency_ms = (time.monotonic() - started) * 1000
        return FetchResult(
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text if resp.status_code != 304 else "",
            content=resp.content,
            headers=dict(resp.headers),
            tier="httpx",
            from_cache=False,
            latency_ms=latency_ms,
        )

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential_jitter(initial=0.5, max=6),
        reraise=True,
    )
    async def _fetch_curl_cffi(
        self, url: str, *, method: str, headers: dict | None, json_body: dict | None = None
    ) -> FetchResult:
        session = await self._curl()
        started = time.monotonic()
        resp = await session.request(
            method, url, headers=headers or {}, json=json_body,
            impersonate="chrome", timeout=self.settings.request_timeout_s,
        )
        latency_ms = (time.monotonic() - started) * 1000
        return FetchResult(
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            text=resp.text,
            content=resp.content,
            headers=dict(resp.headers),
            tier="curl_cffi",
            from_cache=False,
            latency_ms=latency_ms,
        )

    async def _fetch_playwright(self, url: str, *, headers: dict | None) -> FetchResult:
        from petbarn_intel.scraping.fetch.playwright_tier import (
            PlaywrightUnavailable,
            fetch_with_playwright,
        )

        try:
            pw_result = await fetch_with_playwright(url, self.settings.http_user_agent)
        except PlaywrightUnavailable as exc:
            logger.warning("playwright_unavailable", error=str(exc))
            return FetchResult(
                url=url, final_url=url, status_code=0, text="", content=b"", headers={},
                tier="playwright", from_cache=False, latency_ms=0.0, blocked=True,
                block_reason="playwright_unavailable",
            )
        return FetchResult(
            url=url,
            final_url=pw_result.final_url,
            status_code=pw_result.status_code,
            text=pw_result.text,
            content=pw_result.text.encode(),
            headers=pw_result.headers,
            tier="playwright",
            from_cache=False,
            latency_ms=pw_result.latency_ms,
            captured_requests=pw_result.captured_requests,
        )
