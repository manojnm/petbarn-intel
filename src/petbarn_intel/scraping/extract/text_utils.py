"""Small text-cleanup helpers shared by the extraction chain.

Petbarn's EAV attribute values are plain text with HTML entities (e.g.
`&amp;`) or short HTML fragments using `<br>` as the only "structure" (e.g.
the `benefits` attribute). These helpers normalize both without pulling in a
full HTML parser for what is, in practice, always a flat bullet list.
"""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_BULLET_RE = re.compile(r"^[\s\u2022\u00b7•\-*]+")


def clean_text(value: str | None) -> str | None:
    """Unescape HTML entities and collapse whitespace; strip any stray tags."""
    if not value:
        return None
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def html_bullets_to_list(value: str | None) -> list[str]:
    """Split a `<br>`-delimited attribute value (e.g. `benefits`) into a
    clean list of bullet strings, dropping empty lines and leading bullet
    glyphs (e.g. the literal `&nbsp; &bull;` Petbarn prefixes each line
    with).
    """
    if not value:
        return []
    unescaped = html.unescape(value)
    parts = re.split(r"<br\s*/?>", unescaped, flags=re.IGNORECASE)
    out = []
    for part in parts:
        text = _TAG_RE.sub(" ", part)
        text = _BULLET_RE.sub("", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None
