from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PhysicalAsset(BaseModel):
    url: str
    type: str
    resolution: str | None = None
    aspect_ratio: str | None = None
    css_advice: str
    placement_intent: str


class AestheticPayload(BaseModel):
    theme_name: str
    css_variables: dict[str, str] = Field(default_factory=dict)
    tailwind_extend: dict[str, Any] = Field(default_factory=dict)
    fonts: list[str] = Field(default_factory=list)
    physical_assets: dict[str, PhysicalAsset] = Field(default_factory=dict)
    ui_dev_instruction: str
