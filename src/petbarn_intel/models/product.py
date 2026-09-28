"""Product domain models.

`Product` is intentionally a flat, plain model (prices are floats, not
wrapper objects) so agent tools can consume it directly. Provenance rides
alongside as `sources: dict[field_name, SourceKind]` and
`confidence: dict[field_name, float]`, populated by the field merger
(`scraping/extract/merge.py`). This keeps the "is this field trustworthy"
question answerable without forcing every consumer to unwrap `FieldValue[T]`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal["census", "seed", "on_demand"]


class ProductVariant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sku: str
    name: str
    size: str | None = None
    gtin: str | None = None
    price: float | None = None
    currency: str = "AUD"
    member_price: float | None = None
    availability: str | None = None  # e.g. "InStock", "OutOfStock"
    image_url: str | None = None
    url: str | None = None


class VendorSummary(BaseModel):
    """Petbarn's own Bazaarvoice AI "Summary of Reviews" panel.

    Secondary signal only -- see docs/decisions/, section 1.1 of the plan.
    Never presented by the agent as our own analysis.
    """

    model_config = ConfigDict(extra="ignore")

    product_id: str
    paragraph: str | None = None
    bullets: str | None = None
    disclaimer: str | None = None
    vendor_created_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class Product(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: str  # canonical id: base Petbarn SKU or url_key when no SKU
    name: str
    brand: str | None = None
    url: str
    url_key: str | None = None

    categories: list[str] = Field(default_factory=list)
    pet_types: list[str] = Field(default_factory=list)

    description: str | None = None
    specification: dict[str, str] = Field(default_factory=dict)
    ingredients: str | None = None
    feeding_guide: str | None = None
    features: list[str] = Field(default_factory=list)

    variants: list[ProductVariant] = Field(default_factory=list)
    price_min: float | None = None
    price_max: float | None = None
    currency: str = "AUD"
    stock_status: str | None = None

    rating_value: float | None = None
    review_count: int | None = None
    written_review_count: int | None = None

    vendor_summary: VendorSummary | None = None

    tier: Tier = "census"
    sources: dict[str, str] = Field(default_factory=dict)
    confidence: dict[str, float] = Field(default_factory=dict)
    completeness: float = 0.0
    disagreements: dict[str, list[str]] = Field(default_factory=dict)

    scraped_at: datetime | None = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def effective_price(self) -> float | None:
        if self.variants:
            prices = [v.price for v in self.variants if v.price is not None]
            if prices:
                return min(prices)
        return self.price_min
