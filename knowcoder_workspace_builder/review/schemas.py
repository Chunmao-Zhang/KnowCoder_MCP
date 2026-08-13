"""Review HTTP request schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SaveReviewRequest(BaseModel):
    review: dict[str, Any]


class ConfirmReviewRequest(BaseModel):
    expected_version: int | None = Field(default=None, ge=0)


class ReviseReviewRequest(BaseModel):
    instruction: str = Field(min_length=1)
    expected_version: int | None = Field(default=None, ge=0)
