"""Headless-browser fetch tier (Playwright + Chromium), used only when the
cheaper tiers are blocked, when a page is client-side-only, or to capture
network requests for API rediscovery (see `capture_network_requests`).

Deliberately optional: `playwright` is an extra (`pip install -e ".[browser]"`
then `playwright install chromium`), and every caller must handle
`PlaywrightUnavailable` by falling back to the httpx/curl_cffi tiers or
degrading gracefully. See docs/decisions/001-no-headless-by-default.md.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field


class PlaywrightUnavailable(RuntimeError):
    pass


@dataclass
class PlaywrightResult:
    status_code: int
    text: str
    headers: dict
    final_url: str
    latency_ms: float
    captured_requests: list[dict] = field(default_factory=list)


async def fetch_with_playwright(
    url: str,
    user_agent: str,
    wait_for_selector: str | None = None,
    capture_url_substring: str | None = None,
    timeout_s: float = 30.0,
) -> PlaywrightResult:
    """Render `url` in headless Chromium with stealth patches applied.

    If `capture_url_substring` is given, records matching XHR/fetch requests
    (url + headers) in `captured_requests` -- this is how `EndpointRediscovery`
    finds a rotated Bazaarvoice display code or API host without guessing.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PlaywrightUnavailable(
            "playwright is not installed; install the 'browser' extra and run "
            "`playwright install chromium`"
        ) from exc

    started = time.monotonic()
    captured: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=user_agent)
            page = await context.new_page()

            # Minimal stealth: hide the most common automation fingerprints.
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            if capture_url_substring:

                def _on_request(request):  # noqa: ANN001
                    if capture_url_substring in request.url:
                        captured.append(
                            {
                                "url": request.url,
                                "method": request.method,
                                "headers": dict(request.headers),
                            }
                        )

                page.on("request", _on_request)

            response = await page.goto(
                url, wait_until="networkidle", timeout=timeout_s * 1000
            )
            if wait_for_selector:
                with contextlib.suppress(Exception):
                    await page.wait_for_selector(wait_for_selector, timeout=5000)
            text = await page.content()
            status_code = response.status if response else 200
            headers = dict(response.headers) if response else {}
            final_url = page.url
        finally:
            await browser.close()

    latency_ms = (time.monotonic() - started) * 1000
    return PlaywrightResult(
        status_code=status_code,
        text=text,
        headers=headers,
        final_url=final_url,
        latency_ms=latency_ms,
        captured_requests=captured,
    )
