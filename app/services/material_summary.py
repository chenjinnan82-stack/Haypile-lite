from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ROLE_LABELS = {
    "main_background": "背景",
    "hero_image": "主视觉",
    "logo": "Logo",
    "icon": "图标",
    "content_image": "内容图",
    "texture": "纹理",
    "reaction": "反应",
    "sticker": "贴纸",
    "ui_animation": "界面动画",
    "background": "背景",
    "image": "参考图",
    "audio": "音频",
    "unknown": "未确定",
}


@dataclass(slots=True)
class MaterialSummaryItem:
    title: str
    usage_label: str
    confidence_label: str
    status_label: str
    preview_url: str = ""
    theme_id: str = ""
    asset_type: str = ""
    source_key: str = ""
    origin_url: str = ""


@dataclass(slots=True)
class MaterialPanelSummary:
    total_count: int
    recognized_count: int
    pending_count: int
    service_status: str
    recognition_status: str
    recent_items: list[MaterialSummaryItem] = field(default_factory=list)

    def summary_text(self) -> str:
        return (
            f"草堆里有 {self.total_count} 个 bundle，"
            f"{self.recognized_count} 个可用，"
            f"{self.pending_count} 个待确认"
        )


def build_material_panel_summary(
    bundles: Iterable[dict[str, Any]],
    *,
    assets_dir: Path,
    max_items: int = 200,
) -> MaterialPanelSummary:
    items = [_item_from_bundle(bundle) for bundle in bundles]
    items.sort(
        key=lambda item: (_item_mtime(item, assets_dir), item.source_key),
        reverse=True,
    )
    pending_count = sum(item.status_label == "待确认" for item in items)
    missing_count = sum(item.status_label == "副本缺失" for item in items)
    recognized_count = sum(item.status_label == "已识别" for item in items)
    recognition_status = (
        "分类：有副本缺失"
        if missing_count
        else "分类：有待确认"
        if pending_count
        else "分类：可用"
    )
    return MaterialPanelSummary(
        total_count=len(items),
        recognized_count=recognized_count,
        pending_count=pending_count,
        service_status="Haypile：运行中",
        recognition_status=recognition_status,
        recent_items=items[: max(0, int(max_items))],
    )


def _item_from_bundle(bundle: dict[str, Any]) -> MaterialSummaryItem:
    status = str(bundle.get("status") or "pending").lower()
    status_label = {
        "ready": "已识别",
        "missing": "副本缺失",
        "pending": "待确认",
    }.get(status, "待确认")
    source_key = str(bundle.get("source_key") or "")
    asset_type = str(bundle.get("type") or "")
    role = str(bundle.get("role") or "unknown")
    return MaterialSummaryItem(
        title=Path(source_key).name or str(bundle.get("id") or ""),
        usage_label=ROLE_LABELS.get(role, "未确定"),
        confidence_label="中等把握" if status == "ready" else "低把握",
        status_label=status_label,
        preview_url=str(bundle.get("url") or ""),
        theme_id=str(bundle.get("theme_id") or ""),
        asset_type=asset_type,
        source_key=source_key,
        origin_url=str(bundle.get("origin_url") or ""),
    )


def _item_mtime(item: MaterialSummaryItem, assets_dir: Path) -> float:
    try:
        path = (assets_dir / item.source_key).resolve(strict=False)
        if path.is_relative_to(assets_dir.resolve(strict=False)):
            return path.stat().st_mtime
    except OSError:
        pass
    return 0.0
