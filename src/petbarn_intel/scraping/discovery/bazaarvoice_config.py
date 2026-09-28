"""Bazaarvoice deployment config discovery.

Petbarn's reviews widget publishes its own configuration as public
JavaScript (`rating_summary-config.js` etc.) containing the numeric
`displayCode` that every Bazaarvoice BFD API call needs. Rather than hard-
coding that number, we read it from the config file every time (cheap: it is
cached for 24h via the normal raw-response cache) and re-derive it with
`force_refresh=True` if a data call ever comes back 401/403.

This is "EndpointRediscovery" from the plan's self-healing design: a
deterministic, zero-LLM repair for the one credential Petbarn's frontend
rotates.
"""

from __future__ import annotations

import re

from petbarn_intel.logging import get_logger
from petbarn_intel.scraping.fetch import TieredFetcher

logger = get_logger(__name__)

CONFIG_URL_TMPL = (
    "https://apps.bazaarvoice.com/deployments/{client}/main_site/production/"
    "{locale}/rating_summary-config.js"
)

_DISPLAY_CODE_RE = re.compile(r'"displayCode"\s*:\s*"(\d+)"')
_CLIENT_NAME_RE = re.compile(r'"clientName"\s*:\s*"([\w-]+)"')


class BazaarvoiceConfigError(RuntimeError):
    pass


async def get_display_code(
    fetcher: TieredFetcher,
    client: str = "petbarn-au",
    locale: str = "en_AU",
    force_refresh: bool = False,
) -> str:
    url = CONFIG_URL_TMPL.format(client=client, locale=locale)
    result = await fetcher.fetch(
        url,
        resource_type="bv_config",
        respect_robots=False,  # third-party vendor asset, meant to be publicly fetched
        force_refresh=force_refresh,
    )
    if not result.ok():
        raise BazaarvoiceConfigError(f"could not fetch BV config: {result.block_reason}")
    match = _DISPLAY_CODE_RE.search(result.text)
    if not match:
        raise BazaarvoiceConfigError("displayCode not found in rating_summary-config.js")
    code = match.group(1)
    logger.debug("bv_display_code_resolved", code=code, forced=force_refresh)
    return code
