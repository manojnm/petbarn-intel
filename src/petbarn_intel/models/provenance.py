"""Field-level provenance: every extracted field records where it came from.

This is what makes the quality layer and self-healing possible: the field
merger can compare sources, the Ops page can show which layer produced a
value, and `diagnose_page` can explain a missing field precisely.
"""

from __future__ import annotations

from enum import StrEnum


class SourceKind(StrEnum):
    GRAPHQL = "graphql"
    JSON_LD = "json_ld"
    HTML_ANCHOR = "html_anchor"
    BAZAARVOICE = "bazaarvoice"
    VENDOR_SUMMARY = "vendor_summary"
    LLM_FALLBACK = "llm_fallback"
    VISION_FALLBACK = "vision_fallback"
    SITEMAP = "sitemap"
    GENERIC_SCHEMA_ORG = "generic_schema_org"
    GENERIC_LLM = "generic_llm"
    MANUAL = "manual"


class FieldSource(StrEnum):
    """Alias kept for readability at call sites; identical values to SourceKind."""


# Priority order used by the field merger when two sources disagree.
# Lower index = higher trust.
SOURCE_PRIORITY: list[SourceKind] = [
    SourceKind.GRAPHQL,
    SourceKind.JSON_LD,
    SourceKind.BAZAARVOICE,
    SourceKind.HTML_ANCHOR,
    SourceKind.GENERIC_SCHEMA_ORG,
    SourceKind.LLM_FALLBACK,
    SourceKind.GENERIC_LLM,
    SourceKind.VISION_FALLBACK,
    SourceKind.VENDOR_SUMMARY,
    SourceKind.SITEMAP,
    SourceKind.MANUAL,
]


def source_rank(source: SourceKind | str) -> int:
    kind = SourceKind(source) if not isinstance(source, SourceKind) else source
    try:
        return SOURCE_PRIORITY.index(kind)
    except ValueError:
        return len(SOURCE_PRIORITY)
