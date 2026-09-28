"""Bazaarvoice Q&A domain model."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question_id: str
    product_id: str
    question_text: str
    answer_texts: list[str] = Field(default_factory=list)
    submitted_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
