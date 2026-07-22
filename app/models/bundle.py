from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class BundlePayload(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    theme_id: str
    type: str
    role: str
    status: Literal["ready", "pending", "missing"]
    sha256: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    url: str
    access: str
    source_key: str
    origin_url: str = ""
    content_type: str = ""
    downloaded_at: str = ""
    ai_suggestions: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float | None = Field(default=None, ge=0)
    audio_metadata: dict[str, int] = Field(default_factory=dict)
    audio_tags: dict[str, str] = Field(default_factory=dict)
    audio_usage: str = "unknown"


class IngestBatchPayload(BaseModel):
    id: str
    created_at: str
    completed_at: str
    accepted_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
