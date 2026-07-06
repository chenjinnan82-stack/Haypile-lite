from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from app.services.asset_provenance import read_asset_provenance
from app.services.real_project_binding import resolve_haypile_real_project_root


ROLE_LABELS: dict[str, str] = {
    "main_background": "背景",
    "hero_image": "主视觉",
    "icon": "图标",
    "texture": "装饰",
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
    rehearsal_status: str = ""
    rehearsal_status_label: str = ""
    real_project_status: str = ""
    real_project_status_label: str = ""
    real_project_root: str = ""
    project_display_label: str = ""
    project_display_state: str = ""
    panel_status_label: str = ""
    panel_action_label: str = ""
    panel_status_text: str = ""
    panel_display_text: str = ""
    confirmation_available: bool = False
    confirmation_action: str = ""
    confirmation_primary_label: str = ""
    confirmation_title: str = ""
    confirmation_body: str = ""
    confirmation_summary: str = ""
    confirmation_warning: str = ""
    project_picker_preview_available: bool = False
    project_picker_title: str = ""
    project_picker_body_line: str = ""
    project_picker_summary_line: str = ""
    project_picker_primary_label: str = ""
    project_picker_primary_enabled: bool = False
    project_picker_secondary_label: str = ""
    project_picker_blocked_reason_count: int = 0
    project_picker_blocked_reasons: list[str] = field(default_factory=list)
    project_picker_confirmation_title: str = ""
    project_picker_confirmation_body_line: str = ""
    project_picker_confirmation_summary_line: str = ""
    project_picker_next_step: str = ""
    project_picker_contract: dict[str, bool] = field(default_factory=dict)
    project_picker_tooltip: str = ""
    project_picker_preview_path: str = ""
    project_picker_preview_error: str = ""
    project_picker_preview_source: str = ""
    project_picker_preview_file_exists: bool = False
    project_picker_preview_file_status: str = "unset"
    project_picker_preview_loaded_at: str = ""
    project_picker_status_line: str = ""
    project_picker_execution_readiness_available: bool = False
    project_picker_execution_readiness_status: str = ""
    project_picker_execution_button_enabled: bool = False
    project_picker_execution_would_execute: bool = False
    project_picker_operation_paths_hash: str = ""
    project_picker_execution_result_available: bool = False
    project_picker_execution_result_status: str = ""
    project_picker_execution_result_action: str = ""
    project_picker_execution_result_executed: bool = False
    project_picker_execution_result_error_code: str = ""
    project_picker_execution_result_error_message: str = ""
    recent_items: list[MaterialSummaryItem] = field(default_factory=list)

    def summary_text(self) -> str:
        return (
            f"草堆里有 {self.total_count} 个 bundle，"
            f"{self.recognized_count} 个可用，"
            f"{self.pending_count} 个待确认"
        )


def build_material_panel_summary(
    *,
    assets_dir: Path | None = None,
    manifest_path: Path | None = None,
    themes_dir: Path | None = None,
    rehearsal_root: Path | None = None,
    real_project_root: Path | None = None,
    real_project_binding_path: Path | None = None,
    project_picker_preview_path: Path | None = None,
    max_items: int = 200,
) -> MaterialPanelSummary:
    from app.core.config import get_settings

    settings = get_settings()
    if assets_dir is None or manifest_path is None or themes_dir is None:
        assets_dir = assets_dir or settings.ASSETS_DIR
        manifest_path = manifest_path or settings.MANIFEST_PATH
        themes_dir = themes_dir or settings.THEMES_DIR
    resolved_assets_dir = assets_dir
    resolved_manifest_path = manifest_path
    resolved_themes_dir = themes_dir

    manifest = _read_json_object(resolved_manifest_path)
    theme_assets = _read_theme_assets(resolved_themes_dir)
    items = _build_items(
        manifest=manifest,
        theme_assets=theme_assets,
        assets_dir=resolved_assets_dir,
    )
    items.sort(key=lambda item: _item_mtime(item, resolved_assets_dir), reverse=True)

    total_count = len(items)
    pending_count = sum(1 for item in items if item.status_label == "待确认")
    recognized_count = max(0, total_count - pending_count)
    service_status = "Haypile：运行中" if resolved_manifest_path.exists() else "Haypile：等待入库"
    recognition_status = f"{'分类：有待确认' if pending_count else '分类：可用'} · {_classifier_status(settings)}"
    rehearsal_status, rehearsal_status_label = _rehearsal_status(rehearsal_root)
    binding = resolve_haypile_real_project_root(binding_path=real_project_binding_path)
    resolved_real_project_root = real_project_root or (binding.project_root if binding is not None else None)
    real_project_status, real_project_status_label, real_project_operation_count = _real_project_status(
        resolved_real_project_root
    )
    project_display_label = _project_display_label(resolved_real_project_root)
    project_display_state = real_project_status or rehearsal_status
    panel_status_label = real_project_status_label or rehearsal_status_label
    panel_action_label = _panel_action_label(
        real_project_status=real_project_status,
        operation_count=real_project_operation_count,
    )
    panel_status_text = "\n".join(
        label for label in (panel_status_label, panel_action_label) if label
    )
    panel_display_text = _panel_display_text(
        real_project_status=real_project_status,
        rehearsal_status=rehearsal_status,
        operation_count=real_project_operation_count,
    )
    (
        confirmation_available,
        confirmation_action,
        confirmation_primary_label,
        confirmation_title,
        confirmation_body,
        confirmation_summary,
        confirmation_warning,
    ) = _confirmation_prompt(
        real_project_root=resolved_real_project_root,
        real_project_status=real_project_status,
        real_project_status_label=real_project_status_label,
        operation_count=real_project_operation_count,
    )
    project_picker_preview = _project_picker_preview(project_picker_preview_path)
    if project_picker_preview and project_picker_preview.get("preview_file_status") != "unset":
        panel_status_label = project_picker_preview["panel_status_label"]
        panel_action_label = project_picker_preview["primary_label"]
        panel_status_text = project_picker_preview["tooltip"]
        panel_display_text = project_picker_preview["display_text"]
        selected_project_root = project_picker_preview["selected_project_root"]
        if selected_project_root:
            real_project_root_text = selected_project_root
            project_display_label = f"● {project_picker_preview['project_name']}"
        else:
            real_project_root_text = (
                resolved_real_project_root.as_posix() if resolved_real_project_root is not None else ""
            )
        project_display_state = project_picker_preview["project_state"] or project_display_state
        if project_picker_preview["selected_project_root"]:
            project_display_state = project_display_state or "needs_review"
        (
            confirmation_available,
            confirmation_action,
            confirmation_primary_label,
            confirmation_title,
            confirmation_body,
            confirmation_summary,
            confirmation_warning,
        ) = _project_picker_confirmation_prompt(project_picker_preview)
    else:
        real_project_root_text = (
            resolved_real_project_root.as_posix() if resolved_real_project_root is not None else ""
        )

    return MaterialPanelSummary(
        total_count=total_count,
        recognized_count=recognized_count,
        pending_count=pending_count,
        service_status=service_status,
        recognition_status=recognition_status,
        rehearsal_status=rehearsal_status,
        rehearsal_status_label=rehearsal_status_label,
        real_project_status=real_project_status,
        real_project_status_label=real_project_status_label,
        real_project_root=real_project_root_text,
        project_display_label=project_display_label,
        project_display_state=project_display_state,
        panel_status_label=panel_status_label,
        panel_action_label=panel_action_label,
        panel_status_text=panel_status_text,
        panel_display_text=panel_display_text,
        confirmation_available=confirmation_available,
        confirmation_action=confirmation_action,
        confirmation_primary_label=confirmation_primary_label,
        confirmation_title=confirmation_title,
        confirmation_body=confirmation_body,
        confirmation_summary=confirmation_summary,
        confirmation_warning=confirmation_warning,
        project_picker_preview_available=bool(project_picker_preview.get("preview_available")) if project_picker_preview else False,
        project_picker_title=str(project_picker_preview.get("title", "")) if project_picker_preview else "",
        project_picker_body_line=str(project_picker_preview.get("body_line", "")) if project_picker_preview else "",
        project_picker_summary_line=str(project_picker_preview.get("summary_line", "")) if project_picker_preview else "",
        project_picker_primary_label=str(project_picker_preview.get("primary_label", "")) if project_picker_preview else "",
        project_picker_primary_enabled=bool(project_picker_preview.get("primary_enabled", False)) if project_picker_preview else False,
        project_picker_secondary_label=str(project_picker_preview.get("secondary_label", "")) if project_picker_preview else "",
        project_picker_blocked_reason_count=int(project_picker_preview.get("blocked_reason_count", 0)) if project_picker_preview else 0,
        project_picker_blocked_reasons=list(project_picker_preview.get("blocked_reasons", [])) if project_picker_preview else [],
        project_picker_confirmation_title=str(project_picker_preview.get("confirmation_title", "")) if project_picker_preview else "",
        project_picker_confirmation_body_line=str(project_picker_preview.get("confirmation_body_line", "")) if project_picker_preview else "",
        project_picker_confirmation_summary_line=str(project_picker_preview.get("confirmation_summary_line", "")) if project_picker_preview else "",
        project_picker_next_step=str(project_picker_preview.get("next_step", "")) if project_picker_preview else "",
        project_picker_contract=dict(project_picker_preview.get("contract", {})) if project_picker_preview else {},
        project_picker_tooltip=str(project_picker_preview.get("tooltip", "")) if project_picker_preview else "",
        project_picker_preview_path=str(project_picker_preview.get("preview_path", "")) if project_picker_preview else "",
        project_picker_preview_error=str(project_picker_preview.get("preview_error", "")) if project_picker_preview else "",
        project_picker_preview_source=str(project_picker_preview.get("preview_source", "")) if project_picker_preview else "unset",
        project_picker_preview_file_exists=bool(project_picker_preview.get("preview_file_exists", False)) if project_picker_preview else False,
        project_picker_preview_file_status=str(project_picker_preview.get("preview_file_status", "unset")) if project_picker_preview else "unset",
        project_picker_preview_loaded_at=str(project_picker_preview.get("preview_loaded_at", "")) if project_picker_preview else "",
        project_picker_status_line=str(project_picker_preview.get("status_line", "")) if project_picker_preview else "Project Picker：未设置预览文件",
        project_picker_execution_readiness_available=bool(project_picker_preview.get("execution_readiness_available", False)) if project_picker_preview else False,
        project_picker_execution_readiness_status=str(project_picker_preview.get("execution_readiness_status", "")) if project_picker_preview else "",
        project_picker_execution_button_enabled=bool(project_picker_preview.get("execution_button_enabled", False)) if project_picker_preview else False,
        project_picker_execution_would_execute=bool(project_picker_preview.get("execution_would_execute", False)) if project_picker_preview else False,
        project_picker_operation_paths_hash=str(project_picker_preview.get("operation_paths_hash", "")) if project_picker_preview else "",
        project_picker_execution_result_available=bool(project_picker_preview.get("execution_result_available", False)) if project_picker_preview else False,
        project_picker_execution_result_status=str(project_picker_preview.get("execution_result_status", "")) if project_picker_preview else "",
        project_picker_execution_result_action=str(project_picker_preview.get("execution_result_action", "")) if project_picker_preview else "",
        project_picker_execution_result_executed=bool(project_picker_preview.get("execution_result_executed", False)) if project_picker_preview else False,
        project_picker_execution_result_error_code=str(project_picker_preview.get("execution_result_error_code", "")) if project_picker_preview else "",
        project_picker_execution_result_error_message=str(project_picker_preview.get("execution_result_error_message", "")) if project_picker_preview else "",
        recent_items=items[: max(0, max_items)],
    )


def _build_items(
    *,
    manifest: dict[str, Any],
    theme_assets: dict[str, dict[str, Any]],
    assets_dir: Path,
) -> list[MaterialSummaryItem]:
    items: list[MaterialSummaryItem] = []
    seen_urls: set[str] = set()

    for key, value in manifest.items():
        if not isinstance(value, dict):
            continue
        url_path = str(value.get("url_path") or "").strip()
        asset_type = str(value.get("type") or "").strip().lower()
        theme_id = _theme_from_key(key)
        theme_asset = theme_assets.get(url_path, {})
        role = str(theme_asset.get("role") or theme_asset.get("source_key") or key).strip()
        usage_label = _usage_label(role=role, asset_type=asset_type)
        status_label = "待确认" if usage_label == "未确定" else "已识别"
        confidence_label = "低把握" if status_label == "待确认" else "中等把握"
        provenance = read_asset_provenance(assets_dir / key)
        items.append(
            MaterialSummaryItem(
                title=Path(key).name or key,
                usage_label=usage_label,
                confidence_label=confidence_label,
                status_label=status_label,
                preview_url=url_path,
                theme_id=theme_id,
                asset_type=asset_type,
                source_key=key,
                origin_url=str(provenance.get("origin_url") or ""),
            )
        )
        if url_path:
            seen_urls.add(url_path)

    for url_path, value in theme_assets.items():
        if url_path in seen_urls:
            continue
        asset_type = str(value.get("type") or "").strip().lower()
        role = str(value.get("role") or value.get("source_key") or "").strip()
        usage_label = _usage_label(role=role, asset_type=asset_type)
        status_label = "待确认" if usage_label == "未确定" else "已识别"
        source_key = str(value.get("source_key") or "").strip()
        provenance = read_asset_provenance(assets_dir / source_key) if source_key else {}
        items.append(
            MaterialSummaryItem(
                title=Path(url_path).name or str(value.get("source_key") or "material"),
                usage_label=usage_label,
                confidence_label="低把握" if status_label == "待确认" else "中等把握",
                status_label=status_label,
                preview_url=url_path,
                theme_id=str(value.get("theme_id") or "").strip(),
                asset_type=asset_type,
                source_key=source_key,
                origin_url=str(provenance.get("origin_url") or ""),
            )
        )

    return items


def _classifier_status(settings) -> str:
    return _classifier_status_cached(
        bool(settings.VISION_CLASSIFIER_ENABLED),
        str(settings.VISION_CLASSIFIER_MODEL or "").strip() or "unknown",
        str(settings.VISION_CLASSIFIER_BASE_URL or "").rstrip("/"),
    )


@lru_cache(maxsize=1)
def _classifier_status_cached(enabled: bool, model: str, base_url: str) -> str:
    if not enabled:
        return "模型：关闭"
    if not base_url:
        return f"模型：未配置 {model}"
    try:
        with urllib.request.urlopen(base_url + "/api/tags", timeout=0.25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return f"模型：离线 {model}"
    names = {
        str(value).strip()
        for item in payload.get("models", [])
        if isinstance(item, dict)
        for value in (item.get("name"), item.get("model"))
        if value
    }
    return f"模型：可用 {model}" if model in names else f"模型：未安装 {model}"


def _read_theme_assets(themes_dir: Path) -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for theme_file in sorted(themes_dir.glob("*.json")):
        payload = _read_json_object(theme_file)
        physical_assets = payload.get("physical_assets")
        if not isinstance(physical_assets, dict):
            continue
        theme_id = str(payload.get("theme_name") or theme_file.stem).strip()
        for source_key, value in physical_assets.items():
            if not isinstance(value, dict):
                continue
            url = str(value.get("url") or "").strip()
            if not url:
                continue
            record = dict(value)
            record["source_key"] = str(source_key)
            record["theme_id"] = theme_id
            record["role"] = str(value.get("role") or _role_from_asset_key(str(source_key))).strip()
            assets[url] = record
    return assets


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rehearsal_status(rehearsal_root: Path | None) -> tuple[str, str]:
    root = rehearsal_root or _env_rehearsal_root()
    if root is None:
        return "", ""
    report_root = root / "doraemon-rehearsal-reports"
    verification = _first_report(
        report_root,
        "static-compatible-verification-report.json",
        "live-verification-report.json",
        "verification-report.json",
    )
    dom_check = _first_report(
        report_root,
        "static-compatible-dom-resource-check.json",
        "live-dom-resource-check.json",
        "dom-resource-check.json",
    )
    preview_path = root / "doraemon-hydration.html"
    remote_urls = _list_value(verification.get("remote_urls"))
    unregistered_assets = _list_value(verification.get("unregistered_assets"))
    if (
        preview_path.is_file()
        and verification.get("status") == "verified"
        and dom_check.get("status") == "passed"
        and not remote_urls
        and not unregistered_assets
    ):
        return "ready", "演练：可预览"
    if report_root.exists():
        return "blocked", "演练：需处理"
    return "", ""


def _project_picker_preview(path: Path | None) -> dict[str, Any]:
    preview_source = "argument" if path is not None else "env"
    preview_path = path or _env_project_picker_preview_path()
    if preview_path is None:
        return _project_picker_preview_unset()
    preview_path = preview_path.expanduser()
    if not preview_path.is_file():
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_missing",
            preview_source=preview_source,
            preview_file_status="missing",
        )
    payload = _read_json_object(preview_path)
    if payload.get("preview_type") != "doraemon_real_project_picker_ui_preview":
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_unreadable_or_invalid",
            preview_source=preview_source,
            preview_file_status="invalid",
        )
    if not _non_executable_preview(payload):
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_not_non_executable",
            preview_source=preview_source,
            preview_file_status="invalid",
        )
    panel_summary = payload.get("panel_summary")
    if not isinstance(panel_summary, dict):
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_missing_panel_summary",
            preview_source=preview_source,
            preview_file_status="invalid",
        )
    if not _non_executable_contract(panel_summary.get("contract")):
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_unsafe_panel_contract",
            preview_source=preview_source,
            preview_file_status="invalid",
        )

    picker_intent = payload.get("picker_intent")
    if not isinstance(picker_intent, dict):
        picker_intent = {}
    confirmation_intent = payload.get("confirmation_intent")
    if not isinstance(confirmation_intent, dict):
        confirmation_intent = {}
    confirmation_prompt = confirmation_intent.get("ui_prompt")
    if not isinstance(confirmation_prompt, dict):
        confirmation_prompt = {}
    execution_readiness = payload.get("execution_readiness")
    if not isinstance(execution_readiness, dict):
        execution_readiness = {}
    if execution_readiness and not _non_executable_readiness(execution_readiness):
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_unsafe_execution_readiness",
            preview_source=preview_source,
            preview_file_status="invalid",
        )
    execution_result = payload.get("execution_result")
    if not isinstance(execution_result, dict):
        execution_result = {}
    if execution_result and not _display_safe_execution_result(execution_result):
        return _project_picker_preview_unavailable(
            preview_path=preview_path,
            reason="preview_file_unsafe_execution_result",
            preview_source=preview_source,
            preview_file_status="invalid",
        )

    panel_status_label = str(panel_summary.get("panel_status_label") or "").strip()
    project_status_label = str(picker_intent.get("project_status_label") or "").strip()
    body_line = panel_status_label.splitlines()[0] if panel_status_label else project_status_label
    primary_label = str(panel_summary.get("primary_label") or "").strip()
    primary_action = str(panel_summary.get("primary_action") or "").strip()
    picker_status = str(panel_summary.get("picker_status") or "").strip()
    blockers = _list_text(panel_summary.get("blockers"))
    readiness_blockers = _list_text(execution_readiness.get("blocked_by"))
    readiness_status = str(execution_readiness.get("readiness_status") or "").strip()
    result_status = str(execution_result.get("adapter_status") or execution_result.get("status") or "").strip()
    result_action = str(execution_result.get("action") or execution_result.get("requested_action") or "").strip()
    result_executed = bool(execution_result.get("executed", False))
    result_error_code = str(execution_result.get("gui_error_code") or "").strip()
    result_error_message = str(execution_result.get("gui_error_message") or "").strip()
    result_blockers = _list_text(execution_result.get("blocked_by"))
    result_line = _execution_result_line(
        status=result_status,
        action=result_action,
        executed=result_executed,
    )
    primary_enabled = picker_status == "selection_ready" and not blockers
    if execution_readiness:
        primary_enabled = bool(execution_readiness.get("button_enabled", False))
        if readiness_blockers:
            blockers = readiness_blockers
        primary_label = str(execution_readiness.get("primary_label") or primary_label).strip()
    if execution_result:
        primary_enabled = False
        if result_blockers:
            blockers = result_blockers
        elif result_error_code:
            blockers = [result_error_code]
    selected_project_root = str(panel_summary.get("selected_project_root") or picker_intent.get("selected_project_root") or "").strip()
    project_name = str(panel_summary.get("project_name") or picker_intent.get("selected_project_name") or Path(selected_project_root).name or "当前项目").strip()
    title = "真实项目选择"
    loaded_at = _utc_now_iso()
    summary_line = _picker_summary_line(
        project_name=project_name,
        operation_count=_safe_int(panel_summary.get("operation_count")),
    )
    operation_count = _safe_int(panel_summary.get("operation_count"))
    confirmation_title = str(confirmation_prompt.get("title") or "").strip()
    confirmation_body_line = str(confirmation_prompt.get("body_line") or "").strip()
    confirmation_summary_line = str(confirmation_prompt.get("summary_line") or "").strip()
    next_step = str(payload.get("next_step") or panel_summary.get("next_step") or "").strip()
    display_text = _project_picker_display_text(
        body_line=body_line,
        primary_label=primary_label,
        primary_enabled=primary_enabled,
        blockers=blockers,
        confirmation_title=confirmation_title,
        readiness_status=readiness_status,
        execution_result_line=result_line,
    )
    tooltip = _project_picker_tooltip(
        title=title,
        body_line=body_line,
        summary_line=summary_line,
        primary_label=primary_label,
        primary_enabled=primary_enabled,
        secondary_label=str(confirmation_prompt.get("secondary_label") or "查看详情").strip(),
        blockers=blockers,
        confirmation_title=confirmation_title,
        confirmation_body_line=confirmation_body_line,
        confirmation_summary_line=confirmation_summary_line,
        next_step=next_step,
        preview_path=preview_path.as_posix(),
        preview_file_status="loaded",
        preview_loaded_at=loaded_at,
        readiness_status=readiness_status,
        execution_button_enabled=bool(execution_readiness.get("button_enabled", False)),
        execution_would_execute=bool(execution_readiness.get("would_execute", False)),
        operation_paths_hash=str(execution_readiness.get("operation_paths_hash") or "").strip(),
        dry_run_only=bool(execution_readiness.get("dry_run_only", False)),
        execution_result_status=result_status,
        execution_result_action=result_action,
        execution_result_executed=result_executed,
        execution_result_error_code=result_error_code,
        execution_result_error_message=result_error_message,
    )
    status_line = _project_picker_status_line(
        file_status="loaded",
        source=preview_source,
        path=preview_path.as_posix(),
        loaded_at=loaded_at,
    )
    return {
        "title": title,
        "preview_available": True,
        "preview_path": preview_path.as_posix(),
        "preview_error": "",
        "preview_source": preview_source,
        "preview_file_exists": True,
        "preview_file_status": "loaded",
        "preview_loaded_at": loaded_at,
        "status_line": status_line,
        "body_line": body_line,
        "summary_line": summary_line,
        "panel_status_label": panel_status_label or body_line,
        "primary_label": primary_label,
        "primary_action": primary_action,
        "primary_enabled": primary_enabled,
        "secondary_label": str(confirmation_prompt.get("secondary_label") or "查看详情").strip(),
        "blocked_reason_count": len(blockers),
        "blocked_reasons": blockers,
        "confirmation_title": confirmation_title,
        "confirmation_body_line": confirmation_body_line,
        "confirmation_summary_line": confirmation_summary_line,
        "next_step": next_step,
        "contract": {
            "write_allowed": False,
            "execute_allowed": False,
            "worker_allowed": False,
            "saga_mutation_allowed": False,
            "task_qa_publish_allowed": False,
        },
        "display_text": display_text,
        "tooltip": tooltip,
        "selected_project_root": selected_project_root,
        "project_name": project_name,
        "project_state": str(panel_summary.get("project_state") or picker_intent.get("project_state") or "").strip(),
        "operation_count": operation_count,
        "execution_readiness_available": bool(execution_readiness),
        "execution_readiness_status": readiness_status,
        "execution_button_enabled": bool(execution_readiness.get("button_enabled", False)),
        "execution_would_execute": bool(execution_readiness.get("would_execute", False)),
        "operation_paths_hash": str(execution_readiness.get("operation_paths_hash") or "").strip(),
        "execution_result_available": bool(execution_result),
        "execution_result_status": result_status,
        "execution_result_action": result_action,
        "execution_result_executed": result_executed,
        "execution_result_error_code": result_error_code,
        "execution_result_error_message": result_error_message,
    }


def _project_picker_preview_unset() -> dict[str, Any]:
    reason = "preview_path_unset"
    body_line = "Project Picker：未设置预览文件"
    primary_label = "查看详情"
    tooltip = "\n".join(
        (
            "真实项目选择",
            body_line,
            f"原因：{reason}",
            "设置 HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH 后刷新面板。",
            "刷新只会重新读取本地 preview file，不请求 HTTP，也不会执行写入。",
            "再次确认前不会执行写入",
        )
    )
    return {
        "title": "真实项目选择",
        "preview_available": False,
        "preview_path": "",
        "preview_error": reason,
        "preview_source": "unset",
        "preview_file_exists": False,
        "preview_file_status": "unset",
        "preview_loaded_at": "",
        "status_line": "Project Picker：预览未设置 · 刷新只读",
        "body_line": body_line,
        "summary_line": "HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH",
        "panel_status_label": body_line,
        "primary_label": primary_label,
        "primary_enabled": False,
        "secondary_label": "查看详情",
        "blocked_reason_count": 1,
        "blocked_reasons": [reason],
        "confirmation_title": "",
        "confirmation_body_line": "",
        "confirmation_summary_line": "",
        "next_step": "set_project_picker_preview_path",
        "contract": _project_picker_no_execution_contract(),
        "display_text": f"{body_line}\n{primary_label}（不可用）\n{reason}",
        "tooltip": tooltip,
        "selected_project_root": "",
        "project_name": "",
        "project_state": "needs_review",
    }


def _project_picker_preview_unavailable(
    *,
    preview_path: Path,
    reason: str,
    preview_source: str,
    preview_file_status: str,
) -> dict[str, Any]:
    body_line = "Project Picker：预览文件不可用"
    primary_label = "查看详情"
    status_line = _project_picker_status_line(
        file_status=preview_file_status,
        source=preview_source,
        path=preview_path.as_posix(),
        loaded_at="",
    )
    tooltip = "\n".join(
        (
            "真实项目选择",
            body_line,
            f"路径：{preview_path.as_posix()}",
            f"来源：{preview_source}",
            f"状态：{preview_file_status}",
            f"原因：{reason}",
            "请重新生成 preview file handoff，或清空 HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH。",
            "刷新只会重新读取本地 preview file，不请求 HTTP，也不会执行写入。",
            "再次确认前不会执行写入",
        )
    )
    return {
        "title": "真实项目选择",
        "preview_available": False,
        "preview_path": preview_path.as_posix(),
        "preview_error": reason,
        "preview_source": preview_source,
        "preview_file_exists": preview_path.is_file(),
        "preview_file_status": preview_file_status,
        "preview_loaded_at": "",
        "status_line": status_line,
        "body_line": body_line,
        "summary_line": preview_path.name,
        "panel_status_label": body_line,
        "primary_label": primary_label,
        "primary_enabled": False,
        "secondary_label": "查看详情",
        "blocked_reason_count": 1,
        "blocked_reasons": [reason],
        "confirmation_title": "",
        "confirmation_body_line": "",
        "confirmation_summary_line": "",
        "next_step": "regenerate_project_picker_preview_file",
        "contract": _project_picker_no_execution_contract(),
        "display_text": f"{body_line}\n{primary_label}（不可用）\n{reason}",
        "tooltip": tooltip,
        "selected_project_root": "",
        "project_name": "",
        "project_state": "needs_review",
    }


def _env_project_picker_preview_path() -> Path | None:
    import os

    value = (
        os.environ.get("HAYPILE_PROJECT_PICKER_UI_PREVIEW_PATH", "").strip()
        or os.environ.get("DORAEMON_PROJECT_PICKER_UI_PREVIEW_PATH", "").strip()
    )
    return Path(value) if value else None


def _non_executable_preview(payload: dict[str, Any]) -> bool:
    required_false = (
        "write_allowed",
        "execute_allowed",
        "auto_apply_allowed",
        "auto_rollback_allowed",
        "worker_allowed",
        "saga_mutation_allowed",
        "task_qa_publish_allowed",
    )
    return payload.get("non_executable") is True and all(payload.get(field) is False for field in required_false)


def _non_executable_contract(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required_false = (
        "write_allowed",
        "execution_allowed",
        "apply_allowed",
        "rollback_allowed",
        "auto_apply_allowed",
        "auto_rollback_allowed",
        "worker_allowed",
        "saga_mutation_allowed",
        "task_qa_publish_allowed",
    )
    return all(value.get(field) is False for field in required_false)


def _non_executable_readiness(value: dict[str, Any]) -> bool:
    required_false = (
        "write_allowed",
        "apply_allowed",
        "rollback_allowed",
        "execute_allowed",
        "auto_apply_allowed",
        "auto_rollback_allowed",
        "worker_allowed",
        "saga_mutation_allowed",
        "task_qa_publish_allowed",
    )
    return (
        value.get("dry_run_only") is True
        and value.get("button_enabled") is False
        and value.get("would_execute") is False
        and value.get("non_executable") is True
        and all(value.get(field) is False for field in required_false)
    )


def _display_safe_execution_result(value: dict[str, Any]) -> bool:
    required_false = (
        "accepted_for_real_project_execution",
        "apply_allowed",
        "rollback_allowed",
        "execute_allowed",
        "auto_apply_allowed",
        "auto_rollback_allowed",
        "worker_allowed",
        "saga_mutation_allowed",
        "task_qa_publish_allowed",
        "full_argus_inspect_allowed",
    )
    result_type_ok = value.get("result_type") == "doraemon_real_project_picker_real_execution_adapter_result"
    status = str(value.get("adapter_status") or value.get("status") or "").strip()
    executed = bool(value.get("executed", False))
    write_allowed = bool(value.get("write_allowed", False) or value.get("real_project_write_allowed", False))
    if not result_type_ok or status not in {"blocked", "applied", "rolled_back", "failed_needs_manual_review"}:
        return False
    if write_allowed and not executed:
        return False
    return all(value.get(field) is False for field in required_false)


def _picker_summary_line(*, project_name: str, operation_count: int) -> str:
    if operation_count > 0:
        return f"{project_name} · {operation_count} 项"
    return project_name


def _project_picker_display_text(
    *,
    body_line: str,
    primary_label: str,
    primary_enabled: bool,
    blockers: list[str],
    confirmation_title: str,
    readiness_status: str,
    execution_result_line: str,
) -> str:
    lines = [line for line in (body_line, primary_label) if line]
    if primary_label and not primary_enabled:
        lines[-1] = f"{primary_label}（不可用）"
    if execution_result_line:
        lines.append(execution_result_line)
    if blockers:
        lines.append(blockers[0])
    elif readiness_status == "dry_run_ready":
        lines.append("干跑检查就绪")
    elif confirmation_title:
        lines.append(confirmation_title)
    return "\n".join(lines)


def _project_picker_tooltip(
    *,
    title: str,
    body_line: str,
    summary_line: str,
    primary_label: str,
    primary_enabled: bool,
    secondary_label: str,
    blockers: list[str],
    confirmation_title: str,
    confirmation_body_line: str,
    confirmation_summary_line: str,
    next_step: str,
    preview_path: str,
    preview_file_status: str,
    preview_loaded_at: str,
    readiness_status: str,
    execution_button_enabled: bool,
    execution_would_execute: bool,
    operation_paths_hash: str,
    dry_run_only: bool,
    execution_result_status: str,
    execution_result_action: str,
    execution_result_executed: bool,
    execution_result_error_code: str,
    execution_result_error_message: str,
) -> str:
    lines = [
        title,
        body_line,
        summary_line,
        f"预览状态：{preview_file_status}",
        f"预览文件：{preview_path}",
        f"主动作：{primary_label or '查看详情'}",
        f"主动作可用：{'是' if primary_enabled else '否'}",
    ]
    if preview_loaded_at:
        lines.append(f"读取时间：{preview_loaded_at}")
    if readiness_status:
        lines.append(f"执行检查：{readiness_status}")
        lines.append(f"执行按钮可用：{'是' if execution_button_enabled else '否'}")
        lines.append(f"会执行写入：{'是' if execution_would_execute else '否'}")
    if operation_paths_hash:
        lines.append(f"操作路径哈希：{operation_paths_hash}")
    if dry_run_only:
        lines.append("当前为干跑检查，不会执行写入。")
    if execution_result_status:
        lines.append(f"执行结果：{execution_result_status}")
        lines.append(f"执行动作：{execution_result_action or 'unknown'}")
        lines.append(f"已执行：{'是' if execution_result_executed else '否'}")
    if execution_result_error_code:
        lines.append(f"错误代码：{execution_result_error_code}")
    if execution_result_error_message:
        lines.append(f"错误信息：{execution_result_error_message}")
    if secondary_label:
        lines.append(f"次动作：{secondary_label}")
    if blockers:
        lines.append(f"阻塞原因：{len(blockers)} 项")
        lines.extend(blockers[:3])
    if confirmation_title:
        lines.append(f"确认提示：{confirmation_title}")
    if confirmation_body_line:
        lines.append(confirmation_body_line)
    if confirmation_summary_line:
        lines.append(confirmation_summary_line)
    if next_step:
        lines.append(f"下一步：{next_step}")
    lines.append("刷新只会重新读取本地 preview file")
    lines.append("再次确认前不会执行写入")
    return "\n".join(line for line in lines if line)


def _execution_result_line(*, status: str, action: str, executed: bool) -> str:
    if not status:
        return ""
    if status == "applied":
        return "结果：已重新投放"
    if status == "rolled_back":
        return "结果：已回滚"
    if status == "blocked":
        label = "重新投放" if action == "reapply" else "撤回"
        return f"结果：{label}被阻止"
    if status == "failed_needs_manual_review":
        return "结果：需人工复核"
    return "结果：未执行" if not executed else "结果：已返回"


def _project_picker_confirmation_prompt(
    preview: dict[str, Any],
) -> tuple[bool, str, str, str, str, str, str]:
    if not preview.get("preview_available"):
        return False, "", "", "", "", "", ""
    if not preview.get("primary_enabled"):
        return False, "", "", "", "", "", ""
    if preview.get("blocked_reason_count"):
        return False, "", "", "", "", "", ""
    if preview.get("execution_readiness_available") or preview.get("execution_result_available"):
        return False, "", "", "", "", "", ""
    selected_project_root = str(preview.get("selected_project_root") or "").strip()
    if not selected_project_root or not Path(selected_project_root).expanduser().is_absolute():
        return False, "", "", "", "", "", ""
    project_state = str(preview.get("project_state") or "").strip()
    primary_action = str(preview.get("primary_action") or "").strip()
    operation_count = _safe_int(preview.get("operation_count"))
    if operation_count <= 0:
        return False, "", "", "", "", "", ""

    if primary_action == "reapply" and project_state == "rolled_back":
        action = "reapply"
        primary_label = "重新投放"
        title = "重新投放？"
    elif primary_action == "withdrawal" and project_state == "applied_verified":
        action = "rollback"
        primary_label = "撤回投放"
        title = "撤回投放？"
    else:
        return False, "", "", "", "", "", ""

    project_name = str(preview.get("project_name") or Path(selected_project_root).name or "当前项目").strip()
    return (
        True,
        action,
        primary_label,
        title,
        project_name,
        f"{operation_count} 项",
        "再次确认后执行",
    )


def _project_picker_status_line(*, file_status: str, source: str, path: str, loaded_at: str) -> str:
    if file_status == "loaded":
        suffix = f" · {loaded_at}" if loaded_at else ""
        return f"Project Picker：已读取 · {source}{suffix}"
    if file_status == "missing":
        return f"Project Picker：文件缺失 · {source} · {path}"
    if file_status == "invalid":
        return f"Project Picker：文件无效 · {source} · {path}"
    return "Project Picker：预览未设置 · 刷新只读"


def _project_picker_no_execution_contract() -> dict[str, bool]:
    return {
        "write_allowed": False,
        "execute_allowed": False,
        "worker_allowed": False,
        "saga_mutation_allowed": False,
        "task_qa_publish_allowed": False,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_rehearsal_root() -> Path | None:
    import os

    value = (
        os.environ.get("HAYPILE_REHEARSAL_ROOT", "").strip()
        or os.environ.get("DORAEMON_REHEARSAL_ROOT", "").strip()
    )
    return Path(value) if value else None


def _real_project_status(real_project_root: Path | None) -> tuple[str, str, int]:
    root = real_project_root
    if root is None:
        return "", "", 0
    report_root = root / "doraemon-rehearsal-reports"
    apply_report = _read_json_object(report_root / "real-project-minimal-apply-report.json")
    verification = _read_json_object(report_root / "real-project-minimal-post-apply-verification.json")
    rollback = _read_json_object(report_root / "real-project-minimal-rollback-report.json")
    written_files = _list_text(apply_report.get("written_files"))
    live_written = [path for path in written_files if (root / path).exists()]
    remaining_written = _list_text(rollback.get("remaining_written_files"))
    remote_urls = _list_value(verification.get("remote_urls"))
    unregistered_assets = _list_value(verification.get("unregistered_assets"))
    if rollback.get("status") == "restored" and rollback.get("passed") is True:
        if not remaining_written and not live_written:
            return "rolled_back", "真实项目：已撤回", len(written_files)
        return "rollback_incomplete", "真实项目：撤回需处理", len(written_files)
    if verification.get("status") == "verified" and verification.get("passed") is True:
        if written_files and live_written and not remote_urls and not unregistered_assets:
            return "applied_verified", "真实项目：已投放", len(written_files)
        return "needs_review", "真实项目：需处理", len(written_files)
    if apply_report.get("status") == "applied" and apply_report.get("passed") is True:
        return "applied_needs_verification", "真实项目：待验收", len(written_files)
    if report_root.exists():
        return "needs_review", "真实项目：需处理", len(written_files)
    return "", "", 0


def _panel_action_label(*, real_project_status: str, operation_count: int) -> str:
    if operation_count <= 0:
        return ""
    if real_project_status == "rolled_back":
        return f"可确认重新投放 {operation_count} 项"
    if real_project_status == "applied_verified":
        return f"可确认撤回投放 {operation_count} 项"
    if real_project_status == "applied_needs_verification":
        return "待完成验收"
    return ""


def _panel_display_text(
    *,
    real_project_status: str,
    rehearsal_status: str,
    operation_count: int,
) -> str:
    if real_project_status == "rolled_back" and operation_count > 0:
        return f"已撤回 · 投放 {operation_count}"
    if real_project_status == "applied_verified" and operation_count > 0:
        return f"已投放 · 撤回 {operation_count}"
    if real_project_status == "applied_needs_verification":
        return "待验收"
    if real_project_status in {"needs_review", "rollback_incomplete"}:
        return "需处理"
    if rehearsal_status == "ready":
        return "可预览"
    if rehearsal_status == "blocked":
        return "需处理"
    return ""


def _project_display_label(real_project_root: Path | None) -> str:
    if real_project_root is None:
        return ""
    return f"● {real_project_root.name or '当前项目'}"


def _confirmation_prompt(
    *,
    real_project_root: Path | None,
    real_project_status: str,
    real_project_status_label: str,
    operation_count: int,
) -> tuple[bool, str, str, str, str, str, str]:
    if operation_count <= 0:
        return False, "", "", "", "", "", ""
    if real_project_status == "rolled_back":
        action = "reapply"
        primary_label = "重新投放"
        title = "重新投放？"
    elif real_project_status == "applied_verified":
        action = "rollback"
        primary_label = "撤回投放"
        title = "撤回投放？"
    else:
        return False, "", "", "", "", "", ""
    project_name = real_project_root.name if real_project_root is not None else "当前项目"
    return (
        True,
        action,
        primary_label,
        title,
        project_name,
        f"{operation_count} 项",
        "再次确认后执行",
    )

def _first_report(report_root: Path, *names: str) -> dict[str, Any]:
    for name in names:
        report = _read_json_object(report_root / name)
        if report:
            return report
    return {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_text(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip().replace("\\", "/") for item in value if str(item or "").strip()]


def _safe_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _usage_label(*, role: str, asset_type: str) -> str:
    normalized_role = _role_from_asset_key(role)
    if asset_type == "audio":
        return "音频"
    if normalized_role in ROLE_LABELS:
        return ROLE_LABELS[normalized_role]
    if asset_type in ROLE_LABELS:
        return ROLE_LABELS[asset_type]
    return "未确定"


def _role_from_asset_key(key: str) -> str:
    text = str(key or "").strip().lower()
    for role in ("main_background", "hero_image", "icon", "texture"):
        if role in text:
            return role
    if "unknown" in text:
        return "unknown"
    return text


def _theme_from_key(key: str) -> str:
    parts = Path(key).parts
    return parts[0] if parts else ""


def _item_mtime(item: MaterialSummaryItem, assets_dir: Path) -> float:
    source_key = item.source_key.strip()
    if not source_key:
        return 0.0
    path = assets_dir / source_key
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
