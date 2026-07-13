from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BundlePayload(BaseModel):
    id: str
    theme_id: str
    type: str
    role: str
    status: str
    sha256: str
    url: str
    access: str
    source_key: str
    origin_url: str = ""
    content_type: str = ""
    downloaded_at: str = ""
    ai_suggestions: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float | None = None
    audio_metadata: dict[str, int] = Field(default_factory=dict)
    audio_tags: dict[str, str] = Field(default_factory=dict)
    audio_usage: str = "unknown"
