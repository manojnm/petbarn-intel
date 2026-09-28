"""Heuristics that decide whether a response looks like a real page or an
anti-bot block / challenge, so the tiered fetcher knows when to escalate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CHALLENGE_SIGNATURES = [
    r"just a moment\.\.\.",  # Cloudflare JS challenge
    r"attention required.{0,20}cloudflare",
    r"cf-browser-verification",
    r"perimeterx",
    r"px-captcha",
    r"pardon our interruption",
    r"access denied",
    r"request unsuccessful.{0,20}incapsula",
    r"you have been blocked",
    r"unusual traffic",
    r"captcha",
    r"enable javascript and cookies",
    r"__next_error__.*404",  # Next.js hard 404 shell with no data
]
_CHALLENGE_RE = re.compile("|".join(_CHALLENGE_SIGNATURES), re.IGNORECASE)

_BLOCK_STATUS_CODES = {401, 403, 429, 503}


@dataclass
class BlockVerdict:
    blocked: bool
    reason: str | None = None
    confidence: float = 0.0


class BlockDetector:
    def assess(
        self,
        status_code: int,
        text: str,
        expected_markers: list[str] | None = None,
        min_content_length: int = 300,
    ) -> BlockVerdict:
        if status_code in _BLOCK_STATUS_CODES:
            return BlockVerdict(True, f"http_{status_code}", 0.95)

        if status_code >= 500:
            return BlockVerdict(True, f"http_{status_code}", 0.6)

        stripped = text.strip()
        looks_like_json = stripped.startswith("{") or stripped.startswith("[")
        if not looks_like_json and len(text) < min_content_length:
            return BlockVerdict(True, "response_too_short", 0.7)

        if _CHALLENGE_RE.search(text):
            match = _CHALLENGE_RE.search(text)
            return BlockVerdict(True, f"challenge_signature:{match.group(0)[:40]}", 0.9)

        if expected_markers:
            found = any(marker.lower() in text.lower() for marker in expected_markers)
            if not found:
                return BlockVerdict(True, "expected_markers_missing", 0.5)

        return BlockVerdict(False)
