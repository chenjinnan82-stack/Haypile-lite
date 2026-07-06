from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


APPLY_REPORT_NAME = "real-project-minimal-apply-report.json"
VERIFICATION_REPORT_NAME = "real-project-minimal-post-apply-verification.json"
ROLLBACK_REPORT_NAME = "real-project-minimal-rollback-report.json"
ROLLBACK_MANIFEST_PATH = ".doraemon/rollback/doraemon-real-project-minimal-apply.json"
REPORT_DIR = "doraemon-rehearsal-reports"


class HaypileRealProjectOperationError(ValueError):
    """Raised when a Haypile real project operation cannot run safely."""


def execute_haypile_minimal_real_project_reapply(
    *,
    project_root: str | Path,
    human_confirmed: bool,
) -> dict[str, Any]:
    if human_confirmed is not True:
        raise HaypileRealProjectOperationError("human confirmation is required")
    context = _load_context(project_root)
    state = _current_state(context)
    if state != "rolled_back":
        raise HaypileRealProjectOperationError("reapply requires rolled_back state")

    source_root = _source_rehearsal_root(context)
    written_files: list[str] = []
    for entry in context["entries"]:
        path_ref = _safe_relative_path(entry.get("path_ref"))
        target = _resolve_under(context["project_root"], path_ref)
        source = _resolve_under(source_root, path_ref)
        if target.exists():
            raise HaypileRealProjectOperationError("reapply target already exists")
        if not source.is_file():
            raise HaypileRealProjectOperationError("reapply source file is missing")
        expected_sha = str(entry.get("source_sha256") or "").strip()
        if expected_sha and _sha256(source) != expected_sha:
            raise HaypileRealProjectOperationError("reapply source hash mismatch")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        written_files.append(path_ref)

    timestamp = _timestamp()
    apply_report = dict(context["apply_report"])
    apply_report.update(
        {
            "status": "applied",
            "passed": True,
            "reapplied_at": timestamp,
            "user_project_root": context["project_root"].as_posix(),
            "source_rehearsal_root": source_root.as_posix(),
            "written_files": written_files,
            "auto_apply_allowed": False,
        }
    )
    verification_report = {
        "report_type": "doraemon_real_project_minimal_post_apply_verification",
        "version": "doraemon_real_project_minimal_post_apply_verification.v1",
        "status": "verified",
        "passed": True,
        "verified_at": timestamp,
        "remote_urls": [],
        "unregistered_assets": [],
        "written_files": written_files,
        "local_file_load_passed": True,
    }
    rollback_report = dict(context["rollback_report"])
    rollback_report.update(
        {
            "status": "superseded_by_reapply",
            "passed": True,
            "superseded_at": timestamp,
            "remaining_written_files": written_files,
        }
    )
    _write_json(context["report_root"] / APPLY_REPORT_NAME, apply_report)
    _write_json(context["report_root"] / VERIFICATION_REPORT_NAME, verification_report)
    _write_json(context["report_root"] / ROLLBACK_REPORT_NAME, rollback_report)
    return {
        "operation_type": "doraemon_minimal_real_project_reapply",
        "version": "doraemon_minimal_real_project_reapply.v1",
        "status": "applied",
        "passed": True,
        "project_root": context["project_root"].as_posix(),
        "source_rehearsal_root": source_root.as_posix(),
        "written_files": written_files,
        "operation_count": len(written_files),
        "write_scope": "explicit_user_selected_project_root",
        "auto_apply_allowed": False,
        "requires_human_confirmation": True,
    }


def execute_haypile_minimal_real_project_rollback(
    *,
    project_root: str | Path,
    human_confirmed: bool,
) -> dict[str, Any]:
    if human_confirmed is not True:
        raise HaypileRealProjectOperationError("human confirmation is required")
    context = _load_context(project_root)
    state = _current_state(context)
    if state != "applied_verified":
        raise HaypileRealProjectOperationError("rollback requires applied_verified state")

    removed_files: list[str] = []
    missing_files: list[str] = []
    preserved_files: list[str] = []
    for entry in context["entries"]:
        path_ref = _safe_relative_path(entry.get("path_ref"))
        target = _resolve_under(context["project_root"], path_ref)
        if entry.get("existed_before") is True:
            preserved_files.append(path_ref)
            continue
        if target.is_file():
            target.unlink()
            removed_files.append(path_ref)
            _remove_empty_parents(target.parent, context["project_root"])
        else:
            missing_files.append(path_ref)

    remaining_written_files = [
        _safe_relative_path(entry.get("path_ref"))
        for entry in context["entries"]
        if _resolve_under(context["project_root"], _safe_relative_path(entry.get("path_ref"))).exists()
    ]
    rollback_report = {
        "report_type": "doraemon_real_project_minimal_rollback_report",
        "version": "doraemon_real_project_minimal_rollback_report.v1",
        "status": "restored",
        "passed": True,
        "restored_at": _timestamp(),
        "removed_files": removed_files,
        "missing_files": missing_files,
        "preserved_files": preserved_files,
        "remaining_written_files": remaining_written_files,
        "auto_rollback_allowed": False,
    }
    _write_json(context["report_root"] / ROLLBACK_REPORT_NAME, rollback_report)
    return {
        "operation_type": "doraemon_minimal_real_project_rollback",
        "version": "doraemon_minimal_real_project_rollback.v1",
        "status": "restored",
        "passed": True,
        "project_root": context["project_root"].as_posix(),
        "removed_files": removed_files,
        "missing_files": missing_files,
        "remaining_written_files": remaining_written_files,
        "operation_count": len(removed_files),
        "write_scope": "explicit_user_selected_project_root",
        "auto_rollback_allowed": False,
        "requires_human_confirmation": True,
    }


def _load_context(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve(strict=False)
    report_root = _resolve_under(root, REPORT_DIR)
    rollback_manifest_path = _resolve_under(root, ROLLBACK_MANIFEST_PATH)
    apply_report = _read_json(report_root / APPLY_REPORT_NAME)
    verification_report = _read_json(report_root / VERIFICATION_REPORT_NAME)
    rollback_report = _read_json(report_root / ROLLBACK_REPORT_NAME)
    rollback_manifest = _read_json(rollback_manifest_path)
    entries = rollback_manifest.get("entries")
    if not root.is_dir():
        raise HaypileRealProjectOperationError("project root is missing")
    if not isinstance(entries, list) or not entries:
        raise HaypileRealProjectOperationError("rollback manifest entries are missing")
    return {
        "project_root": root,
        "report_root": report_root,
        "apply_report": apply_report,
        "verification_report": verification_report,
        "rollback_report": rollback_report,
        "rollback_manifest": rollback_manifest,
        "entries": entries,
    }


def _current_state(context: dict[str, Any]) -> str:
    project_root = context["project_root"]
    live_written_files = [
        _safe_relative_path(entry.get("path_ref"))
        for entry in context["entries"]
        if _resolve_under(project_root, _safe_relative_path(entry.get("path_ref"))).exists()
    ]
    rollback_report = context["rollback_report"]
    verification_report = context["verification_report"]
    apply_report = context["apply_report"]
    if rollback_report.get("status") == "restored" and rollback_report.get("passed") is True:
        remaining = _string_list(rollback_report.get("remaining_written_files"))
        return "rolled_back" if not remaining and not live_written_files else "rollback_incomplete"
    if verification_report.get("status") == "verified" and verification_report.get("passed") is True:
        return "applied_verified" if live_written_files else "verification_without_live_files"
    if apply_report.get("status") == "applied" and apply_report.get("passed") is True:
        return "applied_needs_verification"
    return "needs_review"


def _source_rehearsal_root(context: dict[str, Any]) -> Path:
    source = (
        context["apply_report"].get("source_rehearsal_root")
        or context["rollback_manifest"].get("source_rehearsal_root")
    )
    text = str(source or "").strip()
    if not text:
        raise HaypileRealProjectOperationError("source rehearsal root is missing")
    root = Path(text).resolve(strict=False)
    if not root.is_dir():
        raise HaypileRealProjectOperationError("source rehearsal root is missing")
    return root


def _safe_relative_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    parts = Path(text).parts
    if not text or Path(text).is_absolute() or "." in parts or ".." in parts:
        raise HaypileRealProjectOperationError("operation path must be a safe relative path")
    return text


def _resolve_under(root: Path, relative_path: str | Path) -> Path:
    target = (root / str(relative_path)).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HaypileRealProjectOperationError("operation path escaped project root") from exc
    return target


DoraemonRealProjectOperationError = HaypileRealProjectOperationError


def execute_doraemon_minimal_real_project_reapply(
    *,
    project_root: str | Path,
    human_confirmed: bool,
) -> dict[str, Any]:
    return execute_haypile_minimal_real_project_reapply(
        project_root=project_root,
        human_confirmed=human_confirmed,
    )


def execute_doraemon_minimal_real_project_rollback(
    *,
    project_root: str | Path,
    human_confirmed: bool,
) -> dict[str, Any]:
    return execute_haypile_minimal_real_project_rollback(
        project_root=project_root,
        human_confirmed=human_confirmed,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip().replace("\\", "/") for item in value if str(item or "").strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path
    while current != stop_at:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
