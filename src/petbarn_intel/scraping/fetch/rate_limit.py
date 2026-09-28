"""Per-host politeness: token-bucket rate limiting, a concurrency semaphore,
and a circuit breaker that stops hammering a host once it looks blocked.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from petbarn_intel.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _HostState:
    last_request_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    consecutive_failures: int = 0
    circuit_open_until: float = 0.0


class CircuitOpenError(RuntimeError):
    def __init__(self, host: str, retry_after_s: float) -> None:
        super().__init__(f"circuit open for {host}, retry after {retry_after_s:.0f}s")
        self.host = host
        self.retry_after_s = retry_after_s


class HostThrottle:
    """One instance shared by a TieredFetcher, keyed by hostname."""

    def __init__(
        self,
        requests_per_second: float = 3.0,
        max_concurrency_per_host: int = 4,
        failure_threshold: int = 5,
        cooldown_s: float = 60.0,
    ) -> None:
        self.min_interval = 1.0 / max(requests_per_second, 0.01)
        self.max_concurrency_per_host = max_concurrency_per_host
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._hosts: dict[str, _HostState] = {}

    def _state(self, host: str) -> _HostState:
        st = self._hosts.get(host)
        if st is None:
            st = _HostState(semaphore=asyncio.Semaphore(self.max_concurrency_per_host))
            self._hosts[host] = st
        return st

    def check_circuit(self, host: str) -> None:
        st = self._state(host)
        now = time.monotonic()
        if st.circuit_open_until > now:
            raise CircuitOpenError(host, st.circuit_open_until - now)

    def slot(self, host: str) -> _Release:
        """Usage: `async with throttle.slot(host):` -- waits for the rate
        limit and a free concurrency slot on entry, releases on exit."""
        return _Release(self, host)

    async def _acquire(self, host: str) -> None:
        st = self._state(host)
        self.check_circuit(host)
        await st.semaphore.acquire()
        async with st.lock:
            now = time.monotonic()
            wait = st.last_request_at + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            st.last_request_at = time.monotonic()

    def release(self, host: str) -> None:
        self._state(host).semaphore.release()

    def record_success(self, host: str) -> None:
        st = self._state(host)
        st.consecutive_failures = 0
        st.circuit_open_until = 0.0

    def record_failure(self, host: str) -> None:
        st = self._state(host)
        st.consecutive_failures += 1
        if st.consecutive_failures >= self.failure_threshold:
            st.circuit_open_until = time.monotonic() + self.cooldown_s
            logger.warning(
                "circuit_opened", host=host, cooldown_s=self.cooldown_s,
                consecutive_failures=st.consecutive_failures,
            )


class _Release:
    """Async context manager returned by `HostThrottle.slot(host)`."""

    def __init__(self, throttle: HostThrottle, host: str) -> None:
        self._throttle = throttle
        self._host = host

    async def __aenter__(self) -> None:
        await self._throttle._acquire(self._host)
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._throttle.release(self._host)
