"""robots.txt compliance, cached per-domain with a TTL.

We never bypass robots.txt. For Petbarn this means `/p/` product pages are
fetched, but `/search`, `/catalogsearch`, `/checkout`, etc. are refused even
if something upstream (e.g. a bad LLM tool call) asks for them.
"""

from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

_TTL_S = 24 * 3600


@dataclass
class _Entry:
    parser: urllib.robotparser.RobotFileParser
    fetched_at: float


class RobotsCache:
    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._cache: dict[str, _Entry] = {}

    def _origin(self, url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    async def _load(self, origin: str, client: httpx.AsyncClient) -> _Entry:
        parser = urllib.robotparser.RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            resp = await client.get(robots_url, timeout=10.0)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            else:
                # No robots.txt / inaccessible -> permissive default, per RFC
                # convention used by most crawlers.
                parser.parse([])
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("robots_fetch_failed", url=robots_url, error=str(exc))
            parser.parse([])
        entry = _Entry(parser=parser, fetched_at=time.monotonic())
        self._cache[origin] = entry
        return entry

    async def is_allowed(self, url: str, client: httpx.AsyncClient) -> bool:
        origin = self._origin(url)
        entry = self._cache.get(origin)
        if entry is None or (time.monotonic() - entry.fetched_at) > _TTL_S:
            entry = await self._load(origin, client)
        try:
            return entry.parser.can_fetch(self.user_agent, url)
        except Exception:  # pragma: no cover
            return True
