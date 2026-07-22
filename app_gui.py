from __future__ import annotations

import asyncio
import ctypes
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import locale
import logging
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4


def _run_early_mode() -> None:
    if __name__ != "__main__":
        return
    args = sys.argv[1:]
    if "--mcp" in args:
        from mcp_server import main as mcp_main

        mcp_main()
        raise SystemExit(0)
    if "--backend" in args:
        from app.core.config import configure_packaged_logging, get_settings

        settings = get_settings()
        configure_packaged_logging("backend", settings.LOG_DIR)
        os.environ["HAYPILE_BACKEND_HOST_ALLOW_START"] = "1"
        from backend_host import main as backend_main

        try:
            exit_code = backend_main()
        except KeyboardInterrupt:
            exit_code = 0
        raise SystemExit(exit_code)


_run_early_mode()

import httpx
from app.core.config import configure_packaged_logging, get_settings, runtime_mode_command
from app.core.exceptions import ResourceExhaustedError
from app.core.file_lock import InterProcessFileLock
from app.core.ipc import send_ipc_request
from app.services.asset_provenance import (
    public_origin_url,
    read_asset_provenance,
    sanitize_provenance,
    write_asset_provenance,
)
from app.services.ai_provider import (
    AIProviderConfig,
    SystemCredentialStore,
    api_authority,
    normalize_api_base_url,
)
from app.services.bundle_service import BundleService
from app.services.json_io import atomic_write_json
from app.services.scanner import (
    AssetScanner,
    ManifestReadinessError,
    mark_manifest_dirty,
    read_manifest_readiness,
)
from app.services.storage_runtime import StorageRuntimeDB
from app.services.style_classifier import StyleClassifier
from app.services.material_summary import build_material_panel_summary
from app.services.media_validator import MediaValidationError, validate_media
from app.services.media_types import AUDIO_CONTENT_TYPE_EXTENSIONS, SUPPORTED_AUDIO_EXTENSIONS
from app.services.real_project_operations import (
    HaypileRealProjectOperationError,
    execute_haypile_minimal_real_project_reapply,
    execute_haypile_minimal_real_project_rollback,
)
from app.services.safe_remote_fetcher import (
    MAX_REMOTE_URLS,
    REMOTE_CONTENT_TYPE_EXTENSIONS,
    SafeFetchError,
    dedupe_remote_urls,
    download_remote_media,
    open_safe_remote,
)
from app.services.theme_registry import ThemeRegistry
from app.services.vfs_storage import VFSStorage
from PySide6.QtCore import (
    QCoreApplication,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QThread,
    QTimer,
    QVariantAnimation,
    Signal,
    QUrl,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPolygonF,
    QPixmap,
    QRadialGradient,
    QResizeEvent,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


_UI_LANGUAGE_CACHE: tuple[tuple[str, ...], str] | None = None
_UI_LANGUAGE_OVERRIDE = "auto"


def _language_from_value(value: str) -> str:
    lowered = value.strip().lower()
    if lowered.startswith("zh"):
        return "zh"
    if lowered.startswith("en"):
        return "en"
    return ""


def _macos_apple_language() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0.3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for raw_line in result.stdout.splitlines():
        value = raw_line.strip().strip('",();')
        detected = _language_from_value(value)
        if detected:
            return detected
    return ""


def ui_language() -> str:
    global _UI_LANGUAGE_CACHE
    if _UI_LANGUAGE_OVERRIDE in {"zh", "en"}:
        return _UI_LANGUAGE_OVERRIDE
    env_values = (
        os.environ.get("HAYPILE_UI_LANG", ""),
        os.environ.get("LC_ALL", ""),
        os.environ.get("LC_MESSAGES", ""),
        os.environ.get("LANGUAGE", ""),
        os.environ.get("LANG", ""),
    )
    if _UI_LANGUAGE_CACHE is not None and _UI_LANGUAGE_CACHE[0] == env_values:
        return _UI_LANGUAGE_CACHE[1]

    language = _language_from_value(env_values[0])
    if not language:
        language = _macos_apple_language()
    candidates = list(env_values[1:])
    try:
        candidates.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    if not language:
        for value in candidates:
            language = _language_from_value(value)
            if language:
                break
    language = language or "en"
    _UI_LANGUAGE_CACHE = (env_values, language)
    return language


def ui_text(zh: str, en: str) -> str:
    return zh if ui_language() == "zh" else en


def set_ui_language(mode: str) -> None:
    global _UI_LANGUAGE_OVERRIDE, _UI_LANGUAGE_CACHE
    _UI_LANGUAGE_OVERRIDE = mode if mode in {"zh", "en"} else "auto"
    _UI_LANGUAGE_CACHE = None


class DroppedMediaHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.audio_urls: set[str] = set()
        self.image_urls: set[str] = set()
        self._audio_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "audio":
            self._audio_depth += 1
        if tag not in {"img", "audio", "source"}:
            return
        values = {name.lower(): value.strip() for name, value in attrs if value}
        source = values.get("src")
        if not source:
            return
        self.urls.append(source)
        media_type = values.get("type", "").lower()
        if tag == "img" or media_type.startswith("image/"):
            self.image_urls.add(source)
        elif tag == "audio" or self._audio_depth or media_type.startswith("audio/"):
            self.audio_urls.add(source)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "audio" and self._audio_depth:
            self._audio_depth -= 1


class RemoteDownloadWorker(QThread):
    finished_signal = Signal(object, str, bool)
    progress_signal = Signal(int, str)

    MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024
    MAX_TOTAL_DOWNLOAD_BYTES = 1024 * 1024 * 1024
    MAX_URLS = MAX_REMOTE_URLS
    TIMEOUT_SECONDS = 15.0
    CONTENT_TYPE_EXTENSIONS = REMOTE_CONTENT_TYPE_EXTENSIONS

    def __init__(self, urls: list[str], incoming_dir: Path) -> None:
        super().__init__()
        self.urls = self._dedupe_urls(urls)
        self.incoming_dir = incoming_dir

    def run(self) -> None:
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.incoming_dir.chmod(0o700)
        downloaded: list[Path] = []
        failed = 0
        too_large = 0
        unsupported = 0
        downloaded_bytes = 0
        total = max(len(self.urls), 1)
        for index, url in enumerate(self.urls, start=1):
            if self.isInterruptionRequested():
                return
            self.progress_signal.emit(int((index - 1) / total * 80) + 8, ui_text(f"获取网页素材 {index}/{total}", f"Fetching web asset {index}/{total}"))
            try:
                remaining = self.MAX_TOTAL_DOWNLOAD_BYTES - downloaded_bytes
                if remaining <= 0:
                    too_large += 1
                    break
                path, reason = self._download_one(url, index, max_bytes=min(self.MAX_FILE_SIZE_BYTES, remaining))
            except (httpx.HTTPError, OSError, SafeFetchError, ValueError) as exc:
                logger.warning(
                    "网页素材下载失败 url=%s error_type=%s",
                    public_origin_url(url),
                    type(exc).__name__,
                )
                failed += 1
                continue
            if path is None:
                if reason == "too_large":
                    too_large += 1
                elif reason == "unsupported":
                    unsupported += 1
                else:
                    failed += 1
                continue
            downloaded.append(path)
            downloaded_bytes += path.stat().st_size

        if not downloaded:
            if unsupported and not failed and not too_large:
                message = ui_text("没有找到可收纳的图片或音频", "No images or audio to store")
            elif too_large and not failed:
                message = ui_text("网页素材超过 500MB", "Web asset is over 500MB")
            else:
                message = ui_text("网页素材无法下载", "Web asset could not be downloaded")
            self.finished_signal.emit([], message, False)
            return
        message = ui_text(f"已获取 {len(downloaded)} 个网页素材", f"Fetched {len(downloaded)} web assets")
        skipped = failed + too_large + unsupported
        if skipped:
            message += ui_text(f"，跳过 {skipped}", f", skipped {skipped}")
        self.progress_signal.emit(95, message)
        self.finished_signal.emit(downloaded, message, True)

    def _download_one(self, url: str, index: int, *, max_bytes: int | None = None) -> tuple[Path | None, str]:
        return download_remote_media(
            url,
            self.incoming_dir,
            index,
            max_bytes=self.MAX_FILE_SIZE_BYTES if max_bytes is None else max_bytes,
            timeout=self.TIMEOUT_SECONDS,
            should_stop=self.isInterruptionRequested,
            opener=open_safe_remote,
        )

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        return dedupe_remote_urls(urls, limit=RemoteDownloadWorker.MAX_URLS)

class IngestWorker(QThread):
    finished_signal = Signal(str, bool)
    progress_signal = Signal(int, str)
    degraded_signal = Signal(str, str, int)
    batch_signal = Signal(str, object)

    SUPPORTED_IMAGE_EXTENSIONS: set[str] = {".png", ".webp", ".svg", ".jpg", ".jpeg"}
    SUPPORTED_AUDIO_EXTENSIONS: set[str] = set(SUPPORTED_AUDIO_EXTENSIONS)
    ALLOWED_IMAGE_MIME: set[str] = {
        "image/png",
        "image/webp",
        "image/jpeg",
        "image/svg+xml",
    }
    ALLOWED_AUDIO_MIME: set[str] = set(AUDIO_CONTENT_TYPE_EXTENSIONS)
    MAX_FILE_SIZE_BYTES: int = 500 * 1024 * 1024
    HASH_CHUNK_SIZE: int = 1024 * 1024

    def __init__(self, files: list[Path], assets_dir: Path, *, ai_enabled: bool | None = None) -> None:
        super().__init__()
        self.files = files
        self.assets_dir = assets_dir
        self.settings = get_settings()
        self.theme_registry = ThemeRegistry()
        self.ai_enabled = bool(ai_enabled)
        self.storage_runtime = StorageRuntimeDB()
        self.vfs_storage = VFSStorage(copy_max_retries=3, copy_base_delay=1.0)
        self.storage_runtime.ensure_ready()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_or_create_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run_coro(self, coro):
        loop = self._get_or_create_loop()
        return loop.run_until_complete(coro)

    def _close_loop(self) -> None:
        if self._loop is None:
            return
        if not self._loop.is_closed():
            self._loop.close()
        self._loop = None

    def run(self) -> None:
        accepted_count = 0
        duplicate_count = 0
        renamed_count = 0
        rejected_count = 0
        recovered_theme_count = 0
        staging_dir = self.settings.STORAGE_DIR / "staging" / "ingest"
        quarantine_dir = self.settings.STORAGE_DIR / "quarantine" / "ingest"
        try:
            mark_manifest_dirty(self.settings.MANIFEST_PATH)
            self.storage_runtime.recover_incomplete_ingest(
                assets_dir=self.assets_dir,
                staging_dir=staging_dir,
                quarantine_dir=quarantine_dir,
            )
            self.storage_runtime.register_legacy_assets(self.assets_dir)
        except (OSError, RuntimeError):
            self.finished_signal.emit(
                ui_text("素材库恢复失败，未开始入库", "Storage recovery failed; import did not start"),
                False,
            )
            self._close_loop()
            return

        batch_id = self.storage_runtime.begin_batch()
        total_files = max(len(self.files), 1)
        self.progress_signal.emit(3, ui_text("正在构建去重索引...", "Building duplicate index..."))
        hash_index = self._build_hash_index()
        if self.isInterruptionRequested():
            self.storage_runtime.interrupt_batch(batch_id)
            self._close_loop()
            return

        for idx, file_path in enumerate(self.files, start=1):
            if self.isInterruptionRequested():
                self.storage_runtime.interrupt_batch(batch_id)
                self._close_loop()
                return
            self.storage_runtime.record_item_discovered(batch_id, idx, file_path.name)
            progress_base = int((idx - 1) / total_files * 84)
            self.progress_signal.emit(
                progress_base + 8,
                ui_text(f"校验文件 {idx}/{total_files}", f"Checking file {idx}/{total_files}"),
            )
            reason = self._preflight_media_file(file_path)
            if reason is not None:
                rejected_count += 1
                self.storage_runtime.reject_item(batch_id, idx, reason)
                continue

            staged = None
            try:
                self.progress_signal.emit(
                    progress_base + 28,
                    ui_text(f"写入暂存区 {idx}/{total_files}", f"Staging file {idx}/{total_files}"),
                )
                staged = self.vfs_storage.stage(
                    file_path,
                    staging_dir,
                    f"{batch_id}-{idx}",
                    should_stop=self.isInterruptionRequested,
                    chunk_size=self.HASH_CHUNK_SIZE,
                )
                validated = validate_media(staged.path)
            except InterruptedError:
                self.storage_runtime.interrupt_item(batch_id, idx, "interrupted")
                self.storage_runtime.interrupt_batch(batch_id)
                self._close_loop()
                return
            except (MediaValidationError, OSError) as exc:
                rejected_count += 1
                if staged is not None:
                    staged.path.unlink(missing_ok=True)
                self.storage_runtime.reject_item(batch_id, idx, type(exc).__name__)
                continue
            file_hash = staged.sha256
            if file_hash in hash_index:
                duplicate_count += 1
                staged.path.unlink(missing_ok=True)
                self.storage_runtime.commit_item(
                    batch_id,
                    idx,
                    sha256_hex=file_hash,
                    src_path=file_path,
                    dst_path=hash_index[file_hash],
                    strategy="duplicate",
                    duplicate=True,
                )
                continue

            theme_id = self.settings.VISION_FALLBACK_THEME
            role = "unknown"

            destination = self._resolve_themed_destination(
                original_name=file_path.name,
                sha256_hex=file_hash,
                theme_id=theme_id,
                media_kind=validated.kind,
                role=role,
            )
            if destination.name != file_path.name:
                renamed_count += 1

            try:
                self.storage_runtime.record_item_staged(
                    batch_id,
                    idx,
                    media_kind=validated.kind,
                    sha256_hex=file_hash,
                    staging_path=staged.path,
                    destination_path=destination,
                )
                self.progress_signal.emit(
                    progress_base + 58,
                    ui_text(f"提交资产 {idx}/{total_files}", f"Committing asset {idx}/{total_files}"),
                )
                strategy = self.vfs_storage.commit_staged(staged.path, destination)
                self.storage_runtime.commit_item(
                    batch_id,
                    idx,
                    sha256_hex=file_hash,
                    src_path=file_path,
                    dst_path=destination,
                    strategy=strategy,
                )
            except (OSError, sqlite3.Error):
                self.storage_runtime.interrupt_item(batch_id, idx, "durable_commit_failed")
                self.storage_runtime.interrupt_batch(batch_id)
                self.finished_signal.emit(
                    ui_text("素材提交中断，重启后将自动恢复", "Asset commit interrupted; restart to recover"),
                    False,
                )
                self._close_loop()
                return

            hash_index[file_hash] = destination
            accepted_count += 1

            try:
                self._persist_asset_provenance(
                    source_path=file_path,
                    destination=destination,
                    sha256_hex=file_hash,
                )
            except OSError:
                logger.warning("Asset provenance projection failed: sha256=%s", file_hash)

            if validated.kind == "image":
                try:
                    self._upsert_theme_contract_for_image(
                        destination=destination,
                        theme_id=theme_id,
                        role=role,
                    )
                    if self.theme_registry.last_recovery is not None:
                        recovered_theme_count += 1
                        self.theme_registry.last_recovery = None
                except (OSError, ValueError):
                    logger.warning("Theme projection failed: sha256=%s", file_hash)

            self.progress_signal.emit(
                progress_base + 84,
                ui_text(f"完成 {idx}/{total_files}", f"Completed {idx}/{total_files}"),
            )

        if self.isInterruptionRequested():
            self.storage_runtime.interrupt_batch(batch_id)
            self._close_loop()
            return
        self.storage_runtime.complete_batch(
            batch_id,
            accepted_count=accepted_count,
            duplicate_count=duplicate_count,
            rejected_count=rejected_count,
        )

        self.progress_signal.emit(92, ui_text("刷新资产清单...", "Refreshing asset manifest..."))
        scanner = AssetScanner()
        manifest_ready = True
        try:
            self._run_coro(scanner.scan_assets_directory())
        except (OSError, RuntimeError, ValueError):
            manifest_ready = False
            logger.warning("Asset manifest projection failed; Agent access is paused until recovery")

        if not manifest_ready:
            self.finished_signal.emit(
                ui_text(
                    "素材已保存，Agent 接口待恢复",
                    "Assets saved; Agent access is pending recovery",
                ),
                False,
            )
            self._close_loop()
            return

        if accepted_count == 0 and duplicate_count == 0:
            self.finished_signal.emit(
                ui_text(
                    "文件被拦截：只支持图片/音频，或体积超过 500MB",
                    "Blocked: only images/audio are supported, or the file is over 500MB",
                ),
                False,
            )
            self._close_loop()
            return

        self.batch_signal.emit(
            batch_id,
            {
                "accepted_count": accepted_count,
                "duplicate_count": duplicate_count,
                "rejected_count": rejected_count,
            },
        )

        message = ui_text(
            f"收纳完成：新增 {accepted_count}，去重 {duplicate_count}",
            f"Stored: {accepted_count} new, {duplicate_count} duplicate",
        )
        if renamed_count > 0:
            message += ui_text(f"，重命名 {renamed_count}", f", renamed {renamed_count}")
        if rejected_count > 0:
            message += ui_text(f"，拦截 {rejected_count}", f", blocked {rejected_count}")
        if recovered_theme_count:
            message += ui_text(
                "；已隔离损坏的主题记录",
                "; a damaged theme record was quarantined",
            )
        self.progress_signal.emit(100, ui_text("入库完成", "Import complete"))
        self.finished_signal.emit(message, True)
        self._close_loop()

    def _build_hash_index(self) -> dict[str, Path]:
        return self.storage_runtime.asset_hash_index(self.assets_dir)

    def _preflight_media_file(self, file_path: Path) -> str | None:
        if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
            return "missing_file"
        try:
            file_size = file_path.stat().st_size
        except OSError:
            return "unreadable_file"
        if file_size <= 0:
            return "empty_file"
        if file_size > self.MAX_FILE_SIZE_BYTES:
            return "file_too_large"
        if not self._is_supported_extension(file_path):
            return "unsupported_extension"
        return None

    def _is_supported_extension(self, file_path: Path) -> bool:
        suffix = file_path.suffix.lower()
        return (
            suffix in self.SUPPORTED_IMAGE_EXTENSIONS
            or suffix in self.SUPPORTED_AUDIO_EXTENSIONS
        )

    def _validate_media_file(
        self, file_path: Path
    ) -> tuple[str | None, str | None, str | None]:
        if not file_path.exists() or not file_path.is_file():
            return None, None, "missing_file"

        file_size = file_path.stat().st_size
        if file_size > self.MAX_FILE_SIZE_BYTES:
            return None, None, "file_too_large"

        try:
            validated = validate_media(file_path)
        except MediaValidationError:
            return None, None, "unsupported_mime"
        return validated.kind, validated.mime_type, None

    def _compute_sha256(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as source:
            for chunk in iter(lambda: source.read(self.HASH_CHUNK_SIZE), b""):
                if self.isInterruptionRequested():
                    raise InterruptedError("hash_interrupted")
                digest.update(chunk)
        return digest.hexdigest()

    def _resolve_destination(self, original_name: str, sha256_hex: str) -> Path:
        candidate = self.assets_dir / original_name
        if not candidate.exists():
            return candidate

        source = Path(original_name)
        short_hash = sha256_hex[:8]
        stem = source.stem
        suffix = source.suffix
        renamed = self.assets_dir / f"{stem}_{short_hash}{suffix}"
        if not renamed.exists():
            return renamed

        counter = 1
        while True:
            resolved = self.assets_dir / f"{stem}_{short_hash}_{counter}{suffix}"
            if not resolved.exists():
                return resolved
            counter += 1

    def _resolve_themed_destination(
        self,
        original_name: str,
        sha256_hex: str,
        theme_id: str,
        media_kind: str,
        role: str,
    ) -> Path:
        safe_theme = self._safe_identifier(
            theme_id or self.settings.VISION_FALLBACK_THEME
        )
        bucket = "images" if media_kind == "image" else "audio"
        extension = Path(original_name).suffix.lower()
        if not extension:
            extension = ".bin"

        short_hash = sha256_hex[:8]
        safe_role = self._safe_identifier(role or "unknown")
        base_name = f"{safe_theme}_{'img' if media_kind == 'image' else 'aud'}_{safe_role}_{short_hash}{extension}"

        themed_dir = self.assets_dir / safe_theme / bucket
        candidate = themed_dir / base_name
        if not candidate.exists():
            return candidate

        counter = 1
        while True:
            resolved = (
                themed_dir
                / f"{safe_theme}_{'img' if media_kind == 'image' else 'aud'}_{safe_role}_{short_hash}_{counter}{extension}"
            )
            if not resolved.exists():
                return resolved
            counter += 1

    def _persist_asset_provenance(
        self,
        *,
        source_path: Path,
        destination: Path,
        sha256_hex: str,
        ai_suggestions: dict[str, object] | None = None,
    ) -> None:
        provenance = read_asset_provenance(source_path)
        if not provenance and not ai_suggestions:
            return
        try:
            source_key = destination.relative_to(self.assets_dir).as_posix()
        except ValueError:
            source_key = destination.name
        provenance.update({"source_key": source_key, "sha256": sha256_hex})
        if ai_suggestions:
            provenance["ai_suggestions"] = ai_suggestions
        try:
            write_asset_provenance(destination, provenance)
        except OSError:
            logger.debug("Failed to persist asset provenance")

    @staticmethod
    def _safe_identifier(text: str) -> str:
        lowered = (text or "").strip().lower()
        sanitized = "".join(
            ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in lowered
        )
        while "__" in sanitized:
            sanitized = sanitized.replace("__", "_")
        sanitized = sanitized.strip("_")
        return sanitized or "generic"

    def _upsert_theme_contract_for_image(
        self,
        destination: Path,
        theme_id: str,
        role: str,
    ) -> None:
        safe_theme = self._safe_identifier(
            theme_id or self.settings.VISION_FALLBACK_THEME
        )
        safe_role = self._safe_identifier(role or "unknown")
        rel_path = destination.relative_to(self.assets_dir).as_posix()
        asset_url = f"/static/{rel_path}"
        asset_key = safe_role if safe_role != "unknown" else destination.stem

        self.theme_registry.upsert_image_asset(
            theme_id=safe_theme,
            asset_key=asset_key,
            asset_url=asset_url,
            role=safe_role,
        )


async def _classify_registered_bundle(
    bundle: dict[str, object],
    assets_dir: Path,
    classifier: StyleClassifier,
    theme_candidates: list[str],
    bundle_service: BundleService,
):
    source_key = str(bundle.get("source_key") or "").strip()
    if str(bundle.get("type") or "").lower() != "image" or not source_key:
        raise ValueError("unsupported_bundle")
    assets_root = assets_dir.resolve(strict=False)
    asset_path = (assets_root / source_key).resolve(strict=False)
    asset_path.relative_to(assets_root)
    if not asset_path.is_file() or asset_path.is_symlink():
        raise OSError("asset_missing")

    classification = await classifier.classify_image(asset_path, candidate_themes=theme_candidates)
    provenance = read_asset_provenance(asset_path)
    provenance.update(
        {
            "source_key": source_key,
            "sha256": str(bundle.get("sha256") or ""),
            "ai_suggestions": classification.ai_suggestions(),
        }
    )
    write_asset_provenance(asset_path, provenance)
    auto_ready = StyleClassifier.is_auto_ready(classification)
    if auto_ready:
        auto_ready = bundle_service.set_bundle_role(str(bundle.get("id") or ""), classification.role) is not None
    return classification, auto_ready


def _persist_ai_failure(bundle: dict[str, object], assets_dir: Path, reason: str) -> None:
    source_key = str(bundle.get("source_key") or "").strip()
    if not source_key:
        return
    try:
        assets_root = assets_dir.resolve(strict=False)
        asset_path = (assets_root / source_key).resolve(strict=False)
        asset_path.relative_to(assets_root)
        if not asset_path.is_file() or asset_path.is_symlink():
            return
        provenance = read_asset_provenance(asset_path)
        provenance.update(
            {
                "source_key": source_key,
                "sha256": str(bundle.get("sha256") or ""),
                "ai_suggestions": {
                    "source": "model_fallback",
                    "usage": "unknown",
                    "quality": "unknown",
                    "quality_reason": "classification_unavailable",
                    "confidence": {"theme": 0.0, "role": 0.0},
                    "reason": str(reason or "model_call_failed")[:80],
                    "tags": [],
                    "agent_summary": "",
                    "trust": "untrusted_advisory",
                    "must_not_execute": True,
                },
            }
        )
        write_asset_provenance(asset_path, provenance)
    except (OSError, ValueError):
        logger.debug("Failed to persist AI failure state")


class AIRefreshWorker(QThread):
    finished_signal = Signal(str, str, bool)

    def __init__(
        self,
        bundle: dict[str, object],
        assets_dir: Path,
        provider: AIProviderConfig | None = None,
    ) -> None:
        super().__init__()
        self.bundle = dict(bundle)
        self.assets_dir = assets_dir
        self.theme_registry = ThemeRegistry()
        self.style_classifier = StyleClassifier(provider)
        self.bundle_service = BundleService()
        self.theme_candidates = self.theme_registry.list_theme_ids()
        fallback = get_settings().VISION_FALLBACK_THEME
        if fallback not in self.theme_candidates:
            self.theme_candidates.append(fallback)

    def run(self) -> None:
        bundle_id = str(self.bundle.get("id") or "")
        loop = asyncio.new_event_loop()
        try:
            classification = loop.run_until_complete(
                _classify_registered_bundle(
                    self.bundle,
                    self.assets_dir,
                    self.style_classifier,
                    self.theme_candidates,
                    self.bundle_service,
                )
            )
        except (ResourceExhaustedError, httpx.TimeoutException, OSError, RuntimeError, ValueError) as exc:
            logger.warning("AI 分拣刷新失败: bundle_id=%s error_type=%s", bundle_id, type(exc).__name__)
            self.finished_signal.emit(bundle_id, ui_text("AI 分拣失败", "AI sorting failed"), False)
            return
        finally:
            loop.close()
        classification, auto_ready = classification
        suggestions = classification.ai_suggestions()
        source = str(suggestions.get("source") or "").strip()
        reason = str(suggestions.get("reason") or source or "unknown").strip()
        success = source not in {"model_fallback", "disabled", "guard", ""}
        message = (
            ui_text("AI 分拣完成 · 已自动可用", "AI sorting complete · ready")
            if auto_ready
            else ui_text("AI 分拣已更新 · 等待确认", "AI sorting updated · review needed")
            if success
            else ui_text(f"AI 分拣未得到模型结果：{reason}", f"AI sorting did not get a model result: {reason}")
        )
        self.finished_signal.emit(bundle_id, message, success)


class AIBatchWorker(QThread):
    finished_signal = Signal(str, str, str)
    progress_signal = Signal(int, str)

    def __init__(
        self,
        batch_id: str,
        bundles: list[dict[str, object]],
        assets_dir: Path,
        provider: AIProviderConfig | None = None,
    ) -> None:
        super().__init__()
        self.batch_id = batch_id
        self.bundles = [dict(bundle) for bundle in bundles]
        self.assets_dir = assets_dir
        self.style_classifier = StyleClassifier(provider)
        self.bundle_service = BundleService()
        registry = ThemeRegistry()
        self.theme_candidates = registry.list_theme_ids()
        fallback = get_settings().VISION_FALLBACK_THEME
        if fallback not in self.theme_candidates:
            self.theme_candidates.append(fallback)

    def run(self) -> None:
        ready_count = 0
        pending_count = 0
        classified_count = 0
        failed_count = 0
        total = max(len(self.bundles), 1)
        loop = asyncio.new_event_loop()
        try:
            for index, bundle in enumerate(self.bundles, start=1):
                if self.isInterruptionRequested():
                    self.finished_signal.emit(
                        self.batch_id,
                        ui_text("AI 整理已取消", "AI sorting cancelled"),
                        "cancelled",
                    )
                    return
                self.progress_signal.emit(
                    int((index - 1) / total * 100),
                    ui_text(f"AI 整理 {index}/{total}", f"AI sorting {index}/{total}"),
                )
                try:
                    classification, auto_ready = loop.run_until_complete(
                        _classify_registered_bundle(
                            bundle,
                            self.assets_dir,
                            self.style_classifier,
                            self.theme_candidates,
                            self.bundle_service,
                        )
                    )
                except (ResourceExhaustedError, httpx.TimeoutException, OSError, RuntimeError, ValueError) as exc:
                    logger.warning(
                        "批次 AI 整理失败: batch=%s bundle=%s error_type=%s",
                        self.batch_id,
                        bundle.get("id"),
                        type(exc).__name__,
                    )
                    _persist_ai_failure(bundle, self.assets_dir, "model_call_failed")
                    auto_ready = False
                    failed_count += 1
                else:
                    if classification.source == "model":
                        classified_count += 1
                    else:
                        failed_count += 1
                if auto_ready:
                    ready_count += 1
                else:
                    pending_count += 1
        finally:
            loop.close()
        if classified_count == len(self.bundles):
            status = "success"
            progress_message = ui_text("AI 整理完成", "AI sorting complete")
            message = ui_text(
                f"AI 整理完成：可用 {ready_count}，待确认 {pending_count}",
                f"AI sorting complete: {ready_count} ready, {pending_count} pending",
            )
        elif classified_count:
            status = "partial_success"
            progress_message = ui_text("AI 整理部分完成", "AI sorting partially complete")
            message = ui_text(
                f"AI 整理部分完成：成功 {classified_count}，失败 {failed_count}",
                f"AI sorting partially complete: {classified_count} succeeded, {failed_count} failed",
            )
        else:
            status = "failed"
            progress_message = ui_text("AI 整理失败", "AI sorting failed")
            message = ui_text(
                f"AI 整理失败：{failed_count} 个素材未得到模型结果",
                f"AI sorting failed: no model result for {failed_count} assets",
            )
        self.progress_signal.emit(100, progress_message)
        self.finished_signal.emit(
            self.batch_id,
            message,
            status,
        )


class ToastLabel(QLabel):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            "QLabel { border: 1px solid rgba(0,0,0,60); border-radius: 16px; "
            "padding: 6px 12px; color: white; background-color: rgba(218, 43, 43, 220); "
            "font-size: 12px; }"
        )
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_animation = QPropertyAnimation(
            self._opacity_effect, b"opacity", self
        )
        self._fade_animation.setDuration(420)
        self._fade_animation.setStartValue(1.0)
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.finished.connect(self.hide)
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.setInterval(2000)
        self._delay_timer.timeout.connect(self._fade_animation.start)
        self.hide()

    def reposition(self, anchor: QRect, available: QRect) -> None:
        if not self.isVisible():
            return
        self._move_to_anchor(anchor, available)

    def _move_to_anchor(self, anchor: QRect, available: QRect) -> None:
        margin = 10
        x = anchor.center().x() - self.width() // 2
        y = anchor.bottom() + 10
        if y > available.bottom() - self.height() - margin:
            side_y = anchor.center().y() - self.height() // 2
            right_x = anchor.right() + 10
            left_x = anchor.left() - self.width() - 10
            if right_x <= available.right() - self.width() - margin:
                x, y = right_x, side_y
            elif left_x >= available.left() + margin:
                x, y = left_x, side_y
            else:
                x = anchor.center().x() - self.width() // 2
                y = anchor.top() - self.height() - 10
        x = max(available.left() + margin, min(x, available.right() - self.width() - margin))
        y = max(available.top() + margin, min(y, available.bottom() - self.height() - margin))
        self.move(x, y)

    def show_message(self, message: str, success: bool, anchor: QRect, available: QRect) -> None:
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        self._delay_timer.stop()

        if success:
            self.setStyleSheet(
                "QLabel { border: 2px solid #6F7F5A; border-radius: 16px; "
                "padding: 6px 12px; color: #4E5F3D; background-color: #FFFDF5; "
                "font-size: 12px; font-weight: bold; }"
            )
        else:
            self.setStyleSheet(
                "QLabel { border: 2px solid #9B4C37; border-radius: 16px; "
                "padding: 6px 12px; color: #9B4C37; background-color: #FFFDF5; "
                "font-size: 12px; font-weight: bold; }"
            )

        self.setText(message)
        self.setMaximumWidth(320)
        self.adjustSize()
        self._move_to_anchor(anchor, available)
        self._opacity_effect.setOpacity(1.0)
        self.show()
        self.raise_()
        self._delay_timer.start()


class UploadProgressWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(248, 92)

        self.container = QWidget(self)
        self.container.setGeometry(0, 0, 248, 92)
        self.container.setStyleSheet(
            "QWidget { background-color: #FFFDF5; "
            "border: 2px solid #6F7F5A; border-radius: 18px; }"
        )

        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        self.title = QLabel(ui_text("正在收纳...", "Storing..."), self.container)
        self.title.setStyleSheet(
            "QLabel { color: #4E5F3D; font-size: 13px; font-weight: bold; "
            "letter-spacing: 0.2px; background: transparent; border: none; }"
        )
        layout.addWidget(self.title)

        self.subtitle = QLabel(ui_text("准备中", "Preparing"), self.container)
        self.subtitle.setStyleSheet(
            "QLabel { color: #555555; font-size: 11px; "
            "letter-spacing: 0.15px; background: transparent; border: none; }"
        )
        layout.addWidget(self.subtitle)

        self.bar = QProgressBar(self.container)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(7)
        self.bar.setStyleSheet(
            "QProgressBar { background: #E9E4D4; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #C8A24A; border-radius: 3px; }"
        )
        layout.addWidget(self.bar)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(1400)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def begin(self) -> None:
        self._hide_timer.stop()
        self.title.setText(ui_text("正在收纳...", "Storing..."))
        self.subtitle.setText(ui_text("准备中", "Preparing"))
        self.bar.setStyleSheet(
            "QProgressBar { background: #E9E4D4; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #C8A24A; border-radius: 3px; }"
        )
        self.bar.setValue(2)
        self.show()
        self.raise_()

    def begin_at(self, position: QPoint) -> None:
        self.move(position)
        self.begin()

    def set_progress(self, percent: int, text: str) -> None:
        value = max(0, min(100, percent))
        self.bar.setValue(value)
        self.subtitle.setText(text)

    def complete(self, success: bool, message: str) -> None:
        self.title.setText(ui_text("收纳完成", "Stored") if success else ui_text("收纳失败", "Store failed"))
        self.subtitle.setText(message)
        if success:
            self.bar.setStyleSheet(
                "QProgressBar { background: #E9E4D4; border: none; border-radius: 3px; }"
                "QProgressBar::chunk { background: #6F7F5A; border-radius: 3px; }"
            )
        else:
            self.bar.setStyleSheet(
                "QProgressBar { background: #F1DED6; border: none; border-radius: 3px; }"
                "QProgressBar::chunk { background: #9B4C37; border-radius: 3px; }"
            )
        self.bar.setValue(100 if success else max(18, self.bar.value()))
        self._hide_timer.start()


class AISetupWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(312, 196)
        self._copy_callback = None
        self._recheck_callback = None

        self.container = QWidget(self)
        self.container.setGeometry(0, 0, 312, 196)
        self.container.setStyleSheet(
            "QWidget { background-color: #FFFDF5; "
            "border: 2px solid #6F7F5A; border-radius: 18px; }"
        )
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self.title = QLabel(ui_text("开启本地 AI 分拣", "Enable local AI sorting"), self.container)
        self.title.setStyleSheet("QLabel { color: #4E5F3D; font-size: 15px; font-weight: bold; background: transparent; border: none; }")
        layout.addWidget(self.title)

        self.body = QLabel("", self.container)
        self.body.setWordWrap(True)
        self.body.setStyleSheet("QLabel { color: #4A463A; font-size: 11px; background: transparent; border: none; }")
        layout.addWidget(self.body)

        self.command = QLabel("", self.container)
        self.command.setWordWrap(True)
        self.command.setStyleSheet(
            "QLabel { color: #2F3A26; font-size: 11px; padding: 6px 8px; "
            "background: #F6F1E4; border: 1px solid #DDD3BB; border-radius: 7px; }"
        )
        layout.addWidget(self.command)

        row = QWidget(self.container)
        row.setStyleSheet("QWidget { background: transparent; border: none; }")
        buttons = QHBoxLayout(row)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(6)
        self.copy_button = QPushButton(ui_text("复制命令", "Copy command"), row)
        self.recheck_button = QPushButton(ui_text("重新检测", "Recheck"), row)
        self.close_button = QPushButton(ui_text("关闭", "Close"), row)
        for button in (self.copy_button, self.recheck_button, self.close_button):
            button.setFixedHeight(26)
            button.setStyleSheet(
                "QPushButton { color: #4E5F3D; background: #F6F1E4; "
                "border: 1px solid #DDD3BB; border-radius: 7px; font-size: 11px; }"
                "QPushButton:hover { background: #EFE3C7; }"
            )
            buttons.addWidget(button)
        layout.addWidget(row)
        self.copy_button.clicked.connect(self._copy)
        self.recheck_button.clicked.connect(self._recheck)
        self.close_button.clicked.connect(self.hide)
        self.hide()

    def show_setup(self, *, model: str, status_text: str, anchor: QRect, available: QRect) -> None:
        command = f"ollama pull {model}"
        self.command.setText(command)
        self.body.setText(
            ui_text(
                f"{status_text}\n安装 Ollama 后运行下面命令，再重新检测。",
                f"{status_text}\nInstall Ollama, run the command below, then recheck.",
            )
        )
        self._move_to_anchor(anchor, available)
        self.show()
        self.raise_()

    def set_handlers(self, copy_callback, recheck_callback) -> None:
        self._copy_callback = copy_callback
        self._recheck_callback = recheck_callback

    def _move_to_anchor(self, anchor: QRect, available: QRect) -> None:
        margin = 12
        x = anchor.center().x() - self.width() // 2
        y = anchor.top() - self.height() - 12
        if y < available.top() + margin:
            y = anchor.bottom() + 12
        x = max(available.left() + margin, min(x, available.right() - self.width() - margin))
        y = max(available.top() + margin, min(y, available.bottom() - self.height() - margin))
        self.move(x, y)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.command.text())
        if self._copy_callback is not None:
            self._copy_callback()

    def _recheck(self) -> None:
        if self._recheck_callback is not None:
            self._recheck_callback()


class ConfirmationPreviewWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(276, 156)
        self._closing_preview = False
        self._armed_for_execution = False
        self._action = ""
        self._project_root = ""
        self._execute_callback = None
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_animation = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.finished.connect(self._on_fade_finished)
        self._slide_animation = QPropertyAnimation(self, b"pos", self)
        self._slide_animation.setDuration(150)
        self._slide_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.container = QWidget(self)
        self.container.setGeometry(0, 0, 276, 156)
        self.container.setStyleSheet(
            "QWidget { background-color: #FFFDF5; "
            "border: 1px solid #DDD3BB; border-radius: 8px; }"
        )

        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(7)

        self.title = QLabel("", self.container)
        self.title.setStyleSheet(
            "QLabel { color: #4E5F3D; font-size: 13px; font-weight: bold; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.title)

        self.body = QLabel("", self.container)
        self.body.setStyleSheet(
            "QLabel { color: #333333; font-size: 12px; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.body)

        self.summary = QLabel("", self.container)
        self.summary.setStyleSheet(
            "QLabel { color: #444444; font-size: 12px; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.summary)

        self.warning = QLabel("", self.container)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "QLabel { color: #666666; font-size: 11px; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.warning)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.primary_button = QPushButton("", self.container)
        self.primary_button.setFixedSize(78, 28)
        self.primary_button.setStyleSheet(
            "QPushButton { color: #FFFFFF; background: #6F7F5A; "
            "border: 1px solid #6F7F5A; border-radius: 6px; font-size: 12px; }"
            "QPushButton:hover { background: #5F704B; }"
        )
        self.primary_button.clicked.connect(self._on_primary_clicked)
        button_row.addWidget(self.primary_button)

        self.close_button = QPushButton(ui_text("取消", "Cancel"), self.container)
        self.close_button.setFixedSize(68, 28)
        self.close_button.setStyleSheet(
            "QPushButton { color: #4E5F3D; background: #F6F1E4; "
            "border: 1px solid #DDD3BB; border-radius: 6px; font-size: 12px; }"
            "QPushButton:hover { background: #EFE3C7; }"
        )
        self.close_button.clicked.connect(self.hide_preview)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)
        self.hide()

    def set_action_handler(self, callback) -> None:
        self._execute_callback = callback

    def update_prompt(
        self,
        *,
        title: str,
        body: str,
        summary: str,
        warning: str,
        action: str = "",
        project_root: str = "",
        primary_label: str = "",
    ) -> None:
        self._armed_for_execution = False
        self._action = action
        self._project_root = project_root
        self.title.setStyleSheet(
            "QLabel { color: #4E5F3D; font-size: 13px; font-weight: bold; "
            "background: transparent; border: none; }"
        )
        self.title.setText(title)
        self.body.setText(body)
        self.summary.setText(summary)
        self.warning.setText(warning)
        self.close_button.setText(ui_text("取消", "Cancel") if primary_label else ui_text("知道了", "OK"))
        self.primary_button.setText(primary_label)
        self.primary_button.setVisible(bool(primary_label))

    def show_result(self, *, success: bool, title: str, body: str, warning: str) -> None:
        self._armed_for_execution = False
        self.title.setText(title)
        self.body.setText(body)
        self.summary.setText("")
        self.warning.setText(warning)
        color = "#4E5F3D" if success else "#9B4C37"
        self.title.setStyleSheet(
            f"QLabel {{ color: {color}; font-size: 13px; font-weight: bold; "
            "background: transparent; border: none; }"
        )
        self.primary_button.hide()
        self.close_button.setText(ui_text("知道了", "OK"))

    def show_at(self, x: int, y: int) -> None:
        self._closing_preview = False
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        if self._slide_animation.state() == QPropertyAnimation.State.Running:
            self._slide_animation.stop()
        start_y = y + 8 if not self.isVisible() else self.y()
        self.move(x, start_y)
        if not self.isVisible():
            self._opacity_effect.setOpacity(0.0)
        self.show()
        self.raise_()
        self._fade_animation.setDuration(150)
        self._fade_animation.setStartValue(self._opacity_effect.opacity())
        self._fade_animation.setEndValue(1.0)
        self._slide_animation.setStartValue(QPoint(x, start_y))
        self._slide_animation.setEndValue(QPoint(x, y))
        self._fade_animation.start()
        self._slide_animation.start()

    def hide_preview(self) -> None:
        if not self.isVisible():
            return
        self._closing_preview = True
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        if self._slide_animation.state() == QPropertyAnimation.State.Running:
            self._slide_animation.stop()
        self._fade_animation.setDuration(110)
        self._fade_animation.setStartValue(self._opacity_effect.opacity())
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.start()

    def _on_fade_finished(self) -> None:
        if self._closing_preview:
            self._closing_preview = False
            self.hide()

    def _on_primary_clicked(self) -> None:
        if not self._action or not self._project_root or self._execute_callback is None:
            return
        if not self._armed_for_execution:
            self._armed_for_execution = True
            self.title.setText(ui_text("再次确认？", "Confirm again?"))
            self.warning.setText(ui_text("确认后会处理项目内文件，可再次撤回。", "This will modify project files and can be rolled back."))
            self.primary_button.setText(ui_text("确认执行", "Confirm"))
            return
        self._execute_callback(self._action, self._project_root)


class MaterialPanelWindow(QWidget):
    def __init__(self, parent: QWidget | None = None, *, embedded: bool = False) -> None:
        super().__init__(parent)
        self._embedded = bool(embedded)
        if not self._embedded:
            self.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.NoDropShadowWindowHint
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if self._embedded:
            self.setMinimumSize(0, 0)
        else:
            self.setFixedSize(336, 608)
        self._confirmation_available = False
        self._recent_items = []
        self._all_recent_items = []
        self._visible_items = []
        self._page_index = 0
        self._filter_mode = "all"
        self._batch_scope = "latest" if self._embedded else "all"
        self._selected_bundle_id = ""
        self._bundle_by_source_key: dict[str, dict[str, object]] = {}
        self._suggested_ai_role = ""
        self._toast_callback = None
        self._theme_recovery_notice_pending = False
        self._retry_batch_callback = None
        self._ai_provider_factory = None
        self._ai_enabled_callback = None
        self.ai_refresh_worker: AIRefreshWorker | None = None
        self.confirmation_preview: ConfirmationPreviewWindow | None = None
        if not self._embedded:
            self.confirmation_preview = ConfirmationPreviewWindow()
            self.confirmation_preview.set_action_handler(self._execute_confirmation_action)
        self._hiding_panel = False
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0 if self._embedded else 0.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._fade_animation = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.finished.connect(self._on_fade_finished)

        self.container = QWidget(self)
        self.container.setGeometry(self.rect())
        self.container.setStyleSheet(
            "QWidget { background: transparent; border: none; }"
            if self._embedded
            else "QWidget { background-color: #FFFDF5; border: 2px solid #6F7F5A; border-radius: 18px; }"
        )

        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self.title = QLabel("Haypile", self.container)
        self.title.setStyleSheet(
            "QLabel { color: #4E5F3D; font-size: 16px; font-weight: bold; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.title)

        self.project_label = QLabel("", self.container)
        self.project_label.setStyleSheet(
            "QLabel { color: #6F7F5A; font-size: 11px; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.project_label)
        self.project_label.hide()

        initial_summary = (
            ui_text("0 个素材 · 可用 0\n待确认 0", "0 assets · ready 0\npending 0")
            if self._embedded
            else ui_text("0 个 bundle · 可用 0 · 待确认 0", "0 bundles · ready 0 · pending 0")
        )
        self.summary_label = QLabel(initial_summary, self.container)
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet(
            "QLabel { color: #333333; font-size: 12px; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.summary_label)

        self.pending_label = QLabel("", self.container)
        self.pending_label.setWordWrap(True)
        self.pending_label.setStyleSheet(
            "QLabel { color: #A67624; font-size: 12px; font-weight: bold; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.pending_label)

        self.scope_row = QWidget(self.container)
        self.scope_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        scope_layout = QHBoxLayout(self.scope_row)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(5)
        self.scope_buttons: dict[str, QPushButton] = {}
        for scope, label in (
            ("latest", ui_text("最新批次", "Latest batch")),
            ("all", ui_text("全部素材", "All assets")),
        ):
            button = QPushButton(label, self.scope_row)
            button.setFixedHeight(24)
            button.clicked.connect(lambda _checked=False, selected_scope=scope: self.set_batch_scope(selected_scope))
            self.scope_buttons[scope] = button
            scope_layout.addWidget(button)
        self.retry_batch_button = QPushButton(ui_text("重试整理", "Retry sorting"), self.scope_row)
        self.retry_batch_button.setFixedHeight(24)
        self.retry_batch_button.clicked.connect(self._retry_latest_batch)
        scope_layout.addWidget(self.retry_batch_button)
        self.scope_row.setVisible(self._embedded)
        self._refresh_scope_buttons()

        self.filter_row = QWidget(self.container)
        self.filter_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        filter_layout = QHBoxLayout(self.filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(4)
        self.filter_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("all", ui_text("全部", "All")),
            ("ready", ui_text("可用", "ready")),
            ("pending", ui_text("待确认", "Pending")),
            ("image", ui_text("图片", "Images")),
            ("audio", ui_text("音频", "Audio")),
        ):
            button = QPushButton(label, self.filter_row)
            button.setFixedHeight(24)
            button.clicked.connect(lambda _checked=False, selected_mode=mode: self._set_filter_mode(selected_mode))
            self.filter_buttons[mode] = button
            filter_layout.addWidget(button)
        layout.addWidget(self.filter_row)
        self._refresh_filter_buttons()

        self.search_input = QLineEdit(self.container)
        self.search_input.setPlaceholderText(ui_text("搜索文件、用途、状态", "Search file, role, status"))
        self.search_input.setFixedHeight(26)
        self.search_input.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.search_input.setStyleSheet(
            "QLineEdit { color: #4A463A; background: #FFF9EA; "
            "border: 1px solid #E5D8B9; border-radius: 7px; padding: 0 8px; font-size: 11px; }"
        )
        self.search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_input)

        self.item_labels: list[QLabel] = []
        for _ in range(3):
            item = QLabel("", self.container)
            item.setWordWrap(True)
            item.setMinimumHeight(46)
            item.setStyleSheet(self._item_label_style(False))
            item.setCursor(Qt.CursorShape.PointingHandCursor)
            item.mousePressEvent = lambda event, index=len(self.item_labels): self._select_recent_item(index, event)
            self.item_labels.append(item)
            layout.addWidget(item)

        self.page_row = QWidget(self.container)
        self.page_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        page_layout = QHBoxLayout(self.page_row)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(6)
        self.previous_page_button = QPushButton("‹", self.page_row)
        self.next_page_button = QPushButton("›", self.page_row)
        for button in (self.previous_page_button, self.next_page_button):
            button.setFixedSize(28, 24)
            button.setStyleSheet(
                "QPushButton { color: #4E5F3D; background: #F6F1E4; border: 1px solid #DDD3BB; "
                "border-radius: 6px; font-size: 15px; }"
                "QPushButton:hover { background: #EFE3C7; }"
                "QPushButton:disabled { color: #B7B09D; background: #F3EFE5; }"
            )
        self.page_label = QLabel("", self.page_row)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_label.setStyleSheet("QLabel { color: #756F61; font-size: 10px; }")
        self.previous_page_button.clicked.connect(lambda: self._change_page(-1))
        self.next_page_button.clicked.connect(lambda: self._change_page(1))
        page_layout.addWidget(self.previous_page_button)
        page_layout.addWidget(self.page_label, 1)
        page_layout.addWidget(self.next_page_button)
        self.page_row.hide()
        layout.addWidget(self.page_row)

        self.detail_label = QLabel("", self.container)
        self.detail_label.setWordWrap(True)
        self.detail_label.setMinimumHeight(104)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.detail_label.setStyleSheet(
            "QLabel { color: #4A463A; font-size: 11px; "
            "padding: 7px 8px; background: #FFF9EA; "
            "border: 1px solid #E5D8B9; border-radius: 8px; }"
        )
        self.detail_label.hide()
        layout.addWidget(self.detail_label)

        self.role_row = QWidget(self.container)
        self.role_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        role_layout = QHBoxLayout(self.role_row)
        role_layout.setContentsMargins(0, 0, 0, 0)
        role_layout.setSpacing(4)
        self.role_buttons: dict[str, QPushButton] = {}
        for role, label in (
            ("main_background", ui_text("背景", "Background")),
            ("hero_image", ui_text("主视觉", "Hero")),
            ("logo", "Logo"),
            ("icon", ui_text("图标", "Icon")),
            ("content_image", ui_text("内容图", "Content")),
            ("texture", ui_text("纹理", "Texture")),
        ):
            button = QPushButton(label, self.role_row)
            button.setFixedHeight(24)
            button.setStyleSheet(self._role_button_style(False))
            button.clicked.connect(lambda _checked=False, selected_role=role: self._set_selected_role(selected_role))
            self.role_buttons[role] = button
            role_layout.addWidget(button)
        self.role_row.hide()
        layout.addWidget(self.role_row)

        self.audio_usage_row = QWidget(self.container)
        self.audio_usage_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        audio_usage_layout = QHBoxLayout(self.audio_usage_row)
        audio_usage_layout.setContentsMargins(0, 0, 0, 0)
        audio_usage_layout.setSpacing(4)
        self.audio_usage_buttons: dict[str, QPushButton] = {}
        for usage, label in (
            ("music", ui_text("音乐", "Music")),
            ("voice", ui_text("人声", "Voice")),
            ("ambience", ui_text("环境", "Ambient")),
            ("sound_effect", ui_text("音效", "SFX")),
            ("loop", ui_text("循环", "Loop")),
        ):
            button = QPushButton(label, self.audio_usage_row)
            button.setFixedHeight(24)
            button.setStyleSheet(self._role_button_style(False))
            button.clicked.connect(lambda _checked=False, selected_usage=usage: self._set_selected_audio_usage(selected_usage))
            self.audio_usage_buttons[usage] = button
            audio_usage_layout.addWidget(button)
        self.audio_usage_row.hide()
        layout.addWidget(self.audio_usage_row)

        self.retry_ai_button = QPushButton(ui_text("重新 AI 分拣", "Retry AI sorting"), self.container)
        self.retry_ai_button.setFixedHeight(24)
        self.retry_ai_button.setStyleSheet(
            "QPushButton { color: #4E5F3D; background: #FFF9EA; "
            "border: 1px solid #E5D8B9; border-radius: 7px; font-size: 11px; }"
            "QPushButton:hover { background: #F6F1E4; }"
            "QPushButton:disabled { color: #A9A08A; background: #F6F1E4; }"
        )
        self.retry_ai_button.clicked.connect(self._retry_ai_classification)
        self.retry_ai_button.hide()
        layout.addWidget(self.retry_ai_button)

        self.accept_ai_button = QPushButton(ui_text("采纳 AI 建议", "Accept AI suggestion"), self.container)
        self.accept_ai_button.setFixedHeight(26)
        self.accept_ai_button.setStyleSheet(
            "QPushButton { color: #FFF9EA; background: #6F7F5A; "
            "border: none; border-radius: 7px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: #5D704A; }"
        )
        self.accept_ai_button.clicked.connect(self._accept_ai_suggestion)
        self.accept_ai_button.hide()
        layout.addWidget(self.accept_ai_button)

        self.preview_label = QLabel("", self.container)
        self.preview_label.setFixedHeight(48)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet(
            "QLabel { background: #F6F1E4; border: 1px solid #DDD3BB; border-radius: 8px; }"
        )
        self.preview_label.hide()
        layout.addWidget(self.preview_label)

        self.copy_selected_button = QPushButton(ui_text("复制 handoff", "Copy handoff"), self.container)
        self.copy_selected_button.setFixedHeight(28)
        self.copy_selected_button.setStyleSheet(
            "QPushButton { color: #FFF9EA; background: #6F7F5A; "
            "border: none; border-radius: 7px; font-size: 12px; font-weight: bold; }"
            "QPushButton:hover { background: #5D704A; }"
            "QPushButton:disabled { color: #B7B09D; background: #E7E0D1; }"
        )
        self.copy_selected_button.clicked.connect(self._copy_selected_handoff)
        self.copy_selected_button.setEnabled(False)
        layout.addWidget(self.copy_selected_button)

        self.copy_ready_button = QPushButton(ui_text("复制可用 handoff", "Copy ready handoff"), self.container)
        self.copy_ready_button.setFixedHeight(28)
        self.copy_ready_button.setStyleSheet(
            "QPushButton { color: #4E5F3D; background: #F6F1E4; "
            "border: 1px solid #DDD3BB; border-radius: 7px; font-size: 12px; }"
            "QPushButton:hover { background: #EFE3C7; }"
        )
        self.copy_ready_button.clicked.connect(self._copy_ready_handoff)
        self.copy_ready_button.hide()
        layout.addWidget(self.copy_ready_button)

        self.copy_recipe_button = QPushButton(ui_text("复制 agent 配方", "Copy agent recipe"), self.container)
        self.copy_recipe_button.setFixedHeight(28)
        self.copy_recipe_button.setStyleSheet(
            "QPushButton { color: #4E5F3D; background: #FFF9EA; "
            "border: 1px solid #E5D8B9; border-radius: 7px; font-size: 12px; }"
            "QPushButton:hover { background: #F6F1E4; }"
        )
        self.copy_recipe_button.clicked.connect(self._copy_agent_recipe)
        layout.addWidget(self.copy_recipe_button)

        layout.addStretch(1)

        self.rehearsal_label = QLabel("", self.container)
        self.rehearsal_label.setWordWrap(True)
        self.rehearsal_label.setStyleSheet(
            "QLabel { color: #6F7F5A; font-size: 11px; "
            "background: transparent; border: none; }"
        )
        self.rehearsal_label.mousePressEvent = self._show_confirmation_preview
        layout.addWidget(self.rehearsal_label)
        self.rehearsal_label.hide()

        self.service_label = QLabel("", self.container)
        self.service_label.setWordWrap(True)
        self.service_label.setStyleSheet(
            "QLabel { color: #666666; font-size: 11px; "
            "background: transparent; border: none; }"
        )
        layout.addWidget(self.service_label)
        self.service_label.hide()
        if self._embedded:
            self._configure_embedded_layout(layout)
        self.hide()

    def _configure_embedded_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            layout.takeAt(0)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.title.hide()
        self.project_label.hide()
        self.copy_ready_button.hide()
        self.copy_recipe_button.hide()
        self.rehearsal_label.hide()
        self.service_label.hide()
        self.filter_buttons["ready"].hide()
        self.filter_buttons["pending"].hide()

        toolbar = QWidget(self.container)
        toolbar.setStyleSheet("QWidget { background: transparent; border: none; }")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        toolbar_layout.addWidget(self.search_input, 1)
        toolbar_layout.addWidget(self.filter_row, 0)
        layout.addWidget(self.scope_row)
        layout.addWidget(toolbar)

        body = QWidget(self.container)
        body.setStyleSheet("QWidget { background: transparent; border: none; }")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)

        list_pane = QWidget(body)
        list_pane.setStyleSheet("QWidget { background: transparent; border: none; }")
        list_layout = QVBoxLayout(list_pane)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(6)
        list_layout.addWidget(self.summary_label)
        self.pending_label.hide()
        for label in self.item_labels:
            list_layout.addWidget(label)
        list_layout.addWidget(self.page_row)
        list_layout.addStretch(1)

        detail_pane = QWidget(body)
        detail_pane.setStyleSheet("QWidget { background: transparent; border: none; }")
        detail_layout = QVBoxLayout(detail_pane)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        self.preview_label.setFixedHeight(72)
        detail_layout.addWidget(self.preview_label)
        detail_layout.addWidget(self.detail_label, 1)
        detail_layout.addWidget(self.accept_ai_button)
        detail_layout.addWidget(self.role_row)
        detail_layout.addWidget(self.audio_usage_row)
        detail_layout.addWidget(self.retry_ai_button)
        detail_layout.addWidget(self.copy_selected_button)

        body_layout.addWidget(list_pane, 42)
        body_layout.addWidget(detail_pane, 58)
        layout.addWidget(body, 1)

    def set_toast_handler(self, callback) -> None:
        self._toast_callback = callback
        if self._theme_recovery_notice_pending and self._toast_callback is not None:
            self._theme_recovery_notice_pending = False
            self._toast_callback(
                ui_text(
                    "已隔离损坏的主题记录并创建恢复副本",
                    "Damaged theme metadata was quarantined and replaced",
                ),
                False,
            )

    def set_ai_handlers(self, *, provider_factory, enabled_callback, retry_batch_callback) -> None:
        self._ai_provider_factory = provider_factory
        self._ai_enabled_callback = enabled_callback
        self._retry_batch_callback = retry_batch_callback

    def retranslate(self) -> None:
        labels = {
            "all": ui_text("全部", "All"),
            "ready": ui_text("可用", "Ready"),
            "pending": ui_text("待确认", "Pending"),
            "image": ui_text("图片", "Images"),
            "audio": ui_text("音频", "Audio"),
        }
        for mode, button in self.filter_buttons.items():
            button.setText(labels[mode])
        self.search_input.setPlaceholderText(ui_text("搜索文件、用途、状态", "Search file, role, status"))
        role_labels = {
            "main_background": ui_text("背景", "Background"),
            "hero_image": ui_text("主视觉", "Hero"),
            "logo": "Logo",
            "icon": ui_text("图标", "Icon"),
            "content_image": ui_text("内容图", "Content"),
            "texture": ui_text("纹理", "Texture"),
        }
        for role, button in self.role_buttons.items():
            button.setText(role_labels[role])
        usage_labels = {
            "music": ui_text("音乐", "Music"),
            "voice": ui_text("人声", "Voice"),
            "ambience": ui_text("环境", "Ambient"),
            "sound_effect": ui_text("音效", "SFX"),
            "loop": ui_text("循环", "Loop"),
        }
        for usage, button in self.audio_usage_buttons.items():
            button.setText(usage_labels[usage])
        self.retry_ai_button.setText(ui_text("重新 AI 分拣", "Retry AI sorting"))
        self.accept_ai_button.setText(ui_text("采纳 AI 建议", "Accept AI suggestion"))
        self.copy_selected_button.setText(ui_text("复制 handoff", "Copy handoff"))
        self.copy_ready_button.setText(ui_text("复制可用 handoff", "Copy ready handoff"))
        self.copy_recipe_button.setText(ui_text("复制 Agent 配方", "Copy Agent recipe"))
        self.scope_buttons["latest"].setText(ui_text("最新批次", "Latest batch"))
        self.scope_buttons["all"].setText(ui_text("全部素材", "All assets"))
        self.retry_batch_button.setText(ui_text("重试整理", "Retry sorting"))
        self.refresh()

    def show_panel(self) -> None:
        if self._embedded:
            self._leave_search_input_mode()
            self.show()
            return
        self._hiding_panel = False
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        if not self.isVisible():
            self._opacity_effect.setOpacity(0.0)
        self.show()
        self.raise_()
        self._leave_search_input_mode()
        QTimer.singleShot(0, self._leave_search_input_mode)
        self._fade_animation.setDuration(170)
        self._fade_animation.setStartValue(self._opacity_effect.opacity())
        self._fade_animation.setEndValue(1.0)
        self._fade_animation.start()

    def hide_panel(self) -> None:
        if self.confirmation_preview is not None:
            self.confirmation_preview.hide_preview()
        self._leave_search_input_mode()
        if self._embedded:
            self.hide()
            return
        if not self.isVisible():
            return
        self._hiding_panel = True
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        self._fade_animation.setDuration(170)
        self._fade_animation.setStartValue(self._opacity_effect.opacity())
        self._fade_animation.setEndValue(0.0)
        self._fade_animation.start()

    def _on_fade_finished(self) -> None:
        if self._hiding_panel:
            self._hiding_panel = False
            self.hide()

    def refresh(self) -> None:
        summary = build_material_panel_summary()
        service = BundleService()
        list_bundles = getattr(service, "list_bundles", None)
        if callable(list_bundles):
            all_bundles = list_bundles()
        else:
            # Keep lightweight adapters usable while the production service
            # still takes the single full-scan path above.
            all_bundles = []
            for item in summary.recent_items:
                source_key = str(item.source_key or "").strip()
                bundle_id = Path(source_key).stem or Path(item.preview_url).stem
                bundle = service.get_bundle(bundle_id) if bundle_id else None
                if isinstance(bundle, dict):
                    all_bundles.append(bundle)
        if getattr(service, "theme_recoveries", []):
            if self._toast_callback is not None:
                self._toast_callback(
                    ui_text(
                        "已隔离损坏的主题记录并创建恢复副本",
                        "Damaged theme metadata was quarantined and replaced",
                    ),
                    False,
                )
            else:
                self._theme_recovery_notice_pending = True
        self._bundle_by_source_key = {
            str(bundle.get("source_key") or ""): bundle
            for bundle in all_bundles
            if str(bundle.get("source_key") or "")
        }
        if self._batch_scope == "latest":
            scoped_bundles = list_bundles(batch_id="latest") if callable(list_bundles) else all_bundles
            scoped_ids = {str(bundle.get("id") or "") for bundle in scoped_bundles}
            scoped_items = [
                item for item in summary.recent_items if self._bundle_for_item(item)["id"] in scoped_ids
            ]
        else:
            scoped_items = list(summary.recent_items)
            scoped_bundles = [
                self._bundle_by_source_key[str(item.source_key or "").strip()]
                for item in scoped_items
                if str(item.source_key or "").strip() in self._bundle_by_source_key
            ]
        self._all_recent_items = scoped_items
        self._recent_items = self._filter_recent_items(scoped_items)
        page_size = len(self.item_labels)
        page_count = max(1, math.ceil(len(self._recent_items) / page_size))
        self._page_index = min(self._page_index, page_count - 1)
        page_start = self._page_index * page_size
        self._visible_items = self._recent_items[page_start : page_start + page_size]
        self.page_row.setVisible(len(self._recent_items) > page_size)
        self.previous_page_button.setEnabled(self._page_index > 0)
        self.next_page_button.setEnabled(self._page_index + 1 < page_count)
        self.page_label.setText(
            ui_text(
                f"第 {self._page_index + 1}/{page_count} 页",
                f"Page {self._page_index + 1}/{page_count}",
            )
        )
        selected_id = self._selected_bundle_id
        self.detail_label.hide()
        self.role_row.hide()
        self.audio_usage_row.hide()
        self.retry_ai_button.hide()
        self.accept_ai_button.hide()
        self._suggested_ai_role = ""
        self.preview_label.hide()
        self.copy_selected_button.setEnabled(False)
        self.copy_ready_button.setVisible(bool(scoped_items) and not self._embedded)
        if not scoped_items:
            self.detail_label.setText(ui_text("拖入图片或音频开始收纳", "Drop images or audio to start storing"))
            self.detail_label.show()
        elif not self._recent_items:
            self.detail_label.setText(ui_text("没有匹配资源", "No matching assets"))
            self.detail_label.show()
        if summary.project_display_label:
            self.project_label.setText(summary.project_display_label)
            self.project_label.setToolTip(summary.real_project_root)
            self.project_label.setStyleSheet(
                "QLabel { "
                f"color: {self._project_display_color(summary.project_display_state)}; "
                "font-size: 11px; background: transparent; border: none; }"
            )
        else:
            self.project_label.setText("")
            self.project_label.setToolTip("")
        self.project_label.hide()

        total_count = len(scoped_bundles)
        recognized_count = sum(1 for bundle in scoped_bundles if bundle.get("status") == "ready")
        pending_count = sum(1 for bundle in scoped_bundles if bundle.get("status") == "pending")
        if self._embedded:
            summary_text = ui_text(
                f"{total_count} 个素材 · 可用 {recognized_count}\n待确认 {pending_count}",
                f"{total_count} assets · ready {recognized_count}\npending {pending_count}",
            )
        else:
            summary_text = ui_text(
                f"{summary.total_count} 个 bundle · 可用 {summary.recognized_count} · 待确认 {summary.pending_count}",
                f"{summary.total_count} bundles · ready {summary.recognized_count} · pending {summary.pending_count}",
            )
        self.summary_label.setText(summary_text)
        if summary.pending_count and not self._embedded:
            self.pending_label.setText(ui_text(f"{summary.pending_count} 个待确认用途", f"{summary.pending_count} roles need review"))
            self.pending_label.show()
        else:
            self.pending_label.setText("")
            self.pending_label.hide()

        selected_item = None
        for idx, label in enumerate(self.item_labels):
            if idx >= len(self._visible_items):
                label.hide()
                continue
            item = self._visible_items[idx]
            bundle = self._bundle_for_item(item)
            selected = bool(selected_id and bundle["id"] == selected_id)
            if selected:
                selected_item = item
            label.setText(
                f"{item.title}\n"
                f"{self._asset_type_label(item.asset_type)} · {self._display_role_label(item.usage_label)} · "
                f"{self._agent_usability_label(bundle['status'])}"
            )
            label.setStyleSheet(self._item_label_style(selected))
            label.setToolTip(item.origin_url or item.source_key or item.preview_url)
            label.show()

        if selected_item is not None:
            selected_bundle = self._bundle_for_item(selected_item)
            self._show_preview_for_item(selected_item)
            self._show_detail_for_bundle(selected_bundle)

        service_lines = [summary.recognition_status, summary.service_status]
        if summary.project_picker_status_line:
            service_lines.append(summary.project_picker_status_line)
        self.service_label.setText("\n".join(line for line in service_lines if line))
        if summary.panel_display_text:
            self.rehearsal_label.setText(summary.panel_display_text)
            self.rehearsal_label.setToolTip(summary.project_picker_tooltip or summary.panel_status_text)
        else:
            self.rehearsal_label.setText("")
            self.rehearsal_label.setToolTip("")
        self.rehearsal_label.hide()
        self.service_label.hide()
        self._confirmation_available = summary.confirmation_available
        latest = service.get_latest_batch() if hasattr(service, "get_latest_batch") else None
        self.retry_batch_button.setEnabled(
            self._batch_scope == "latest" and latest is not None and bool(scoped_bundles)
        )
        if self.confirmation_preview is not None:
            self.confirmation_preview.update_prompt(
                title=summary.confirmation_title,
                body=summary.confirmation_body,
                summary=summary.confirmation_summary,
                warning=summary.confirmation_warning,
                action=summary.confirmation_action,
                project_root=summary.real_project_root,
                primary_label=summary.confirmation_primary_label,
            )
            if not self._confirmation_available:
                self.confirmation_preview.hide()

    def _set_filter_mode(self, mode: str) -> None:
        self._leave_search_input_mode()
        self._filter_mode = mode
        self._page_index = 0
        self._refresh_filter_buttons()
        self.refresh()

    def set_batch_scope(self, scope: str) -> None:
        normalized = scope if scope in {"latest", "all"} else "latest"
        if normalized == self._batch_scope:
            self.refresh()
            return
        self._batch_scope = normalized
        self._selected_bundle_id = ""
        self._page_index = 0
        self._refresh_scope_buttons()
        self.refresh()

    def _on_search_changed(self, _text: str) -> None:
        self._page_index = 0
        self.refresh()

    def _change_page(self, delta: int) -> None:
        page_size = len(self.item_labels)
        page_count = max(1, math.ceil(len(self._recent_items) / page_size))
        target = max(0, min(self._page_index + int(delta), page_count - 1))
        if target == self._page_index:
            return
        self._page_index = target
        self.refresh()

    def _refresh_scope_buttons(self) -> None:
        for scope, button in self.scope_buttons.items():
            active = scope == self._batch_scope
            button.setStyleSheet(
                "QPushButton { "
                f"color: {'#FFF9EA' if active else '#4E5F3D'}; "
                f"background: {'#6F7F5A' if active else '#F6F1E4'}; "
                "border: 1px solid #DDD3BB; border-radius: 6px; font-size: 10px; }"
            )
        self.retry_batch_button.setStyleSheet(
            "QPushButton { color: #4E5F3D; background: #FFF9EA; "
            "border: 1px solid #E5D8B9; border-radius: 6px; font-size: 10px; }"
        )

    def _retry_latest_batch(self) -> None:
        if self._retry_batch_callback is not None:
            self._retry_batch_callback()

    def _refresh_filter_buttons(self) -> None:
        for mode, button in self.filter_buttons.items():
            active = mode == self._filter_mode
            button.setStyleSheet(
                "QPushButton { "
                f"color: {'#FFF9EA' if active else '#4E5F3D'}; "
                f"background: {'#6F7F5A' if active else '#F6F1E4'}; "
                "border: 1px solid #DDD3BB; border-radius: 6px; font-size: 10px; }"
                "QPushButton:hover { background: #EFE3C7; color: #4E5F3D; }"
            )

    def _filter_recent_items(self, items) -> list:
        query = self.search_input.text().strip().lower()
        return [
            item
            for item in items
            if self._item_matches_filter_mode(item) and self._item_matches_query(item, query)
        ]

    def _item_matches_filter_mode(self, item) -> bool:
        mode = self._filter_mode
        if mode == "all":
            return True
        if mode in {"image", "audio"}:
            return str(item.asset_type or "").lower() == mode
        return self._bundle_for_item(item)["status"] == mode

    def _item_matches_query(self, item, query: str) -> bool:
        if not query:
            return True
        bundle = self._bundle_for_item(item)
        haystack = "\n".join(
            str(value or "").lower()
            for value in (
                item.title,
                item.usage_label,
                self._display_role_label(item.usage_label),
                self._asset_type_label(item.asset_type),
                item.status_label,
                self._bundle_status_label(bundle["status"]),
                self._agent_usability_label(bundle["status"]),
                bundle["id"],
                bundle["role"],
                self._role_label(bundle["role"]),
                bundle["status"],
                bundle["type"],
                bundle.get("audio_usage", ""),
                bundle.get("audio_tags", {}),
                item.preview_url,
                item.theme_id,
                item.asset_type,
                item.source_key,
                item.origin_url,
                bundle["source_key"],
                bundle["url"],
                bundle.get("origin_url", ""),
            )
        )
        return query in haystack

    def _select_recent_item(self, index: int, event: QMouseEvent) -> None:
        if index >= len(self._visible_items):
            event.ignore()
            return
        self._leave_search_input_mode()
        item = self._visible_items[index]
        bundle = self._bundle_for_item(item)
        self._selected_bundle_id = bundle["id"]
        self._refresh_item_selection_styles()
        self._show_preview_for_item(item)
        self._show_detail_for_bundle(bundle)
        event.accept()

    def _show_detail_for_bundle(self, bundle: dict[str, object], *, copied: bool = False, confirmed: bool = False) -> None:
        handoff_line = (
            ui_text("已复制 handoff · provenance 已包含", "handoff copied · provenance included")
            if copied
            else ui_text("handoff 可复制 · provenance 已包含", "handoff ready · provenance included")
        )
        status_line = (
            ui_text(
                f"已确认：{self._role_label(bundle['role'])} · {self._agent_usability_label(bundle['status'])}",
                f"Confirmed: {self._role_label(bundle['role'])} · {self._agent_usability_label(bundle['status'])}",
            )
            if confirmed
            else f"{bundle['id']} · {self._bundle_status_label(bundle['status'])} · {self._agent_usability_label(bundle['status'])}"
        )
        origin_url = public_origin_url(str(bundle.get("origin_url") or ""))
        origin_line = f"\norigin {self._compact_text(origin_url, 48)}" if origin_url else ""
        ai_line = self._ai_suggestion_line(bundle.get("ai_suggestions"))
        is_audio = str(bundle.get("type") or "").lower() == "audio"
        audio_line = self._audio_detail_line(bundle) if is_audio else ""
        self.detail_label.setText(
            f"{status_line}\n"
            f"{ui_text('用途', 'Role')} {self._role_label(bundle['role'])} · {ui_text('类型', 'Type')} {self._asset_type_label(bundle['type'])}\n"
            f"sha256 {str(bundle['sha256'])[:12] or '-'} · key {self._compact_text(str(bundle['source_key']) or '-', 24)}{audio_line}\n"
            f"url {self._compact_text(bundle['url'])}{origin_line}{ai_line}\n"
            f"{handoff_line}"
        )
        self.detail_label.show()
        self.copy_selected_button.setEnabled(bool(bundle.get("id")))
        self.role_row.setVisible(not is_audio and bundle["status"] != "missing")
        self.audio_usage_row.setVisible(is_audio and bundle["status"] != "missing")
        self._suggested_ai_role = self._suggested_role(bundle.get("ai_suggestions"))
        self.accept_ai_button.setVisible(
            not is_audio
            and bundle["status"] != "missing"
            and bool(self._suggested_ai_role)
            and self._suggested_ai_role != str(bundle.get("role") or "")
        )
        if is_audio:
            self._refresh_audio_usage_buttons(str(bundle.get("audio_usage") or "unknown"))
        else:
            self._refresh_role_buttons(bundle["role"])
        self._refresh_retry_ai_button(bundle)

    def _refresh_retry_ai_button(self, bundle: dict[str, object]) -> None:
        visible = (
            str(bundle.get("type") or "").lower() == "image"
            and str(bundle.get("status") or "") != "missing"
            and bool(str(bundle.get("source_key") or "").strip())
        )
        self.retry_ai_button.setVisible(visible)
        if not visible:
            return
        busy = self.ai_refresh_worker is not None and self.ai_refresh_worker.isRunning()
        self.retry_ai_button.setEnabled(not busy)
        self.retry_ai_button.setText(
            ui_text("AI 分拣中...", "AI sorting...")
            if busy
            else ui_text("重新 AI 分拣", "Retry AI sorting")
        )

    def _retry_ai_classification(self) -> None:
        self._leave_search_input_mode()
        if self.ai_refresh_worker is not None and self.ai_refresh_worker.isRunning():
            return
        if not self._selected_bundle_id:
            return
        if not self._panel_ai_enabled():
            self.detail_label.setText(ui_text("AI 分拣未开启\n请先在设置中开启 AI", "AI sorting is off\nEnable it in Settings"))
            self.detail_label.show()
            if self._toast_callback is not None:
                self._toast_callback(ui_text("AI 分拣未开启", "AI sorting is off"), False)
            return
        bundle = BundleService().get_bundle(self._selected_bundle_id)
        if bundle is None:
            self.detail_label.setText(ui_text("资源不存在", "Asset not found"))
            self.detail_label.show()
            return
        self.retry_ai_button.setEnabled(False)
        self.retry_ai_button.setText(ui_text("AI 分拣中...", "AI sorting..."))
        if self._ai_provider_factory is None:
            self.ai_refresh_worker = AIRefreshWorker(bundle, get_settings().ASSETS_DIR)
        else:
            self.ai_refresh_worker = AIRefreshWorker(
                bundle,
                get_settings().ASSETS_DIR,
                self._ai_provider_factory(),
            )
        self.ai_refresh_worker.finished_signal.connect(self._on_ai_refresh_finished)
        self.ai_refresh_worker.start()

    def _on_ai_refresh_finished(self, bundle_id: str, message: str, success: bool) -> None:
        worker = self.ai_refresh_worker
        self.ai_refresh_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._toast_callback is not None:
            self._toast_callback(message, success)
        if bundle_id != self._selected_bundle_id:
            current = BundleService().get_bundle(self._selected_bundle_id) if self._selected_bundle_id else None
            if current is not None:
                self._refresh_retry_ai_button(current)
            return
        bundle = BundleService().get_bundle(bundle_id)
        if bundle is not None:
            self._show_detail_for_bundle(bundle, copied=False)
            if not success:
                self.detail_label.setText(f"{self.detail_label.text()}\n{message}")
        else:
            self.detail_label.setText(message)
            self.detail_label.show()
            self.retry_ai_button.hide()

    def _panel_ai_enabled(self) -> bool:
        if self._ai_enabled_callback is not None:
            return bool(self._ai_enabled_callback())
        settings = get_settings()
        if settings.HAYPILE_LOW_POWER_MODE or not settings.VISION_CLASSIFIER_ENABLED:
            return False
        try:
            payload = json.loads((settings.INDEX_DIR / "gui_state.json").read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return True
        if isinstance(payload, dict) and payload.get("low_power_enabled") is True:
            return False
        stored = payload.get("ai_enabled") if isinstance(payload, dict) else None
        return bool(stored) if isinstance(stored, bool) else True

    def _set_selected_role(self, role: str) -> None:
        if not self._selected_bundle_id:
            return
        try:
            updated = BundleService().set_bundle_role(self._selected_bundle_id, role)
        except ValueError:
            updated = None
        if updated is None:
            self.detail_label.setText(ui_text("用途更新失败", "Role update failed"))
            self.detail_label.show()
            return
        self._selected_bundle_id = updated["id"]
        self.refresh()
        self._show_detail_for_bundle(updated, confirmed=True)

    def _set_selected_audio_usage(self, usage: str) -> None:
        if not self._selected_bundle_id:
            return
        try:
            updated = BundleService().set_bundle_audio_usage(self._selected_bundle_id, usage)
        except ValueError:
            updated = None
        if updated is None:
            self.detail_label.setText(ui_text("音频用途更新失败", "Audio usage update failed"))
            self.detail_label.show()
            return
        self._selected_bundle_id = updated["id"]
        self.refresh()
        self._show_detail_for_bundle(updated, confirmed=True)

    def _copy_selected_handoff(self) -> None:
        self._leave_search_input_mode()
        if not self._selected_bundle_id:
            return
        bundle = BundleService().get_bundle(self._selected_bundle_id)
        if bundle is None:
            return
        QApplication.clipboard().setText(
            json.dumps(self._handoff_for_bundles([bundle]), ensure_ascii=False, indent=2)
        )
        self._show_detail_for_bundle(bundle, copied=True)
        if self._toast_callback is not None:
            self._toast_callback(ui_text("已复制 handoff", "Handoff copied"), True)

    @staticmethod
    def _suggested_role(value: object) -> str:
        if not isinstance(value, dict):
            return ""
        role = str(value.get("usage") or "").strip()
        return role if role in {
            "main_background",
            "hero_image",
            "logo",
            "icon",
            "content_image",
            "texture",
        } else ""

    def _accept_ai_suggestion(self) -> None:
        if self._suggested_ai_role:
            self._set_selected_role(self._suggested_ai_role)

    def _show_preview_for_item(self, item) -> None:
        self.preview_label.clear()
        asset_type = str(item.asset_type or "").lower()
        if asset_type == "audio":
            self.preview_label.setText(ui_text("音频资源\n不在这里播放，agent 通过 URL 使用", "Audio asset\nAgents use it through URL"))
            self.preview_label.show()
            return
        if asset_type != "image" or not item.source_key:
            self.preview_label.hide()
            return
        pixmap = QPixmap(str(get_settings().ASSETS_DIR / item.source_key))
        if pixmap.isNull():
            self.preview_label.hide()
            return
        self.preview_label.setPixmap(
            pixmap.scaled(
                292,
                64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.preview_label.show()

    @staticmethod
    def _asset_type_label(asset_type: str) -> str:
        value = str(asset_type or "").lower()
        if value == "image":
            return ui_text("图片", "Image")
        if value == "audio":
            return ui_text("音频", "Audio")
        return value or ui_text("资源", "Asset")

    @staticmethod
    def _audio_usage_label(audio_usage: str) -> str:
        return {
            "music": ui_text("音乐", "Music"),
            "voice": ui_text("人声", "Voice"),
            "ambience": ui_text("环境", "Ambient"),
            "sound_effect": ui_text("音效", "SFX"),
            "loop": ui_text("循环", "Loop"),
            "unknown": ui_text("未确定", "Unconfirmed"),
        }.get(str(audio_usage or "").lower(), ui_text("未确定", "Unconfirmed"))

    @classmethod
    def _audio_detail_line(cls, bundle: dict[str, object]) -> str:
        details = [
            f"{ui_text('音频用途', 'Audio usage')} {cls._audio_usage_label(str(bundle.get('audio_usage') or 'unknown'))}"
        ]
        tags = bundle.get("audio_tags")
        if isinstance(tags, dict):
            details.extend(
                str(tags.get(key) or "").strip()[:48]
                for key in ("title", "artist", "album")
                if str(tags.get(key) or "").strip()
            )
        try:
            duration = float(bundle.get("duration_seconds") or 0)
        except (TypeError, ValueError):
            duration = 0
        if duration > 0:
            details.append(f"{duration:.1f}s")
        metadata = bundle.get("audio_metadata")
        if isinstance(metadata, dict):
            sample_rate = metadata.get("sample_rate_hz")
            channels = metadata.get("channels")
            if isinstance(sample_rate, (int, float)) and sample_rate > 0:
                details.append(f"{round(sample_rate / 1000):.0f} kHz")
            if isinstance(channels, (int, float)) and channels > 0:
                details.append(ui_text(f"{int(channels)} 声道", f"{int(channels)} ch"))
        return "\n" + " · ".join(details)

    @staticmethod
    def _agent_usability_label(status: str) -> str:
        return {
            "ready": ui_text("agent 可用", "agent-ready"),
            "pending": ui_text("需确认后给 agent", "review before agent"),
            "missing": ui_text("agent 不可用", "not available to agent"),
        }.get(status, ui_text("需复核", "needs review"))

    @staticmethod
    def _ai_suggestion_line(value: object) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        tags = value.get("tags")
        tag_text = "、".join(str(item).strip() for item in tags if str(item).strip()) if isinstance(tags, list) else ""
        parts = [
            str(value.get("quality") or "").strip(),
            tag_text,
            str(value.get("agent_summary") or "").strip(),
            str(value.get("reason") or "").strip(),
        ]
        text = " · ".join(part for part in parts if part)
        return f"\nAI {text}" if text else ""

    @staticmethod
    def _compact_text(value: str, max_chars: int = 36) -> str:
        if len(value) <= max_chars:
            return value
        return f"{value[:18]}...{value[-15:]}"

    @staticmethod
    def _item_label_style(selected: bool) -> str:
        if selected:
            return (
                "QLabel { color: #2F3A26; font-size: 11px; font-weight: bold; "
                "padding: 7px 8px; background: #FFF2C4; "
                "border: 2px solid #C8A24A; border-radius: 8px; }"
            )
        return (
            "QLabel { color: #444444; font-size: 11px; "
            "padding: 7px 8px; background: #F6F1E4; "
            "border: 1px solid #DDD3BB; border-radius: 8px; }"
        )

    @staticmethod
    def _role_button_style(active: bool) -> str:
        return (
            "QPushButton { "
            f"color: {'#FFF9EA' if active else '#4E5F3D'}; "
            f"background: {'#6F7F5A' if active else '#F6F1E4'}; "
            "border: 1px solid #DDD3BB; border-radius: 6px; font-size: 10px; }"
            "QPushButton:hover { background: #EFE3C7; color: #4E5F3D; }"
        )

    def _refresh_role_buttons(self, active_role: str = "") -> None:
        for role, button in self.role_buttons.items():
            button.setStyleSheet(self._role_button_style(role == active_role))

    def _refresh_audio_usage_buttons(self, active_usage: str = "") -> None:
        for usage, button in self.audio_usage_buttons.items():
            button.setStyleSheet(self._role_button_style(usage == active_usage))

    def _refresh_item_selection_styles(self) -> None:
        for idx, label in enumerate(self.item_labels):
            if idx >= len(self._visible_items) or label.isHidden():
                continue
            bundle = self._bundle_for_item(self._visible_items[idx])
            label.setStyleSheet(self._item_label_style(bundle["id"] == self._selected_bundle_id))

    @staticmethod
    def _bundle_status_label(status: str) -> str:
        return {
            "ready": ui_text("可用", "ready"),
            "pending": ui_text("待确认", "pending"),
            "missing": ui_text("缺失", "missing"),
        }.get(status, status or "unknown")

    @staticmethod
    def _role_label(role: str) -> str:
        return {
            "main_background": ui_text("背景", "Background"),
            "hero_image": ui_text("主视觉", "Hero"),
            "logo": "Logo",
            "icon": ui_text("图标", "Icon"),
            "content_image": ui_text("内容图", "Content image"),
            "texture": ui_text("纹理", "Texture"),
            "audio": ui_text("音频", "Audio"),
            "unknown": ui_text("未确定", "Unknown"),
            "参考图": ui_text("参考图", "Reference"),
            "未确定": ui_text("未确定", "Unknown"),
            "背景": ui_text("背景", "Background"),
            "主视觉": ui_text("主视觉", "Hero"),
            "图标": ui_text("图标", "Icon"),
            "纹理": ui_text("纹理", "Texture"),
        }.get(role, role)

    @classmethod
    def _display_role_label(cls, role: str) -> str:
        return cls._role_label(str(role or "unknown"))

    def _copy_ready_handoff(self) -> None:
        self._leave_search_input_mode()
        bundles = BundleService().list_bundles(status="ready")
        if not bundles:
            self.detail_label.setText(ui_text("没有可用 assets\n先拖入或确认用途", "No ready assets\nDrop files or confirm roles"))
            self.detail_label.show()
            return
        QApplication.clipboard().setText(json.dumps(self._handoff_for_bundles(bundles), ensure_ascii=False, indent=2))
        self.copy_ready_button.setText(ui_text(f"已复制 {len(bundles)} 个可用", f"Copied {len(bundles)} ready"))
        QTimer.singleShot(900, lambda: self.copy_ready_button.setText(ui_text("复制可用 handoff", "Copy ready handoff")))
        self.detail_label.setText(ui_text(f"已复制 {len(bundles)} 个可用 assets\n可交给 agent", f"Copied {len(bundles)} ready assets\nReady for agent"))
        self.detail_label.show()

    def _copy_agent_recipe(self) -> None:
        self._leave_search_input_mode()
        QApplication.clipboard().setText(self._agent_recipe_text())
        self.copy_recipe_button.setText(ui_text("已复制 agent 配方", "Agent recipe copied"))
        QTimer.singleShot(900, lambda: self.copy_recipe_button.setText(ui_text("复制 agent 配方", "Copy agent recipe")))
        self.detail_label.setText(ui_text("已复制 agent 配方\n按步骤读取可用 assets", "Agent recipe copied\nUse ready assets only"))
        self.detail_label.show()

    def _agent_recipe_text(self) -> str:
        base_url = self._base_url()
        return "\n".join(
            [
                "Haypile agent recipe",
                f"Base URL: {base_url}",
                f"List latest ready assets: GET {base_url}/api/v1/bundles?status=ready&batch_id=latest",
                'Default batch selector: batch_id="latest".',
                "Use each bundle's id, sha256, source_key, url, resolved_url, and provenance.",
                "Fetch files through resolved_url or the MCP haypile_list_bundles tool.",
                "Do not read Haypile's local asset directory directly.",
            ]
        )

    def _bundle_for_item(self, item) -> dict[str, str]:
        source_key = str(item.source_key or "").strip()
        bundle = self._bundle_by_source_key.get(source_key) if source_key else None
        if bundle is not None:
            return bundle
        return {
            "id": Path(source_key).stem or Path(item.preview_url).stem or "bundle",
            "theme_id": item.theme_id,
            "type": item.asset_type or "asset",
            "role": item.usage_label,
            "status": "pending" if str(item.status_label).lower() in {"待确认", "pending"} else "ready",
            "sha256": "",
            "url": item.preview_url,
            "access": "manifest_static",
            "source_key": source_key,
            "origin_url": item.origin_url,
            "content_type": "",
            "downloaded_at": "",
            "ai_suggestions": {},
            "duration_seconds": None,
            "audio_metadata": {},
            "audio_tags": {},
            "audio_usage": "unknown",
        }

    def _handoff_for_bundles(
        self,
        bundles: list[dict[str, object]],
        *,
        batch_id: str = "",
    ) -> dict[str, object]:
        base_url = self._base_url()
        try:
            readiness = read_manifest_readiness(get_settings().MANIFEST_PATH)
            manifest_generation = str(readiness["manifest_generation"])
        except ManifestReadinessError:
            manifest_generation = ""
        payload = {
            "handoff_version": "haypile.asset-handoff.v1",
            "handoff_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "haypile",
            "base_url": base_url,
            "manifest_generation": manifest_generation,
            "asset_count": len(bundles),
            "total_matching": len(bundles),
            "complete": True,
            "next_cursor": None,
            "assets": [self._handoff_asset(item, base_url) for item in bundles],
        }
        if batch_id:
            payload["batch_id"] = batch_id
        return payload

    @staticmethod
    def _handoff_asset(item: dict[str, object], base_url: str) -> dict[str, object]:
        resolved_url = base_url + str(item["url"])
        public_metadata = sanitize_provenance(
            {
                "origin_url": item.get("origin_url", ""),
                "content_type": item.get("content_type", ""),
                "downloaded_at": item.get("downloaded_at", ""),
                "ai_suggestions": item.get("ai_suggestions", {}),
            }
        )
        return {
            "id": item["id"],
            "theme_id": item["theme_id"],
            "type": item["type"],
            "role": item["role"],
            "status": item["status"],
            "sha256": item["sha256"],
            "source_key": item["source_key"],
            "url": item["url"],
            "access": item["access"],
            "resolved_url": resolved_url,
            "ai_suggestions": public_metadata.get("ai_suggestions", {}),
            "duration_seconds": item.get("duration_seconds"),
            "audio_metadata": item.get("audio_metadata", {}),
            "audio_tags": item.get("audio_tags", {}),
            "audio_usage": item.get("audio_usage", "unknown"),
            "provenance": {
                "source": "haypile",
                "id": item["id"],
                "sha256": item["sha256"],
                "source_key": item["source_key"],
                "url": item["url"],
                "resolved_url": resolved_url,
                "access": item["access"],
                "origin_url": public_metadata.get("origin_url", ""),
                "content_type": public_metadata.get("content_type", ""),
                "downloaded_at": public_metadata.get("downloaded_at", ""),
            },
        }

    @staticmethod
    def _base_url() -> str:
        settings = get_settings()
        host = settings.HOST if settings.HOST != "0.0.0.0" else "127.0.0.1"
        return f"http://{host}:{settings.PORT}"

    def _leave_search_input_mode(self) -> None:
        self.search_input.clearFocus()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._leave_search_input_mode()
        super().mousePressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.container.setGeometry(self.rect())

    @staticmethod
    def _project_display_color(state: str) -> str:
        if state == "applied_verified":
            return "#4E7A46"
        if state == "rolled_back":
            return "#6F7F5A"
        if state in {"needs_review", "rollback_incomplete"}:
            return "#A67624"
        return "#666666"

    def hideEvent(self, event) -> None:
        if self.confirmation_preview is not None:
            self.confirmation_preview.hide_preview()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        if self.confirmation_preview is not None:
            self.confirmation_preview.hide()
            self.confirmation_preview.close()
        super().closeEvent(event)

    def _show_confirmation_preview(self, event: QMouseEvent) -> None:
        preview = self.confirmation_preview
        if not self._confirmation_available or preview is None:
            event.ignore()
            return
        anchor = self.frameGeometry()
        preview_x = anchor.left()
        preview_y = anchor.bottom() + 8
        screen = QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if preview_y + preview.height() > available.bottom() - 10:
                preview_y = anchor.top() - preview.height() - 8
            preview_x = max(
                available.left() + 10,
                min(preview_x, available.right() - preview.width() - 10),
            )
            preview_y = max(
                available.top() + 10,
                min(preview_y, available.bottom() - preview.height() - 10),
            )
        preview.move(preview_x, preview_y)
        preview.show_at(preview_x, preview_y)
        event.accept()

    def _execute_confirmation_action(self, action: str, project_root: str) -> None:
        preview = self.confirmation_preview
        if preview is None:
            return
        try:
            if action == "reapply":
                result = execute_haypile_minimal_real_project_reapply(
                    project_root=project_root,
                    human_confirmed=True,
                )
                title = ui_text("已重新投放", "Reapplied")
            elif action == "rollback":
                result = execute_haypile_minimal_real_project_rollback(
                    project_root=project_root,
                    human_confirmed=True,
                )
                title = ui_text("已撤回", "Rolled back")
            else:
                raise HaypileRealProjectOperationError("unknown confirmation action")
        except HaypileRealProjectOperationError as exc:
            preview.show_result(
                success=False,
                title=ui_text("未执行", "Not executed"),
                body=str(exc),
                warning=ui_text("请复核项目状态后再试。", "Review project state and try again."),
            )
            return
        self.refresh()
        preview.show_result(
            success=True,
            title=title,
            body=ui_text(f"{result.get('operation_count', 0)} 项已处理", f"{result.get('operation_count', 0)} items processed"),
            warning=ui_text("状态已刷新，可继续验收。", "Status refreshed."),
        )


class _LegacyQuickMenuWindow(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedSize(240, 240)
        self._action_callback = None
        self._hovered_action = ""
        self._track_center = QPointF(120, 120)
        self._content_shift = QPointF(0, 0)
        self._attention_action = ""
        self._ai_enabled = False
        self._hide_after_slide = False
        self.setWindowOpacity(0.0)
        self._fade_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_animation.setDuration(170)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.finished.connect(self._on_fade_finished)
        self._slide_animation = QVariantAnimation(self)
        self._slide_animation.setDuration(170)
        self._slide_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide_animation.valueChanged.connect(self._set_content_shift)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3600)
        self._hide_timer.timeout.connect(self.hide_menu)

        self.actions = [
            ("mcp", "mcp", ui_text("复制 MCP 配置", "Copy MCP config")),
            ("http", "http", ui_text("复制 HTTP 地址", "Copy HTTP URL")),
            ("assets", "assets", "Haypile"),
            ("status", "status", ui_text("服务状态", "Service status")),
            ("ai", "ai", ui_text("AI 分拣", "AI sorting")),
        ]
        self.action_tooltips = {action: tooltip for action, _icon, tooltip in self.actions}
        self.hide()

    def set_action_handler(self, callback) -> None:
        self._action_callback = callback

    def set_track_center(self, center: QPointF) -> None:
        self._track_center = QPointF(center)
        self.update()

    def set_attention_action(self, action: str) -> None:
        self._attention_action = action
        self.update()

    def set_ai_enabled(self, enabled: bool, status_text: str = "") -> None:
        self._ai_enabled = bool(enabled)
        self.action_tooltips["ai"] = status_text or ui_text(
            "AI 分拣：开" if self._ai_enabled else "AI 分拣：关",
            "AI sorting: on" if self._ai_enabled else "AI sorting: off",
        )
        self.update()

    def _get_content_shift(self) -> QPointF:
        return QPointF(self._content_shift)

    def _set_content_shift(self, shift: QPointF) -> None:
        self._content_shift = QPointF(shift)
        self.update()

    def show_menu(self, x: int, y: int) -> None:
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        if self._slide_animation.state() == QPropertyAnimation.State.Running:
            self._slide_animation.stop()
        self._hovered_action = ""
        self.setToolTip("")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hide_after_slide = False
        self.move(QPoint(x, y))
        start_shift = QPointF(0, 0)
        self._set_content_shift(start_shift)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade_animation.setDuration(170)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide_animation.setDuration(170)
        self._slide_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_animation.setStartValue(0.0)
        self._fade_animation.setEndValue(1.0)
        self._slide_animation.setStartValue(start_shift)
        self._slide_animation.setEndValue(QPointF(0, 0))
        self._fade_animation.start()
        self._slide_animation.start()
        self._hide_timer.start()

    def hide_menu(self) -> None:
        self._hide_timer.stop()
        self._hovered_action = ""
        self.setToolTip("")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        if not self.isVisible():
            return
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        if self._slide_animation.state() == QPropertyAnimation.State.Running:
            self._slide_animation.stop()
        self._hide_after_slide = True
        self._fade_animation.setDuration(135)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._slide_animation.setDuration(135)
        self._slide_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._fade_animation.setStartValue(self.windowOpacity())
        self._fade_animation.setEndValue(0.0)
        self._slide_animation.setStartValue(self._content_shift)
        self._slide_animation.setEndValue(self._slide_offset())
        self._fade_animation.start()
        self._slide_animation.start()

    def _on_fade_finished(self) -> None:
        if self._hide_after_slide:
            self._hide_after_slide = False
            self._set_content_shift(QPointF(0, 0))
            self.hide()

    def _slide_offset(self) -> QPointF:
        dx = self._track_center.x() - self.width() / 2
        dy = self._track_center.y() - self.height() / 2
        length = math.hypot(dx, dy)
        if length < 1:
            return QPointF(0, 22)
        scale = 26 / length
        return QPointF(dx * scale, dy * scale)

    def enterEvent(self, event) -> None:
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered_action = ""
        self.setToolTip("")
        self.update()
        self._hide_timer.start()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        action = self._action_at(event.position())
        if action != self._hovered_action:
            self._hovered_action = action
            self.setToolTip(self.action_tooltips.get(action, ""))
            self.setCursor(Qt.CursorShape.PointingHandCursor if action else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            action = self._action_at(event.position())
            if action:
                self._emit_action(action)
                event.accept()
                return
            self.hide_menu()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        if getattr(self, "_feedback_only", False):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self._content_shift)

        track_center, track_radius = self._track_geometry()
        arc_rect = QRectF(
            track_center.x() - track_radius,
            track_center.y() - track_radius,
            track_radius * 2,
            track_radius * 2,
        )
        shadow_pen = QPen(QColor(0, 0, 0, 10), 14)
        shadow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        shadow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(shadow_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        start_angle, span_angle = self._arc_angles()
        painter.drawArc(arc_rect.translated(2, 4), start_angle * 16, span_angle * 16)

        lift_pen = QPen(QColor(255, 249, 225, 104), 16)
        lift_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        lift_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(lift_pen)
        painter.drawArc(arc_rect, start_angle * 16, span_angle * 16)

        arc_pen = QPen(QColor(54, 69, 46, 108), 12)
        arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        arc_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(arc_pen)
        painter.drawArc(arc_rect, start_angle * 16, span_angle * 16)

        highlight_pen = QPen(QColor(110, 148, 79, 52), 12)
        highlight_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        highlight_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(highlight_pen)
        painter.drawArc(arc_rect, (start_angle + span_angle // 2 - 34) * 16, min(68, span_angle) * 16)

        for action, icon_name, _tooltip in self.actions:
            slot_rect = self._slot_rect(action)
            hovered = action == self._hovered_action
            attention = action == self._attention_action
            selected = action == getattr(self, "_drawer_page", "")
            active = attention or selected or (action == "ai" and self._ai_enabled)
            ai_on = action == "ai" and self._ai_enabled
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 30))
            painter.drawEllipse(slot_rect.translated(1.0, 1.5))
            if active and not hovered:
                painter.setBrush(QColor(233, 182, 54, 96 if ai_on else 58))
                painter.drawEllipse(slot_rect.adjusted(-6 if ai_on else -4, -6 if ai_on else -4, 6 if ai_on else 4, 6 if ai_on else 4))
            painter.setPen(QPen(QColor(255, 249, 234, 150 if (hovered or active) else 84), 1.1))
            painter.setBrush(
                QColor(233, 182, 54, 214)
                if hovered
                else QColor(224, 167, 45, 246)
                if ai_on
                else QColor(65, 78, 42, 246)
                if active
                else QColor(47, 61, 39, 238)
            )
            painter.drawEllipse(slot_rect)
            fg = QColor("#2E3A26") if (hovered or ai_on) else QColor("#FFF9EA")
            self._draw_action_icon(painter, icon_name, slot_rect.center(), fg)
        self._paint_overlay(painter)
        painter.end()

    def _paint_overlay(self, painter: QPainter) -> None:
        del painter

    def _track_geometry(self) -> tuple[QPointF, int]:
        return QPointF(self._track_center), 76

    def _slot_rect(self, action: str) -> QRectF:
        center, radius = self._track_geometry()
        angles = dict(zip([action for action, _icon, _tooltip in self.actions], self._slot_angles()))
        angle = math.radians(angles[action])
        x = center.x() + radius * math.cos(angle)
        y = center.y() + radius * math.sin(angle)
        return QRectF(x - 14, y - 14, 28, 28)

    def _slot_angles(self) -> list[int]:
        center, _radius = self._track_geometry()
        left = center.x() < 76
        right = center.x() > self.width() - 76
        top = center.y() < 76
        bottom = center.y() > self.height() - 76
        if left and top:
            return self._spread_angles(10, 85)
        if left and bottom:
            return self._spread_angles(-85, -10)
        if right and top:
            return self._spread_angles(95, 170)
        if right and bottom:
            return self._spread_angles(-170, -95)
        if left:
            return self._spread_angles(-70, 70)
        if right:
            return self._spread_angles(110, 250)
        if top:
            return self._spread_angles(20, 160)
        if bottom:
            return self._spread_angles(-160, -20)
        return [-55, -105, -155, 155, 105][: len(self.actions)]

    def _spread_angles(self, start: int, end: int) -> list[int]:
        count = len(self.actions)
        if count <= 1:
            return [start]
        step = (end - start) / (count - 1)
        return [round(start + step * index) for index in range(count)]

    def _arc_angles(self) -> tuple[int, int]:
        angles = self._slot_angles()
        pad = 10
        screen_start = min(angles) - pad
        screen_end = max(angles) + pad
        return -screen_end, screen_end - screen_start

    def _action_at(self, point: QPointF) -> str:
        point = QPointF(point) - self._content_shift
        for action, _icon_name, _tooltip in self.actions:
            if self._slot_rect(action).contains(point):
                return action
        return ""

    def _draw_action_icon(self, painter: QPainter, icon_name: str, center: QPointF, color: QColor) -> None:
        painter.save()
        painter.translate(center)
        painter.setPen(QPen(color, 2.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if icon_name == "assets":
            path = QPainterPath()
            path.moveTo(-8, -3)
            path.lineTo(-3, -3)
            path.lineTo(-1, -6)
            path.lineTo(9, -6)
            path.lineTo(9, 7)
            path.lineTo(-8, 7)
            path.closeSubpath()
            painter.drawPath(path)
        elif icon_name == "mcp":
            painter.drawEllipse(QRectF(-9, -10, 6, 6))
            painter.drawEllipse(QRectF(4, -10, 6, 6))
            painter.drawEllipse(QRectF(-2.5, 4, 6, 6))
            painter.drawLine(QPointF(-3, -7), QPointF(4, -7))
            painter.drawLine(QPointF(0, -4), QPointF(0, 4))
        elif icon_name == "http":
            painter.drawArc(QRectF(-10, -5, 12, 10), 35 * 16, 230 * 16)
            painter.drawArc(QRectF(-2, -5, 12, 10), -145 * 16, 230 * 16)
            painter.drawLine(QPointF(-4, 0), QPointF(4, 0))
        elif icon_name == "status":
            painter.drawLine(QPointF(-7, 7), QPointF(-7, 1))
            painter.drawLine(QPointF(0, 7), QPointF(0, -6))
            painter.drawLine(QPointF(7, 7), QPointF(7, -2))
        elif icon_name == "ai":
            path = QPainterPath()
            path.moveTo(0, -10)
            path.lineTo(2.5, -2.5)
            path.lineTo(10, 0)
            path.lineTo(2.5, 2.5)
            path.lineTo(0, 10)
            path.lineTo(-2.5, 2.5)
            path.lineTo(-10, 0)
            path.lineTo(-2.5, -2.5)
            path.closeSubpath()
            painter.drawPath(path)
        painter.restore()

    def _emit_action(self, action: str) -> None:
        if action == self._attention_action:
            self._attention_action = ""
        self.hide_menu()
        if self._action_callback is not None:
            self._action_callback(action)


class QuickMenuWindow(_LegacyQuickMenuWindow):
    RING_SIZE = 240
    DRAWER_WIDTH = 456
    DRAWER_MIN_WIDTH = 408
    DRAWER_HEIGHT = 392
    CONNECTOR_REACH = 48

    def __init__(self) -> None:
        super().__init__()
        self._hide_timer.stop()
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.resize(self.RING_SIZE, self.RING_SIZE)
        self.actions = [
            ("assets", "assets", ui_text("素材", "Assets")),
            ("agent", "mcp", "Agent"),
            ("settings", "status", ui_text("设置", "Settings")),
        ]
        self.action_tooltips = {action: label for action, _icon, label in self.actions}
        self._close_callback = None
        self._drawer_page = ""
        self._drawer_side = "right"
        self._anchor = QRect()
        self._available = QRect(0, 0, 1280, 720)
        self._drawer_global_rect = QRect()
        self._feedback_only = False
        self._progress_active = False
        self._page_shift_direction = 1
        self._detail_buttons: dict[str, QPushButton] = {}
        self._low_power_enabled = False
        self._ai_provider_mode = "off"
        self._language_mode = "auto"
        self._drawer_transition_id = 0
        self._hide_transition_id: int | None = None
        self._build_drawer()
        self._hide_finalize_timer = QTimer(self)
        self._hide_finalize_timer.setSingleShot(True)
        self._hide_finalize_timer.setInterval(175)
        self._hide_finalize_timer.timeout.connect(self._finalize_pending_hide)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.setInterval(1700)
        self._feedback_timer.timeout.connect(self._hide_feedback)
        self._agent_timer = QTimer(self)
        self._agent_timer.setInterval(5000)
        self._agent_timer.timeout.connect(self.refresh_agent_status)
        self.hide()

    def _build_drawer(self) -> None:
        self.drawer_shell = QWidget(self)
        self.drawer_shell.setObjectName("attachedDrawer")
        self.drawer_shell.setStyleSheet(
            "QWidget#attachedDrawer { background: #FFFDF5; border: 1px solid #6F7F5A; border-radius: 8px; }"
            "QLabel { background: transparent; border: none; color: #443F33; }"
        )
        drawer_layout = QVBoxLayout(self.drawer_shell)
        drawer_layout.setContentsMargins(14, 12, 14, 10)
        drawer_layout.setSpacing(8)

        self.drawer_title = QLabel("", self.drawer_shell)
        self.drawer_title.setStyleSheet(
            "QLabel { color: #4E5F3D; font-size: 14px; font-weight: bold; background: transparent; border: none; }"
        )
        drawer_layout.addWidget(self.drawer_title)

        self.drawer_stack = QStackedWidget(self.drawer_shell)
        self.drawer_stack.setStyleSheet("QStackedWidget { background: transparent; border: none; }")
        self.material_panel = MaterialPanelWindow(self.drawer_stack, embedded=True)
        self.agent_page = self._build_agent_page()
        self.settings_page = self._build_settings_page()
        self.ai_page = self._build_ai_page()
        self._pages = {
            "assets": self.material_panel,
            "agent": self.agent_page,
            "settings": self.settings_page,
            "ai": self.ai_page,
        }
        for page in self._pages.values():
            self.drawer_stack.addWidget(page)
        drawer_layout.addWidget(self.drawer_stack, 1)

        self.feedback_label = QLabel("", self.drawer_shell)
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.feedback_label.setFixedHeight(28)
        self.feedback_label.hide()
        drawer_layout.addWidget(self.feedback_label)

        self.progress_bar = QProgressBar(self.drawer_shell)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background: #E9E4D4; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #C8A24A; border-radius: 3px; }"
        )
        self.progress_bar.hide()
        drawer_layout.addWidget(self.progress_bar)

        self._drawer_motion = QPropertyAnimation(self.drawer_shell, b"pos", self)
        self._drawer_motion.setDuration(150)
        self._drawer_motion.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._page_slide = QPropertyAnimation(self.drawer_stack, b"pos", self)
        self._page_slide.setDuration(130)
        self._page_slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.drawer_shell.hide()

    def _build_agent_page(self) -> QWidget:
        page = QWidget(self.drawer_stack)
        page.setStyleSheet("QWidget { background: transparent; border: none; }")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.agent_status_label = QLabel(ui_text("MCP 未连接", "MCP offline"), page)
        self.agent_status_label.setStyleSheet(
            "QLabel { color: #A67624; font-size: 12px; font-weight: bold; padding: 4px 0; }"
        )
        layout.addWidget(self.agent_status_label)
        self.connection_section = self._section_label(ui_text("连接", "Connection"), page)
        layout.addWidget(self.connection_section)
        layout.addWidget(self._action_button(ui_text("复制 MCP 配置", "Copy MCP config"), "mcp", page))
        layout.addWidget(self._action_button(ui_text("复制 HTTP 地址", "Copy HTTP URL"), "http", page))
        layout.addSpacing(6)
        self.delivery_section = self._section_label(ui_text("交付", "Delivery"), page)
        layout.addWidget(self.delivery_section)
        layout.addWidget(self._action_button(ui_text("复制最新批次 handoff", "Copy latest batch handoff"), "latest_handoff", page))
        layout.addWidget(self._action_button(ui_text("复制全部可用素材", "Copy all ready assets"), "ready_handoff", page))
        layout.addWidget(self._action_button(ui_text("复制 Agent 配方", "Copy Agent recipe"), "agent_recipe", page))
        layout.addStretch(1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget(self.drawer_stack)
        page.setStyleSheet("QWidget { background: transparent; border: none; }")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.ai_settings_button = self._action_button(ui_text("AI 分拣", "AI sorting"), "ai_setup", page)
        self.low_power_button = self._action_button(ui_text("低功耗：关", "Low power: off"), "low_power", page)
        layout.addWidget(self.ai_settings_button)
        layout.addWidget(self.low_power_button)

        self.language_section = self._section_label(ui_text("语言", "Language"), page)
        layout.addWidget(self.language_section)
        language_row = QWidget(page)
        language_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        language_layout = QHBoxLayout(language_row)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_layout.setSpacing(5)
        self.language_buttons: dict[str, QPushButton] = {}
        for mode, label in (("auto", ui_text("自动", "Auto")), ("zh", "简体中文"), ("en", "English")):
            button = self._action_button(label, f"language:{mode}", language_row)
            self.language_buttons[mode] = button
            language_layout.addWidget(button)
        layout.addWidget(language_row)

        self.service_section = self._section_label(ui_text("服务与日志", "Service & logs"), page)
        layout.addWidget(self.service_section)
        self.service_status_label = QLabel("", page)
        self.service_status_label.setWordWrap(True)
        self.service_status_label.setStyleSheet(
            "QLabel { color: #625B4C; font-size: 11px; padding: 7px 8px; background: #F6F1E4; border-radius: 7px; }"
        )
        layout.addWidget(self.service_status_label)
        layout.addWidget(self._action_button(ui_text("打开日志目录", "Open logs folder"), "logs", page))
        layout.addStretch(1)
        self.exit_button = self._action_button(ui_text("退出 Haypile", "Quit Haypile"), "exit", page, danger=True)
        layout.addWidget(self.exit_button)
        return page

    def _build_ai_page(self) -> QWidget:
        page = QWidget(self.drawer_stack)
        page.setStyleSheet("QWidget { background: transparent; border: none; }")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        back = self._action_button(ui_text("返回设置", "Back to settings"), "settings", page)
        layout.addWidget(back)
        self.ai_status_label = QLabel("", page)
        self.ai_status_label.setWordWrap(True)
        self.ai_status_label.setStyleSheet(
            "QLabel { color: #4A463A; font-size: 12px; padding: 10px; background: #F6F1E4; border-radius: 8px; }"
        )
        layout.addWidget(self.ai_status_label)

        provider_row = QWidget(page)
        provider_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        provider_layout = QHBoxLayout(provider_row)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(5)
        self.ai_provider_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("local", ui_text("本地模型", "Local model")),
            ("api", "API"),
            ("off", ui_text("关闭", "Off")),
        ):
            button = self._action_button(label, f"ai_provider:{mode}", provider_row)
            button.setStyleSheet(
                "QPushButton { text-align: center; color: #4E5F3D; background: #F6F1E4; "
                "border: 1px solid #DDD3BB; border-radius: 7px; font-size: 10px; }"
            )
            self.ai_provider_buttons[mode] = button
            provider_layout.addWidget(button)
        layout.addWidget(provider_row)

        self.ai_api_base_input = QLineEdit(page)
        self.ai_api_base_input.setPlaceholderText("https://provider.example/v1")
        self.ai_api_model_input = QLineEdit(page)
        self.ai_api_model_input.setPlaceholderText(ui_text("模型名称", "Model name"))
        self.ai_api_key_input = QLineEdit(page)
        self.ai_api_key_input.setPlaceholderText(ui_text("API 密钥（不会写入配置文件）", "API key (not stored in config)"))
        self.ai_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        for field in (self.ai_api_base_input, self.ai_api_model_input, self.ai_api_key_input):
            field.setFixedHeight(28)
            field.setStyleSheet(
                "QLineEdit { color: #4A463A; background: #FFF9EA; border: 1px solid #E5D8B9; "
                "border-radius: 7px; padding: 0 8px; font-size: 11px; }"
            )
            layout.addWidget(field)
        self.ai_api_save_button = self._action_button(
            ui_text("保存并授权此域名", "Save and authorize domain"), "ai_save_api", page
        )
        layout.addWidget(self.ai_api_save_button)

        self.ai_command_button = self._action_button(ui_text("复制模型安装命令", "Copy model install command"), "ai_copy_command", page)
        self.ai_recheck_button = self._action_button(ui_text("重新检测", "Check again"), "ai_recheck", page)
        layout.addWidget(self.ai_command_button)
        layout.addWidget(self.ai_recheck_button)
        layout.addStretch(1)
        return page

    @staticmethod
    def _section_label(text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setStyleSheet(
            "QLabel { color: #6F7F5A; font-size: 10px; font-weight: bold; padding-top: 3px; }"
        )
        return label

    def _action_button(self, text: str, action: str, parent: QWidget, *, danger: bool = False) -> QPushButton:
        button = QPushButton(text, parent)
        button.setFixedHeight(30)
        color = "#9B4C37" if danger else "#4E5F3D"
        button.setStyleSheet(
            "QPushButton { text-align: left; padding: 0 10px; "
            f"color: {color}; background: #F6F1E4; border: 1px solid #DDD3BB; border-radius: 7px; font-size: 11px; }}"
            "QPushButton:hover { background: #EFE3C7; }"
        )
        button.clicked.connect(lambda _checked=False, selected=action: self._emit_action(selected))
        self._detail_buttons[action] = button
        return button

    def retranslate(self) -> None:
        self.actions = [
            ("assets", "assets", ui_text("素材", "Assets")),
            ("agent", "mcp", "Agent"),
            ("settings", "status", ui_text("设置", "Settings")),
        ]
        self.action_tooltips = {action: label for action, _icon, label in self.actions}
        button_labels = {
            "mcp": ui_text("复制 MCP 配置", "Copy MCP config"),
            "http": ui_text("复制 HTTP 地址", "Copy HTTP URL"),
            "latest_handoff": ui_text("复制最新批次 handoff", "Copy latest batch handoff"),
            "ready_handoff": ui_text("复制全部可用素材", "Copy all ready assets"),
            "agent_recipe": ui_text("复制 Agent 配方", "Copy Agent recipe"),
            "ai_setup": ui_text("AI 分拣", "AI sorting"),
            "logs": ui_text("打开日志目录", "Open logs folder"),
            "exit": ui_text("退出 Haypile", "Quit Haypile"),
            "ai_copy_command": ui_text("复制模型安装命令", "Copy model install command"),
            "ai_recheck": ui_text("重新检测", "Check again"),
            "ai_save_api": ui_text("保存并授权此域名", "Save and authorize domain"),
            "settings": ui_text("返回设置", "Back to settings"),
            "ai_provider:local": ui_text("本地模型", "Local model"),
            "ai_provider:api": "API",
            "ai_provider:off": ui_text("关闭", "Off"),
            "language:auto": ui_text("自动", "Auto"),
            "language:zh": "简体中文",
            "language:en": "English",
        }
        for action, label in button_labels.items():
            button = self._detail_buttons.get(action)
            if button is not None:
                button.setText(label)
        self.connection_section.setText(ui_text("连接", "Connection"))
        self.delivery_section.setText(ui_text("交付", "Delivery"))
        self.language_section.setText(ui_text("语言", "Language"))
        self.service_section.setText(ui_text("服务与日志", "Service & logs"))
        self.low_power_button.setText(
            ui_text("低功耗：开", "Low power: on")
            if self._low_power_enabled
            else ui_text("低功耗：关", "Low power: off")
        )
        self.material_panel.retranslate()
        if self._drawer_page:
            self._switch_page(self._drawer_page, animate=False)
        self.refresh_agent_status()
        self.update()

    def set_close_handler(self, callback) -> None:
        self._close_callback = callback

    def show_menu(self, x: int, y: int) -> None:
        self._anchor = QRect(x + 84, y + 84, 72, 72)
        self._available = QRect(x, y, self.RING_SIZE, self.RING_SIZE)
        self._drawer_page = ""
        self._feedback_only = False
        self.drawer_shell.hide()
        self.setGeometry(x, y, self.RING_SIZE, self.RING_SIZE)
        self.set_track_center(QPointF(120, 120))
        self._show_ring_animation()

    def show_attached(self, anchor: QRect, available: QRect) -> None:
        self._anchor = QRect(anchor)
        self._available = QRect(available)
        self._drawer_page = ""
        self._feedback_only = False
        self.drawer_shell.hide()
        self._apply_attached_geometry(drawer_open=False, allow_flip=True)
        self._show_ring_animation()

    def _show_ring_animation(self) -> None:
        self._drawer_transition_id += 1
        self._cancel_pending_hide()
        self._hovered_action = ""
        offset = self._slide_offset()
        start_shift = QPointF(offset.x() * 0.55, offset.y() * 0.55)
        self._set_content_shift(start_shift)
        self.setWindowOpacity(0.45)
        self.show()
        self.raise_()
        self._fade_animation.stop()
        self._slide_animation.stop()
        self._fade_animation.setDuration(125)
        self._fade_animation.setStartValue(0.45)
        self._fade_animation.setEndValue(1.0)
        self._slide_animation.setDuration(125)
        self._slide_animation.setStartValue(self._content_shift)
        self._slide_animation.setEndValue(QPointF())
        self._fade_animation.start()
        self._slide_animation.start()

    @staticmethod
    def _animations_enabled() -> bool:
        return os.environ.get("QT_QPA_PLATFORM", "").strip().lower() != "offscreen"

    def _drawer_anchor_offset(self, distance: int) -> QPoint:
        return QPoint(-distance if self._drawer_side == "right" else distance, 0)

    def _start_drawer_motion(
        self,
        start: QPoint,
        end: QPoint,
        duration: int,
        easing: QEasingCurve.Type,
    ) -> None:
        self._drawer_motion.stop()
        self.drawer_shell.move(start)
        self._drawer_motion.setDuration(duration)
        self._drawer_motion.setEasingCurve(easing)
        self._drawer_motion.setStartValue(start)
        self._drawer_motion.setEndValue(end)
        self._drawer_motion.start()

    def hide_menu(self) -> None:
        self._drawer_transition_id += 1
        transition_id = self._drawer_transition_id
        self._hide_finalize_timer.stop()
        self._hide_transition_id = None
        self._feedback_timer.stop()
        self._agent_timer.stop()
        had_drawer = self.drawer_shell.isVisible() and not self._feedback_only
        self._drawer_page = ""
        self._progress_active = False
        self._hovered_action = ""
        self.setToolTip("")
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        if not self.isVisible():
            self._hide_after_slide = False
            self.drawer_shell.hide()
            self.progress_bar.hide()
            self.feedback_label.hide()
            return
        self._fade_animation.stop()
        self._slide_animation.stop()
        self._drawer_motion.stop()
        if self._page_slide.state() == QPropertyAnimation.State.Running:
            page_end = self._page_slide.endValue()
            self._page_slide.stop()
            if isinstance(page_end, QPoint):
                self.drawer_stack.move(page_end)
        if had_drawer and self._animations_enabled():
            current = self.drawer_shell.pos()
            self._start_drawer_motion(
                current,
                current + self._drawer_anchor_offset(14),
                150,
                QEasingCurve.Type.InOutCubic,
            )
        else:
            self.drawer_shell.hide()
        self._hide_transition_id = transition_id
        self._hide_after_slide = True
        self._fade_animation.setDuration(150)
        self._fade_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._fade_animation.setStartValue(self.windowOpacity())
        self._fade_animation.setEndValue(0.0)
        self._slide_animation.setDuration(150)
        self._slide_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._slide_animation.setStartValue(self._content_shift)
        self._slide_animation.setEndValue(self._slide_offset())
        self._fade_animation.start()
        self._slide_animation.start()
        self._hide_finalize_timer.start()

    def _on_fade_finished(self) -> None:
        if self._hide_after_slide:
            self._finalize_pending_hide()
            return
        super()._on_fade_finished()

    def _cancel_pending_hide(self) -> None:
        self._hide_finalize_timer.stop()
        self._hide_transition_id = None
        self._hide_after_slide = False

    def _finalize_pending_hide(self) -> None:
        transition_id = self._hide_transition_id
        if (
            transition_id is None
            or transition_id != self._drawer_transition_id
            or not self._hide_after_slide
        ):
            return
        self._hide_finalize_timer.stop()
        self._hide_transition_id = None
        self._hide_after_slide = False
        self._fade_animation.stop()
        self._slide_animation.stop()
        self._drawer_motion.stop()
        self._page_slide.stop()
        self._set_content_shift(QPointF())
        self.drawer_shell.hide()
        self.progress_bar.hide()
        self.feedback_label.hide()
        self.hide()

    def enterEvent(self, event) -> None:
        super(_LegacyQuickMenuWindow, self).enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered_action = ""
        self.setToolTip("")
        self.update()
        super(_LegacyQuickMenuWindow, self).leaveEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._close_callback is not None:
                self._close_callback()
            else:
                self.hide_menu()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            anchor_rect = QRect(self.mapFromGlobal(self._anchor.topLeft()), self._anchor.size())
            if anchor_rect.contains(event.position().toPoint()):
                if self._close_callback is not None:
                    self._close_callback()
                else:
                    self.hide_menu()
                event.accept()
                return
            action = self._action_at(event.position())
            if action:
                self._emit_action(action)
                event.accept()
                return
        QWidget.mousePressEvent(self, event)

    def _emit_action(self, action: str) -> None:
        if action == self._attention_action:
            self._attention_action = ""
        if self._action_callback is not None:
            self._action_callback(action)

    def open_drawer(self, page: str, anchor: QRect | None = None, available: QRect | None = None) -> None:
        if page not in self._pages:
            return
        self._drawer_transition_id += 1
        self._cancel_pending_hide()
        if self._fade_animation.state() == QPropertyAnimation.State.Running:
            self._fade_animation.stop()
        if self._slide_animation.state() == QPropertyAnimation.State.Running:
            self._slide_animation.stop()
        self.setWindowOpacity(1.0)
        self._set_content_shift(QPointF())
        if anchor is not None:
            self._anchor = QRect(anchor)
        if available is not None:
            self._available = QRect(available)
        old_page = self._drawer_page
        self._drawer_page = page
        self._feedback_only = False
        self.drawer_title.show()
        self.drawer_stack.show()
        self._apply_attached_geometry(drawer_open=True, allow_flip=True)
        if page == "assets":
            self.material_panel.show()
        if page == "agent":
            self.refresh_agent_status()
            self._agent_timer.start()
        else:
            self._agent_timer.stop()
        self._switch_page(page, animate=bool(old_page and old_page != page))
        self.drawer_shell.show()
        if not self.isVisible():
            self.setWindowOpacity(1.0)
            self.show()
        self.raise_()
        final_pos = self.drawer_shell.pos()
        if not old_page and self._animations_enabled():
            self._start_drawer_motion(
                final_pos + self._drawer_anchor_offset(8),
                final_pos,
                150,
                QEasingCurve.Type.OutCubic,
            )
        else:
            self._drawer_motion.stop()
            self.drawer_shell.move(final_pos)
        if page == "assets":
            QTimer.singleShot(0, self.material_panel.refresh)
        self.update()

    def _switch_page(self, page: str, *, animate: bool = True) -> None:
        animate = animate and self._animations_enabled()
        if self._page_slide.state() == QPropertyAnimation.State.Running:
            end = self._page_slide.endValue()
            self._page_slide.stop()
            if isinstance(end, QPoint):
                self.drawer_stack.move(end)
        widget = self._pages[page]
        self.drawer_stack.setCurrentWidget(widget)
        self.drawer_title.setText(
            {
                "assets": ui_text("素材", "Assets"),
                "agent": "Agent",
                "settings": ui_text("设置", "Settings"),
                "ai": ui_text("AI 分拣", "AI sorting"),
            }[page]
        )
        if not animate:
            return
        base = self.drawer_stack.pos()
        offset = QPoint(6 * self._page_shift_direction, 0)
        self._page_shift_direction *= -1
        self.drawer_stack.move(base + offset)
        self._page_slide.stop()
        self._page_slide.setStartValue(base + offset)
        self._page_slide.setEndValue(base)
        self._page_slide.start()

    def close_drawer(self) -> None:
        if not self._drawer_page:
            return
        self._drawer_transition_id += 1
        transition_id = self._drawer_transition_id
        self._drawer_page = ""
        self._agent_timer.stop()
        if not self._animations_enabled():
            self.drawer_shell.hide()
            self._apply_attached_geometry(drawer_open=False, allow_flip=False)
            self.update()
            return
        current = self.drawer_shell.pos()
        self._start_drawer_motion(
            current,
            current + self._drawer_anchor_offset(8),
            150,
            QEasingCurve.Type.InOutCubic,
        )

        def finish() -> None:
            if transition_id != self._drawer_transition_id or self._drawer_page:
                return
            self.drawer_shell.hide()
            self._apply_attached_geometry(drawer_open=False, allow_flip=False)
            self.update()

        QTimer.singleShot(150, finish)

    def is_drawer_open(self) -> bool:
        return bool(self._drawer_page and self.drawer_shell.isVisible())

    def current_page(self) -> str:
        return self._drawer_page

    def reposition(self, anchor: QRect, available: QRect, *, allow_flip: bool = True) -> None:
        self._anchor = QRect(anchor)
        self._available = QRect(available)
        if not self.isVisible():
            return
        previous_side = self._drawer_side
        desired_side = self._choose_drawer_side()
        if self.is_drawer_open() and allow_flip and desired_side != previous_side:
            if not self._animations_enabled():
                self._drawer_side = desired_side
                self._apply_attached_geometry(drawer_open=True, allow_flip=False)
                return
            page = self._drawer_page
            self._drawer_transition_id += 1
            transition_id = self._drawer_transition_id
            current = self.drawer_shell.pos()
            self._start_drawer_motion(
                current,
                current + self._drawer_anchor_offset(12),
                120,
                QEasingCurve.Type.InOutCubic,
            )

            def mirror() -> None:
                if transition_id != self._drawer_transition_id or self._drawer_page != page:
                    return
                self.drawer_shell.hide()
                self._drawer_side = desired_side
                self._apply_attached_geometry(drawer_open=True, allow_flip=False)
                self._switch_page(page, animate=False)
                final_pos = self.drawer_shell.pos()
                self._start_drawer_motion(
                    final_pos + self._drawer_anchor_offset(12),
                    final_pos,
                    160,
                    QEasingCurve.Type.OutCubic,
                )
                self.drawer_shell.show()

            QTimer.singleShot(120, mirror)
            return
        if allow_flip:
            self._drawer_side = desired_side
        self._apply_attached_geometry(drawer_open=self.is_drawer_open(), allow_flip=False)

    def _choose_drawer_side(self) -> str:
        center_x = self._anchor.center().x()
        right_space = self._available.right() - (center_x + self.CONNECTOR_REACH)
        left_space = (center_x - self.CONNECTOR_REACH) - self._available.left()
        if right_space >= self.DRAWER_MIN_WIDTH:
            return "right"
        if left_space >= self.DRAWER_MIN_WIDTH:
            return "left"
        return "right" if right_space >= left_space else "left"

    def _apply_attached_geometry(self, *, drawer_open: bool, allow_flip: bool) -> None:
        if allow_flip:
            self._drawer_side = self._choose_drawer_side()
        center = self._anchor.center()
        ring = QRect(
            center.x() - self.RING_SIZE // 2,
            center.y() - self.RING_SIZE // 2,
            self.RING_SIZE,
            self.RING_SIZE,
        )
        visible_ring = ring.intersected(self._available)
        if visible_ring.isEmpty():
            visible_ring = QRect(center.x(), center.y(), 1, 1)
        union = QRect(visible_ring)
        self._drawer_global_rect = QRect()
        if drawer_open:
            available_width = (
                self._available.right() - (center.x() + self.CONNECTOR_REACH)
                if self._drawer_side == "right"
                else (center.x() - self.CONNECTOR_REACH) - self._available.left()
            )
            drawer_width = min(self.DRAWER_WIDTH, max(self.DRAWER_MIN_WIDTH, available_width - 8))
            drawer_y = center.y() - self.DRAWER_HEIGHT // 2
            drawer_y = max(
                self._available.top() + 8,
                min(drawer_y, self._available.bottom() - self.DRAWER_HEIGHT - 8),
            )
            if self._drawer_side == "right":
                drawer_x = center.x() + self.CONNECTOR_REACH
            else:
                drawer_x = center.x() - self.CONNECTOR_REACH - drawer_width
            drawer_x = max(
                self._available.left() + 8,
                min(drawer_x, self._available.right() - drawer_width - 8),
            )
            self._drawer_global_rect = QRect(drawer_x, drawer_y, drawer_width, self.DRAWER_HEIGHT)
            union = union.united(self._drawer_global_rect)
        self.setGeometry(union)
        self.set_track_center(QPointF(center.x() - union.left(), center.y() - union.top()))
        if drawer_open:
            self.drawer_shell.setGeometry(
                self._drawer_global_rect.translated(-union.left(), -union.top())
            )
        self.update()

    def _slot_angles(self) -> list[int]:
        center = self._anchor.center()
        left = center.x() - self._available.left() < 88
        right = self._available.right() - center.x() < 88
        top = center.y() - self._available.top() < 88
        bottom = self._available.bottom() - center.y() < 88
        if left and top:
            return [18, 54, 90]
        if right and top:
            return [90, 126, 162]
        if left and bottom:
            return [-90, -54, -18]
        if right and bottom:
            return [-162, -126, -90]
        if left:
            return [-64, 0, 64]
        if right:
            return [116, 180, 244]
        if top:
            return [26, 90, 154]
        if bottom:
            return [-154, -90, -26]
        if self._drawer_side == "right":
            return [112, 180, 248]
        return [-68, 0, 68]

    def _label_rect(self, action: str) -> QRectF:
        slot = self._slot_rect(action)
        center = slot.center()
        width, height = 52.0, 16.0
        x = max(2.0, min(center.x() - width / 2, self.width() - width - 2.0))
        y = slot.bottom() + 2.0
        if y + height > self.height() - 2.0:
            y = slot.top() - height - 2.0
        return QRectF(x, max(2.0, y), width, height)

    def _action_at(self, point: QPointF) -> str:
        point = QPointF(point) - self._content_shift
        for action, _icon, _label in self.actions:
            if self._slot_rect(action).contains(point) or self._label_rect(action).contains(point):
                return action
        return ""

    def _paint_overlay(self, painter: QPainter) -> None:
        if self.is_drawer_open():
            drawer = QRectF(self.drawer_shell.geometry())
            center = QPointF(self._track_center)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 18))
            painter.drawRoundedRect(drawer.translated(0, 3), 8, 8)
            if self._drawer_side == "right":
                neck_left = center.x() + 30
                neck = QRectF(
                    neck_left,
                    center.y() - 9,
                    max(8.0, drawer.left() + 8 - neck_left),
                    18,
                )
            else:
                neck_left = drawer.right() - 8
                neck = QRectF(
                    neck_left,
                    center.y() - 9,
                    max(8.0, center.x() - 30 - neck_left),
                    18,
                )
            painter.setPen(QPen(QColor("#6F7F5A"), 1.0))
            painter.setBrush(QColor("#FFFDF5"))
            painter.drawRoundedRect(neck, 8, 8)
        font = painter.font()
        font.setPointSizeF(8.0)
        painter.setFont(font)
        for action, _icon, label in self.actions:
            label_rect = self._label_rect(action)
            alignment = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
            painter.setPen(QColor(255, 253, 245, 205))
            for offset in (QPointF(-1, 0), QPointF(1, 0), QPointF(0, -1), QPointF(0, 1)):
                painter.drawText(label_rect.translated(offset), alignment, label)
            painter.setPen(QColor("#4E5F3D"))
            painter.drawText(label_rect, alignment, label)

    def show_feedback(self, message: str, success: bool, anchor: QRect, available: QRect) -> None:
        self._drawer_transition_id += 1
        self._cancel_pending_hide()
        self._feedback_timer.stop()
        self.feedback_label.setText(message)
        self.feedback_label.setStyleSheet(
            "QLabel { padding: 4px 8px; border-radius: 8px; font-size: 11px; font-weight: bold; "
            f"color: {'#4E5F3D' if success else '#9B4C37'}; background: {'#F3EDDA' if success else '#F4E2DC'}; }}"
        )
        self.feedback_label.show()
        if self.is_drawer_open():
            self._feedback_timer.start()
            return
        self._anchor = QRect(anchor)
        self._available = QRect(available)
        self._feedback_only = True
        width, height = 270, 44
        right = anchor.right() + 10
        x = right if right + width <= available.right() - 8 else anchor.left() - width - 10
        y = max(available.top() + 8, min(anchor.center().y() - height // 2, available.bottom() - height - 8))
        self.setGeometry(x, y, width, height)
        self.drawer_shell.setGeometry(0, 0, width, height)
        self.drawer_title.hide()
        self.drawer_stack.hide()
        self.progress_bar.setVisible(self._progress_active)
        self.drawer_shell.show()
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._feedback_timer.start()

    def _hide_feedback(self) -> None:
        if self._progress_active:
            return
        self.feedback_label.hide()
        if self._feedback_only:
            self._feedback_only = False
            self.drawer_shell.hide()
            self.hide()

    def begin_progress(self, anchor: QRect, available: QRect, text: str) -> None:
        self._progress_active = True
        self.progress_bar.setValue(3)
        self.progress_bar.show()
        self.show_feedback(text, True, anchor, available)

    def set_progress(self, percent: int, text: str) -> None:
        self.progress_bar.setValue(max(0, min(100, percent)))
        self.feedback_label.setText(text)

    def complete_progress(self, success: bool, message: str) -> None:
        self._progress_active = False
        self.progress_bar.setValue(100 if success else max(15, self.progress_bar.value()))
        self.progress_bar.hide()
        anchor = self._anchor if not self._anchor.isNull() else QRect(60, 60, 72, 72)
        self.show_feedback(message, success, anchor, self._available)

    def refresh_agent_status(self) -> None:
        try:
            from mcp_server import active_mcp_sessions

            sessions = active_mcp_sessions(get_settings().INDEX_DIR)
        except (OSError, ValueError):
            sessions = []
        if sessions:
            self.agent_status_label.setText(ui_text(f"MCP 已连接 · {len(sessions)} 个会话", f"MCP connected · {len(sessions)} session(s)"))
            self.agent_status_label.setStyleSheet(
                "QLabel { color: #4E7A46; font-size: 12px; font-weight: bold; padding: 4px 0; }"
            )
        else:
            self.agent_status_label.setText(ui_text("MCP 未连接", "MCP offline"))
            self.agent_status_label.setStyleSheet(
                "QLabel { color: #A67624; font-size: 12px; font-weight: bold; padding: 4px 0; }"
            )

    def update_settings_state(
        self,
        *,
        ai_enabled: bool,
        ai_status: str,
        low_power: bool,
        language: str,
        service_status: str,
        ai_provider: str,
        api_base_url: str,
        api_model: str,
        api_key_present: bool,
    ) -> None:
        self.low_power_button.setText(
            ui_text("低功耗：开", "Low power: on") if low_power else ui_text("低功耗：关", "Low power: off")
        )
        self._low_power_enabled = bool(low_power)
        self._language_mode = language
        self._ai_provider_mode = ai_provider
        self.ai_settings_button.setText(
            ui_text("AI 分拣：开", "AI sorting: on") if ai_enabled else ui_text("AI 分拣：关", "AI sorting: off")
        )
        self.ai_status_label.setText(ai_status)
        self.ai_api_base_input.setText(api_base_url)
        self.ai_api_model_input.setText(api_model)
        self.ai_api_key_input.clear()
        self.ai_api_key_input.setPlaceholderText(
            ui_text("密钥已存入系统凭据库", "Key stored in system credential store")
            if api_key_present
            else ui_text("API 密钥（不会写入配置文件）", "API key (not stored in config)")
        )
        api_visible = ai_provider == "api"
        for widget in (
            self.ai_api_base_input,
            self.ai_api_model_input,
            self.ai_api_key_input,
            self.ai_api_save_button,
        ):
            widget.setVisible(api_visible)
        self.ai_command_button.setVisible(ai_provider == "local")
        self.ai_recheck_button.setVisible(ai_provider == "local")
        for mode, button in self.ai_provider_buttons.items():
            active = mode == ai_provider
            button.setStyleSheet(
                "QPushButton { text-align: center; "
                f"color: {'#FFF9EA' if active else '#4E5F3D'}; "
                f"background: {'#6F7F5A' if active else '#F6F1E4'}; "
                "border: 1px solid #DDD3BB; border-radius: 7px; font-size: 10px; }"
            )
        self.service_status_label.setText(service_status)
        for mode, button in self.language_buttons.items():
            active = mode == language
            button.setStyleSheet(
                "QPushButton { text-align: center; padding: 0 5px; "
                f"color: {'#FFF9EA' if active else '#4E5F3D'}; background: {'#6F7F5A' if active else '#F6F1E4'}; "
                "border: 1px solid #DDD3BB; border-radius: 7px; font-size: 10px; }"
            )


AttachedHubWindow = QuickMenuWindow


class HaypileFloatingBall(QWidget):
    COLLAPSED_SIZE = 72
    EXPANDED_SIZE = 300

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()
        self.project_root: Path = self.settings.BASE_DIR
        self.assets_dir: Path = self.settings.ASSETS_DIR
        self.themes_dir: Path = self.settings.THEMES_DIR
        self.manifest_path: Path = self.settings.MANIFEST_PATH
        self.haypile_icon = QPixmap(str(self.project_root / "ui_assets" / "haypile-icon.png"))
        self._haypile_alpha_image = self.haypile_icon.toImage()
        self._haypile_glow_pixmap = self._tinted_haypile_pixmap(QColor("#FFD66D"))
        self._haypile_direction_glow_pixmap = self._tinted_haypile_pixmap(QColor("#FFF1B2"))
        self._haypile_exit_glow_pixmap = self._tinted_haypile_pixmap(QColor("#9B4C37"))
        self._drop_leaf_frame_runs = self._load_drop_leaf_frame_runs()
        self._drop_leaf_frame_renderer = QSvgRenderer(str(self.project_root / "ui_assets" / "drop-leaf-frame.svg"))
        self._drop_leaf_renderers = self._load_drop_leaf_renderers()
        self._audio_leaf_layer_key: tuple[int, int, int] | None = None
        self._audio_leaf_layer_buffers: tuple[QPixmap, ...] = ()
        self._gui_state_path = self.settings.INDEX_DIR / "gui_state.json"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.themes_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)

        stored_state = self._read_gui_state()
        self.language_mode = str(stored_state.get("language") or "auto")
        if self.language_mode not in {"auto", "zh", "en"}:
            self.language_mode = "auto"
        set_ui_language(self.language_mode)
        self.low_power_enabled = bool(
            stored_state.get("low_power_enabled", self.settings.HAYPILE_LOW_POWER_MODE)
        )
        stored_ai = stored_state.get("ai_enabled")
        self._ai_preference = (
            bool(stored_ai) if isinstance(stored_ai, bool) else bool(self.settings.VISION_CLASSIFIER_ENABLED)
        )
        default_provider = "local" if self._ai_preference else "off"
        self.ai_provider_mode = str(stored_state.get("ai_provider") or default_provider).strip().lower()
        if self.ai_provider_mode not in {"local", "api", "off"}:
            self.ai_provider_mode = default_provider
        self.ai_api_base_url = str(stored_state.get("ai_api_base_url") or "").strip()
        self.ai_api_model = str(stored_state.get("ai_api_model") or "").strip()
        self.ai_api_authorized_host = str(stored_state.get("ai_api_authorized_host") or "").strip()
        self.ai_api_key_present = bool(stored_state.get("ai_api_key_present", False))
        self._session_api_key = ""
        if self.ai_provider_mode == "api" and self.ai_api_key_present:
            try:
                current_host = api_authority(self.ai_api_base_url)
            except ValueError:
                current_host = ""
            if current_host and current_host == self.ai_api_authorized_host:
                self._session_api_key = SystemCredentialStore.get(current_host)
            self.ai_api_key_present = bool(self._session_api_key)

        self.api_process: subprocess.Popen[str] | None = None
        self.api_owned_by_gui = False
        self.worker: IngestWorker | None = None
        self.remote_worker: RemoteDownloadWorker | None = None
        self.ai_batch_worker: AIBatchWorker | None = None
        self._ai_batch_queue: list[str] = []
        self.latest_batch_id = ""
        self.ai_enabled = self._restore_ai_enabled()

        self.drag_offset = QPoint()
        self._press_global_pos = QPoint()
        self._last_drag_global_pos = QPoint()
        self._last_drag_sample_at = 0.0
        self._drag_moved = False
        self._window_drag_active = False
        self._pointer_press_owned = False
        self._drag_velocity = QPointF(0, 0)
        self.is_expanded = False
        self._hovered = False
        self._drag_hover = False
        self._drop_feedback_until = 0.0
        self._bounce_feedback_started_at = 0.0
        self._bounce_feedback_until = 0.0
        self._nudge_feedback_started_at = 0.0
        self._nudge_feedback_until = 0.0
        self._reject_feedback_started_at = 0.0
        self._reject_feedback_until = 0.0
        self._drag_release_feedback_started_at = 0.0
        self._drag_release_feedback_until = 0.0
        self._has_pending_assets = False
        self._drag_prepare_active = False
        self._drop_anchor_global: QPoint | None = None
        self._drop_visual_kind = "leaf"
        self._audio_suction_progress = 0.0
        self._audio_suction_animation: QVariantAnimation | None = None
        self._external_drag_candidate = False
        self._global_drag_origin: QPoint | None = None
        self._drag_awareness_angle = -math.pi / 2
        self._drag_awareness_target_angle = -math.pi / 2
        self._drag_awareness_distance = math.inf
        self._drag_awareness_has_direction = False
        self._cg_button_state = None
        self._drop_open_progress = 0.0
        self._drop_open_animation: QVariantAnimation | None = None
        self._pulse_phase = 0.0
        self._geometry_animation: QPropertyAnimation | None = None
        self._closing = False
        self._cleanup_done = False
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(170)
        self._collapse_timer.timeout.connect(
            lambda: self._animate_size(self.COLLAPSED_SIZE)
        )
        self._drag_prepare_timer = QTimer(self)
        self._drag_prepare_timer.setSingleShot(True)
        self._drag_prepare_timer.setInterval(110)
        self._drag_prepare_timer.timeout.connect(self._open_drop_target)
        self._exit_armed = False
        self._exit_timer = QTimer(self)
        self._exit_timer.setSingleShot(True)
        self._exit_timer.setInterval(2000)
        self._exit_timer.timeout.connect(self._clear_exit_armed)
        self._visual_timer = QTimer(self)
        self._visual_timer.setInterval(58)
        self._visual_timer.timeout.connect(self._advance_visual_state)
        self._drag_awareness_timer = QTimer(self)
        self._drag_awareness_timer.setInterval(80)
        self._drag_awareness_timer.timeout.connect(self._poll_external_drag_candidate)

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAutoFillBackground(False)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(self.COLLAPSED_SIZE, self.COLLAPSED_SIZE)
        self.move(self._restore_window_position())
        self._update_window_mask()

        self.quick_menu = QuickMenuWindow()
        self.material_panel = self.quick_menu.material_panel
        self.material_panel.set_toast_handler(self.show_toast)
        self.material_panel.set_ai_handlers(
            provider_factory=self._current_ai_provider_config,
            enabled_callback=lambda: self.ai_enabled and not self.low_power_enabled,
            retry_batch_callback=self._retry_latest_ai_batch,
        )
        self.quick_menu.set_action_handler(self._handle_quick_menu_action)
        self.quick_menu.set_close_handler(self._close_attached_ui)
        self._refresh_ai_menu_status()
        self._refresh_pending_badge()

        self.start_api_server()

    def _configure_window_surface(self) -> None:
        if sys.platform.startswith("win"):
            self._disable_windows_shadow()

    def _disable_windows_shadow(self) -> None:
        try:
            hwnd = int(self.winId())
            dwmapi = ctypes.windll.dwmapi
            user32 = ctypes.windll.user32

            # DWMNCRP_DISABLED
            ncrp_disabled = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(
                hwnd, 2, ctypes.byref(ncrp_disabled), ctypes.sizeof(ncrp_disabled)
            )
            # DWMWA_TRANSITIONS_FORCEDISABLED
            transitions_disabled = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(
                hwnd,
                3,
                ctypes.byref(transitions_disabled),
                ctypes.sizeof(transitions_disabled),
            )
            # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_DONOTROUND
            no_round = ctypes.c_int(1)
            dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(no_round), ctypes.sizeof(no_round)
            )

            # 清理窗口类上的阴影样式（CS_DROPSHADOW）
            GCL_STYLE = -26
            CS_DROPSHADOW = 0x00020000
            class_style = user32.GetClassLongW(hwnd, GCL_STYLE)
            user32.SetClassLongW(hwnd, GCL_STYLE, class_style & ~CS_DROPSHADOW)

            # 强制重绘窗口边框
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
            )
        except Exception:
            logger.debug("Failed to apply Windows DWM window tweaks")
            return

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        if self.is_expanded or self._drop_open_progress > 0.0:
            progress = self._drop_open_progress
            painter.save()
            painter.translate(self._drop_visual_offset(progress))
            panel_size = min(self.width(), self.height()) * 0.47
            panel_rect = QRectF(
                (self.width() - panel_size) / 2,
                (self.height() - panel_size) / 2,
                panel_size,
                panel_size,
            )
            if self._drag_hover and self._drop_visual_kind != "audio":
                painter.save()
                painter.setOpacity(progress)
                drop_glow = QRadialGradient(panel_rect.center(), panel_size * 0.55)
                drop_glow.setColorAt(0.0, QColor(255, 252, 232, 42))
                drop_glow.setColorAt(0.72, QColor(255, 252, 232, 64))
                drop_glow.setColorAt(1.0, QColor(255, 252, 232, 0))
                painter.setBrush(drop_glow)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(panel_rect.adjusted(-6, -6, 6, 6))
                painter.restore()

            if self._drop_visual_kind == "audio":
                self._draw_audio_intake(
                    painter,
                    panel_rect,
                    progress,
                    self._audio_suction_progress,
                )
            else:
                self._draw_drop_leaf_frame(painter, panel_rect, progress)
                self._draw_drop_center_cutout(painter, panel_rect, progress)

            transition_span = 0.24 if self._drop_visual_kind == "audio" else 0.45
            pile_opacity = max(0.0, 1.0 - progress / transition_span)
            if pile_opacity > 0.0:
                painter.save()
                painter.setOpacity(pile_opacity)
                self._draw_collapsed_haypile(painter, show_pending=False)
                painter.restore()

            painter.restore()
            return

        self._draw_collapsed_haypile(painter)

    def _draw_collapsed_haypile(self, painter: QPainter, *, show_pending: bool = True) -> None:
        circle_rect = self._get_collapsed_circle_rect()
        outer_rect = QRectF(circle_rect)

        pulse = 0.5 + 0.5 * math.sin(self._pulse_phase)

        busy = bool(
            (self.worker is not None and self.worker.isRunning())
            or (self.ai_batch_worker is not None and self.ai_batch_worker.isRunning())
        )
        drop_feedback = self._drop_feedback_active()
        icon_rect = outer_rect.adjusted(
            -1,
            -5 if drop_feedback else (-4 if self._drag_hover else -3),
            1,
            1,
        )
        if self._bounce_feedback_active():
            icon_rect = self._bounced_icon_rect(icon_rect)
        elif self._drag_release_feedback_active():
            icon_rect = self._drag_release_icon_rect(icon_rect)
        elif self._window_drag_active:
            icon_rect = self._dragged_icon_rect(icon_rect)
        elif self._nudge_feedback_active():
            icon_rect = self._nudged_icon_rect(icon_rect)
        elif self._reject_feedback_active():
            icon_rect = self._rejected_icon_rect(icon_rect)
        elif busy:
            icon_rect = self._busy_breath_icon_rect(icon_rect, pulse)
        active = (
            self._drag_hover
            or self._external_drag_candidate
            or self._hovered
            or busy
            or drop_feedback
            or self._nudge_feedback_active()
            or self._reject_feedback_active()
            or self._window_drag_active
            or self._drag_release_feedback_active()
        )
        self._draw_haypile_aura(painter, icon_rect, pulse, active=active)
        self._draw_haypile_icon(
            painter,
            icon_rect,
            active=active,
        )
        self._draw_directional_haypile_aura(painter, icon_rect, pulse)
        if show_pending and self._has_pending_assets and not busy:
            self._draw_pending_badge(painter, outer_rect)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.setFixedSize(self.size())
        self._configure_window_surface()
        if (
            not self.low_power_enabled
            and (sys.platform == "darwin" or sys.platform.startswith("win"))
            and not self._drag_awareness_timer.isActive()
        ):
            self._drag_awareness_timer.start()

    def hideEvent(self, event) -> None:
        self._drag_awareness_timer.stop()
        self._pointer_press_owned = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_external_drag_candidate()
        self._reset_drop_visual_state()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_window_mask()
        self._reposition_quick_menu()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.is_expanded:
            circle_rect = self._get_collapsed_circle_rect()
            center = circle_rect.center()
            click_pos = event.position().toPoint()
            dx = click_pos.x() - center.x()
            dy = click_pos.y() - center.y()
            radius = circle_rect.width() / 2
            if dx * dx + dy * dy > radius * radius:
                event.ignore()
                return

        if event.button() == Qt.MouseButton.RightButton:
            self.quick_menu.hide_menu()
            if self._exit_armed:
                self.close()
                return
            self._exit_armed = True
            self._exit_timer.start()
            self._shake_window()
            self._sync_visual_timer()
            self.update()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._clear_exit_armed()
            self._pointer_press_owned = True
            self._clear_external_drag_candidate()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self._press_global_pos = event.globalPosition().toPoint()
            self._last_drag_global_pos = self._press_global_pos
            self._last_drag_sample_at = time.monotonic()
            self._drag_velocity = QPointF(0, 0)
            self._window_drag_active = False
            self._drag_moved = False
            self.drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        if not self._pointer_press_owned:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_visual_timer()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._sync_visual_timer()
        self.update()
        super().leaveEvent(event)

    def _shake_window(self) -> None:
        if (
            hasattr(self, "_shake_animation")
            and self._shake_animation.state() == QPropertyAnimation.State.Running
        ):
            return

        self._shake_animation = QPropertyAnimation(self, b"pos")
        self._shake_animation.setDuration(300)

        current_pos = self._clamped_window_point(self.pos())
        if current_pos != self.pos():
            self.move(current_pos)
        offset = 8

        self._shake_animation.setKeyValueAt(0, current_pos)
        self._shake_animation.setKeyValueAt(
            0.2, self._clamped_window_point(QPoint(current_pos.x() - offset, current_pos.y()))
        )
        self._shake_animation.setKeyValueAt(
            0.4, self._clamped_window_point(QPoint(current_pos.x() + offset, current_pos.y()))
        )
        self._shake_animation.setKeyValueAt(
            0.6, self._clamped_window_point(QPoint(current_pos.x() - offset, current_pos.y()))
        )
        self._shake_animation.setKeyValueAt(
            0.8, self._clamped_window_point(QPoint(current_pos.x() + offset, current_pos.y()))
        )
        self._shake_animation.setKeyValueAt(1, current_pos)

        self._shake_animation.start()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.close()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_quick_menu()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            if (event.globalPosition().toPoint() - self._press_global_pos).manhattanLength() > 5:
                self._drag_moved = True
                self._window_drag_active = True
            self._sample_drag_velocity(event.globalPosition().toPoint())
            self.move(self._clamped_window_point(event.globalPosition().toPoint() - self.drag_offset))
            if self.quick_menu.isVisible():
                self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=False)
            self._sync_visual_timer()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pointer_press_owned = False
            self._clear_external_drag_candidate()
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            if not self._drag_moved and not self.is_expanded:
                self._toggle_quick_menu()
            elif self._drag_moved:
                self._start_drag_release_feedback()
                self._save_window_position()
                if self.quick_menu.isVisible():
                    self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=True)
            self._window_drag_active = False
            self._drag_velocity = QPointF(0, 0)
            self._sync_visual_timer()
            self._drag_moved = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        files = self._extract_local_files(event.mimeData())
        remote_urls = self._extract_remote_media_urls(event.mimeData())
        if files or remote_urls:
            self._close_attached_ui()
            self._cancel_audio_suction()
            self._drop_visual_kind = self._drop_visual_kind_for_mime_data(event.mimeData())
            self._drag_hover = True
            self._drag_prepare_active = True
            self._update_drag_awareness_target_local(event.position())
            self._collapse_timer.stop()
            event.acceptProposedAction()
            self._drag_prepare_timer.start()
            self._sync_visual_timer()
            self.update()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not self._drag_hover:
            event.ignore()
            return
        self._update_drag_awareness_target_local(event.position())
        event.acceptProposedAction()
        self.update()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        event.accept()
        self._drag_hover = False
        self._drag_prepare_active = False
        self._drag_prepare_timer.stop()
        self._cancel_audio_suction()
        self._close_drop_target()
        self._sync_visual_timer()

    def dropEvent(self, event: QDropEvent) -> None:
        files = self._extract_local_files(event.mimeData())
        remote_urls = self._extract_remote_media_urls(event.mimeData())
        self._drop_visual_kind = self._drop_visual_kind_for_mime_data(event.mimeData())
        self._close_attached_ui()
        self._drag_hover = False
        self._clear_external_drag_candidate()
        self._drag_prepare_active = False
        self._drag_prepare_timer.stop()
        self._collapse_timer.stop()
        event.acceptProposedAction()
        if self._drop_visual_kind == "audio" and self._drop_open_progress > 0.05:
            self._animate_audio_suction()
        else:
            self._close_drop_target()
        self._sync_visual_timer()

        if not files and not remote_urls:
            self.show_toast(ui_text("没有找到可收纳的图片或音频", "No images or audio to store"), success=False)
            return
        if self._ingest_busy():
            self.show_toast(ui_text("正在入库中，请稍后", "Import in progress"), success=False)
            return
        if remote_urls:
            self._start_remote_download_worker(remote_urls, files)
            return
        self._drop_feedback_until = time.monotonic() + 0.65
        self._sync_visual_timer()
        self.update()
        self._start_worker(files)

    def _open_drop_target(self) -> None:
        if not self._drag_hover:
            return
        self._drag_prepare_active = False
        if self._drop_anchor_global is None:
            self._drop_anchor_global = self.mapToGlobal(self._get_collapsed_circle_rect().center())
        self._animate_drop_open(True)
        self._sync_visual_timer()
        self._animate_size(self.EXPANDED_SIZE)

    def _poll_external_drag_candidate(self) -> None:
        if self._closing:
            return
        # ponytail: this may glow for other external drags; MIME validation still gates intake.
        cursor_pos = QCursor.pos()
        if self._pointer_press_owned or self._window_drag_active or not self._global_left_button_down():
            self._clear_external_drag_candidate()
            return
        if self._global_drag_origin is None:
            self._global_drag_origin = QPoint(cursor_pos)
            return
        if self.frameGeometry().contains(self._global_drag_origin):
            if self._external_drag_candidate:
                self._external_drag_candidate = False
                if not self._drag_hover:
                    self._drag_awareness_has_direction = False
                self._sync_visual_timer()
                self.update()
            return
        if not self._external_drag_candidate:
            if (cursor_pos - self._global_drag_origin).manhattanLength() <= 8:
                return
            self._external_drag_candidate = True
        self._update_drag_awareness_target_global(cursor_pos)
        self._sync_visual_timer()
        self.update()

    def _global_left_button_down(self) -> bool:
        if sys.platform == "darwin":
            if self._cg_button_state is None:
                try:
                    framework = ctypes.CDLL(
                        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
                    )
                    button_state = framework.CGEventSourceButtonState
                    button_state.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
                    button_state.restype = ctypes.c_bool
                    self._cg_button_state = button_state
                except (AttributeError, OSError):
                    self._cg_button_state = False
            return bool(self._cg_button_state and self._cg_button_state(1, 0))
        if sys.platform.startswith("win"):
            try:
                return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
            except (AttributeError, OSError):
                return False
        return False

    def _clear_external_drag_candidate(self) -> None:
        changed = self._external_drag_candidate or self._global_drag_origin is not None
        self._external_drag_candidate = False
        self._global_drag_origin = None
        if not self._drag_hover:
            self._drag_awareness_has_direction = False
        if changed:
            self._sync_visual_timer()
            self.update()

    def _update_drag_awareness_target_global(self, cursor_pos: QPoint) -> None:
        center = self.mapToGlobal(self._get_collapsed_circle_rect().center())
        self._set_drag_awareness_target(QPointF(cursor_pos - center))

    def _update_drag_awareness_target_local(self, cursor_pos: QPointF) -> None:
        center = QPointF(self._get_collapsed_circle_rect().center())
        self._set_drag_awareness_target(cursor_pos - center)

    def _set_drag_awareness_target(self, direction: QPointF) -> None:
        distance = math.hypot(direction.x(), direction.y())
        self._drag_awareness_distance = distance
        if distance < 0.5:
            return
        target = math.atan2(direction.y(), direction.x())
        self._drag_awareness_target_angle = target
        if not self._drag_awareness_has_direction:
            self._drag_awareness_angle = target
            self._drag_awareness_has_direction = True

    def closeEvent(self, event) -> None:
        self._drag_awareness_timer.stop()
        self._clear_external_drag_candidate()
        self.shutdown()
        event.accept()
        super().closeEvent(event)

    def start_api_server(self) -> None:
        if self.api_process is not None and self.api_process.poll() is None:
            return

        if self._probe_backend_via_ipc():
            self.api_owned_by_gui = False
            return
        if self._is_port_open(self.settings.HOST, self.settings.PORT):
            self.api_owned_by_gui = False
            return

        allow_gui_backend_start = (
            os.environ.get("HAYPILE_GUI_ALLOW_BACKEND_START", "").strip().lower()
        )
        if allow_gui_backend_start in {"0", "false", "no", "off"}:
            self.api_owned_by_gui = False
            self.show_toast(ui_text("Haypile 后台未启动，当前配置禁止界面自动启动", "Haypile backend is not running; auto-start is disabled"), success=False)
            return

        command = runtime_mode_command("backend", source_root=self.project_root)
        env = os.environ.copy()
        env["HAYPILE_BACKEND_HOST_ALLOW_START"] = "1"
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        self.api_process = subprocess.Popen(
            command,
            cwd=str(self.project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            creationflags=creationflags,
        )
        if self._wait_backend_ready(timeout_seconds=5.0):
            self.api_owned_by_gui = True
            return
        self.stop_api_server()
        self.show_toast(ui_text("后台服务启动失败", "Backend failed to start"), success=False)

    def stop_api_server(self) -> None:
        if self.api_owned_by_gui:
            send_ipc_request({"type": "stop"}, timeout=0.6)
        if self.api_process is None:
            return
        if self.api_process.poll() is None:
            self.api_process.terminate()
            try:
                self.api_process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.api_process.kill()
                self.api_process.wait(timeout=2)
            if self.api_process.poll() is None:
                self._kill_process_tree(self.api_process.pid)
        self.api_process = None
        self.api_owned_by_gui = False

    def _probe_backend_via_ipc(self) -> bool:
        response = send_ipc_request({"type": "ping"}, timeout=0.45)
        return bool(response and response.get("ok"))

    def _wait_backend_ready(self, timeout_seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = send_ipc_request({"type": "ping"}, timeout=0.45)
            if response and response.get("ok") and response.get("ready"):
                return True
            time.sleep(0.12)
        return False

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.35)
            return sock.connect_ex((host, port)) == 0

    @staticmethod
    def _extract_local_files(mime_data) -> list[Path]:
        urls = mime_data.urls()
        files: list[Path] = []
        for url in urls:
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.exists() and path.is_file():
                files.append(path)
        return files

    @staticmethod
    def _extract_remote_media_urls(mime_data) -> list[str]:
        urls: list[str] = []
        for url in mime_data.urls():
            if url.isLocalFile():
                continue
            value = url.toString().strip()
            if HaypileFloatingBall._is_http_url(value):
                urls.append(value)
        if mime_data.hasHtml():
            parser = DroppedMediaHTMLParser()
            parser.feed(mime_data.html())
            urls.extend(value for value in parser.urls if HaypileFloatingBall._is_http_url(value))
        if mime_data.hasText():
            urls.extend(value for value in mime_data.text().split() if HaypileFloatingBall._is_http_url(value))
        return RemoteDownloadWorker._dedupe_urls(urls)

    @classmethod
    def _drop_visual_kind_for_mime_data(cls, mime_data) -> str:
        local_files = [
            Path(url.toLocalFile())
            for url in mime_data.urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_file()
        ]
        if any(path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS for path in local_files):
            return "leaf"

        parser = DroppedMediaHTMLParser()
        if mime_data.hasHtml():
            parser.feed(mime_data.html())
        if parser.image_urls:
            return "leaf"
        if parser.audio_urls:
            unknown_html = set(parser.urls) - parser.audio_urls - parser.image_urls
            return "audio" if not unknown_html else "leaf"

        remote_urls = {
            url.toString().strip()
            for url in mime_data.urls()
            if not url.isLocalFile() and cls._is_http_url(url.toString().strip())
        }
        remote_urls.update(parser.urls)
        if mime_data.hasText():
            remote_urls.update(value for value in mime_data.text().split() if cls._is_http_url(value))
        candidates = [*local_files, *remote_urls]
        if not candidates:
            return "leaf"
        return "audio" if all(
            candidate.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
            if isinstance(candidate, Path)
            else cls._is_audio_url(candidate)
            for candidate in candidates
        ) else "leaf"

    @staticmethod
    def _is_http_url(value: str) -> bool:
        return urlparse(value).scheme.lower() in {"http", "https"}

    @staticmethod
    def _is_audio_url(value: str) -> bool:
        return Path(urlparse(value).path).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS

    def _ingest_busy(self) -> bool:
        return bool(
            (self.worker is not None and self.worker.isRunning())
            or (self.remote_worker is not None and self.remote_worker.isRunning())
        )

    def _start_remote_download_worker(self, urls: list[str], local_files: list[Path] | None = None) -> None:
        self.remote_worker = RemoteDownloadWorker(urls, self.settings.STORAGE_DIR / "incoming" / "browser")
        self.remote_worker.progress_signal.connect(self._on_ingest_progress)
        self.remote_worker.finished_signal.connect(
            lambda downloaded, message, success: self._on_remote_download_finished(downloaded, message, success, local_files or [])
        )
        self.remote_worker.start()
        self.show_toast(ui_text("正在获取网页素材...", "Fetching web assets..."), success=True)
        self.quick_menu.begin_progress(
            self._toast_anchor(),
            self._available_geometry(),
            ui_text("正在获取网页素材...", "Fetching web assets..."),
        )
        self.quick_menu.set_progress(5, ui_text("正在获取网页素材...", "Fetching web assets..."))

    def _on_remote_download_finished(
        self,
        downloaded_files: list[Path],
        message: str,
        success: bool,
        local_files: list[Path],
    ) -> None:
        if self.remote_worker is not None:
            self.remote_worker.deleteLater()
            self.remote_worker = None
        if not success and not local_files:
            self.show_toast(message, success=False)
            self.quick_menu.complete_progress(False, message)
            return
        files = [*local_files, *downloaded_files]
        if not files:
            self.show_toast(ui_text("没有找到可收纳的图片或音频", "No images or audio to store"), success=False)
            self.quick_menu.complete_progress(False, message)
            return
        self._drop_feedback_until = time.monotonic() + 0.65
        self._sync_visual_timer()
        self.update()
        self._start_worker(files)

    def _start_worker(self, files: list[Path]) -> None:
        merged_files: list[Path] = []
        seen: set[str] = set()
        for file_path in files:
            key = str(file_path.resolve())
            if key in seen:
                continue
            if file_path.exists() and file_path.is_file():
                merged_files.append(file_path)
                seen.add(key)
        if not merged_files:
            self.show_toast(ui_text("没有可收纳的文件", "No files to store"), success=False)
            return

        self.worker = IngestWorker(merged_files, self.assets_dir, ai_enabled=self.ai_enabled)
        self.worker.finished_signal.connect(self._on_ingest_finished)
        self.worker.progress_signal.connect(self._on_ingest_progress)
        self.worker.batch_signal.connect(self._on_ingest_batch)
        self.worker.start()
        self._sync_visual_timer()
        self.show_toast(ui_text(f"已接收 {len(merged_files)} 个文件，正在收纳...", f"Received {len(merged_files)} files, storing..."), success=True)
        self.quick_menu.begin_progress(
            self._toast_anchor(),
            self._available_geometry(),
            ui_text("正在收纳...", "Storing..."),
        )

    def _on_ingest_finished(self, message: str, success: bool) -> None:
        if success:
            now = time.monotonic()
            if self._is_duplicate_only_result(message):
                self._drop_feedback_until = 0.0
                self._bounce_feedback_until = 0.0
                self._nudge_feedback_started_at = now
                self._nudge_feedback_until = now + 0.7
            else:
                self._nudge_feedback_until = 0.0
                self._reject_feedback_until = 0.0
                self._bounce_feedback_started_at = now
                self._bounce_feedback_until = now + 0.55
                self._drop_feedback_until = self._bounce_feedback_until
                self.quick_menu.set_attention_action("assets")
            self._sync_visual_timer()
            self.update()
        else:
            now = time.monotonic()
            self._drop_feedback_until = 0.0
            self._bounce_feedback_until = 0.0
            self._nudge_feedback_until = 0.0
            self._reject_feedback_started_at = now
            self._reject_feedback_until = now + 0.32
            self._sync_visual_timer()
            self.update()
        self.show_toast(message, success=success)
        self.quick_menu.complete_progress(success, message)
        if self.quick_menu.current_page() == "assets":
            self.material_panel.refresh()
        self._refresh_pending_badge()
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    def _on_ingest_batch(self, batch_id: str, _summary: object) -> None:
        self.latest_batch_id = batch_id
        if hasattr(self.material_panel, "set_batch_scope"):
            self.material_panel.set_batch_scope("latest")
        if self.ai_enabled and not self.low_power_enabled:
            self._enqueue_ai_batch(batch_id)

    def _enqueue_ai_batch(self, batch_id: str) -> None:
        normalized = str(batch_id or "").strip()
        if not normalized or normalized in self._ai_batch_queue:
            return
        if self.ai_batch_worker is not None and self.ai_batch_worker.batch_id == normalized:
            return
        self._ai_batch_queue.append(normalized)
        self._start_next_ai_batch()

    def _start_next_ai_batch(self) -> None:
        if self.ai_batch_worker is not None or not self.ai_enabled or self.low_power_enabled:
            return
        while self._ai_batch_queue:
            batch_id = self._ai_batch_queue.pop(0)
            bundles = BundleService().list_bundles(
                status="pending",
                asset_type="image",
                batch_id=batch_id,
            )
            if not bundles:
                continue
            self.ai_batch_worker = AIBatchWorker(
                batch_id,
                bundles,
                self.assets_dir,
                self._current_ai_provider_config(),
            )
            self.ai_batch_worker.finished_signal.connect(self._on_ai_batch_finished)
            self.ai_batch_worker.start()
            self._sync_visual_timer()
            return

    def _on_ai_batch_finished(self, batch_id: str, message: str, result_status: str) -> None:
        worker = self.ai_batch_worker
        self.ai_batch_worker = None
        if worker is not None:
            worker.deleteLater()
        if self.quick_menu.current_page() == "assets":
            self.material_panel.refresh()
        self._refresh_pending_badge()
        if batch_id == self.latest_batch_id:
            self.show_toast(
                message,
                success=result_status in {"success", "partial_success"},
            )
        self._sync_visual_timer()
        self._start_next_ai_batch()

    def _retry_latest_ai_batch(self) -> None:
        latest = BundleService().get_latest_batch()
        if latest is None:
            self.show_toast(ui_text("还没有可重试的批次", "No batch to retry"), success=False)
            return
        if not self.ai_enabled or self.low_power_enabled:
            self.show_toast(ui_text("请先开启 AI 整理", "Enable AI sorting first"), success=False)
            return
        self._enqueue_ai_batch(str(latest["id"]))
        self.show_toast(ui_text("已加入 AI 整理队列", "Added to AI sorting queue"), success=True)

    def _on_ingest_progress(self, percent: int, text: str) -> None:
        self.quick_menu.set_progress(percent, text)

    def show_toast(self, message: str, *, success: bool) -> None:
        self.quick_menu.show_feedback(
            message,
            success,
            self._toast_anchor(),
            self._available_geometry(),
        )

    def _reposition_toast(self) -> None:
        self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=False)

    def _toast_anchor(self) -> QRect:
        circle = self._get_collapsed_circle_rect()
        top_left = self.mapToGlobal(circle.topLeft())
        return QRect(top_left, circle.size())

    def _ball_anchor_rect(self) -> QRect:
        return self._toast_anchor()

    def _animate_size(self, target_size: int) -> None:
        if self._closing:
            return
        # Keep native resize handles disabled while idle; only Haypile may resize itself.
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        if self._drop_anchor_global is not None:
            if self._geometry_animation is not None:
                self._geometry_animation.stop()
            self.is_expanded = target_size > self.COLLAPSED_SIZE
            self.setGeometry(self._clamped_geometry_for_size(target_size))
            self.setFixedSize(target_size, target_size)
            self._update_window_mask()
            if target_size == self.COLLAPSED_SIZE:
                self._drop_anchor_global = None
                self._reset_drop_visual_state()
            self.update()
            return
        if self.width() == target_size and self.height() == target_size:
            self.is_expanded = target_size > self.COLLAPSED_SIZE
            clamped = self._clamped_geometry_for_size(target_size)
            if clamped.topLeft() != self.pos():
                self.setGeometry(clamped)
            self.setFixedSize(target_size, target_size)
            if target_size == self.COLLAPSED_SIZE:
                self._reset_drop_visual_state()
            self._update_window_mask()
            self.update()
            return

        self.is_expanded = target_size > self.COLLAPSED_SIZE
        self._update_window_mask()
        current = self.geometry()
        target_rect = self._clamped_geometry_for_size(target_size)
        animation = QPropertyAnimation(self, b"geometry")
        self._geometry_animation = animation
        animation.setDuration(220)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.setStartValue(current)
        animation.setEndValue(target_rect)
        animation.valueChanged.connect(lambda _value: self.update())
        animation.finished.connect(lambda: self._on_resize_animation_done(target_size))
        animation.start()

    def _on_resize_animation_done(self, target_size: int) -> None:
        self.is_expanded = target_size > self.COLLAPSED_SIZE
        self.setFixedSize(target_size, target_size)
        if target_size == self.COLLAPSED_SIZE:
            self._reset_drop_visual_state()
        self._update_window_mask()
        self.update()

    def _clamped_geometry_for_size(self, target_size: int) -> QRect:
        current = self.geometry()
        center = self._drop_anchor_global or current.center()
        half_span = (target_size - 1) // 2
        x, y = self._clamp_window_position(
            center.x() - half_span,
            center.y() - half_span,
            target_size,
            target_size,
        )
        return QRect(x, y, target_size, target_size)

    def _clamped_window_point(self, point: QPoint) -> QPoint:
        x, y = self._clamp_window_position(point.x(), point.y(), self.width(), self.height())
        return QPoint(x, y)

    def _read_gui_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self._gui_state_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_gui_state(self, updates: dict[str, object]) -> None:
        payload = self._read_gui_state()
        payload.update(updates)
        atomic_write_json(self._gui_state_path, payload)

    def _restore_window_position(self) -> QPoint:
        try:
            payload = self._read_gui_state()
            point = QPoint(int(payload.get("x", 60)), int(payload.get("y", 60)))
        except (ValueError, TypeError):
            point = QPoint(60, 60)
        return self._clamped_window_point(point)

    def _save_window_position(self) -> None:
        try:
            self._save_gui_state({"x": self.x(), "y": self.y()})
        except OSError:
            logger.debug("Failed to save Haypile window position")

    def _default_ai_enabled(self) -> bool:
        return bool(self.settings.VISION_CLASSIFIER_ENABLED) and not self.low_power_enabled

    def _restore_ai_enabled(self) -> bool:
        if self.low_power_enabled or self.ai_provider_mode == "off":
            return False
        if self.ai_provider_mode == "api":
            return bool(self.ai_api_base_url and self.ai_api_model and self._session_api_key)
        return bool(self._ai_preference)

    def _save_ai_enabled(self) -> None:
        if not self.low_power_enabled:
            self._ai_preference = bool(self.ai_enabled)
        try:
            self._save_gui_state(
                {
                    "ai_enabled": self._ai_preference,
                    "ai_provider": self.ai_provider_mode,
                }
            )
        except OSError:
            logger.debug("Failed to save Haypile AI setting")

    def _set_low_power_enabled(self, enabled: bool) -> None:
        if enabled and self.ai_enabled and self.ai_provider_mode == "off":
            self.ai_provider_mode = "local"
        self.low_power_enabled = bool(enabled)
        if self.low_power_enabled:
            self._ai_preference = bool(self.ai_enabled or self._ai_preference)
            self.ai_enabled = False
            self._drag_awareness_timer.stop()
            self._clear_external_drag_candidate()
            self._ai_batch_queue.clear()
            self._shutdown_ai_batch_worker()
        else:
            self.ai_enabled = self._restore_ai_enabled()
            if self.isVisible() and (sys.platform == "darwin" or sys.platform.startswith("win")):
                self._drag_awareness_timer.start()
        try:
            self._save_gui_state(
                {
                    "low_power_enabled": self.low_power_enabled,
                    "ai_enabled": self._ai_preference,
                }
            )
        except OSError:
            logger.debug("Failed to save Haypile low-power setting")
        self._sync_visual_timer()
        self._refresh_ai_menu_status()
        self.show_toast(
            ui_text("低功耗模式已开启", "Low power enabled")
            if self.low_power_enabled
            else ui_text("低功耗模式已关闭", "Low power disabled"),
            success=True,
        )

    def _current_ai_provider_config(self) -> AIProviderConfig:
        if not self.ai_enabled or self.low_power_enabled or self.ai_provider_mode == "off":
            return AIProviderConfig(mode="off")
        if self.ai_provider_mode == "api":
            return AIProviderConfig(
                mode="api",
                base_url=self.ai_api_base_url,
                model=self.ai_api_model,
                api_key=self._session_api_key,
                authorized_host=self.ai_api_authorized_host,
            )
        return AIProviderConfig(
            mode="local",
            base_url=str(self.settings.VISION_CLASSIFIER_BASE_URL),
            model=str(self.settings.VISION_CLASSIFIER_MODEL),
        )

    def _set_ai_provider_mode(self, mode: str) -> None:
        if mode not in {"local", "api", "off"}:
            return
        self.ai_provider_mode = mode
        self._ai_preference = mode != "off"
        if mode == "off" or self.low_power_enabled:
            self.ai_enabled = False
            self._ai_batch_queue.clear()
            self._shutdown_ai_batch_worker()
        elif mode == "local":
            self.ai_enabled = True
        else:
            self.ai_enabled = bool(
                self.ai_api_base_url and self.ai_api_model and self._session_api_key
            )
        try:
            self._save_gui_state(
                {
                    "ai_provider": self.ai_provider_mode,
                    "ai_enabled": self._ai_preference,
                }
            )
        except OSError:
            logger.debug("Failed to save Haypile AI provider")
        self._refresh_ai_menu_status()
        if mode == "api" and not self.ai_enabled:
            self.show_toast(ui_text("填写 API 配置后保存授权", "Enter API settings and authorize"), success=False)
        else:
            self.show_toast(self._ai_status_text(), success=True)

    def _save_api_provider(self) -> None:
        base_value = self.quick_menu.ai_api_base_input.text().strip()
        model = self.quick_menu.ai_api_model_input.text().strip()
        entered_key = self.quick_menu.ai_api_key_input.text().strip()
        try:
            base_url = normalize_api_base_url(base_value)
            host = api_authority(base_url)
        except ValueError:
            self.show_toast(
                ui_text("API 地址无效；远程服务必须使用 HTTPS", "Invalid API URL; remote services require HTTPS"),
                success=False,
            )
            return
        if not model:
            self.show_toast(ui_text("请填写模型名称", "Enter a model name"), success=False)
            return
        host_changed = bool(self.ai_api_authorized_host and host != self.ai_api_authorized_host)
        if host_changed and not entered_key:
            self.show_toast(ui_text("更换域名后请重新填写密钥授权", "Enter the key again for the new domain"), success=False)
            return
        key = entered_key
        if not key and host == self.ai_api_authorized_host:
            key = self._session_api_key or SystemCredentialStore.get(host)
        if not key:
            self.show_toast(ui_text("请填写 API 密钥", "Enter an API key"), success=False)
            return

        stored = SystemCredentialStore.set(host, key)
        self.ai_provider_mode = "api"
        self.ai_api_base_url = base_url
        self.ai_api_model = model
        self.ai_api_authorized_host = host
        self.ai_api_key_present = stored
        self._session_api_key = key
        self._ai_preference = True
        self.ai_enabled = not self.low_power_enabled
        try:
            self._save_gui_state(
                {
                    "ai_provider": "api",
                    "ai_enabled": True,
                    "ai_api_base_url": base_url,
                    "ai_api_model": model,
                    "ai_api_authorized_host": host,
                    "ai_api_key_present": stored,
                }
            )
        except OSError:
            logger.debug("Failed to save Haypile API provider settings")
        self.quick_menu.ai_api_key_input.clear()
        self._refresh_ai_menu_status()
        self.show_toast(
            ui_text("API 已授权", "API authorized")
            if stored
            else ui_text("API 仅在本次会话可用", "API available for this session only"),
            success=True,
        )

    def _set_language_mode(self, mode: str) -> None:
        self.language_mode = mode if mode in {"auto", "zh", "en"} else "auto"
        set_ui_language(self.language_mode)
        try:
            self._save_gui_state({"language": self.language_mode})
        except OSError:
            logger.debug("Failed to save Haypile language setting")
        self.quick_menu.retranslate()
        self.material_panel.refresh()
        self._refresh_ai_menu_status()
        self.show_toast(ui_text("语言已更新", "Language updated"), success=True)

    def _clear_exit_armed(self) -> None:
        self._exit_armed = False
        self._sync_visual_timer()
        self.update()

    def _shutdown_worker(self) -> None:
        if self.worker is None:
            return
        if self.worker.isRunning():
            self.worker.requestInterruption()
            if not self.worker.wait(1800):
                self.worker.terminate()
                self.worker.wait(600)
        self.worker.deleteLater()
        self.worker = None

    def _shutdown_remote_worker(self) -> None:
        if self.remote_worker is None:
            return
        if self.remote_worker.isRunning():
            self.remote_worker.requestInterruption()
            if not self.remote_worker.wait(1800):
                self.remote_worker.terminate()
                self.remote_worker.wait(600)
        self.remote_worker.deleteLater()
        self.remote_worker = None

    def _shutdown_ai_batch_worker(self) -> None:
        if self.ai_batch_worker is None:
            return
        if self.ai_batch_worker.isRunning():
            self.ai_batch_worker.requestInterruption()
            if not self.ai_batch_worker.wait(1800):
                self.ai_batch_worker.terminate()
                self.ai_batch_worker.wait(600)
        self.ai_batch_worker.deleteLater()
        self.ai_batch_worker = None

    def shutdown(self) -> None:
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self._closing = True
        self._collapse_timer.stop()
        self._drag_prepare_timer.stop()
        self._exit_timer.stop()
        self._visual_timer.stop()
        self._drag_awareness_timer.stop()
        if self._drop_open_animation is not None:
            self._drop_open_animation.stop()
        if self._audio_suction_animation is not None:
            self._audio_suction_animation.stop()
        self._shutdown_remote_worker()
        self._shutdown_worker()
        self._shutdown_ai_batch_worker()
        self.stop_api_server()
        self.quick_menu.hide()
        self.quick_menu.close()
        QTimer.singleShot(0, QCoreApplication.quit)

    def _reposition_progress_window(self) -> None:
        if self.quick_menu.isVisible():
            self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=False)

    def _progress_window_position(self) -> QPoint:
        x, y = self._side_window_position(270, 44)
        return QPoint(x, y)

    def _toggle_material_panel(self) -> None:
        self.quick_menu.open_drawer("assets", self._ball_anchor_rect(), self._available_geometry())

    def _reposition_material_panel(self) -> None:
        self._reposition_quick_menu()

    def _close_attached_ui(self) -> None:
        self.quick_menu.hide_menu()
        QTimer.singleShot(0, self._refresh_pending_badge)

    def _toggle_quick_menu(self) -> None:
        if self.quick_menu.isVisible():
            self._close_attached_ui()
            return
        if self._has_pending_assets and not self.quick_menu._attention_action:
            self.quick_menu.set_attention_action("assets")
        self.quick_menu.show_attached(self._ball_anchor_rect(), self._available_geometry())

    def _reposition_quick_menu(self) -> None:
        if self.quick_menu.isVisible():
            self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=False)

    def _align_quick_menu_track_to_ball(self) -> None:
        self._reposition_quick_menu()

    def _handle_quick_menu_action(self, action: str) -> None:
        if action == "assets":
            self._toggle_material_panel()
            return
        if action in {"agent", "settings"}:
            self.quick_menu.open_drawer(action, self._ball_anchor_rect(), self._available_geometry())
            if action == "settings":
                QTimer.singleShot(0, self._refresh_ai_menu_status)
            return
        if action == "mcp":
            QApplication.clipboard().setText(self._mcp_config_text())
            self.show_toast(ui_text("已复制 MCP 配置", "MCP config copied"), success=True)
            return
        if action == "http":
            base_url = self._base_url()
            QApplication.clipboard().setText(base_url)
            self.show_toast(ui_text(f"已复制 HTTP 地址 {base_url}", f"HTTP URL copied {base_url}"), success=True)
            return
        if action == "latest_handoff":
            service = BundleService()
            latest = service.get_latest_batch()
            if latest is None:
                self.show_toast(ui_text("还没有最新批次", "No latest batch"), success=False)
                return
            batch_id = str(latest["id"])
            bundles = service.list_bundles(status="ready", batch_id=batch_id)
            if not bundles:
                self.show_toast(ui_text("最新批次还没有可用素材", "Latest batch has no ready assets"), success=False)
                return
            QApplication.clipboard().setText(
                json.dumps(
                    self.material_panel._handoff_for_bundles(bundles, batch_id=batch_id),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self.show_toast(
                ui_text(f"已复制最新批次 {len(bundles)} 个素材", f"Copied {len(bundles)} latest assets"),
                success=True,
            )
            return
        if action == "ready_handoff":
            bundles = BundleService().list_bundles(status="ready")
            if not bundles:
                self.show_toast(ui_text("没有可用素材", "No ready assets"), success=False)
                return
            QApplication.clipboard().setText(
                json.dumps(self.material_panel._handoff_for_bundles(bundles), ensure_ascii=False, indent=2)
            )
            self.show_toast(ui_text(f"已复制 {len(bundles)} 个可用素材", f"Copied {len(bundles)} ready assets"), success=True)
            return
        if action == "agent_recipe":
            QApplication.clipboard().setText(self.material_panel._agent_recipe_text())
            self.show_toast(ui_text("已复制 Agent 配方", "Agent recipe copied"), success=True)
            return
        if action == "ai_setup":
            self._show_ai_setup_panel(self._ai_model_status_text())
            return
        if action.startswith("ai_provider:"):
            self._set_ai_provider_mode(action.partition(":")[2])
            return
        if action == "ai_save_api":
            self._save_api_provider()
            return
        if action == "ai_toggle":
            if self.ai_enabled:
                self._set_ai_provider_mode("off")
                return
            self.ai_provider_mode = "local"
            state, status_text = self._ai_model_state()
            if state != "ready":
                self.ai_enabled = False
                self._show_ai_setup_panel(status_text)
                return
            self._set_ai_provider_mode("local")
            return
        if action == "ai_copy_command":
            model = str(self.settings.VISION_CLASSIFIER_MODEL or "qwen2.5vl:3b")
            QApplication.clipboard().setText(f"ollama pull {model}")
            self.show_toast(ui_text("已复制模型安装命令", "Model install command copied"), success=True)
            return
        if action == "ai_recheck":
            self._recheck_ai_setup()
            return
        if action == "low_power":
            self._set_low_power_enabled(not self.low_power_enabled)
            return
        if action.startswith("language:"):
            self._set_language_mode(action.partition(":")[2])
            return
        if action == "logs":
            self.settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.LOG_DIR)))
            self.show_toast(ui_text("已打开日志目录", "Logs folder opened"), success=bool(opened))
            return
        if action == "exit":
            if self._ingest_busy():
                if getattr(self, "_busy_exit_armed", False):
                    self.close()
                    return
                self._busy_exit_armed = True
                self.quick_menu.exit_button.setText(ui_text("再次点击以退出", "Click again to quit"))
                self.show_toast(ui_text("仍在收纳，退出会中断任务", "Import is active; quitting will stop it"), success=False)
                QTimer.singleShot(2200, self._clear_busy_exit_armed)
                return
            self.close()

    def _refresh_ai_menu_status(self) -> None:
        if not hasattr(self, "quick_menu"):
            return
        ai_status = self._ai_status_text()
        self.quick_menu.set_ai_enabled(self.ai_enabled, ai_status)
        self.quick_menu.update_settings_state(
            ai_enabled=self.ai_enabled,
            ai_status=ai_status,
            low_power=self.low_power_enabled,
            language=self.language_mode,
            service_status=self._status_text(),
            ai_provider=self.ai_provider_mode,
            api_base_url=self.ai_api_base_url,
            api_model=self.ai_api_model,
            api_key_present=self.ai_api_key_present,
        )

    def _ai_status_text(self) -> str:
        if self.low_power_enabled:
            return ui_text("低功耗模式 · AI 分拣关闭", "Low power · AI sorting off")
        if self.ai_provider_mode == "off":
            return ui_text("AI 分拣已关闭", "AI sorting off")
        if self.ai_provider_mode == "api":
            return (
                ui_text(f"API 模式 · {self.ai_api_model}", f"API mode · {self.ai_api_model}")
                if self.ai_enabled
                else ui_text("API 模式 · 等待授权", "API mode · authorization needed")
            )
        return ui_text("本地模型模式", "Local model mode")

    def _ai_model_status_text(self) -> str:
        return self._ai_model_state()[1]

    def _ai_model_state(self) -> tuple[str, str]:
        if self.ai_provider_mode == "off":
            return "off", ui_text("AI 分拣已关闭", "AI sorting off")
        if self.ai_provider_mode == "api":
            if self.ai_enabled and self._session_api_key:
                return "ready", ui_text(f"API 已配置 {self.ai_api_model}", f"API configured {self.ai_api_model}")
            return "missing", ui_text("API 等待授权", "API authorization needed")
        model = str(self.settings.VISION_CLASSIFIER_MODEL or "").strip() or "unknown"
        base_url = str(self.settings.VISION_CLASSIFIER_BASE_URL or "").rstrip("/")
        if not base_url:
            return "missing", ui_text(f"模型未配置 {model}", f"Model not configured {model}")
        try:
            response = httpx.get(f"{base_url}/api/tags", timeout=0.25, trust_env=False)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return "offline", ui_text(f"模型离线 {model}", f"Model offline {model}")
        names = {
            str(value).strip()
            for item in payload.get("models", [])
            if isinstance(item, dict)
            for value in (item.get("name"), item.get("model"))
            if value
        }
        if model in names:
            return "ready", ui_text(f"模型可用 {model}", f"Model ready {model}")
        return "missing", ui_text(f"模型未安装 {model}", f"Model missing {model}")

    def _show_ai_setup_panel(self, status_text: str) -> None:
        self.quick_menu.open_drawer("ai", self._ball_anchor_rect(), self._available_geometry())
        self.quick_menu.ai_status_label.setText(status_text)
        if self.ai_provider_mode == "local" and self._ai_model_state()[0] != "ready":
            self.show_toast(ui_text("先安装本地视觉模型", "Install the local vision model first"), success=False)

    def _recheck_ai_setup(self) -> None:
        if self.ai_provider_mode == "off":
            self.ai_provider_mode = "local"
        if self.ai_provider_mode != "local":
            self._refresh_ai_menu_status()
            return
        state, status_text = self._ai_model_state()
        if state == "ready":
            self.ai_enabled = True
            self._ai_preference = True
            self._save_ai_enabled()
            self._refresh_ai_menu_status()
            self.show_toast(self._ai_status_text(), success=True)
            return
        self._show_ai_setup_panel(status_text)

    def _clear_busy_exit_armed(self) -> None:
        self._busy_exit_armed = False
        if hasattr(self, "quick_menu"):
            self.quick_menu.exit_button.setText(ui_text("退出 Haypile", "Quit Haypile"))

    def _status_text(self) -> str:
        try:
            read_manifest_readiness(self.settings.MANIFEST_PATH)
        except ManifestReadinessError:
            return ui_text(
                "素材已保存 · Agent 接口待恢复",
                "Assets saved · Agent access pending recovery",
            )
        summary = build_material_panel_summary()
        return ui_text(
            f"运行中 · 可用 {summary.recognized_count} · 待确认 {summary.pending_count}",
            f"Running · ready {summary.recognized_count} · pending {summary.pending_count}",
        )

    def _base_url(self) -> str:
        host = self.settings.HOST if self.settings.HOST != "0.0.0.0" else "127.0.0.1"
        return f"http://{host}:{self.settings.PORT}"

    def _mcp_config_text(self) -> str:
        command = runtime_mode_command("mcp", source_root=self.project_root)
        payload = {
            "mcpServers": {
                "haypile": {
                    "command": command[0],
                    "args": command[1:],
                    "env": {"HAYPILE_BASE_URL": self._base_url()},
                }
            }
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _available_geometry(self) -> QRect:
        screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)

    def _side_window_position(self, width: int, height: int) -> tuple[int, int]:
        frame = self.frameGeometry()
        available = self._available_geometry()
        margin = 10
        gap = 12
        right_x = frame.right() + gap
        if right_x + width <= available.right() - margin:
            x = right_x
        else:
            x = frame.left() - width - gap
        y = frame.center().y() - height // 2
        return self._clamp_window_position(x, y, width, height)

    def _clamp_window_position(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> tuple[int, int]:
        available = self._available_geometry()
        margin = 10
        min_x = available.left() + margin
        max_x = available.right() - width - margin
        min_y = available.top() + margin
        max_y = available.bottom() - height - margin
        if max_x < min_x:
            max_x = min_x
        if max_y < min_y:
            max_y = min_y
        return (
            max(min_x, min(int(x), max_x)),
            max(min_y, min(int(y), max_y)),
        )

    def _get_collapsed_circle_rect(self) -> QRect:
        size = 68
        offset_x = max((self.width() - size) // 2, 0)
        offset_y = max((self.height() - size) // 2, 0)
        return QRect(offset_x, offset_y, size, size)

    def _get_drop_open_progress(self) -> float:
        return self._drop_open_progress

    def _set_drop_open_progress(self, value: float) -> None:
        self._drop_open_progress = max(0.0, min(float(value), 1.0))
        self.update()

    def _drop_visual_offset(self, progress: float) -> QPointF:
        if self._drop_anchor_global is None:
            return QPointF()
        progress = max(0.0, min(float(progress), 1.0))
        eased = progress * progress * (3.0 - 2.0 * progress)
        center = QRectF(self.rect()).center()
        anchor = QPointF(self.mapFromGlobal(self._drop_anchor_global))
        return (anchor - center) * (1.0 - eased)

    def _get_audio_suction_progress(self) -> float:
        return self._audio_suction_progress

    def _set_audio_suction_progress(self, value: float) -> None:
        self._audio_suction_progress = max(0.0, min(float(value), 1.0))
        self.update()

    def _cancel_audio_suction(self) -> None:
        if self._audio_suction_animation is not None:
            self._audio_suction_animation.stop()
            self._audio_suction_animation = None
        self._set_audio_suction_progress(0.0)

    def _animate_audio_suction(self) -> None:
        self._cancel_audio_suction()
        animation = QVariantAnimation(self)
        self._audio_suction_animation = animation
        animation.valueChanged.connect(self._set_audio_suction_progress)
        animation.setDuration(150)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.finished.connect(self._finish_audio_suction)
        animation.start()

    def _finish_audio_suction(self) -> None:
        self._audio_suction_animation = None
        self._close_drop_target()

    def _close_drop_target(self) -> None:
        self._animate_drop_open(False)
        self._collapse_timer.start()

    def _reset_drop_visual_state(self) -> None:
        self._cancel_audio_suction()
        self._drop_visual_kind = "leaf"

    def _animate_drop_open(self, opened: bool) -> None:
        target = 1.0 if opened else 0.0
        if self._drop_open_animation is not None and self._drop_open_animation.state() == QVariantAnimation.State.Running:
            self._drop_open_animation.stop()
        if abs(self._drop_open_progress - target) < 0.01:
            self._set_drop_open_progress(target)
            return
        animation = QVariantAnimation(self)
        self._drop_open_animation = animation
        animation.valueChanged.connect(self._set_drop_open_progress)
        animation.setDuration(210 if opened else 170)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic if opened else QEasingCurve.Type.InOutCubic)
        animation.setStartValue(self._drop_open_progress)
        animation.setEndValue(target)
        animation.start()

    def _draw_audio_intake(
        self,
        painter: QPainter,
        panel_rect: QRectF,
        progress: float,
        suction: float,
    ) -> None:
        progress = max(0.0, min(progress, 1.0))
        suction = max(0.0, min(suction, 1.0))
        if progress <= 0.0:
            return
        self._draw_audio_leaf_nest(painter, panel_rect, progress, suction)
        self._draw_audio_center_cutout(painter, panel_rect, progress)

    def _draw_audio_leaf_nest(
        self,
        painter: QPainter,
        panel_rect: QRectF,
        progress: float,
        suction: float,
    ) -> None:
        if not self._drop_leaf_renderers:
            self._draw_drop_leaf_frame(painter, panel_rect, progress, leaf_width_scale=0.78)
            return

        layers = (
            (
                QColor("#A8A96F"),
                (
                    (0, -165, 0.96, 0.40, 28, 0.42),
                    (1, -105, 0.92, 0.37, -25, 0.39),
                    (0, -45, 0.98, 0.41, 30, 0.43),
                    (1, 15, 0.93, 0.38, -26, 0.40),
                    (0, 75, 0.97, 0.40, 27, 0.42),
                    (1, 135, 0.92, 0.37, -24, 0.39),
                ),
            ),
            (
                QColor("#C4963C"),
                (
                    (2, -145, 0.86, 0.34, -24, 0.72),
                    (2, -85, 0.81, 0.32, 26, 0.69),
                    (2, -25, 0.88, 0.35, -22, 0.73),
                    (2, 35, 0.82, 0.33, 28, 0.70),
                    (2, 95, 0.87, 0.34, -25, 0.72),
                    (2, 155, 0.80, 0.32, 24, 0.69),
                ),
            ),
            (
                QColor("#4D582F"),
                (
                    (4, 179, 0.70, 0.33, -10, 0.92),
                    (3, -121, 0.75, 0.30, 8, 0.90),
                    (4, -61, 0.69, 0.32, -9, 0.93),
                    (3, -1, 0.74, 0.30, 11, 0.90),
                    (4, 59, 0.71, 0.33, -8, 0.92),
                    (3, 119, 0.76, 0.30, 10, 0.91),
                ),
            ),
        )
        buffers = self._audio_leaf_buffers()
        center = panel_rect.center()
        source_angle = self._drag_awareness_angle if self._drag_awareness_has_direction else -math.pi / 2
        source_degrees = math.degrees(source_angle)
        source_vector = QPointF(math.cos(source_angle), math.sin(source_angle))
        layer_delays = (0.0, 25.0 / 210.0, 50.0 / 210.0)
        tint_opacities = (0.82, 0.92, 1.0)

        for layer_index, ((color, placements), buffer) in enumerate(zip(layers, buffers)):
            buffer.fill(Qt.GlobalColor.transparent)
            layer_painter = QPainter(buffer)
            layer_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            layer_painter.setClipPath(self._drop_outer_path(panel_rect))
            for leaf_index, angle, radius_scale, width_scale, rotation_offset, opacity in placements:
                if leaf_index >= len(self._drop_leaf_renderers):
                    continue
                renderer = self._drop_leaf_renderers[leaf_index]
                svg_size = renderer.defaultSize()
                if svg_size.width() <= 0:
                    continue

                delta = (angle - source_degrees + 180.0) % 360.0 - 180.0
                source_weight = max(0.0, math.cos(math.radians(delta))) if abs(delta) <= 65.0 else 0.0
                delay = max(0.0, layer_delays[layer_index] - source_weight * (15.0 / 210.0))
                leaf_progress = self._staggered_progress(progress, delay)
                suction_delay = (55.0 / 150.0) * (abs(delta) / 180.0)
                leaf_suction = self._staggered_progress(suction, suction_delay)

                radians = math.radians(angle)
                radius = panel_rect.width() * radius_scale
                full_center = center + QPointF(math.cos(radians) * radius, math.sin(radians) * radius)
                slide = 0.98 - 0.18 * leaf_progress
                draw_center = center + (full_center - center) * slide
                greeting = (6.0 if layer_index < 2 else 1.8) * source_weight * leaf_progress * (1.0 - leaf_suction)
                draw_center += source_vector * greeting
                toward_center = center - draw_center
                toward_length = math.hypot(toward_center.x(), toward_center.y())
                if toward_length > 0.5:
                    draw_center += toward_center * ((9.0 * leaf_suction) / toward_length)

                open_scale = 0.72 + 0.28 * leaf_progress
                suction_scale = 1.0 - 0.06 * leaf_suction
                draw_width = min(self.width(), self.height()) * width_scale * open_scale * 0.68 * suction_scale
                draw_height = draw_width * svg_size.height() / svg_size.width()
                direction_turn = -math.sin(math.radians(delta)) * 10.0 * source_weight * (1.0 - leaf_suction)
                draw_rotation = angle - 90 + rotation_offset * (1.0 - 0.38 * leaf_suction) + direction_turn

                layer_painter.save()
                layer_painter.setOpacity(
                    opacity
                    * (0.18 + 0.82 * leaf_progress)
                    * (1.0 - 0.68 * leaf_suction)
                )
                layer_painter.translate(draw_center)
                layer_painter.rotate(draw_rotation)
                clip_height = 0.42 if leaf_index in {3, 4} else 0.48
                layer_painter.setClipRect(
                    QRectF(-draw_width * 0.56, -draw_height * 0.52, draw_width * 1.12, draw_height * clip_height),
                    Qt.ClipOperation.IntersectClip,
                )
                renderer.render(
                    layer_painter,
                    QRectF(-draw_width * 0.5, -draw_height * 0.5, draw_width, draw_height),
                )
                layer_painter.restore()

            layer_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            layer_color = QColor(color)
            layer_color.setAlphaF(tint_opacities[layer_index])
            layer_painter.fillRect(QRectF(self.rect()), layer_color)
            layer_painter.end()
            painter.drawPixmap(QPointF(0.0, 0.0), buffer)

    def _audio_leaf_buffers(self) -> tuple[QPixmap, ...]:
        dpr = max(1.0, self.devicePixelRatioF())
        key = (self.width(), self.height(), round(dpr * 100))
        if self._audio_leaf_layer_key != key:
            pixel_size = self.size() * dpr
            self._audio_leaf_layer_buffers = tuple(QPixmap(pixel_size) for _ in range(3))
            for buffer in self._audio_leaf_layer_buffers:
                buffer.setDevicePixelRatio(dpr)
            self._audio_leaf_layer_key = key
        return self._audio_leaf_layer_buffers

    @staticmethod
    def _staggered_progress(value: float, delay: float) -> float:
        if value <= delay:
            return 0.0
        progress = min(1.0, (value - delay) / max(0.001, 1.0 - delay))
        return progress * progress * (3.0 - 2.0 * progress)

    @staticmethod
    def _audio_center_path(panel_rect: QRectF, progress: float) -> QPainterPath:
        center = panel_rect.center()
        radius = panel_rect.width() * 0.155 * (0.72 + 0.28 * progress)
        points = (
            (-8, 0.93), (24, 1.13), (57, 0.86), (91, 1.08),
            (126, 0.90), (163, 1.16), (199, 0.84), (234, 1.09),
            (270, 0.89), (306, 1.14), (339, 0.87),
        )
        vertices = [
            center
            + QPointF(
                math.cos(math.radians(angle)) * radius * scale,
                math.sin(math.radians(angle)) * radius * scale,
            )
            for angle, scale in points
        ]
        path = QPainterPath()
        path.moveTo((vertices[-1] + vertices[0]) * 0.5)
        for index, point in enumerate(vertices):
            path.quadTo(point, (point + vertices[(index + 1) % len(vertices)]) * 0.5)
        path.closeSubpath()
        return path

    def _draw_audio_center_cutout(self, painter: QPainter, panel_rect: QRectF, progress: float) -> None:
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(Qt.GlobalColor.transparent)
        painter.drawPath(self._audio_center_path(panel_rect, progress))
        painter.restore()

    def _load_drop_leaf_renderers(self) -> list[QSvgRenderer]:
        leaf_dir = self.project_root / "ui_assets"
        return [
            renderer
            for renderer in (
                QSvgRenderer(str(leaf_dir / f"drop-leaf-{index}.svg"))
                for index in range(1, 6)
            )
            if renderer.isValid()
        ]

    def _load_drop_leaf_frame_runs(self) -> list[tuple[int, ...]]:
        runs_path = self.project_root / "ui_assets" / "drop-leaf-frame-runs.txt"
        try:
            return [
                tuple(int(part) for part in line.split())
                for line in runs_path.read_text(encoding="ascii").splitlines()
                if line.strip()
            ]
        except (OSError, ValueError):
            return []

    def _draw_drop_leaf_frame(
        self,
        painter: QPainter,
        panel_rect: QRectF,
        progress: float,
        *,
        leaf_width_scale: float = 1.0,
    ) -> None:
        progress = max(0.0, min(progress, 1.0))
        if self._drop_leaf_renderers:
            self._draw_vector_leaf_frame(painter, panel_rect, progress, leaf_width_scale)
            return
        if self._drop_leaf_frame_renderer.isValid():
            size = min(self.width(), self.height())
            scale = 0.88 + 0.12 * progress
            draw_size = size * scale
            frame_rect = QRectF(
                (self.width() - draw_size) * 0.5,
                (self.height() - draw_size) * 0.5,
                draw_size,
                draw_size,
            )
            painter.save()
            painter.setOpacity(0.18 + 0.82 * progress)
            self._drop_leaf_frame_renderer.render(painter, frame_rect)
            painter.restore()
            return
        if self._drop_leaf_frame_runs:
            size = min(self.width(), self.height())
            scale = 0.88 + 0.12 * progress
            draw_size = size * scale
            offset_x = (self.width() - draw_size) * 0.5
            offset_y = (self.height() - draw_size) * 0.5
            step = draw_size / 512.0
            leaf_colors = (QColor("#7b9b3a"), QColor("#556729"), QColor("#3c4819"))
            fallback_color = leaf_colors[1]
            painter.save()
            painter.setOpacity(0.18 + 0.82 * progress)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            for run in self._drop_leaf_frame_runs:
                x, y, width = run[:3]
                bucket = run[3] if len(run) > 3 else 1
                leaf_color = leaf_colors[bucket] if 0 <= bucket < len(leaf_colors) else fallback_color
                painter.fillRect(QRectF(offset_x + x * step, offset_y + y * step, width * step, step), leaf_color)
            painter.restore()
            return
    def _draw_vector_leaf_frame(
        self,
        painter: QPainter,
        panel_rect: QRectF,
        progress: float,
        leaf_width_scale: float,
    ) -> None:
        center = panel_rect.center()
        placements = [
            (0, -171, 0.91, 0.39, -10, 0.44),
            (1, -132, 0.93, 0.43, 8, 0.46),
            (0, -91, 0.90, 0.38, -7, 0.42),
            (1, -49, 0.94, 0.42, 10, 0.45),
            (0, -8, 0.91, 0.39, -8, 0.42),
            (1, 34, 0.93, 0.41, 9, 0.44),
            (0, 76, 0.89, 0.37, -9, 0.42),
            (1, 118, 0.94, 0.42, 7, 0.45),
            (0, 158, 0.90, 0.38, -10, 0.42),
            (2, -150, 0.78, 0.31, 7, 0.54),
            (2, -102, 0.75, 0.29, -6, 0.52),
            (2, -57, 0.77, 0.31, 8, 0.56),
            (2, -15, 0.74, 0.28, -7, 0.52),
            (2, 31, 0.76, 0.30, 6, 0.54),
            (2, 78, 0.74, 0.28, -9, 0.52),
            (2, 126, 0.77, 0.30, 8, 0.56),
            (4, -178, 0.68, 0.34, 6, 0.90),
            (3, -126, 0.67, 0.29, -8, 0.92),
            (4, -73, 0.69, 0.33, 9, 0.92),
            (3, -21, 0.66, 0.28, -7, 0.90),
            (4, 36, 0.68, 0.32, 8, 0.91),
            (3, 91, 0.66, 0.28, -9, 0.92),
            (4, 146, 0.69, 0.33, 7, 0.90),
        ]
        painter.save()
        painter.setClipPath(self._drop_outer_path(panel_rect))
        for leaf_index, angle, radius_scale, width_scale, rotation_offset, opacity in placements:
            if leaf_index >= len(self._drop_leaf_renderers):
                continue
            renderer = self._drop_leaf_renderers[leaf_index]
            svg_size = renderer.defaultSize()
            if svg_size.width() <= 0:
                continue
            radians = math.radians(angle)
            full_width = min(self.width(), self.height()) * width_scale
            full_height = full_width * svg_size.height() / svg_size.width()
            radius = panel_rect.width() * radius_scale
            full_center = center + QPointF(math.cos(radians) * radius, math.sin(radians) * radius)
            slide = 0.98 - 0.18 * progress
            scale = 0.72 + 0.28 * progress
            draw_center = center + (full_center - center) * slide
            draw_width = full_width * scale * 0.84 * leaf_width_scale
            draw_height = full_height * scale * 0.84

            painter.save()
            painter.setOpacity(opacity * (0.18 + 0.82 * progress))
            painter.translate(draw_center)
            painter.rotate(angle - 90 + rotation_offset)
            clip_height = 0.42 if leaf_index in {3, 4} else 0.48
            painter.setClipRect(
                QRectF(-draw_width * 0.56, -draw_height * 0.52, draw_width * 1.12, draw_height * clip_height),
                Qt.ClipOperation.IntersectClip,
            )
            renderer.render(painter, QRectF(-draw_width * 0.5, -draw_height * 0.5, draw_width, draw_height))
            painter.restore()
        painter.restore()

    @staticmethod
    def _drop_outer_path(panel_rect: QRectF) -> QPainterPath:
        center = panel_rect.center()
        radius = panel_rect.width() * 0.51
        points = [
            (-2, 0.94), (22, 1.08), (49, 0.96), (73, 1.06),
            (101, 0.93), (128, 1.09), (154, 0.97), (181, 1.07),
            (208, 0.94), (236, 1.08), (263, 0.93), (291, 1.07),
            (319, 0.96), (343, 1.05),
        ]
        outer_points = [
            center + QPointF(math.cos(math.radians(angle)) * radius * scale, math.sin(math.radians(angle)) * radius * scale)
            for angle, scale in points
        ]
        path = QPainterPath()
        first_mid = (outer_points[-1] + outer_points[0]) * 0.5
        path.moveTo(first_mid)
        for index, point in enumerate(outer_points):
            next_point = outer_points[(index + 1) % len(outer_points)]
            path.quadTo(point, (point + next_point) * 0.5)
        path.closeSubpath()
        return path

    def _draw_drop_center_cutout(self, painter: QPainter, panel_rect: QRectF, progress: float) -> None:
        progress = max(0.0, min(progress, 1.0))
        center = panel_rect.center()
        base = panel_rect.width() * (0.095 + 0.072 * progress)
        points = [
            (0, 0.82), (17, 1.22), (39, 0.78), (62, 1.08),
            (84, 0.90), (109, 1.18), (132, 0.76), (153, 1.05),
            (177, 0.84), (199, 1.24), (223, 0.86), (247, 1.12),
            (270, 0.79), (292, 1.18), (318, 0.81), (342, 1.10),
        ]
        cutout_points = []
        for angle, scale in points:
            radians = math.radians(angle)
            cutout_points.append(center + QPointF(math.cos(radians) * base * scale, math.sin(radians) * base * scale))
        path = QPainterPath()
        first_mid = (cutout_points[-1] + cutout_points[0]) * 0.5
        path.moveTo(first_mid)
        for index, point in enumerate(cutout_points):
            next_point = cutout_points[(index + 1) % len(cutout_points)]
            midpoint = (point + next_point) * 0.5
            path.quadTo(point, midpoint)
        path.closeSubpath()

        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 0))
        painter.drawPath(path)
        painter.restore()

        painter.save()
        mist = QRadialGradient(center, base * 1.15)
        mist.setColorAt(0.0, QColor(255, 252, 232, 30))
        mist.setColorAt(1.0, QColor(255, 252, 232, 18))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mist)
        painter.drawPath(path)
        painter.restore()

    def _draw_drop_inner_leaf_edges(self, painter: QPainter, panel_rect: QRectF, progress: float) -> None:
        if not self._drop_leaf_renderers:
            return
        progress = max(0.0, min(progress, 1.0))
        center = panel_rect.center()
        base = panel_rect.width() * (0.11 + 0.075 * progress)
        placements = [
            (3, -162, 0.92, 0.15, -7, 0.76),
            (4, -116, 1.03, 0.18, 8, 0.70),
            (3, -63, 0.94, 0.14, -8, 0.76),
            (4, -10, 1.02, 0.17, 7, 0.68),
            (3, 42, 0.93, 0.14, -7, 0.74),
            (4, 95, 1.03, 0.17, 8, 0.68),
            (3, 148, 0.94, 0.14, -8, 0.74),
        ]
        painter.save()
        clip = QPainterPath()
        clip.addEllipse(panel_rect.adjusted(-1, -1, 1, 1))
        painter.setClipPath(clip)
        leaf_band = QPainterPath()
        outer_radius = base * 1.45
        inner_radius = base * 0.98
        leaf_band.addEllipse(QRectF(center.x() - outer_radius, center.y() - outer_radius, outer_radius * 2, outer_radius * 2))
        inner_hole = QPainterPath()
        inner_hole.addEllipse(QRectF(center.x() - inner_radius, center.y() - inner_radius, inner_radius * 2, inner_radius * 2))
        painter.setClipPath(leaf_band.subtracted(inner_hole), Qt.ClipOperation.IntersectClip)
        for leaf_index, angle, radius_scale, width_scale, rotation_offset, opacity in placements:
            if leaf_index >= len(self._drop_leaf_renderers):
                continue
            renderer = self._drop_leaf_renderers[leaf_index]
            svg_size = renderer.defaultSize()
            if svg_size.width() <= 0:
                continue
            radians = math.radians(angle)
            draw_center = center + QPointF(math.cos(radians) * base * radius_scale, math.sin(radians) * base * radius_scale)
            draw_width = min(self.width(), self.height()) * width_scale
            draw_height = draw_width * svg_size.height() / svg_size.width()
            painter.save()
            painter.setOpacity(opacity * (0.18 + 0.82 * progress))
            painter.translate(draw_center)
            painter.rotate(angle - 90 + rotation_offset)
            renderer.render(painter, QRectF(-draw_width * 0.5, -draw_height * 0.5, draw_width, draw_height))
            painter.restore()
        painter.restore()

    def _tinted_haypile_pixmap(self, color: QColor) -> QPixmap:
        if self.haypile_icon.isNull():
            return QPixmap()
        tinted = QPixmap(self.haypile_icon.size())
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(tinted.rect(), self.haypile_icon)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), color)
        painter.end()
        return tinted

    def _draw_haypile_aura(
        self,
        painter: QPainter,
        rect: QRectF,
        pulse: float,
        *,
        active: bool,
    ) -> None:
        if not active or self._haypile_glow_pixmap.isNull():
            return
        if self._exit_armed:
            glow_pixmap = self._haypile_exit_glow_pixmap
            intensity = 1.0
        elif self._drag_hover:
            glow_pixmap = self._haypile_glow_pixmap
            intensity = 1.0
        elif self._external_drag_candidate:
            glow_pixmap = self._haypile_glow_pixmap
            intensity = 0.74
        elif self._hovered:
            glow_pixmap = self._haypile_glow_pixmap
            intensity = 0.88
        else:
            glow_pixmap = self._haypile_glow_pixmap
            intensity = 0.44

        inherited_opacity = painter.opacity()
        painter.save()
        painter.setClipRect(
            QRectF(
                rect.left() - 8,
                rect.top() - 8,
                rect.width() + 16,
                rect.height() * 0.90 + 8,
            )
        )
        source = QRectF(glow_pixmap.rect())
        breath = 0.96 + pulse * 0.04
        for expansion, opacity in ((0.030, 0.44), (0.075, 0.25), (0.130, 0.13)):
            width = rect.width() * (1.0 + expansion)
            height = rect.height() * (1.0 + expansion)
            target = QRectF(
                rect.center().x() - width / 2,
                rect.bottom() - height,
                width,
                height,
            )
            painter.setOpacity(inherited_opacity * opacity * intensity * breath)
            painter.drawPixmap(target, glow_pixmap, source)
        painter.restore()

    def _draw_directional_haypile_aura(
        self,
        painter: QPainter,
        rect: QRectF,
        pulse: float,
    ) -> None:
        intensity = self._drag_awareness_intensity()
        if intensity <= 0.0 or not self._drag_awareness_has_direction or self._haypile_direction_glow_pixmap.isNull():
            return

        edge = self._haypile_edge_point(rect, self._drag_awareness_angle)
        direction = QPointF(
            math.cos(self._drag_awareness_angle),
            math.sin(self._drag_awareness_angle),
        )
        width = max(1, math.ceil(rect.width()))
        height = max(1, math.ceil(rect.height()))
        local_rect = QRectF(0, 0, width, height)
        center = QPointF(width / 2, height * 0.58)
        local_edge = QPointF(edge.x() - rect.left(), edge.y() - rect.top())
        gradient = QLinearGradient(
            center - direction * (width * 0.28),
            local_edge,
        )
        gradient.setColorAt(0.0, QColor(255, 255, 255, 0))
        gradient.setColorAt(0.48, QColor(255, 255, 255, 36))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 255))

        highlight = QPixmap(width, height)
        highlight.fill(Qt.GlobalColor.transparent)
        highlight_painter = QPainter(highlight)
        highlight_painter.drawPixmap(
            local_rect,
            self._haypile_direction_glow_pixmap,
            QRectF(self._haypile_direction_glow_pixmap.rect()),
        )
        highlight_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        highlight_painter.fillRect(local_rect, gradient)
        highlight_painter.end()

        inherited_opacity = painter.opacity()
        breath = 0.96 + pulse * 0.04
        painter.save()
        painter.setOpacity(inherited_opacity * 0.62 * intensity * breath)
        painter.drawPixmap(rect, highlight, QRectF(highlight.rect()))
        painter.restore()

    def _drag_awareness_intensity(self) -> float:
        if not (self._external_drag_candidate or self._drag_hover):
            return 0.0
        if self._drag_hover:
            intensity = 1.0
        else:
            proximity = 1.0 - min(self._drag_awareness_distance / 420.0, 1.0)
            intensity = 0.40 + proximity * 0.60
        return intensity * max(0.0, 1.0 - self._drop_open_progress * 2.0)

    def _haypile_edge_point(self, rect: QRectF, angle: float) -> QPointF:
        direction = QPointF(math.cos(angle), math.sin(angle))
        center = QPointF(rect.center().x(), rect.top() + rect.height() * 0.58)
        if self._haypile_alpha_image.isNull() or rect.width() <= 0 or rect.height() <= 0:
            return center + direction * (min(rect.width(), rect.height()) * 0.42)

        last_opaque = center
        max_distance = math.hypot(rect.width(), rect.height())
        image_width = self._haypile_alpha_image.width()
        image_height = self._haypile_alpha_image.height()
        for step in range(1, 97):
            distance = max_distance * step / 96.0
            point = center + direction * distance
            unit_x = (point.x() - rect.left()) / rect.width()
            unit_y = (point.y() - rect.top()) / rect.height()
            if not (0.0 <= unit_x < 1.0 and 0.0 <= unit_y < 1.0):
                break
            source_x = min(int(unit_x * image_width), image_width - 1)
            source_y = min(int(unit_y * image_height), image_height - 1)
            if self._haypile_alpha_image.pixelColor(source_x, source_y).alpha() >= 20:
                last_opaque = point
        return last_opaque + direction * 1.2

    def _draw_haypile_icon(self, painter: QPainter, rect: QRectF, *, active: bool) -> None:
        painter.save()
        if not self.haypile_icon.isNull():
            bend = self._drag_bend_values() if self._window_drag_active else (0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0)
            painter.setBrush(QColor(0, 0, 0, 26))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(
                QRectF(
                    rect.left() + rect.width() * 0.12,
                    rect.top() + rect.height() * 0.86,
                    rect.width() * 0.76,
                    rect.height() * 0.09,
                )
            )
            self._draw_drag_trails(painter, rect, bend)
            self._draw_bent_haypile_pixmap(painter, rect, bend)
            painter.restore()
            return

        left = rect.left()
        top = rect.top()
        width = rect.width()
        height = rect.height()

        def point(x: float, y: float) -> QPointF:
            return QPointF(left + x / 64.0 * width, top + y / 64.0 * height)

        painter.setBrush(QColor(0, 0, 0, 26))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(left + width * 0.12, top + height * 0.84, width * 0.76, height * 0.11))

        body = QPolygonF(
            [
                point(8, 58), point(7, 51), point(9, 42), point(12, 32),
                point(17, 23), point(23, 14), point(29, 7), point(35, 7),
                point(42, 15), point(48, 25), point(53, 37), point(57, 51),
                point(55, 58), point(44, 59), point(31, 58), point(18, 59),
            ]
        )
        fill = QRadialGradient(point(33, 33), width * 0.58)
        fill.setColorAt(0.0, QColor("#FFD45A" if active else "#F4C13D"))
        fill.setColorAt(0.72, QColor("#E5A626"))
        fill.setColorAt(1.0, QColor("#B96E1B"))
        painter.setBrush(fill)
        painter.setPen(QPen(QColor("#9B5B18"), 1.0))
        painter.drawPolygon(body)

        edge_pen = QPen(QColor("#F1B235" if active else "#D99225"), 1.35 if not active else 1.7)
        edge_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        edge_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(edge_pen)
        for start, c1, c2, end in [
            ((19, 18), (16, -2), (41, 3), (34, 18)),
            ((29, 14), (39, -3), (55, 11), (42, 23)),
            ((8, 47), (-1, 42), (7, 35), (17, 38)),
            ((10, 36), (3, 29), (12, 23), (21, 28)),
            ((17, 22), (14, 7), (29, 16), (27, 25)),
            ((30, 11), (29, -2), (43, 8), (39, 20)),
            ((40, 19), (53, 9), (55, 23), (46, 30)),
            ((51, 32), (65, 32), (59, 45), (49, 42)),
            ((10, 45), (-8, 47), (2, 28), (20, 35)),
            ((50, 37), (69, 40), (62, 21), (45, 28)),
            ((7, 56), (19, 64), (30, 55), (41, 61)),
            ((22, 53), (30, 58), (40, 52), (55, 56)),
        ]:
            path = QPainterPath(point(*start))
            path.cubicTo(point(*c1), point(*c2), point(*end))
            painter.drawPath(path)

        painter.setPen(QPen(QColor("#8A521D"), 1.05))
        for sx, sy, ex, ey in [
            (13, 48, 25, 43), (17, 38, 28, 31), (31, 44, 44, 35),
            (22, 55, 48, 53), (21, 25, 34, 19),
        ]:
            painter.drawLine(point(sx, sy), point(ex, ey))

        painter.setPen(QPen(QColor(41, 28, 13, 155), 1.2))
        for sx, sy, ex, ey in [
            (18, 36, 22, 34), (27, 23, 31, 20), (43, 31, 47, 29),
            (18, 49, 23, 47), (40, 49, 45, 50),
        ]:
            painter.drawLine(point(sx, sy), point(ex, ey))
        painter.restore()

    def _draw_bent_haypile_pixmap(
        self,
        painter: QPainter,
        rect: QRectF,
        bend: tuple[float, float, float, float, float, float, float],
    ) -> None:
        _vx, _vy, drag, rotation, shear_x, scale_x, scale_y = bend
        if drag < 0.02:
            painter.drawPixmap(rect, self.haypile_icon, QRectF(self.haypile_icon.rect()))
            return
        center = rect.center()
        target = QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height())
        painter.save()
        painter.translate(center)
        painter.rotate(rotation)
        painter.shear(shear_x, 0.0)
        painter.scale(scale_x, scale_y)
        painter.drawPixmap(target, self.haypile_icon, QRectF(self.haypile_icon.rect()))
        painter.restore()

    def _draw_drag_trails(
        self,
        painter: QPainter,
        rect: QRectF,
        bend: tuple[float, float, float, float, float, float, float],
    ) -> None:
        vx, vy, drag, _rotation, _shear_x, _scale_x, _scale_y = bend
        if drag < 0.10:
            return
        # ponytail: two faint ghost draws are cheaper and calmer than particle grass.
        source = QRectF(self.haypile_icon.rect())
        painter.save()
        for index, opacity in ((2, 0.045), (1, 0.075)):
            painter.setOpacity(opacity * drag)
            painter.drawPixmap(rect.translated(-vx * 5.0 * index, -vy * 3.5 * index), self.haypile_icon, source)
        painter.restore()

    def _drag_bend_values(self) -> tuple[float, float, float, float, float, float, float]:
        vx = max(-1.0, min(1.0, self._drag_velocity.x() / 760.0))
        vy = max(-1.0, min(1.0, self._drag_velocity.y() / 760.0))
        drag = min(1.0, abs(vx) + abs(vy) * 0.65)
        if drag < 0.02:
            return 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0
        rotation = max(-7.0, min(7.0, -vx * 5.8))
        shear_x = vx * 0.075
        vertical = abs(vy)
        scale_y = 1.0 + (0.045 * vertical if vy < 0 else -0.035 * vertical)
        scale_x = 1.0 - (scale_y - 1.0) * 0.42
        return vx, vy, drag, rotation, shear_x, scale_x, scale_y

    def _dragged_icon_rect(self, rect: QRectF) -> QRectF:
        vx = max(-1.0, min(1.0, self._drag_velocity.x() / 760.0))
        vy = max(-1.0, min(1.0, self._drag_velocity.y() / 760.0))
        if abs(vx) < 0.02 and abs(vy) < 0.02:
            return rect
        drag = min(1.0, abs(vx) + abs(vy) * 0.65)
        center = rect.center() + QPointF(-vx * 2.6, -vy * 1.8)
        width = rect.width() * (1.0 + drag * 0.035)
        height = rect.height() * (1.0 - drag * 0.028)
        return QRectF(center.x() - width / 2, center.y() - height / 2, width, height)

    def _advance_visual_state(self) -> None:
        if self._closing:
            return
        if not self._visual_state_active():
            self._visual_timer.stop()
            self.update()
            return
        self._pulse_phase = (self._pulse_phase + 0.18) % (math.pi * 2)
        if self._drag_awareness_has_direction and (self._external_drag_candidate or self._drag_hover):
            delta = (
                self._drag_awareness_target_angle
                - self._drag_awareness_angle
                + math.pi
            ) % (math.pi * 2) - math.pi
            self._drag_awareness_angle += delta * 0.35
        self.update()

    def _visual_state_active(self) -> bool:
        busy = bool(
            (self.worker is not None and self.worker.isRunning())
            or (self.ai_batch_worker is not None and self.ai_batch_worker.isRunning())
        )
        if self.low_power_enabled:
            return (
                self._drag_hover
                or self._drop_open_progress > 0.0
                or self._exit_armed
                or busy
                or self._drop_feedback_active()
                or self._bounce_feedback_active()
                or self._nudge_feedback_active()
                or self._reject_feedback_active()
                or self._window_drag_active
                or self._drag_release_feedback_active()
            )
        return (
            self._hovered
            or self._external_drag_candidate
            or self._drag_hover
            or self._drop_open_progress > 0.0
            or self._exit_armed
            or busy
            or self._drop_feedback_active()
            or self._bounce_feedback_active()
            or self._nudge_feedback_active()
            or self._reject_feedback_active()
            or self._window_drag_active
            or self._drag_release_feedback_active()
        )

    def _sync_visual_timer(self) -> None:
        if self._closing:
            return
        if self._visual_state_active():
            if not self._visual_timer.isActive():
                self._visual_timer.start()
        elif self._visual_timer.isActive():
            self._visual_timer.stop()

    def _drop_feedback_active(self) -> bool:
        return time.monotonic() < self._drop_feedback_until

    def _bounce_feedback_active(self) -> bool:
        return time.monotonic() < self._bounce_feedback_until

    def _nudge_feedback_active(self) -> bool:
        return time.monotonic() < self._nudge_feedback_until

    def _reject_feedback_active(self) -> bool:
        return time.monotonic() < self._reject_feedback_until

    def _drag_release_feedback_active(self) -> bool:
        return time.monotonic() < self._drag_release_feedback_until

    def _sample_drag_velocity(self, global_pos: QPoint) -> None:
        now = time.monotonic()
        elapsed = max(now - self._last_drag_sample_at, 0.016)
        delta = global_pos - self._last_drag_global_pos
        # ponytail: tiny low-pass velocity, enough for drag feel without a physics loop.
        vx = max(-900.0, min(900.0, delta.x() / elapsed))
        vy = max(-900.0, min(900.0, delta.y() / elapsed))
        self._drag_velocity = QPointF(self._drag_velocity.x() * 0.55 + vx * 0.45, self._drag_velocity.y() * 0.55 + vy * 0.45)
        self._last_drag_global_pos = QPoint(global_pos)
        self._last_drag_sample_at = now

    def _start_drag_release_feedback(self) -> None:
        now = time.monotonic()
        self._drag_release_feedback_started_at = now
        self._drag_release_feedback_until = now + 0.26

    def _drag_release_icon_rect(self, rect: QRectF) -> QRectF:
        rect = rect.adjusted(0, 2, 0, -1)
        duration = max(self._drag_release_feedback_until - self._drag_release_feedback_started_at, 0.001)
        progress = max(0.0, min((time.monotonic() - self._drag_release_feedback_started_at) / duration, 1.0))
        if progress < 0.31:
            ease = 1 - (1 - progress / 0.31) ** 3
            scale_x, scale_y, bottom_lift = 1 + 0.10 * ease, 1 - 0.15 * ease, 0.0
        elif progress < 0.65:
            ease = 1 - (1 - (progress - 0.31) / 0.34) ** 3
            scale_x, scale_y, bottom_lift = 1.10 - 0.10 * ease, 0.85 + 0.14 * ease, 1.0 * ease
        else:
            ease = 1 - (1 - (progress - 0.65) / 0.35) ** 3
            scale_x, scale_y, bottom_lift = 1.0, 0.99 + 0.01 * ease, 1.0 * (1 - ease)
        width = rect.width() * scale_x
        height = rect.height() * scale_y
        bottom = rect.bottom() - bottom_lift
        return QRectF(
            rect.center().x() - width / 2,
            bottom - height,
            width,
            height,
        )

    @staticmethod
    def _is_duplicate_only_result(message: str) -> bool:
        lowered = message.lower()
        return ("新增 0" in message and "去重 " in message) or ("0 new" in lowered and "duplicate" in lowered)

    def _refresh_pending_badge(self) -> None:
        try:
            self._has_pending_assets = build_material_panel_summary().pending_count > 0
        except Exception:
            logger.debug("Failed to refresh Haypile pending badge")
            self._has_pending_assets = False
        if not self._has_pending_assets and self.quick_menu._attention_action == "status":
            self.quick_menu.set_attention_action("")
        self.update()

    def _busy_breath_icon_rect(self, rect: QRectF, pulse: float) -> QRectF:
        scale = 1.0 + 0.018 * pulse
        center = rect.center()
        width = rect.width() * scale
        height = rect.height() * scale
        return QRectF(center.x() - width / 2, center.y() - height / 2, width, height)

    def _bounced_icon_rect(self, rect: QRectF) -> QRectF:
        duration = max(self._bounce_feedback_until - self._bounce_feedback_started_at, 0.001)
        progress = max(0.0, min((time.monotonic() - self._bounce_feedback_started_at) / duration, 1.0))
        if progress < 0.22:
            ease = 1 - (1 - progress / 0.22) ** 3
            scale_x, scale_y, offset_y = 1 + 0.10 * ease, 1 - 0.16 * ease, 6 * ease
        elif progress < 0.52:
            ease = 1 - (1 - (progress - 0.22) / 0.30) ** 3
            scale_x, scale_y, offset_y = 1.10 - 0.14 * ease, 0.84 + 0.24 * ease, 6 - 10 * ease
        elif progress < 0.74:
            ease = 1 - (1 - (progress - 0.52) / 0.22) ** 3
            scale_x, scale_y, offset_y = 0.96 + 0.05 * ease, 1.08 - 0.07 * ease, -4 + 3 * ease
        else:
            ease = 1 - (1 - (progress - 0.74) / 0.26) ** 3
            scale_x, scale_y, offset_y = 1.01 - 0.01 * ease, 1.01 - 0.01 * ease, -1 + ease
        center = rect.center() + QPointF(0, offset_y)
        width = rect.width() * scale_x
        height = rect.height() * scale_y
        bounds = QRectF(0, 0, self.width(), self.height()).adjusted(1, 1, -1, -1)
        width = min(width, bounds.width())
        height = min(height, bounds.height())
        bounced = QRectF(center.x() - width / 2, center.y() - height / 2, width, height)
        if bounced.left() < bounds.left():
            bounced.moveLeft(bounds.left())
        if bounced.right() > bounds.right():
            bounced.moveRight(bounds.right())
        if bounced.top() < bounds.top():
            bounced.moveTop(bounds.top())
        if bounced.bottom() > bounds.bottom():
            bounced.moveBottom(bounds.bottom())
        return bounced

    def _nudged_icon_rect(self, rect: QRectF) -> QRectF:
        duration = max(self._nudge_feedback_until - self._nudge_feedback_started_at, 0.001)
        progress = max(0.0, min((time.monotonic() - self._nudge_feedback_started_at) / duration, 1.0))
        offset_x = math.sin(progress * math.pi * 4) * (1 - progress) * 5
        nudged = QRectF(rect)
        nudged.translate(offset_x, 0)
        bounds = QRectF(0, 0, self.width(), self.height()).adjusted(1, 1, -1, -1)
        if nudged.left() < bounds.left():
            nudged.moveLeft(bounds.left())
        if nudged.right() > bounds.right():
            nudged.moveRight(bounds.right())
        return nudged

    def _rejected_icon_rect(self, rect: QRectF) -> QRectF:
        duration = max(self._reject_feedback_until - self._reject_feedback_started_at, 0.001)
        progress = max(0.0, min((time.monotonic() - self._reject_feedback_started_at) / duration, 1.0))
        ease = math.sin(progress * math.pi)
        scale = 1 - 0.08 * ease
        offset_y = 3 * ease
        center = rect.center() + QPointF(0, offset_y)
        width = rect.width() * scale
        height = rect.height() * scale
        return QRectF(center.x() - width / 2, center.y() - height / 2, width, height)

    def _draw_pending_badge(self, painter: QPainter, outer_rect: QRectF) -> None:
        center = QPointF(outer_rect.right() - 8, outer_rect.top() + 11)
        glow = QRadialGradient(center, 8)
        glow.setColorAt(0.0, QColor(233, 182, 54, 170))
        glow.setColorAt(1.0, QColor(233, 182, 54, 0))
        painter.setBrush(glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(center.x() - 8, center.y() - 8, 16, 16))
        painter.setBrush(QColor(198, 139, 36, 235))
        painter.setPen(QPen(QColor(255, 250, 232, 210), 1.1))
        painter.drawEllipse(QRectF(center.x() - 3.4, center.y() - 3.4, 6.8, 6.8))

    def _update_window_mask(self) -> None:
        # Keep the window unmasked: QRegion uses binary clipping and makes the
        # circular edge visibly jagged on macOS. The paint path already clears
        # the transparent corners and avoids external black shadows.
        self.clearMask()

    @staticmethod
    def _kill_process_tree(pid: int) -> None:
        if not sys.platform.startswith("win"):
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as exc:
            logger.debug("Failed to kill process tree pid=%s error_type=%s", pid, type(exc).__name__)
            return

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--backend" in args:
        settings = get_settings()
        configure_packaged_logging("backend", settings.LOG_DIR)
        os.environ["HAYPILE_BACKEND_HOST_ALLOW_START"] = "1"
        from backend_host import main as backend_main

        try:
            return backend_main()
        except KeyboardInterrupt:
            return 0
    if "--mcp" in args:
        from mcp_server import main as mcp_main

        mcp_main()
        return 0

    settings = get_settings()
    configure_packaged_logging("gui", settings.LOG_DIR)
    instance_lock = InterProcessFileLock(settings.INDEX_DIR / "gui.instance.lock")
    if not instance_lock.acquire(timeout=0.1):
        logger.info("Haypile GUI is already running")
        return 0
    app = QApplication([sys.argv[0], *args])
    app.setQuitOnLastWindowClosed(True)
    widget = HaypileFloatingBall()
    app.aboutToQuit.connect(widget.shutdown)
    widget.show()
    try:
        return app.exec()
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
