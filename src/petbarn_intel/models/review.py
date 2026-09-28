"""Review domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SampleReason = Literal["recent", "low_rating", "most_helpful", "full_sync"]
AspectSentiment = Literal["positive", "negative", "neutral", "mixed"]


class Review(BaseModel):
    model_config = ConfigDict(extra="ignore")

    review_id: str
    product_id: str
    rating: int
    title: str | None = None
    text: str
    author: str | None = None
    submitted_at: datetime | None = None
    is_recommended: bool | None = None
    helpful_votes: int = 0
    not_helpful_votes: int = 0
    verified_purchaser: bool | None = None
    incentivized: bool | None = None
    syndicated: bool | None = None

    # Local, free enrichment (VADER), always computed.
    vader_compound: float | None = None
    vader_label: Literal["positive", "negative", "neutral"] | None = None

    sample_reasons: list[SampleReason] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class ReviewAspect(BaseModel):
    """LLM-extracted, aspect-level sentiment for one review.

    Cached by (review_id, aspect, prompt_version) -- see
    docs/caching-observability.md.
    """

    model_config = ConfigDict(extra="ignore")

    review_id: str
    product_id: str
    aspect: str  # e.g. "price", "quality", "palatability", "packaging", "delivery"
    sentiment: AspectSentiment
    evidence: str | None = None
    prompt_version: str
    model: str
    extracted_at: datetime = Field(default_factory=datetime.utcnow)


ASPECT_TAXONOMY: list[str] = [
    "price_value",
    "quality",
    "palatability",
    "health_digestion",
    "packaging",
    "delivery_service",
    "customer_service",
]
