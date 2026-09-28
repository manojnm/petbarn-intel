"""LLM field-level fallback extraction -- plan section 1.7, layer 4 of the
self-healing design, and the last piece of the `quality` todo.

This is the *last resort*, used only when a field is still empty after every
structured source (GraphQL `custom_attributesV2`, then JSON-LD). On Petbarn
that GraphQL response reliably covers description/ingredients/feeding_guide
(see `docs/decisions/002-graphql-attrs-over-html.md`), so this should fire
rarely -- exactly the point: pay the LLM cost only on genuine failure, not
on every page.

Flow, mirroring layer 2 + layer 4 of plan 1.7:
1. Isolate a small chunk of the rendered PDP HTML near a stable
   `data-identifier-id` anchor or a heading whose text names the field --
   never a hashed CSS class, which breaks on every Petbarn deploy.
2. If no such section exists at all, don't call the LLM -- there's nothing
   to extract from, and it would just hallucinate.
3. One structured-output (`response_format=json_object`) call extracts only
   the missing fields from those chunks.
4. Only non-empty string values the model actually returned are trusted;
   anything else is left for the caller to flag as a genuine incident.

Cost: ~$0.001/page (small model, small inputs), paid only on failure.
"""

from __future__ import annotations

import json

from bs4 import BeautifulSoup

from petbarn_intel.agent.llm import achat
from petbarn_intel.logging import get_logger

logger = get_logger(__name__)

# Stable `data-identifier-id` hooks (plan section 0 recon) checked first --
# cheaper and more precise than heading-text search.
_ANCHOR_BY_FIELD = {
    "description": "description",
    "price_min": "price-section",
}

# Heading text (case-insensitive substring match) that introduces each
# field's section, used when there's no anchor hit. Also never CSS-based.
_HEADINGS_BY_FIELD = {
    "description": ("description", "about this product"),
    "ingredients": ("ingredients", "composition"),
    "feeding_guide": ("feeding guide", "feeding instructions", "how to use"),
    "price_min": ("price",),
}

_MAX_CHARS_PER_SECTION = 4000
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "button", "summary", "dt")


def _text(node) -> str:
    return node.get_text(" ", strip=True)


def _section_near_heading(soup: BeautifulSoup, needles: tuple[str, ...]) -> str | None:
    for tag in soup.find_all(_HEADING_TAGS):
        heading_text = _text(tag).lower()
        if not heading_text or not any(n in heading_text for n in needles):
            continue
        # Walk forward through a few siblings collecting text until either
        # content is found or we've clearly run past this section.
        chunks: list[str] = []
        node = tag.find_next_sibling()
        hops = 0
        while node is not None and hops < 6:
            text = _text(node)
            if text:
                chunks.append(text)
            node = node.find_next_sibling()
            hops += 1
        if chunks:
            return " ".join(chunks)[:_MAX_CHARS_PER_SECTION]
    return None


def _extract_section_text(html_text: str, field: str) -> str | None:
    """Best-effort isolation of the HTML chunk relevant to `field`. Returns
    None (never raises) if no anchor or heading match is found."""
    soup = BeautifulSoup(html_text, "lxml")

    anchor = _ANCHOR_BY_FIELD.get(field)
    if anchor:
        node = soup.find(attrs={"data-identifier-id": anchor})
        if node:
            text = _text(node)
            if text:
                return text[:_MAX_CHARS_PER_SECTION]

    needles = _HEADINGS_BY_FIELD.get(field)
    if needles:
        return _section_near_heading(soup, needles)
    return None


async def llm_extract_fields(
    html_text: str, product_name: str, missing_fields: list[str],
) -> dict[str, str]:
    """Attempt to fill `missing_fields` from `html_text`. Returns only the
    subset the model confidently extracted (non-empty strings) -- callers
    should still treat any field absent from the result as genuinely
    missing (open the usual `missing_field` incident)."""
    sections: dict[str, str] = {}
    for field in missing_fields:
        text = _extract_section_text(html_text, field)
        if text:
            sections[field] = text

    if not sections:
        logger.info("llm_fallback_no_section_found", fields=missing_fields)
        return {}

    prompt_parts = [
        f'Product: "{product_name}"',
        "",
        "Below are HTML-derived text sections from this product's page, one "
        "per candidate field. For each field, extract its value ONLY if the "
        "corresponding section actually contains that information -- omit "
        "the key entirely if it doesn't (never invent a value). Respond with "
        "strict JSON of the form {\"field_name\": \"extracted value\"}, no "
        "commentary, no markdown fences.",
        "",
    ]
    for field, text in sections.items():
        prompt_parts.append(f"--- {field} ---\n{text}\n")
    prompt = "\n".join(prompt_parts)

    try:
        # No `temperature` override: reasoning models (e.g. gpt-5.x) reject
        # anything but their default (temperature=1, tuned via
        # `reasoning_effort` instead) -- determinism here comes from the
        # narrow, structured-output prompt, not sampling temperature.
        reply = await achat(
            [{"role": "user", "content": prompt}],
            role="mini",
            response_format={"type": "json_object"},
        )
        parsed = json.loads(reply)
    except Exception as exc:  # noqa: BLE001 - fallback must never crash the scrape
        logger.warning("llm_fallback_call_failed", error=str(exc), fields=list(sections))
        return {}

    if not isinstance(parsed, dict):
        logger.warning("llm_fallback_bad_response_shape", reply=reply[:200])
        return {}

    out: dict[str, str] = {}
    for field in missing_fields:
        value = parsed.get(field)
        if isinstance(value, str) and value.strip():
            out[field] = value.strip()

    logger.info(
        "llm_fallback_extracted",
        attempted=missing_fields,
        sections_found=list(sections),
        recovered=list(out),
    )
    return out
