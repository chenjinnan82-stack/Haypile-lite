from __future__ import annotations

import asyncio
import ctypes
from dataclasses import replace
from html import escape as escape_html
from html.parser import HTMLParser
import json
import locale
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urldefrag, urlparse
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
from app.core.config import (
    APP_VERSION,
    Settings,
    configure_packaged_logging,
    get_settings,
    is_packaged_app,
    runtime_mode_command,
)
from app.core.exceptions import ResourceExhaustedError
from app.core.file_lock import InterProcessFileLock
from app.gui.backend_runtime import BackendRuntimeController
from app.gui.native_drag import execute_copy_drag
from app.gui.desktop_startup import (
    DesktopStartupResult,
    prepare_desktop_runtime,
    read_desktop_gui_state,
)
from app.gui.intake_visual import (
    GIF_EXPAND_SECONDS as INTAKE_GIF_EXPAND_SECONDS,
    GIF_FRAME_STAGGER_SECONDS as INTAKE_GIF_FRAME_STAGGER_SECONDS,
    GIF_SUCTION_SECONDS as INTAKE_GIF_SUCTION_SECONDS,
    GIF_TICK_SECONDS as INTAKE_GIF_TICK_SECONDS,
    IntakeEntryRenderer,
    IntakeVisualState,
)
from app.gui.sound_feedback import SoundFeedbackController
from app.services.asset_provenance import (
    provenance_path_for,
    public_origin_url,
    read_asset_provenance,
    write_asset_provenance,
)
from app.services.handoff import build_asset_handoff, build_handoff_asset
from app.services.asset_collections import read_collections, save_collection, dissolve_collection, set_collection_asset, collection_handoff
from app.services.ai_provider import (
    AIProviderConfig,
    SystemCredentialStore,
    api_authority,
    normalize_api_base_url,
)
from app.services.bundle_service import BundleService
from app.services.desktop_diagnostics import (
    build_desktop_diagnostic_summary,
    read_build_commit,
)
from app.services.ingest_service import (
    IngestCandidate,
    IngestResult,
    IngestService,
)
from app.services.intake_route import (
    ROUTE_EMPTY_GIF,
    ROUTE_LOCAL_FILES,
    ROUTE_LOCAL_FILES_AND_REMOTE_URLS,
    ROUTE_RAW_GIF,
    ROUTE_REMOTE_URL,
    ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK,
    ROUTE_STATIC_PNG,
    ROUTE_UNSUPPORTED,
    resolve_intake_route,
)
from app.services.json_io import atomic_write_json
from app.services.scanner import (
    ManifestReadinessError,
    read_manifest_readiness,
)
from app.services.style_classifier import StyleClassifier
from app.services.material_summary import build_material_panel_summary
from app.services.media_validator import (
    MAX_GIF_BYTES,
    MAX_RASTER_DIMENSION,
    MAX_RASTER_PIXELS,
    MIN_GIF_FRAME_DELAY_MS,
)
from app.services.media_types import SUPPORTED_AUDIO_EXTENSIONS
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
from app.services.font_service import DEFAULT_FONT_TEXT, export_font_original, render_font_preview
from app.services.web_font_service import MAX_WEB_FONT_BYTES, MAX_WEB_FONTS, WEB_FONT_PREVIEW_TEXT, WebFontCandidate, download_web_fonts, read_web_text
from app.services.web_image_service import MAX_WEB_IMAGE_BYTES, MAX_WEB_IMAGES, WebImageCandidate, download_web_images
from PySide6.QtCore import (
    QCoreApplication,
    QSize,
    QEasingCurve,
    QEvent,
    QMimeData,
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
    qVersion,
)
from PySide6.QtGui import (
    QDrag,
    QImage,
    QIcon,
    QColor,
    QCursor,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QLinearGradient,
    QMouseEvent,
    QMovie,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPolygonF,
    QPixmap,
    QImageReader,
    QRadialGradient,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QMenu,
    QFileDialog,
    QFormLayout,
    QButtonGroup,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_UI_MOSS = "#4E5F3D"
_UI_GOLD_TEXT = "#A67624"
_UI_ERROR = "#9B4C37"

_CHOICE_BUTTON_STYLE = (
    "QPushButton { color: #4E5F3D; background: #F6F1E4; "
    "border: 1px solid #DDD3BB; border-radius: 6px; padding: 2px; }"
    "QPushButton:hover { background: #EFE3C7; color: #4E5F3D; }"
    "QPushButton:checked { color: #FFF9EA; background: #6F7F5A; border: 2px solid #4E5F3D; }"
    "QPushButton:focus { border: 2px solid #C8A24A; }"
    "QPushButton:disabled { color: #A9A08A; background: #F6F1E4; }"
)

_ITEM_BUTTON_STYLE = (
    "QPushButton { color: #444444; text-align: left; "
    "padding: 7px 8px; background: #F6F1E4; "
    "border: 1px solid #DDD3BB; border-radius: 8px; }"
    "QPushButton:hover { background: #EFE3C7; }"
    "QPushButton:checked { color: #2F3A26; font-weight: bold; "
    "background: #FFF2C4; border: 2px solid #C8A24A; }"
    "QPushButton:focus { border: 2px solid #C8A24A; }"
)

_LINE_EDIT_STYLE = (
    "QLineEdit { color: #4A463A; background: #FFF9EA; "
    "border: 1px solid #E5D8B9; border-radius: 7px; padding: 0 8px; }"
    "QLineEdit:focus { border: 2px solid #C8A24A; }"
)

_ACTION_BUTTON_STYLE = (
    "QPushButton { text-align: left; padding: 0 10px; color: #4E5F3D; "
    "background: #F6F1E4; border: 1px solid #DDD3BB; border-radius: 7px; }"
    "QPushButton:hover { background: #EFE3C7; }"
    "QPushButton:checked { color: #FFF9EA; background: #6F7F5A; border: 2px solid #4E5F3D; }"
    "QPushButton:focus { border: 2px solid #C8A24A; }"
)

_FEEDBACK_TONES = {
    "progress": (_UI_GOLD_TEXT, "#F6F1E4"),
    "success": (_UI_MOSS, "#F3EDDA"),
    "pending": (_UI_GOLD_TEXT, "#FFF2C4"),
    "duplicate": ("#625B4C", "#E9E4D4"),
    "error": (_UI_ERROR, "#F4E2DC"),
}


def _configure_choice_button(button: QPushButton, group: QButtonGroup) -> None:
    button.setCheckable(True)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    button.setMinimumHeight(26)
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    button.setStyleSheet(_CHOICE_BUTTON_STYLE)
    group.addButton(button)


class _MaterialItemButton(QPushButton):
    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


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


def _plain_tooltip(text: str) -> str:
    return "<qt>" + escape_html(text).replace("\n", "<br>") + "</qt>"


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
        self.link_urls: list[str] = []
        self._audio_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href" and value), "")
            if href:
                self.link_urls.append(href.strip())
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

    MAX_FILE_SIZE_BYTES = IngestService.DEFAULT_MAX_FILE_BYTES
    MAX_TOTAL_DOWNLOAD_BYTES = 1024 * 1024 * 1024
    MAX_URLS = MAX_REMOTE_URLS
    TIMEOUT_SECONDS = 15.0
    CONTENT_TYPE_EXTENSIONS = REMOTE_CONTENT_TYPE_EXTENSIONS

    def __init__(self, urls: list[str], incoming_dir: Path) -> None:
        super().__init__()
        self.urls = self._dedupe_urls(urls)
        self.incoming_dir = incoming_dir
        self.font_candidates: list[WebFontCandidate] = []
        self.image_candidates: list[WebImageCandidate] = []
        self.discovery_note = ""

    def run(self) -> None:
        downloaded: list[Path] = []
        failed = False
        result = ([], "", False)
        try:
            result = self._run_downloads(downloaded) or result
        except Exception as exc:
            failed = True
            self.font_candidates.clear()
            self.image_candidates.clear()
            logger.warning("网页素材获取异常 error_type=%s", type(exc).__name__)
            result = ([], ui_text("网页素材无法获取，请重试", "Web assets could not be fetched; retry"), False)
        finally:
            if failed or self.isInterruptionRequested():
                self.font_candidates.clear()
                self.image_candidates.clear()
                for path in downloaded:
                    path.unlink(missing_ok=True)
                    provenance_path_for(path).unlink(missing_ok=True)
            if self.isInterruptionRequested():
                result = ([], "", False)
        self.finished_signal.emit(*result)

    def _run_downloads(self, downloaded: list[Path]) -> tuple[list[Path], str, bool] | None:
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.incoming_dir.chmod(0o700)
        failed = 0
        too_large = 0
        gif_too_large = 0
        font_too_large = 0
        unsupported = 0
        video_not_supported = 0
        downloaded_bytes = 0
        web_font_bytes = 0
        web_image_bytes = 0
        web_pages = 0
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
                if reason == "web_page":
                    web_pages += 1
                    self.progress_signal.emit(40, ui_text("正在查找网页字体和图片…", "Finding page fonts and images…"))
                    document = read_web_text(url, opener=open_safe_remote, timeout=self.TIMEOUT_SECONDS,
                                             should_stop=self.isInterruptionRequested)
                    candidates, truncated, web_failed = download_web_fonts(
                        url, self.incoming_dir, timeout=self.TIMEOUT_SECONDS, document=document,
                        should_stop=self.isInterruptionRequested, opener=open_safe_remote,
                        max_bytes=min(remaining, MAX_WEB_FONT_BYTES - web_font_bytes),
                        max_fonts=MAX_WEB_FONTS - len(self.font_candidates),
                    )
                    downloaded.extend(candidate.path for candidate in candidates)
                    hashes = {candidate.sha256 for candidate in self.font_candidates}
                    for candidate in candidates:
                        size = candidate.path.stat().st_size
                        web_font_bytes += size
                        downloaded_bytes += size
                        if candidate.sha256 in hashes:
                            candidate.path.unlink(missing_ok=True)
                            provenance_path_for(candidate.path).unlink(missing_ok=True)
                            downloaded.remove(candidate.path)
                        else:
                            hashes.add(candidate.sha256)
                            self.font_candidates.append(candidate)
                    images, image_truncated, image_failed = download_web_images(
                        url, self.incoming_dir, timeout=self.TIMEOUT_SECONDS, document=document,
                        should_stop=self.isInterruptionRequested, opener=open_safe_remote,
                        max_bytes=min(self.MAX_TOTAL_DOWNLOAD_BYTES - downloaded_bytes, MAX_WEB_IMAGE_BYTES - web_image_bytes),
                        max_images=MAX_WEB_IMAGES - len(self.image_candidates),
                    )
                    downloaded.extend(candidate.path for candidate in images)
                    hashes = {candidate.sha256 for candidate in self.image_candidates}
                    for candidate in images:
                        size = candidate.path.stat().st_size
                        web_image_bytes += size
                        downloaded_bytes += size
                        try:
                            if candidate.sha256 not in hashes:
                                preview = self._image_preview(candidate)
                                self.image_candidates.append(preview)
                                hashes.add(candidate.sha256)
                                continue
                        except (OSError, ValueError):
                            image_failed += 1
                        candidate.path.unlink(missing_ok=True)
                        provenance_path_for(candidate.path).unlink(missing_ok=True)
                        downloaded.remove(candidate.path)
                    if truncated or image_truncated:
                        self.discovery_note = ui_text("已达到网页扫描上限，显示已找到的素材。", "Page scan limit reached; showing assets found so far.")
                    elif (web_failed or image_failed) and not self.discovery_note:
                        self.discovery_note = ui_text("部分网页素材无法获取，显示成功获取的候选。", "Some page assets could not be fetched; showing available candidates.")
                    continue
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
                elif reason == "gif_too_large":
                    gif_too_large += 1
                elif reason == "font_too_large":
                    font_too_large += 1
                elif reason == "video_not_supported":
                    video_not_supported += 1
                elif reason == "unsupported":
                    unsupported += 1
                else:
                    failed += 1
                continue
            downloaded.append(path)
            downloaded_bytes += path.stat().st_size

        if not downloaded:
            if web_pages:
                message = ui_text("网页中没有找到可获取的字体或图片；可以直接拖入素材链接", "No downloadable fonts or images found; try a direct asset link")
            elif video_not_supported and not failed and not too_large and not gif_too_large:
                message = ui_text(
                    "不支持视频；请拖入真实 GIF 图片",
                    "Video is not supported; drop a true GIF image",
                )
            elif unsupported and not failed and not too_large:
                message = ui_text("没有找到可收纳的图片、音频或字体", "No images, audio, or fonts to store")
            elif gif_too_large and not failed and not too_large:
                message = ui_text("GIF 素材超过 50MB", "GIF asset is over 50MB")
            elif font_too_large and not failed and not too_large:
                message = ui_text("字体素材超过 50MB", "Font asset is over 50MB")
            elif too_large and not failed:
                message = ui_text("网页素材超过 500MB", "Web asset is over 500MB")
            else:
                message = ui_text("网页素材无法下载", "Web asset could not be downloaded")
            return [], message, False
        message = ui_text(f"已获取 {len(downloaded)} 个网页素材", f"Fetched {len(downloaded)} web assets")
        skipped = failed + too_large + gif_too_large + font_too_large + unsupported + video_not_supported
        if skipped:
            message += ui_text(f"，跳过 {skipped}", f", skipped {skipped}")
        self.progress_signal.emit(95, message)
        return downloaded, message, True

    def _download_one(self, url: str, index: int, *, max_bytes: int | None = None) -> tuple[Path | None, str]:
        return download_remote_media(
            url,
            self.incoming_dir,
            index,
            max_bytes=self.MAX_FILE_SIZE_BYTES if max_bytes is None else max_bytes,
            timeout=self.TIMEOUT_SECONDS,
            should_stop=self.isInterruptionRequested,
            opener=open_safe_remote,
            allow_web_page=True,
        )

    @staticmethod
    def _image_preview(candidate: WebImageCandidate) -> WebImageCandidate:
        # QImage is safe in the worker; only the card creates the QPixmap.
        reader = QImageReader(str(candidate.path))
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid():
            size = QSize(candidate.metadata["width"], candidate.metadata["height"])
        reader.setScaledSize(size.scaled(QSize(508, 200), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            raise ValueError("image_preview_failed")
        image = image.scaled(508, 200, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        return replace(candidate, pixels=bytes(image.constBits()), width=image.width(), height=image.height())

    @staticmethod
    def _dedupe_urls(urls: list[str]) -> list[str]:
        return dedupe_remote_urls([urldefrag(url.strip())[0] for url in urls], limit=RemoteDownloadWorker.MAX_URLS)


class IngestWorker(QThread):
    finished_signal = Signal(str, bool)
    progress_signal = Signal(int, str)
    degraded_signal = Signal(str, str, int)
    batch_signal = Signal(str, object)

    MAX_FILE_SIZE_BYTES: int = IngestService.DEFAULT_MAX_FILE_BYTES
    MAX_DROP_FILES: int = IngestService.DEFAULT_MAX_FILES
    MAX_DROP_BYTES: int = IngestService.DEFAULT_MAX_BATCH_BYTES
    MIN_FREE_RESERVE_BYTES: int = IngestService.DEFAULT_RESERVE_BYTES
    HASH_CHUNK_SIZE: int = IngestService.DEFAULT_HASH_CHUNK_SIZE

    def __init__(self, files: list[Path], assets_dir: Path, *, settings: Settings | None = None) -> None:
        super().__init__()
        self.files = files
        self.assets_dir = assets_dir
        self.settings = settings or get_settings()
        self.result: IngestResult | None = None

    def run(self) -> None:
        try:
            self._run_ingest()
        except Exception as exc:
            logger.exception("Unexpected ingest worker failure error_type=%s", type(exc).__name__)
            if not self.isInterruptionRequested():
                self.finished_signal.emit(
                    ui_text("入库意外中断，重启后将自动恢复", "Import stopped unexpectedly; restart to recover"),
                    False,
                )

    def _run_ingest(self) -> None:
        service = IngestService(
            storage_dir=self.settings.STORAGE_DIR,
            assets_dir=self.assets_dir,
            index_dir=self.settings.INDEX_DIR,
            themes_dir=self.settings.THEMES_DIR,
            manifest_path=self.settings.MANIFEST_PATH,
            fallback_theme=self.settings.VISION_FALLBACK_THEME,
            max_file_bytes=self.MAX_FILE_SIZE_BYTES,
            max_files=self.MAX_DROP_FILES,
            max_batch_bytes=self.MAX_DROP_BYTES,
            reserve_bytes=self.MIN_FREE_RESERVE_BYTES,
            hash_chunk_size=self.HASH_CHUNK_SIZE,
        )
        self.result = service.ingest(
            [IngestCandidate(path) for path in self.files],
            should_stop=self.isInterruptionRequested,
            progress=self._on_service_progress,
        )
        if self.result.status == "cancelled":
            return
        if not self.result.success:
            self.finished_signal.emit(self._error_message(self.result.error_code), False)
            return

        self.batch_signal.emit(
            str(self.result.batch_id),
            {
                "accepted_count": self.result.accepted_count,
                "duplicate_count": self.result.duplicate_count,
                "rejected_count": self.result.rejected_count,
            },
        )
        message = ui_text(
            f"收纳完成：新增 {self.result.accepted_count}，去重 {self.result.duplicate_count}",
            f"Stored: {self.result.accepted_count} new, {self.result.duplicate_count} duplicate",
        )
        if self.result.renamed_count:
            message += ui_text(
                f"，重命名 {self.result.renamed_count}",
                f", renamed {self.result.renamed_count}",
            )
        if self.result.rejected_count:
            message += ui_text(
                f"，拦截 {self.result.rejected_count}",
                f", blocked {self.result.rejected_count}",
            )
        if self.result.recovered_theme_count:
            message += ui_text(
                "；已隔离损坏的主题记录",
                "; a damaged theme record was quarantined",
            )
        self.finished_signal.emit(message, True)

    def _on_service_progress(self, stage: str, data: dict[str, int]) -> None:
        index, total = data["index"], data["total"]
        messages = {
            "build_hash_index": ui_text(
                "正在构建去重索引...", "Building duplicate index..."
            ),
            "validate": ui_text(
                f"校验文件 {index}/{total}", f"Checking file {index}/{total}"
            ),
            "stage": ui_text(
                f"写入暂存区 {index}/{total}", f"Staging file {index}/{total}"
            ),
            "commit": ui_text(
                f"提交资产 {index}/{total}", f"Committing asset {index}/{total}"
            ),
            "item_complete": ui_text(
                f"完成 {index}/{total}", f"Completed {index}/{total}"
            ),
            "project_manifest": ui_text(
                "刷新资产清单...", "Refreshing asset manifest..."
            ),
            "complete": ui_text("入库完成", "Import complete"),
        }
        self.progress_signal.emit(data["percent"], messages.get(stage, stage))

    def _error_message(self, code: str | None) -> str:
        messages = {
            "ingest_busy": ui_text(
                "另一个入口正在收纳，请稍后重试",
                "Another import is in progress; try again shortly",
            ),
            "batch_file_limit": ui_text(
                f"单次最多收纳 {self.MAX_DROP_FILES} 个文件",
                f"A single drop can contain at most {self.MAX_DROP_FILES} files",
            ),
            "batch_byte_limit": ui_text(
                "单次收纳总量不能超过 2GB",
                "A single drop cannot exceed 2GB",
            ),
            "storage_space_unavailable": ui_text(
                "无法确认素材库剩余空间",
                "Could not check asset storage space",
            ),
            "storage_space_low": ui_text(
                "素材库空间不足，请至少保留 256MB 余量",
                "Not enough storage space; keep at least 256MB free",
            ),
            "storage_recovery_failed": ui_text(
                "素材库恢复失败，未开始入库",
                "Storage recovery failed; import did not start",
            ),
            "durable_commit_failed": ui_text(
                "素材提交中断，重启后将自动恢复",
                "Asset commit interrupted; restart to recover",
            ),
            "ingest_journal_failed": ui_text(
                "素材未提交，请重试",
                "Asset was not committed; please retry",
            ),
            "manifest_projection_failed": ui_text(
                "素材已保存，Agent 接口待恢复",
                "Assets saved; Agent access is pending recovery",
            ),
            "video_not_supported": ui_text(
                "不支持视频；请拖入真实 GIF 图片",
                "Video is not supported; drop a true GIF image",
            ),
            "unsupported_media": ui_text(
                "文件被拦截：只支持安全的图片、音频或字体，或素材超过允许的体积",
                "Blocked: only safe images, audio, or fonts are supported, or an asset exceeds its size limit",
            ),
        }
        return messages.get(
            code,
            ui_text(
                "入库意外中断，重启后将自动恢复",
                "Import stopped unexpectedly; restart to recover",
            ),
        )


async def _classify_registered_bundle(
    bundle: dict[str, object],
    assets_dir: Path,
    classifier: StyleClassifier,
    theme_candidates: list[str],
    bundle_service: BundleService,
):
    source_key = str(bundle.get("source_key") or "").strip()
    if (
        str(bundle.get("type") or "").lower() != "image"
        or str(bundle.get("content_type") or "").lower() == "image/gif"
        or not source_key
    ):
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
    if str(bundle.get("content_type") or "").lower() == "image/gif":
        return
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
        existing = provenance.get("ai_suggestions")
        if isinstance(existing, dict) and existing.get("source") == "model":
            return
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
        if self.isInterruptionRequested():
            return
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
        if self.isInterruptionRequested():
            return
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
        self.bundles = [
            dict(bundle)
            for bundle in bundles
            if str(bundle.get("content_type") or "").lower() != "image/gif"
        ]
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


class FontPreviewWorker(QThread):
    def __init__(self, request: tuple, parent: QWidget, *, preview_width: int = 720) -> None:
        super().__init__(parent)
        self.request = request
        self.preview_width = preview_width
        self.result: tuple[bytes, int, int, str] | None = None
        self.error = False

    def run(self) -> None:
        _generation, _bundle_id, path, text, size, sha256 = self.request
        try:
            if self.isInterruptionRequested():
                return
            if BundleService._sha256(path) != sha256:
                raise ValueError("font_hash_mismatch")
            canvas, missing = render_font_preview(path, text, size, width=self.preview_width)
            with canvas:
                if not self.isInterruptionRequested():
                    self.result = (canvas.tobytes(), canvas.width, canvas.height, missing)
        except Exception:
            self.error = True


class MaterialPanelWindow(QWidget):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        embedded: bool = False,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings or get_settings()
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
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self._collection_id = ""
        self._collections = []
        self._collections_ready = True
        self._collection_seed_id = ""
        self._collection_edit_id = ""
        self._collection_editor_action = "create"
        self._selected_bundle_id = ""
        self._bundle_by_source_key: dict[str, dict[str, object]] = {}
        self._suggested_ai_role = ""
        self._toast_callback = None
        self._theme_recovery_notice_pending = False
        self._retry_batch_callback = None
        self._ai_provider_factory = None
        self._ai_enabled_callback = None
        self._low_power_enabled = False
        self._gif_movie: QMovie | None = None
        self._gif_frame_count = 0
        self._gif_stop_timer = QTimer(self)
        self._gif_stop_timer.setSingleShot(True)
        self._gif_stop_timer.timeout.connect(self._finish_gif_preview)
        self.ai_refresh_worker: AIRefreshWorker | None = None
        self.font_preview_worker: FontPreviewWorker | None = None
        self._font_preview_generation = 0
        self._font_preview_request: tuple | None = None
        self._font_preview_closing = False
        self._font_close_pending = False
        self._font_preview_timer = QTimer(self)
        self._font_preview_timer.setSingleShot(True)
        self._font_preview_timer.setInterval(180)
        self._font_preview_timer.timeout.connect(self._queue_font_preview)
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
            ui_text("0 个素材 · 可用 0\n待确认 0", "0 assets · Ready 0\nPending 0")
            if self._embedded
            else ui_text("0 个 bundle · 可用 0 · 待确认 0", "0 bundles · Ready 0 · Pending 0")
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
        self.scope_button_group = QButtonGroup(self)
        self.scope_buttons: dict[str, QPushButton] = {}
        for scope, label in (
            ("latest", ui_text("最新批次", "Latest batch")),
            ("all", ui_text("全部素材", "All assets")),
        ):
            button = QPushButton(label, self.scope_row)
            _configure_choice_button(button, self.scope_button_group)
            button.clicked.connect(lambda _checked=False, selected_scope=scope: self.set_batch_scope(selected_scope))
            self.scope_buttons[scope] = button
            scope_layout.addWidget(button)
        self.retry_batch_button = QPushButton(ui_text("重试整理", "Retry sorting"), self.scope_row)
        self.retry_batch_button.setMinimumHeight(26)
        self.retry_batch_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.retry_batch_button.clicked.connect(self._retry_latest_batch)
        scope_layout.addWidget(self.retry_batch_button)
        self.scope_row.setVisible(self._embedded)
        self._refresh_scope_buttons()

        self.collection_picker = QComboBox(self.scope_row)
        self.collection_picker.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.collection_picker.setMinimumHeight(26)
        self.collection_picker.setMinimumWidth(100)
        self.collection_picker.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.collection_picker.setStyleSheet(
            "QComboBox { color: #4E5F3D; background: #FFF9EA; border: 1px solid #DDD3BB; "
            "border-radius: 6px; padding: 2px 6px; font-size: 11px; }"
            "QComboBox:focus { border: 2px solid #C8A24A; }"
            "QComboBox QAbstractItemView { color: #443F33; background: #FFFDF5; "
            "selection-color: #4E5F3D; selection-background-color: #E9ECDD; }"
        )
        self.collection_picker.setAccessibleName(ui_text("查看素材组合", "View asset group"))
        self.collection_picker.currentIndexChanged.connect(self._select_collection)
        self.new_collection_button = QPushButton(ui_text("管理组合", "Manage groups"), self.scope_row)
        self.new_collection_button.setMinimumHeight(26)
        self.new_collection_button.clicked.connect(self._manage_collections)
        scope_layout.insertWidget(2, self.collection_picker, 1)
        scope_layout.insertWidget(3, self.new_collection_button)

        self.collection_editor = QWidget(self.container)
        editor_layout = QVBoxLayout(self.collection_editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_row = QHBoxLayout()
        editor_layout.addLayout(editor_row)
        self.collection_name = QLineEdit(self.collection_editor)
        self.collection_name.setMaxLength(64)
        self.collection_name.setAccessibleName(ui_text("组合名称", "Group name"))
        self.collection_name.setPlaceholderText(ui_text("例如：个人网站", "e.g. Personal website"))
        self.collection_name.setStyleSheet(_LINE_EDIT_STYLE)
        self.collection_name.returnPressed.connect(self._save_collection)
        self.collection_name.installEventFilter(self)
        self.collection_save = QPushButton(ui_text("创建", "Create"), self.collection_editor)
        self.collection_save.clicked.connect(self._save_collection)
        self.collection_cancel = QPushButton(ui_text("取消", "Cancel"), self.collection_editor)
        self.collection_cancel.clicked.connect(self._cancel_collection)
        self.collection_save.installEventFilter(self)
        self.collection_cancel.installEventFilter(self)
        editor_row.addWidget(self.collection_name, 1)
        editor_row.addWidget(self.collection_save)
        editor_row.addWidget(self.collection_cancel)
        self.collection_notice = QLabel(self.collection_editor)
        self.collection_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.collection_notice.setWordWrap(True)
        self.collection_notice.setStyleSheet("QLabel { color: #665229; background: transparent; border: none; font-size: 11px; }")
        self.collection_notice.hide()
        self.collection_name.textEdited.connect(self.collection_notice.hide)
        editor_layout.addWidget(self.collection_notice)
        self.collection_editor.hide()

        self.collection_actions = QWidget(self.container)
        actions_layout = QHBoxLayout(self.collection_actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        self.collection_summary = QLabel(self.collection_actions)
        self.collection_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.collection_copy = QPushButton(ui_text("复制这组可用素材", "Copy ready group assets"), self.collection_actions)
        self.collection_copy.setMinimumHeight(28)
        self.collection_copy.clicked.connect(self._copy_collection)
        actions_layout.addWidget(self.collection_summary, 1)
        actions_layout.addWidget(self.collection_copy)
        self.collection_actions.hide()
        self.collection_member_button = QPushButton(ui_text("加入组合…", "Add to group…"), self.container)
        self.collection_member_button.setMinimumHeight(28)
        self.collection_member_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.collection_member_button.clicked.connect(self._collection_menu)
        self.collection_member_button.setStyleSheet(_ACTION_BUTTON_STYLE)
        self.collection_member_button.hide()
        for button in (self.new_collection_button, self.collection_save, self.collection_cancel, self.collection_copy):
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setMinimumHeight(26)
            button.setStyleSheet(_ACTION_BUTTON_STYLE + "QPushButton { font-size: 11px; padding: 0 7px; }")

        self.filter_row = QWidget(self.container)
        self.filter_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        filter_layout = QHBoxLayout(self.filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(4)
        self.filter_button_group = QButtonGroup(self)
        self.filter_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("all", ui_text("全部", "All")),
            ("ready", ui_text("可用", "Ready")),
            ("pending", ui_text("待确认", "Pending")),
            ("image", ui_text("图片", "Images")),
            ("audio", ui_text("音频", "Audio")),
            ("font", ui_text("字体", "Fonts")),
        ):
            button = QPushButton(label, self.filter_row)
            _configure_choice_button(button, self.filter_button_group)
            button.clicked.connect(lambda _checked=False, selected_mode=mode: self._set_filter_mode(selected_mode))
            self.filter_buttons[mode] = button
            filter_layout.addWidget(button)
        layout.addWidget(self.filter_row)
        self._refresh_filter_buttons()

        self.search_input = QLineEdit(self.container)
        self.search_input.setPlaceholderText(ui_text("搜索名称、字体、来源、用途", "Search name, font, source, role"))
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleDescription(ui_text("可组合多个关键词，用空格分开", "Combine keywords separated by spaces"))
        self.search_input.setMinimumHeight(28)
        self.search_input.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.search_input.setAccessibleName(ui_text("搜索素材", "Search assets"))
        self.search_input.setStyleSheet(_LINE_EDIT_STYLE)
        self._search_refresh_timer = QTimer(self)
        self._search_refresh_timer.setSingleShot(True)
        self._search_refresh_timer.setInterval(180)
        self._search_refresh_timer.timeout.connect(self.refresh)
        self.search_input.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_input)

        self.paste_ingest_button = QPushButton(
            ui_text("从剪贴板收纳", "Import from clipboard"),
            self.container,
        )
        self.paste_ingest_button.setMinimumHeight(30)
        self.paste_ingest_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.paste_ingest_button.setAccessibleDescription(
            ui_text(
                "收纳剪贴板中的图片、音频、GIF 或字体素材",
                "Store an image, audio file, GIF, or font from the clipboard",
            )
        )
        self.paste_ingest_button.setStyleSheet(_ACTION_BUTTON_STYLE)
        self.paste_ingest_button.hide()

        self.item_labels: list[QPushButton] = []
        for _ in range(3):
            item = _MaterialItemButton("", self.container)
            item.setCheckable(True)
            item.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            item.setMinimumHeight(46)
            item.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            item.setStyleSheet(_ITEM_BUTTON_STYLE)
            item.setCursor(Qt.CursorShape.PointingHandCursor)
            item.clicked.connect(lambda _checked=False, index=len(self.item_labels): self._select_recent_item(index))
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

        self.empty_state_label = QLabel("", self.container)
        self.empty_state_label.setWordWrap(True)
        self.empty_state_label.setMinimumHeight(88)
        self.empty_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state_label.setStyleSheet(
            "QLabel { color: #4E5F3D; font-size: 11px; "
            "padding: 12px; background: #FFF9EA; "
            "border: 1px solid #D5A73D; border-radius: 8px; }"
        )
        self.empty_state_label.hide()
        layout.addWidget(self.empty_state_label)

        self.detail_label = QLabel("", self.container)
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
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
        role_layout = QGridLayout(self.role_row)
        role_layout.setContentsMargins(0, 0, 0, 0)
        role_layout.setSpacing(4)
        self.image_role_button_group = QButtonGroup(self)
        self.role_buttons: dict[str, QPushButton] = {}
        for index, (role, label) in enumerate((
            ("main_background", ui_text("背景", "Backdrop")),
            ("hero_image", ui_text("主视觉", "Hero")),
            ("logo", "Logo"),
            ("icon", ui_text("图标", "Icon")),
            ("content_image", ui_text("内容图", "Content")),
            ("texture", ui_text("纹理", "Texture")),
        )):
            button = QPushButton(label, self.role_row)
            _configure_choice_button(button, self.image_role_button_group)
            button.clicked.connect(lambda _checked=False, selected_role=role: self._set_selected_role(selected_role))
            self.role_buttons[role] = button
            role_layout.addWidget(button, index // 3, index % 3)
            button.installEventFilter(self)
        self.role_row.installEventFilter(self)
        self.role_row.hide()
        layout.addWidget(self.role_row)

        self.gif_role_row = QWidget(self.container)
        self.gif_role_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        gif_role_layout = QHBoxLayout(self.gif_role_row)
        gif_role_layout.setContentsMargins(0, 0, 0, 0)
        gif_role_layout.setSpacing(4)
        self.gif_role_buttons: dict[str, QPushButton] = {}
        for role, label in (
            ("reaction", ui_text("反应", "Reaction")),
            ("sticker", ui_text("贴纸", "Sticker")),
            ("ui_animation", ui_text("界面动画", "UI motion")),
        ):
            button = QPushButton(label, self.gif_role_row)
            _configure_choice_button(button, self.image_role_button_group)
            button.clicked.connect(
                lambda _checked=False, selected_role=role: self._set_selected_role(selected_role)
            )
            self.gif_role_buttons[role] = button
            gif_role_layout.addWidget(button)
        self.gif_role_row.hide()
        layout.addWidget(self.gif_role_row)

        self.audio_usage_row = QWidget(self.container)
        self.audio_usage_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        audio_usage_layout = QHBoxLayout(self.audio_usage_row)
        audio_usage_layout.setContentsMargins(0, 0, 0, 0)
        audio_usage_layout.setSpacing(4)
        self.audio_usage_button_group = QButtonGroup(self)
        self.audio_usage_buttons: dict[str, QPushButton] = {}
        for usage, label in (
            ("music", ui_text("音乐", "Music")),
            ("voice", ui_text("人声", "Voice")),
            ("ambience", ui_text("环境", "Ambient")),
            ("sound_effect", ui_text("音效", "SFX")),
            ("loop", ui_text("循环", "Loop")),
        ):
            button = QPushButton(label, self.audio_usage_row)
            _configure_choice_button(button, self.audio_usage_button_group)
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
        self.preview_label.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet(
            "QLabel { background: #F6F1E4; border: 1px solid #DDD3BB; border-radius: 8px; }"
        )
        self.preview_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_label.mousePressEvent = self._replay_gif_preview
        self.preview_label.hide()
        layout.addWidget(self.preview_label)
        self.font_preview_scroll = QScrollArea(self.container)
        self.font_preview_scroll.setFixedHeight(72)
        self.font_preview_label = QLabel()
        self.font_preview_label.setTextFormat(Qt.TextFormat.PlainText)
        self.font_preview_label.setStyleSheet("QLabel { background: #F6F1E4; color: #333333; }")
        self.font_preview_scroll.setWidget(self.font_preview_label)
        self.font_preview_scroll.setAccessibleName(ui_text("字体原尺寸预览，可滚动", "Font preview at actual size; scroll to view"))
        self.font_preview_scroll.hide()
        layout.addWidget(self.font_preview_scroll)

        self.font_controls = QWidget(self.container)
        font_layout = QVBoxLayout(self.font_controls)
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.setSpacing(4)
        sample_row = QHBoxLayout()
        self.font_text_input = QLineEdit(DEFAULT_FONT_TEXT, self.font_controls)
        self.font_text_input.setMaxLength(120)
        self.font_text_input.setMinimumHeight(26)
        self.font_text_input.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.font_text_input.setStyleSheet(_LINE_EDIT_STYLE)
        self.font_size_input = QSpinBox(self.font_controls)
        self.font_size_input.setRange(12, 72)
        self.font_size_input.setValue(32)
        self.font_size_input.setSuffix(" px")
        self.font_size_input.setMinimumHeight(26)
        self.font_size_input.setFixedWidth(72)
        self.font_size_input.setStyleSheet(_LINE_EDIT_STYLE.replace("QLineEdit", "QSpinBox"))
        self.font_text_input.textChanged.connect(self._on_font_preview_changed)
        self.font_size_input.valueChanged.connect(self._on_font_preview_changed)
        sample_row.addWidget(self.font_text_input, 1)
        sample_row.addWidget(self.font_size_input)
        font_layout.addLayout(sample_row)
        self.font_missing_label = QLabel("", self.font_controls)
        self.font_missing_label.setWordWrap(True)
        self.font_missing_label.setTextFormat(Qt.TextFormat.PlainText)
        self.font_missing_label.setStyleSheet("QLabel { color: #A67624; font-size: 11px; }")
        font_layout.addWidget(self.font_missing_label)
        usage_row = QGridLayout()
        self.font_usage_buttons: dict[str, QPushButton] = {}
        self.font_usage_button_group = QButtonGroup(self)
        for index, usage in enumerate(("heading", "body", "decorative", "unknown")):
            button = QPushButton(self.font_controls)
            _configure_choice_button(button, self.font_usage_button_group)
            button.setMinimumHeight(24)
            button.clicked.connect(lambda _checked=False, value=usage: self._set_selected_font_usage(value))
            self.font_usage_buttons[usage] = button
            usage_row.addWidget(button, index // 2, index % 2)
        font_layout.addLayout(usage_row)
        action_row = QVBoxLayout()
        self.font_notes_button = QPushButton(self.font_controls)
        self.font_export_button = QPushButton(self.font_controls)
        for button in (self.font_notes_button, self.font_export_button):
            button.setStyleSheet(_ACTION_BUTTON_STYLE)
            button.setMinimumHeight(26)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            action_row.addWidget(button)
        self.font_notes_button.clicked.connect(self._edit_font_notes)
        self.font_export_button.clicked.connect(self._export_selected_font)
        font_layout.addLayout(action_row)
        self._retranslate_font_controls()
        self.font_controls.hide()
        layout.addWidget(self.font_controls)

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
        self.paste_ingest_button.show()
        layout.addWidget(self.paste_ingest_button)
        layout.addWidget(self.scope_row)
        layout.addWidget(self.collection_editor)
        layout.addWidget(self.collection_actions)
        layout.addWidget(toolbar)

        body = QWidget(self.container)
        body.setStyleSheet("QWidget { background: transparent; border: none; }")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)

        list_pane = QWidget(body)
        list_pane.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        detail_pane.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        detail_pane.setStyleSheet("QWidget { background: transparent; border: none; }")
        detail_layout = QVBoxLayout(detail_pane)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)
        self.preview_label.setFixedHeight(72)
        self.detail_label.setWordWrap(False)
        self.detail_label.setMinimumHeight(54)
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        detail_layout.addWidget(self.empty_state_label, 1)
        detail_layout.addWidget(self.preview_label)
        detail_layout.addWidget(self.font_preview_scroll)
        detail_layout.addWidget(self.detail_label, 1)
        detail_layout.addWidget(self.accept_ai_button)
        detail_layout.addWidget(self.role_row)
        detail_layout.addWidget(self.gif_role_row)
        detail_layout.addWidget(self.audio_usage_row)
        detail_layout.addWidget(self.font_controls)
        detail_layout.addWidget(self.retry_ai_button)
        detail_layout.addWidget(self.collection_member_button)
        detail_layout.addWidget(self.copy_selected_button)

        body_layout.addWidget(list_pane, 42)
        detail_scroll = QScrollArea(body)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        detail_scroll.setWidget(detail_pane)
        body_layout.addWidget(detail_scroll, 58)
        layout.addWidget(body, 1)

        focus_order = [
            self.paste_ingest_button,
            *self.scope_buttons.values(),
            self.collection_picker,
            self.new_collection_button,
            self.retry_batch_button,
            self.collection_name,
            self.collection_save,
            self.collection_cancel,
            self.collection_copy,
            self.search_input,
            *self.filter_buttons.values(),
            *self.item_labels,
            self.previous_page_button,
            self.next_page_button,
            self.accept_ai_button,
            *self.role_buttons.values(),
            *self.gif_role_buttons.values(),
            *self.audio_usage_buttons.values(),
            self.font_text_input, self.font_size_input,
            *self.font_usage_buttons.values(),
            self.font_notes_button, self.font_export_button,
            self.retry_ai_button,
            self.collection_member_button,
            self.copy_selected_button,
        ]
        for current, following in zip(focus_order, focus_order[1:]):
            QWidget.setTabOrder(current, following)

    def _layout_image_role_buttons(self) -> None:
        role_layout = self.role_row.layout()
        buttons = list(self.role_buttons.values())
        available_width = self.role_row.contentsRect().width()
        spacing = max(0, role_layout.horizontalSpacing())
        required_width = max(
            max(button.sizeHint().width(), button.fontMetrics().horizontalAdvance(button.text()) + 4)
            for button in buttons
        )
        columns = 3
        while (
            columns > 1
            and available_width > 0
            and columns * required_width + (columns - 1) * spacing > available_width
        ):
            columns -= 1
        for index, button in enumerate(buttons):
            role_layout.addWidget(button, index // columns, index % columns)
        role_layout.activate()

    def eventFilter(self, watched, event) -> bool:
        if watched in (self.collection_name, self.collection_save, self.collection_cancel) and self.collection_editor.isVisible() and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._cancel_collection()
            return True
        if (
            watched is self.role_row
            and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}
        ) or (
            watched in self.role_buttons.values()
            and event.type() == QEvent.Type.FontChange
        ):
            QTimer.singleShot(0, self._layout_image_role_buttons)
        return super().eventFilter(watched, event)

    def set_toast_handler(self, callback) -> None:
        self._toast_callback = callback
        if self._theme_recovery_notice_pending and self._toast_callback is not None:
            self._theme_recovery_notice_pending = False
            self._toast_callback(
                ui_text(
                    "已隔离损坏的主题记录并创建恢复副本",
                    "Damaged theme metadata was quarantined and replaced",
                ),
                tone="error",
            )

    def set_ai_handlers(self, *, provider_factory, enabled_callback, retry_batch_callback) -> None:
        self._ai_provider_factory = provider_factory
        self._ai_enabled_callback = enabled_callback
        self._retry_batch_callback = retry_batch_callback

    def set_low_power_enabled(self, enabled: bool) -> None:
        self._low_power_enabled = bool(enabled)
        if self._low_power_enabled:
            self._stop_gif_preview(reset_to_first=True)

    def retranslate(self) -> None:
        self._retranslate_font_controls()
        labels = {
            "all": ui_text("全部", "All"),
            "ready": ui_text("可用", "Ready"),
            "pending": ui_text("待确认", "Pending"),
            "image": ui_text("图片", "Images"),
            "audio": ui_text("音频", "Audio"),
            "font": ui_text("字体", "Fonts"),
        }
        for mode, button in self.filter_buttons.items():
            button.setText(labels[mode])
        self._refresh_filter_buttons()
        self.search_input.setPlaceholderText(ui_text("搜索名称、字体、来源、用途", "Search name, font, source, role"))
        self.search_input.setAccessibleDescription(ui_text("可组合多个关键词，用空格分开", "Combine keywords separated by spaces"))
        self.search_input.setAccessibleName(ui_text("搜索素材", "Search assets"))
        self.paste_ingest_button.setText(ui_text("从剪贴板收纳", "Import from clipboard"))
        self.paste_ingest_button.setAccessibleDescription(
            ui_text(
                "收纳剪贴板中的图片、音频、GIF、字体或网页链接",
                "Store an image, audio file, GIF, font, or page link from the clipboard",
            )
        )
        role_labels = {
            "main_background": ui_text("背景", "Backdrop"),
            "hero_image": ui_text("主视觉", "Hero"),
            "logo": "Logo",
            "icon": ui_text("图标", "Icon"),
            "content_image": ui_text("内容图", "Content"),
            "texture": ui_text("纹理", "Texture"),
        }
        for role, button in self.role_buttons.items():
            button.setText(role_labels[role])
        gif_role_labels = {
            "reaction": ui_text("反应", "Reaction"),
            "sticker": ui_text("贴纸", "Sticker"),
            "ui_animation": ui_text("界面动画", "UI motion"),
        }
        for role, button in self.gif_role_buttons.items():
            button.setText(gif_role_labels[role])
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
        self.collection_picker.setAccessibleName(ui_text("查看素材组合", "View asset group"))
        self.new_collection_button.setText(ui_text("管理组合", "Manage groups"))
        self.collection_name.setAccessibleName(ui_text("组合名称", "Group name"))
        self.collection_name.setPlaceholderText(ui_text("例如：个人网站", "e.g. Personal website"))
        self.collection_save.setText(ui_text("确认解散", "Dissolve") if self._collection_editor_action == "dissolve" else
                                     ui_text("保存", "Save") if self._collection_editor_action == "rename" else ui_text("创建", "Create"))
        if self._collection_editor_action == "dissolve":
            self.collection_notice.setText(ui_text("解散组合后，原素材仍保留。", "Dissolving this group keeps all original assets."))
        self.collection_cancel.setText(ui_text("取消", "Cancel"))
        self.collection_copy.setText(ui_text("复制这组可用素材", "Copy ready group assets"))
        self.collection_member_button.setText(ui_text("加入组合…", "Add to group…"))
        QTimer.singleShot(0, self._layout_image_role_buttons)
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
        self._stop_gif_preview(reset_to_first=True, discard=True)
        self._refresh_collections()
        service = self._bundle_service()
        catalog_unavailable = False
        try:
            scoped_bundles = service.list_bundles(
                batch_id="latest" if self._batch_scope == "latest" else None
            )
            if self._collection_id:
                collection = next((item for item in self._collections if item["id"] == self._collection_id), None)
                members = set(collection["asset_ids"]) if collection else set()
                scoped_bundles = [bundle for bundle in scoped_bundles if bundle["id"] in members]
        except ManifestReadinessError:
            scoped_bundles = []
            catalog_unavailable = True
        summary = build_material_panel_summary(
            scoped_bundles,
            assets_dir=self.settings.ASSETS_DIR,
            max_items=len(scoped_bundles),
        )
        if getattr(service, "theme_recoveries", []):
            if self._toast_callback is not None:
                self._toast_callback(
                    ui_text(
                        "已隔离损坏的主题记录并创建恢复副本",
                        "Damaged theme metadata was quarantined and replaced",
                    ),
                    tone="error",
                )
            else:
                self._theme_recovery_notice_pending = True
        self._bundle_by_source_key = {
            str(bundle.get("source_key") or ""): bundle
            for bundle in scoped_bundles
            if str(bundle.get("source_key") or "")
        }
        scoped_items = [] if catalog_unavailable else list(summary.recent_items)
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
        self.empty_state_label.hide()
        self.detail_label.hide()
        self.role_row.hide()
        self.gif_role_row.hide()
        self.audio_usage_row.hide()
        self.font_controls.hide()
        self._cancel_font_preview()
        self.retry_ai_button.hide()
        self.accept_ai_button.hide()
        self._suggested_ai_role = ""
        self.preview_label.hide()
        self.font_preview_scroll.hide()
        self.copy_selected_button.setEnabled(False)
        self.collection_member_button.hide()
        self.copy_ready_button.setVisible(bool(scoped_items) and not self._embedded)
        if not scoped_items:
            empty_text = (
                ui_text(
                    "素材目录暂不可用\n素材已保存 · Agent 接口待恢复\n等待恢复后再试",
                    "The asset catalog is unavailable\nAssets saved · Agent access pending recovery\nTry again after recovery",
                )
                if catalog_unavailable
                else ui_text(
                    "草窝还空着\n把图片、GIF、音频或字体拖到桌面草堆\n也可以用上方按钮收纳剪贴板内容",
                    "The nest is empty\nDrop an image, GIF, audio file, or font onto the desktop pile\nor use the clipboard button above",
                )
                if self._embedded
                else ui_text(
                    "草窝还空着\n拖入图片、GIF、音频或字体开始收纳",
                    "The nest is empty\nDrop an image, GIF, audio file, or font to start storing",
                )
            )
            self.empty_state_label.setText(empty_text)
            if self._collection_id and not catalog_unavailable:
                self.empty_state_label.setText(ui_text(
                    "这组还没有可展示的素材\n切换到全部素材，选择素材后加入组合",
                    "No assets to display in this group\nSwitch to All assets, then add an item to the group",
                ))
                empty_text = self.empty_state_label.text()
            self.empty_state_label.setAccessibleName(
                ui_text("素材目录状态", "Asset catalog status")
            )
            self.empty_state_label.setAccessibleDescription(empty_text.replace("\n", " "))
            self.empty_state_label.show()
        elif not self._recent_items:
            empty_text = ui_text(
                "没有匹配素材\n清除搜索或切换筛选条件",
                "No matching assets\nClear the search or change the filter",
            )
            self.empty_state_label.setText(empty_text)
            self.empty_state_label.setAccessibleName(
                ui_text("没有匹配素材", "No matching assets")
            )
            self.empty_state_label.setAccessibleDescription(empty_text.replace("\n", " "))
            self.empty_state_label.show()
        project_summary = None
        if not self._embedded:
            from app.services.experimental_project_summary import (
                build_material_panel_summary as build_experimental_project_summary,
            )

            project_summary = build_experimental_project_summary(
                assets_dir=self.settings.ASSETS_DIR,
                manifest_path=self.settings.MANIFEST_PATH,
                themes_dir=self.settings.THEMES_DIR,
            )
            if project_summary.project_display_label:
                self.project_label.setText(project_summary.project_display_label)
                self.project_label.setToolTip(project_summary.real_project_root)
                self.project_label.setStyleSheet(
                    "QLabel { "
                    f"color: {self._project_display_color(project_summary.project_display_state)}; "
                    "font-size: 11px; background: transparent; border: none; }"
                )
            else:
                self.project_label.setText("")
                self.project_label.setToolTip("")
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
                f"{total_count} assets · Ready {recognized_count}\nPending {pending_count}",
            )
        else:
            summary_text = ui_text(
                f"{summary.total_count} 个 bundle · 可用 {summary.recognized_count} · 待确认 {summary.pending_count}",
                f"{summary.total_count} bundles · Ready {summary.recognized_count} · Pending {summary.pending_count}",
            )
        self.summary_label.setText(summary_text)
        self.collection_actions.setVisible(self._embedded and bool(self._collection_id))
        self.collection_copy.setEnabled(self._collections_ready and not catalog_unavailable and recognized_count > 0)
        if self._collection_id:
            collection = next((item for item in self._collections if item["id"] == self._collection_id), None)
            member_count = len(collection["asset_ids"]) if collection else 0
            omitted = member_count - recognized_count
            self.collection_summary.setText(ui_text(
                f"{member_count} 项 · 可用 {recognized_count} · 未交付 {omitted}",
                f"{member_count} items · Ready {recognized_count} · Omitted {omitted}",
            ) if self._collections_ready and collection else ui_text("组合记录暂不可用", "Group record unavailable"))
        if summary.pending_count and not self._embedded:
            self.pending_label.setText(ui_text(f"{summary.pending_count} 个待确认用途", f"{summary.pending_count} roles need review"))
            self.pending_label.show()
        else:
            self.pending_label.setText("")
            self.pending_label.hide()

        selected_item = None
        for idx, label in enumerate(self.item_labels):
            if idx >= len(self._visible_items):
                label.setChecked(False)
                label.setAccessibleName("")
                label.setAccessibleDescription("")
                label.hide()
                continue
            item = self._visible_items[idx]
            bundle = self._bundle_for_item(item)
            selected = bool(selected_id and bundle["id"] == selected_id)
            if selected:
                selected_item = item
            label.setText(
                f"{item.title.replace('&', '&&')}\n"
                f"{self._asset_type_label(item.asset_type)} · {self._display_role_label(item.usage_label)} · "
                f"{self._agent_usability_label(bundle['status'])}"
            )
            label.setChecked(selected)
            label.setAccessibleName(item.title)
            label.setAccessibleDescription(
                f"{self._asset_type_label(item.asset_type)} · "
                f"{self._display_role_label(item.usage_label)} · "
                f"{self._bundle_status_label(str(bundle['status']))}"
            )
            label.setToolTip(_plain_tooltip("\n".join(dict.fromkeys(value for value in
                (item.title, item.origin_url, item.source_key or item.preview_url) if value))))
            label.show()

        if selected_item is not None:
            selected_bundle = self._bundle_for_item(selected_item)
            self._show_preview_for_item(selected_item, selected_bundle)
            self._show_detail_for_bundle(selected_bundle)

        service_lines = [summary.recognition_status, summary.service_status]
        if project_summary is not None and project_summary.project_picker_status_line:
            service_lines.append(project_summary.project_picker_status_line)
        self.service_label.setText("\n".join(line for line in service_lines if line))
        if project_summary is not None and project_summary.panel_display_text:
            self.rehearsal_label.setText(project_summary.panel_display_text)
            self.rehearsal_label.setToolTip(
                project_summary.project_picker_tooltip or project_summary.panel_status_text
            )
        else:
            self.rehearsal_label.setText("")
            self.rehearsal_label.setToolTip("")
        self.rehearsal_label.hide()
        self.service_label.hide()
        self._confirmation_available = bool(
            project_summary is not None and project_summary.confirmation_available
        )
        latest = (
            service.get_latest_batch()
            if not catalog_unavailable and hasattr(service, "get_latest_batch")
            else None
        )
        self.retry_batch_button.setEnabled(
            self._batch_scope == "latest" and latest is not None and bool(scoped_bundles)
        )
        if self.confirmation_preview is not None and project_summary is not None:
            self.confirmation_preview.update_prompt(
                title=project_summary.confirmation_title,
                body=project_summary.confirmation_body,
                summary=project_summary.confirmation_summary,
                warning=project_summary.confirmation_warning,
                action=project_summary.confirmation_action,
                project_root=project_summary.real_project_root,
                primary_label=project_summary.confirmation_primary_label,
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
        if not self.collection_editor.isHidden():
            self._cancel_collection()
        self._collection_id = ""
        normalized = scope if scope in {"latest", "all"} else "latest"
        if normalized == self._batch_scope:
            self.refresh()
            return
        self._batch_scope = normalized
        self._selected_bundle_id = ""
        self._page_index = 0
        self._refresh_scope_buttons()
        self.refresh()

    def _collections_path(self) -> Path:
        return self.settings.STORAGE_DIR / "collections.json"

    def _refresh_collections(self) -> None:
        try:
            self._collections = read_collections(self._collections_path())
            self._collections_ready = True
        except (OSError, ValueError):
            self._collections = []
            self._collections_ready = False
        blocked = self.collection_picker.blockSignals(True)
        self.collection_picker.clear()
        self.collection_picker.addItem(ui_text("素材组合", "Asset groups"), "")
        for item in self._collections:
            self.collection_picker.addItem(item["name"], item["id"])
        index = self.collection_picker.findData(self._collection_id)
        if index < 0:
            self.collection_picker.addItem(ui_text("组合不可用", "Group unavailable"), self._collection_id)
            index = self.collection_picker.count() - 1
        self.collection_picker.setCurrentIndex(index)
        self.collection_picker.setToolTip(_plain_tooltip(self.collection_picker.currentText()))
        self.collection_picker.blockSignals(blocked)
        self.collection_picker.setEnabled(self._collections_ready)
        self.new_collection_button.setEnabled(self._collections_ready)
        self.new_collection_button.setToolTip("" if self._collections_ready else ui_text("组合记录不可读，请恢复后重试", "Group records cannot be read; restore them and retry"))

    def _select_collection(self, _index: int) -> None:
        if not self.collection_editor.isHidden():
            self._cancel_collection()
        self._collection_id = str(self.collection_picker.currentData() or "")
        self._batch_scope = "all"
        self._selected_bundle_id = ""
        self._page_index = 0
        self._filter_mode = "all"
        self.search_input.clear()
        self._search_refresh_timer.stop()
        self._refresh_filter_buttons()
        self._refresh_scope_buttons()
        self.refresh()

    def _manage_collections(self) -> None:
        menu = QMenu(self.new_collection_button)
        menu.addAction(ui_text("新建组合…", "New group…"), lambda: self._begin_collection())
        current = next((item for item in self._collections if item["id"] == self._collection_id), None)
        for action, label in (("rename", ui_text("重命名当前组合…", "Rename current group…")),
                              ("dissolve", ui_text("解散当前组合…", "Dissolve current group…"))):
            item = menu.addAction(label)
            item.setEnabled(self._collections_ready and current is not None)
            item.triggered.connect(lambda _checked=False, selected=action: self._begin_collection(action=selected))
        menu.exec(self.new_collection_button.mapToGlobal(QPoint(0, self.new_collection_button.height())))
        menu.deleteLater()

    def _begin_collection(self, asset_id: str = "", *, action: str = "create") -> None:
        collection = next((item for item in self._collections if item["id"] == self._collection_id), None)
        if action != "create" and collection is None:
            self._collection_error(ValueError("collection_not_found"))
            return
        self._collection_seed_id = asset_id
        self._collection_edit_id = collection["id"] if action != "create" else ""
        self._collection_editor_action = action
        self.collection_name.setText(collection["name"] if action != "create" else "")
        self.collection_name.setReadOnly(action == "dissolve")
        self.collection_name.setToolTip(_plain_tooltip(collection["name"]) if action == "dissolve" else "")
        self.collection_notice.setText(ui_text("解散组合后，原素材仍保留。", "Dissolving this group keeps all original assets.") if action == "dissolve" else "")
        self.collection_notice.setVisible(action == "dissolve")
        self.collection_save.setText(ui_text("确认解散", "Dissolve") if action == "dissolve" else
                                     ui_text("保存", "Save") if action == "rename" else ui_text("创建", "Create"))
        self.collection_editor.show()
        if action == "dissolve":
            self.collection_cancel.setFocus()
        else:
            self.collection_name.setFocus()
            self.collection_name.selectAll()

    def _cancel_collection(self) -> None:
        self.collection_editor.hide()
        self.collection_name.clear()
        self.collection_name.setReadOnly(False)
        self.collection_notice.hide()
        self._collection_seed_id = ""
        self._collection_edit_id = ""
        self._collection_editor_action = "create"
        self.collection_save.setText(ui_text("创建", "Create"))
        self.new_collection_button.setFocus()

    def _collection_error(self, exc) -> None:
        if str(exc) == "collection_name_exists":
            text = ui_text("组合名称已存在，请换一个名称", "Group name already exists; choose another name")
        elif str(exc) == "invalid_collection_name":
            text = ui_text("请输入 1–64 字的组合名称", "Enter a group name of 1–64 characters")
        elif str(exc) == "collection_has_no_ready_assets":
            text = ui_text("这组暂无可用素材，请先确认用途", "This group has no ready assets; confirm usage first")
        else:
            text = ui_text("操作未完成，请确认素材与组合记录可用后重试", "Action not completed; check the assets and group records, then retry")
        if self.collection_editor.isVisible():
            self.collection_notice.setText(text)
            self.collection_notice.show()
        if self._toast_callback is not None:
            self._toast_callback(text, tone="error")
        else:
            self.detail_label.setText(text)
            self.detail_label.show()

    def _save_collection(self) -> None:
        action = self._collection_editor_action
        try:
            if action == "dissolve":
                dissolve_collection(self._collections_path(), self._collection_edit_id)
                self._cancel_collection()
                self.set_batch_scope("all")
                if self._toast_callback is not None:
                    self._toast_callback(ui_text("组合已解散，原素材保留", "Group dissolved; original assets kept"), tone="success")
                return
            if self._collection_seed_id:
                bundle = self._bundle_service().get_bundle(self._collection_seed_id)
                if bundle is None or bundle["status"] == "missing":
                    raise ValueError("collection_asset_unavailable")
            collection = save_collection(self._collections_path(), self.collection_name.text(),
                                         asset_id=self._collection_seed_id, collection_id=self._collection_edit_id)
        except (OSError, ValueError, TimeoutError, ManifestReadinessError) as exc:
            self._collection_error(exc)
            (self.collection_cancel if action == "dissolve" else self.collection_name).setFocus()
            return
        self._cancel_collection()
        if action == "rename":
            self.refresh()
            self.collection_picker.setFocus()
            if self._toast_callback is not None:
                self._toast_callback(ui_text("组合已重命名", "Group renamed"), tone="success")
            return
        self._collection_id = collection["id"]
        self._batch_scope = "all"
        self._page_index = 0
        self._filter_mode = "all"
        self.search_input.clear()
        self._search_refresh_timer.stop()
        self._refresh_filter_buttons()
        self._refresh_scope_buttons()
        self.refresh()
        self.collection_picker.setFocus()

    def _collection_menu(self) -> None:
        asset_id = self._selected_bundle_id
        if not asset_id:
            return
        try:
            collections = read_collections(self._collections_path())
            bundle = self._bundle_service().get_bundle(asset_id)
            if bundle is None:
                raise ValueError("collection_asset_unavailable")
        except (OSError, ValueError, ManifestReadinessError) as exc:
            self._collection_error(exc)
            return
        menu = QMenu(self.collection_member_button)
        for item in collections:
            action = menu.addAction(item["name"].replace("&", "&&"))
            action.setCheckable(True)
            action.setChecked(asset_id in item["asset_ids"])
            action.setEnabled(action.isChecked() or bundle["status"] != "missing")
            action.triggered.connect(lambda included, target=item["id"]: self._set_collection_asset(target, asset_id, included))
        if collections:
            menu.addSeparator()
        action = menu.addAction(ui_text("新建组合…", "New group…"))
        action.setEnabled(bundle["status"] != "missing")
        action.triggered.connect(lambda: self._begin_collection(asset_id))
        menu.exec(self.collection_member_button.mapToGlobal(QPoint(0, self.collection_member_button.height())))
        menu.deleteLater()

    def _set_collection_asset(self, collection_id: str, asset_id: str, included: bool) -> None:
        try:
            if included:
                bundle = self._bundle_service().get_bundle(asset_id)
                if bundle is None or bundle["status"] == "missing":
                    raise ValueError("collection_asset_unavailable")
            set_collection_asset(self._collections_path(), collection_id, asset_id, included=included)
        except (OSError, ValueError, TimeoutError, ManifestReadinessError) as exc:
            self._collection_error(exc)
            return
        self.refresh()

        if self._toast_callback is not None:
            self._toast_callback(ui_text("已加入组合", "Added to group") if included else
                                 ui_text("已移出组合", "Removed from group"), tone="success")

    def _copy_collection(self) -> None:
        try:
            payload = collection_handoff(self._collections_path(), self._collection_id, self._bundle_service(), base_url=self._base_url())
        except (OSError, ValueError, ManifestReadinessError) as exc:
            self._collection_error(exc)
            return
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False, indent=2))
        if self._toast_callback is not None:
            count, omitted = payload["asset_count"], payload["collection"]["omitted_count"]
            self._toast_callback(ui_text(f"已复制 {count} 项可用素材 · {omitted} 项未交付", f"Copied {count} ready assets · {omitted} omitted"), tone="success")

    def _on_search_changed(self, _text: str) -> None:
        self._page_index = 0
        self._search_refresh_timer.start()

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
            button.setChecked(scope == self._batch_scope)
        self.retry_batch_button.setStyleSheet(
            "QPushButton { color: #4E5F3D; background: #FFF9EA; "
            "border: 1px solid #E5D8B9; border-radius: 6px; font-size: 10px; }"
            "QPushButton:focus { border: 2px solid #C8A24A; }"
        )

    def _retry_latest_batch(self) -> None:
        if self._retry_batch_callback is not None:
            self._retry_batch_callback()

    def _refresh_filter_buttons(self) -> None:
        for mode, button in self.filter_buttons.items():
            button.setMinimumWidth(button.fontMetrics().horizontalAdvance(button.text()) + 12)
            button.setChecked(mode == self._filter_mode)

    def _filter_recent_items(self, items) -> list:
        query = self.search_input.text().strip().casefold()
        return [
            item
            for item in items
            if self._item_matches_filter_mode(item) and self._item_matches_query(item, query)
        ]

    def _item_matches_filter_mode(self, item) -> bool:
        mode = self._filter_mode
        if mode == "all":
            return True
        if mode in {"image", "audio", "font"}:
            return str(item.asset_type or "").lower() == mode
        return self._bundle_for_item(item)["status"] == mode

    def _item_matches_query(self, item, query: str) -> bool:
        if not query:
            return True
        bundle = self._bundle_for_item(item)
        haystack = "\n".join(
            str(value or "").casefold()
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
                bundle.get("font_usage", ""),
                bundle.get("font_metadata", {}),
                bundle.get("font_annotation", {}),
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
        return all(term in haystack for term in query.split())

    def _select_recent_item(self, index: int, event: QMouseEvent | None = None) -> None:
        if index >= len(self._visible_items):
            if event is not None:
                event.ignore()
            return
        self._leave_search_input_mode()
        item = self._visible_items[index]
        bundle = self._bundle_for_item(item)
        self._selected_bundle_id = bundle["id"]
        self._refresh_item_selection_styles()
        self._show_preview_for_item(item, bundle)
        self._show_detail_for_bundle(bundle)
        if event is not None:
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
        is_gif = (
            str(bundle.get("content_type") or "").lower() == "image/gif"
            and bundle.get("frame_count") is not None
        )
        is_font = str(bundle.get("type") or "").lower() == "font"
        ai_line = "" if is_gif or is_font else self._ai_suggestion_line(bundle.get("ai_suggestions"))
        is_audio = str(bundle.get("type") or "").lower() == "audio"
        audio_line = self._audio_detail_line(bundle) if is_audio else ""
        full_detail_text = (
            f"{status_line}\n"
            f"{ui_text('用途', 'Role')} {self._role_label(bundle['role'])} · {ui_text('类型', 'Type')} {self._asset_type_label(bundle['type'])}\n"
            f"sha256 {str(bundle['sha256'])[:12] or '-'} · key {self._compact_text(str(bundle['source_key']) or '-', 24)}{audio_line}\n"
            f"url {self._compact_text(bundle['url'])}{origin_line}{ai_line}\n"
            f"{handoff_line}"
        )
        if self._embedded:
            compact_status = (
                status_line
                if confirmed
                else (
                    f"{self._compact_text(str(bundle['id']), 14)} · "
                    f"{self._bundle_status_label(str(bundle['status']))} · "
                    f"{self._agent_usability_label(str(bundle['status']))}"
                )
            )
            compact_audio = self._compact_text(
                audio_line.replace("\n", " · "),
                34,
            )
            self.detail_label.setText(
                f"{compact_status}\n"
                f"{ui_text('用途', 'Role')} {self._role_label(bundle['role'])} · "
                f"{ui_text('类型', 'Type')} {self._asset_type_label(bundle['type'])}{compact_audio}\n"
                f"sha256 {str(bundle['sha256'])[:12] or '-'} · "
                f"key {self._compact_text(str(bundle['source_key']) or '-', 18)}"
            )
            self.detail_label.setToolTip(_plain_tooltip(full_detail_text))
        else:
            self.detail_label.setText(full_detail_text)
            self.detail_label.setToolTip("")
        self.detail_label.show()
        self.copy_selected_button.setEnabled(bool(bundle.get("id")))
        self.collection_member_button.setVisible(self._embedded)
        self.collection_member_button.setEnabled(self._collections_ready)
        self.role_row.setVisible(not is_audio and not is_font and bundle["status"] != "missing")
        self.gif_role_row.setVisible(is_gif and bundle["status"] != "missing")
        self.audio_usage_row.setVisible(is_audio and bundle["status"] != "missing")
        self._suggested_ai_role = self._suggested_role(bundle.get("ai_suggestions"))
        self.accept_ai_button.setVisible(
            not is_audio
            and not is_font
            and not is_gif
            and bundle["status"] != "missing"
            and bool(self._suggested_ai_role)
            and self._suggested_ai_role != str(bundle.get("role") or "")
        )
        if is_audio:
            self._refresh_audio_usage_buttons(str(bundle.get("audio_usage") or "unknown"))
        else:
            self._refresh_role_buttons(bundle["role"])
            self._refresh_gif_role_buttons(str(bundle.get("role") or ""))
        self.font_controls.setVisible(is_font)
        if is_font:
            available = bundle["status"] != "missing"
            self.font_controls.setEnabled(available)
            self.font_usage_button_group.setExclusive(False)
            for usage, button in self.font_usage_buttons.items():
                button.setChecked(usage == bundle.get("font_usage", "unknown"))
            self.font_usage_button_group.setExclusive(True)
            metadata = bundle.get("font_metadata") or {}
            license_known = bool(metadata.get("license_description") or metadata.get("license_url"))
            font_detail = (f"{metadata.get('family', '')} · {metadata.get('style', '')}\n"
                           f"{self._bundle_status_label(str(bundle['status']))} · {self._font_usage_label(str(bundle.get('font_usage', 'unknown')))} · "
                           + ui_text("可变默认实例" if metadata.get("is_variable") else "静态字体",
                                     "Variable default" if metadata.get("is_variable") else "Static font")
                           + "\n" + ui_text("内嵌许可：见来源与许可" if license_known else "许可未知，请核实后使用",
                                           "Embedded license: view notes" if license_known else "License unknown; verify use"))
            self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
            self.detail_label.setText(font_detail)
            self.detail_label.setToolTip(full_detail_text)
        self._refresh_retry_ai_button(bundle)

    def _refresh_retry_ai_button(self, bundle: dict[str, object]) -> None:
        visible = (
            str(bundle.get("type") or "").lower() == "image"
            and str(bundle.get("content_type") or "").lower() != "image/gif"
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
                self._toast_callback(
                    ui_text("AI 分拣未开启", "AI sorting is off"),
                    tone="error",
                )
            return
        bundle = self._get_bundle_safely(self._selected_bundle_id)
        if bundle is None:
            self.detail_label.setText(ui_text("资源不存在", "Asset not found"))
            self.detail_label.show()
            return
        if str(bundle.get("content_type") or "").lower() == "image/gif":
            self.retry_ai_button.hide()
            return
        self.retry_ai_button.setEnabled(False)
        self.retry_ai_button.setText(ui_text("AI 分拣中...", "AI sorting..."))
        if self._ai_provider_factory is None:
            self.ai_refresh_worker = AIRefreshWorker(bundle, self.settings.ASSETS_DIR)
        else:
            self.ai_refresh_worker = AIRefreshWorker(
                bundle,
                self.settings.ASSETS_DIR,
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
            self._toast_callback(message, tone="success" if success else "error")
        if bundle_id != self._selected_bundle_id:
            current = self._get_bundle_safely(self._selected_bundle_id) if self._selected_bundle_id else None
            if current is not None:
                self._refresh_retry_ai_button(current)
            return
        bundle = self._get_bundle_safely(bundle_id)
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
        settings = self.settings
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
            updated = self._bundle_service().set_bundle_role(self._selected_bundle_id, role)
        except (ManifestReadinessError, ValueError):
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
            updated = self._bundle_service().set_bundle_audio_usage(self._selected_bundle_id, usage)
        except (ManifestReadinessError, ValueError):
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
        bundle = self._get_bundle_safely(self._selected_bundle_id)
        if bundle is None:
            return
        QApplication.clipboard().setText(
            json.dumps(self._handoff_for_bundles([bundle]), ensure_ascii=False, indent=2)
        )
        self._show_detail_for_bundle(bundle, copied=True)
        if self._toast_callback is not None:
            self._toast_callback(
                ui_text("已复制 handoff", "Handoff copied"),
                tone="success",
            )

    def _get_bundle_safely(self, bundle_id: str) -> dict[str, object] | None:
        try:
            return self._bundle_service().get_bundle(bundle_id)
        except ManifestReadinessError:
            self.detail_label.setText(
                ui_text(
                    "素材已保存，Agent 接口待恢复",
                    "Assets are saved; Agent access is pending recovery",
                )
            )
            self.detail_label.show()
            return None

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

    @staticmethod
    def _font_usage_label(usage: str) -> str:
        return {
            "heading": ui_text("标题", "Heading"), "body": ui_text("正文", "Body"),
            "decorative": ui_text("装饰", "Decorative"), "unknown": ui_text("待确认", "Pending"),
        }.get(usage, usage)

    def _retranslate_font_controls(self) -> None:
        self.font_text_input.setAccessibleName(ui_text("字体预览文字，最多 120 字", "Font preview text, up to 120 characters"))
        self.font_text_input.setToolTip(ui_text("使用当前字体文件预览", "Preview this exact font file"))
        self.font_size_input.setAccessibleName(ui_text("字号，12 到 72 像素", "Font size, 12 to 72 pixels"))
        for usage, button in self.font_usage_buttons.items():
            button.setText(self._font_usage_label(usage))
        self.font_notes_button.setText(ui_text("来源与许可", "Source / license"))
        self.font_export_button.setText(ui_text("导出原文件", "Export original"))

    def _cancel_font_preview(self) -> None:
        self._font_preview_generation += 1
        self._font_preview_timer.stop()
        self._font_preview_request = None
        if self.font_preview_worker is not None:
            self.font_preview_worker.requestInterruption()

    def _on_font_preview_changed(self, _value=None) -> None:
        self._cancel_font_preview()
        if not self._font_preview_closing:
            self._font_preview_timer.start()

    def _queue_font_preview(self) -> None:
        if self._font_preview_closing:
            return
        bundle = next((value for value in self._bundle_by_source_key.values()
                       if value.get("id") == self._selected_bundle_id), None)
        if not bundle or bundle.get("type") != "font":
            return
        self.font_preview_scroll.show()
        if bundle.get("status") == "missing":
            self.font_preview_label.setText(ui_text("字体副本缺失", "Font copy missing"))
            self.font_preview_label.adjustSize()
            self.font_missing_label.clear()
            return
        path = self._bundle_service()._asset_path(str(bundle["source_key"]))
        if path is None:
            return
        self._font_preview_generation += 1
        self._font_preview_request = (
            self._font_preview_generation, self._selected_bundle_id, path,
            self.font_text_input.text(), self.font_size_input.value(), str(bundle.get("sha256") or ""),
        )
        self.font_preview_label.setText(ui_text("正在预览字体…", "Rendering font…"))
        self.font_preview_label.adjustSize()
        self.font_missing_label.clear()
        self._start_font_preview()

    def _start_font_preview(self) -> None:
        if self.font_preview_worker is not None or self._font_preview_request is None:
            return
        self.font_preview_worker = FontPreviewWorker(self._font_preview_request, self)
        self._font_preview_request = None
        self.font_preview_worker.finished.connect(self._finish_font_preview)
        self.font_preview_worker.start()

    def _finish_font_preview(self) -> None:
        worker = self.font_preview_worker
        if worker is None:
            return
        self.font_preview_worker = None
        if (worker.request[0] == self._font_preview_generation
                and worker.request[1] == self._selected_bundle_id
                and not self._font_preview_closing):
            if worker.result is not None:
                pixels, width, height, missing = worker.result
                bitmap = QImage(pixels, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
                self.font_preview_label.setPixmap(QPixmap.fromImage(bitmap))
                self.font_preview_label.setFixedSize(width, height)
                self.font_missing_label.setText(
                    ui_text(f"缺字：{missing}", f"Missing glyphs: {missing}") if missing
                    else ui_text("当前文字均有字形", "All sample characters have glyphs")
                )
            elif worker.error:
                self.font_preview_label.setText(ui_text("字体预览失败，请检查原文件", "Font preview failed; check the original file"))
        worker.deleteLater()
        if self._font_close_pending:
            self._font_preview_request = None
            self.close()
        else:
            self._start_font_preview()

    def _set_selected_font_usage(self, usage: str) -> None:
        try:
            bundle = self._bundle_service().update_bundle_font(self._selected_bundle_id, font_usage=usage)
        except (OSError, TimeoutError, ValueError, ManifestReadinessError):
            bundle = None
        if bundle is None:
            self.refresh()
            self.detail_label.setText(ui_text("用途保存失败，请重试", "Usage could not be saved; retry"))
            return
        self.refresh()

    def _edit_font_notes(self) -> None:
        bundle = self._get_bundle_safely(self._selected_bundle_id)
        if not bundle or bundle.get("type") != "font" or bundle.get("status") == "missing":
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(ui_text("字体来源与许可", "Font source and license"))
        dialog.resize(460, 410)
        form = QFormLayout(dialog)
        metadata = bundle.get("font_metadata") or {}
        embedded = QTextEdit(dialog)
        embedded.setReadOnly(True)
        embedded.setPlainText("\n".join(str(metadata.get(key) or "") for key in ("license_description", "license_url")).strip()
                              or ui_text("许可未知", "License unknown"))
        embedded.setMaximumHeight(140)
        form.addRow(ui_text("内嵌许可", "Embedded license"), embedded)
        source = QLineEdit(str((bundle.get("font_annotation") or {}).get("source_note") or ""), dialog)
        source.setMaxLength(512)
        source.setAccessibleName(ui_text("来源备注", "Source note"))
        form.addRow(ui_text("来源备注", "Source note"), source)
        license_note = QTextEdit(dialog)
        license_note.setAcceptRichText(False)
        license_note.setPlainText(str((bundle.get("font_annotation") or {}).get("license_note") or ""))
        license_note.setAccessibleName(ui_text("许可备注，最多 2048 字", "License note, up to 2048 characters"))
        form.addRow(ui_text("许可备注", "License note"), license_note)
        message = QLabel(ui_text("请核实许可范围；备注为人工记录，来源 URL 仅保存站点。", "Verify permitted use. Notes are manual records; source URLs retain the site only."), dialog)
        message.setWordWrap(True)
        message.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, dialog)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(ui_text("保存", "Save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(ui_text("取消", "Cancel"))
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        def save() -> None:
            if len(license_note.toPlainText()) > 2048:
                message.setText(ui_text("许可备注不能超过 2048 字", "License notes cannot exceed 2048 characters"))
                return
            try:
                updated = self._bundle_service().update_bundle_font(str(bundle["id"]), annotation={
                    "source_note": source.text(), "license_note": license_note.toPlainText(),
                })
            except (OSError, TimeoutError, ValueError, ManifestReadinessError):
                updated = None
            if updated is None:
                message.setText(ui_text("保存失败，原记录已保留，请重试", "Save failed; the previous record is retained. Retry."))
                return
            dialog.accept()
            self.refresh()

        buttons.accepted.connect(save)
        dialog.exec()
        dialog.deleteLater()

    def _export_selected_font(self) -> None:
        bundle = self._get_bundle_safely(self._selected_bundle_id)
        if not bundle or bundle.get("type") != "font" or bundle.get("status") == "missing":
            return
        source = self._bundle_service()._asset_path(str(bundle["source_key"]))
        if source is None:
            return
        target, _filter = QFileDialog.getSaveFileName(
            self, ui_text("导出字体原文件", "Export original font"), source.name,
            f"{source.suffix.upper()[1:]} (*{source.suffix})",
        )
        if not target:
            return
        try:
            export_font_original(source, Path(target), sha256=str(bundle["sha256"]), storage_dir=self.settings.STORAGE_DIR,
                                 protected_paths=(self.settings.ASSETS_DIR, self.settings.INDEX_DIR,
                                                  self.settings.THEMES_DIR, self.settings.MANIFEST_PATH))
        except (OSError, ValueError):
            self.detail_label.setText(ui_text("导出失败，目标原文件已保留；请选择素材库外的位置", "Export failed; the previous destination is retained. Choose a location outside the library."))
        else:
            self.detail_label.setText(ui_text("已导出字体原文件", "Original font exported"))

    def _show_preview_for_item(self, item, bundle: dict[str, object] | None = None) -> None:
        self._stop_gif_preview(reset_to_first=True, discard=True)
        self._cancel_font_preview()
        self.preview_label.clear()
        self.font_preview_scroll.hide()
        asset_type = str(item.asset_type or "").lower()
        if asset_type == "font":
            self.preview_label.hide()
            self._queue_font_preview()
            return
        if asset_type == "audio":
            self.preview_label.setText(ui_text("音频资源\n不在这里播放，agent 通过 URL 使用", "Audio asset\nAgents use it through URL"))
            self.preview_label.show()
            return
        if asset_type != "image" or not item.source_key:
            self.preview_label.hide()
            return
        asset_path = self.settings.ASSETS_DIR / item.source_key
        pixmap = QPixmap(str(asset_path))
        if pixmap.isNull():
            self.preview_label.hide()
            return
        content_type = str((bundle or {}).get("content_type") or "").lower()
        try:
            frame_count = int((bundle or {}).get("frame_count") or 0)
        except (TypeError, ValueError):
            frame_count = 0
        if (
            content_type == "image/gif"
            and frame_count > 1
            and not self._low_power_enabled
            and self.isVisible()
        ):
            movie = QMovie(str(asset_path), parent=self)
            if movie.isValid():
                scaled_size = pixmap.size().scaled(
                    292,
                    64,
                    Qt.AspectRatioMode.KeepAspectRatio,
                )
                movie.setScaledSize(scaled_size)
                movie.frameChanged.connect(self._on_gif_frame_changed)
                self._gif_movie = movie
                self._gif_frame_count = frame_count
                self.preview_label.setMovie(movie)
                self.preview_label.show()
                movie.start()
                return
            movie.deleteLater()
        self.preview_label.setPixmap(
            pixmap.scaled(
                292,
                64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.preview_label.show()

    def _on_gif_frame_changed(self, frame_index: int) -> None:
        movie = self._gif_movie
        if movie is None or self._gif_frame_count <= 1:
            return
        if frame_index < self._gif_frame_count - 1:
            return
        movie.setPaused(True)
        self._gif_stop_timer.start(
            max(MIN_GIF_FRAME_DELAY_MS, int(movie.nextFrameDelay()))
        )

    def _finish_gif_preview(self) -> None:
        self._stop_gif_preview(reset_to_first=True)

    def _stop_gif_preview(self, *, reset_to_first: bool, discard: bool = False) -> None:
        self._gif_stop_timer.stop()
        movie = self._gif_movie
        if movie is None:
            return
        movie.stop()
        if reset_to_first:
            movie.jumpToFrame(0)
        if discard:
            try:
                movie.frameChanged.disconnect(self._on_gif_frame_changed)
            except (RuntimeError, TypeError):
                pass
            self.preview_label.setMovie(None)
            self.preview_label.clear()
            movie.setFileName("")
            movie.deleteLater()
            self._gif_movie = None
            self._gif_frame_count = 0

    def _replay_gif_preview(self, event: QMouseEvent) -> None:
        movie = self._gif_movie
        if (
            event.button() != Qt.MouseButton.LeftButton
            or movie is None
            or self._low_power_enabled
        ):
            event.ignore()
            return
        self._gif_stop_timer.stop()
        movie.stop()
        movie.jumpToFrame(0)
        movie.start()
        event.accept()

    @staticmethod
    def _asset_type_label(asset_type: str) -> str:
        value = str(asset_type or "").lower()
        if value == "image":
            return ui_text("图片", "Image")
        if value == "audio":
            return ui_text("音频", "Audio")
        if value == "font":
            return ui_text("字体", "Font")
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

    def _refresh_role_buttons(self, active_role: str = "") -> None:
        self._refresh_image_role_buttons(active_role)

    def _refresh_gif_role_buttons(self, active_role: str = "") -> None:
        self._refresh_image_role_buttons(active_role)

    def _refresh_image_role_buttons(self, active_role: str = "") -> None:
        self.image_role_button_group.setExclusive(False)
        for role, button in {
            **self.role_buttons,
            **self.gif_role_buttons,
        }.items():
            button.setChecked(role == active_role)
        self.image_role_button_group.setExclusive(True)

    def _refresh_audio_usage_buttons(self, active_usage: str = "") -> None:
        self.audio_usage_button_group.setExclusive(False)
        for usage, button in self.audio_usage_buttons.items():
            button.setChecked(usage == active_usage)
        self.audio_usage_button_group.setExclusive(True)

    def _refresh_item_selection_styles(self) -> None:
        for idx, label in enumerate(self.item_labels):
            if idx >= len(self._visible_items) or label.isHidden():
                continue
            bundle = self._bundle_for_item(self._visible_items[idx])
            label.setChecked(bundle["id"] == self._selected_bundle_id)

    @staticmethod
    def _bundle_status_label(status: str) -> str:
        return {
            "ready": ui_text("可用", "Ready"),
            "pending": ui_text("待确认", "Pending"),
            "missing": ui_text("缺失", "Missing"),
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
            "reaction": ui_text("反应", "Reaction"),
            "sticker": ui_text("贴纸", "Sticker"),
            "ui_animation": ui_text("界面动画", "UI animation"),
            "audio": ui_text("音频", "Audio"),
            "font": ui_text("字体", "Font"),
            "unknown": ui_text("未确定", "Unknown"),
            "参考图": ui_text("参考图", "Reference"),
            "未确定": ui_text("未确定", "Unknown"),
            "标题字体": ui_text("标题字体", "Heading font"),
            "正文字体": ui_text("正文字体", "Body font"),
            "装饰字体": ui_text("装饰字体", "Decorative font"),
            "字体待确认": ui_text("字体待确认", "Font pending"),
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
        try:
            bundles = self._bundle_service().list_bundles(status="ready")
        except ManifestReadinessError:
            self.detail_label.setText(
                ui_text(
                    "素材已保存，Agent 接口待恢复",
                    "Assets are saved; Agent access is pending recovery",
                )
            )
            self.detail_label.show()
            return
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
                "Also preserve content_type; for image/gif, respect frame_count, duration_seconds, loop_count, and the confirmed role.",
                "For fonts, preserve font_usage, font_metadata, and font_annotation; verify the license before use.",
                "Fetch files through resolved_url or the MCP haypile_list_bundles tool.",
                "Do not read Haypile's local asset directory directly.",
            ]
        )

    def _bundle_for_item(self, item) -> dict[str, object]:
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
            "frame_count": None,
            "loop_count": None,
            "audio_metadata": {},
            "audio_tags": {},
            "audio_usage": "unknown",
        }

    def _bundle_service(self) -> BundleService:
        return BundleService(
            assets_dir=self.settings.ASSETS_DIR,
            manifest_path=self.settings.MANIFEST_PATH,
            themes_dir=self.settings.THEMES_DIR,
            runtime_db_path=self.settings.INDEX_DIR / "storage_runtime.db",
        )

    def _handoff_for_bundles(
        self,
        bundles: list[dict[str, object]],
        *,
        batch_id: str = "",
    ) -> dict[str, object]:
        base_url = self._base_url()
        try:
            readiness = read_manifest_readiness(self.settings.MANIFEST_PATH)
            manifest_generation = str(readiness["manifest_generation"])
        except ManifestReadinessError:
            manifest_generation = ""
        return build_asset_handoff(
            bundles,
            base_url=base_url,
            batch_id=batch_id or None,
            manifest_generation=manifest_generation,
        )

    @staticmethod
    def _handoff_asset(item: dict[str, object], base_url: str) -> dict[str, object]:
        return build_handoff_asset(item, base_url=base_url)

    def _base_url(self) -> str:
        host = self.settings.HOST if self.settings.HOST != "0.0.0.0" else "127.0.0.1"
        return f"http://{host}:{self.settings.PORT}"

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
        self._cancel_font_preview()
        self._stop_gif_preview(reset_to_first=True, discard=True)
        if self.confirmation_preview is not None:
            self.confirmation_preview.hide_preview()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self._cancel_font_preview()
        if self.font_preview_worker is not None and self.font_preview_worker.isRunning():
            self._font_close_pending = True
            event.ignore()
            self.hide()
            return
        self._stop_gif_preview(reset_to_first=True, discard=True)
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


class QuickMenuWindow(QWidget):
    """Explicitly opened management window; daily intake uses DesktopIntakeCard."""
    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or get_settings()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Haypile")
        self._action_callback = None
        self._close_callback = None
        self._attention_action = ""
        self._drawer_page = ""
        self._anchor = QRect()
        self._available = QRect(0, 0, 1280, 720)
        self._detail_buttons = {}
        self._ai_enabled = False
        self._low_power_enabled = False
        self._sound_enabled = True
        self._ai_provider_mode = "off"
        self._language_mode = "auto"
        self._progress_active = False
        self.feedback_card = None
        self._build_drawer()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.drawer_shell)
        self.resize(620, 520)
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self.feedback_label.hide)
        self._agent_timer = QTimer(self)
        self._agent_timer.setInterval(5000)
        self._agent_timer.timeout.connect(self.refresh_agent_status)

    def set_action_handler(self, callback) -> None:
        self._action_callback = callback

    def set_close_handler(self, callback) -> None:
        self._close_callback = callback

    def set_attention_action(self, action: str) -> None:
        self._attention_action = action

    def set_ai_enabled(self, enabled: bool, status_text: str = "") -> None:
        self._ai_enabled = enabled

    def _emit_action(self, action: str) -> None:
        if action == self._attention_action:
            self._attention_action = ""
        if self._action_callback is not None:
            self._action_callback(action)

    def _switch_page(self, page: str, *, animate: bool = False) -> None:
        self.drawer_stack.setCurrentWidget(self._pages[page])
        self.drawer_title.setText({"assets": ui_text("全部素材", "All assets"), "agent": "Agent",
                                   "settings": ui_text("设置", "Settings"), "ai": ui_text("AI 分拣", "AI sorting")}[page])

    def open_drawer(self, page: str, anchor: QRect | None = None, available: QRect | None = None) -> None:
        if page not in self._pages:
            return
        if self.feedback_card is not None:
            self.feedback_card.dismiss("management")
        self._drawer_page = page
        if anchor is not None:
            self._anchor = QRect(anchor)
        if available is not None:
            self._available = QRect(available)
        self._switch_page(page)
        self.resize(min(620, self._available.width() - 20), min(520, self._available.height() - 40))
        self.move(self._available.center() - self.rect().center())
        self.feedback_label.hide()
        self.progress_bar.hide()
        self.show()
        self.raise_()
        self.activateWindow()
        if page == "assets":
            self.material_panel.refresh()
        if page == "agent":
            self.refresh_agent_status()
            self._agent_timer.start()
        else:
            self._agent_timer.stop()

    def hide_menu(self) -> None:
        self._drawer_page = ""
        self._agent_timer.stop()
        self.hide()

    def closeEvent(self, event) -> None:
        self.hide_menu()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide_menu()
            event.accept()
        else:
            super().keyPressEvent(event)

    def current_page(self) -> str:
        return self._drawer_page

    def reposition(self, anchor: QRect, available: QRect, *, allow_flip: bool = True) -> None:
        self._anchor, self._available = QRect(anchor), QRect(available)
        if self.feedback_card is not None and self.feedback_card.isVisible():
            self.feedback_card.reposition()

    def show_feedback(self, message: str, tone: str, anchor: QRect, available: QRect) -> None:
        self._anchor, self._available = QRect(anchor), QRect(available)
        if self.isVisible():
            self.feedback_label.setText(message)
            foreground, background = _FEEDBACK_TONES.get(tone, _FEEDBACK_TONES["success"])
            self.feedback_label.setStyleSheet(f"color: {foreground}; background: {background}; padding: 5px;")
            self.feedback_label.setProperty("feedbackTone", tone)
            self.feedback_label.show()
            self._feedback_timer.start(4500 if tone == "error" else 1700)
        elif self.feedback_card is not None:
            self.feedback_card.show_message(message, tone)

    def begin_progress(self, anchor: QRect, available: QRect, text: str) -> None:
        self._progress_active = True
        self.show_feedback(text, "progress", anchor, available)

    def set_progress(self, percent: int, text: str) -> None:
        if self.isVisible():
            self.feedback_label.setText(text)
        elif self.feedback_card is not None and self.feedback_card.mode == "message" and self.feedback_card.isVisible():
            self.feedback_card.status.setText(text)

    def complete_progress(self, tone: str, message: str) -> None:
        self._progress_active = False
        # Completion feedback was already sent by the owner; never reopen dismissed UI.

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
        self.material_panel = MaterialPanelWindow(
            self.drawer_stack,
            embedded=True,
            settings=self.settings,
        )
        self.material_panel.paste_ingest_button.clicked.connect(
            lambda: self._emit_action("paste_ingest")
        )
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

        ai_row = QWidget(page)
        ai_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        ai_layout = QHBoxLayout(ai_row)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_layout.setSpacing(6)
        self.ai_settings_button = self._action_button(
            ui_text("AI 分拣", "AI sorting"),
            "ai_setup",
            ai_row,
        )
        self.ai_settings_button.setAccessibleDescription(
            ui_text("打开 AI 分拣设置", "Open AI sorting settings")
        )
        self.ai_state_badge = QLabel(ui_text("已关闭", "Off"), ai_row)
        self.ai_state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ai_state_badge.setMinimumWidth(58)
        self.ai_state_badge.setStyleSheet(
            "QLabel { color: #625B4C; background: #E9E4D4; border: 1px solid #DDD3BB; "
            "border-radius: 7px; padding: 4px 7px; font-size: 10px; font-weight: bold; }"
        )
        ai_layout.addWidget(self.ai_settings_button, 1)
        ai_layout.addWidget(self.ai_state_badge)
        layout.addWidget(ai_row)

        self.low_power_button = self._action_button(
            ui_text("低功耗：关", "Low power: off"),
            "low_power",
            page,
        )
        self.low_power_button.setCheckable(True)
        self.low_power_button.setAccessibleDescription(
            ui_text("减少持续动画并暂停 AI 分拣", "Reduce continuous motion and pause AI sorting")
        )
        layout.addWidget(self.low_power_button)

        self.sound_button = self._action_button(
            ui_text("操作音效：开", "Action sounds: on"),
            "sound",
            page,
        )
        self.sound_button.setCheckable(True)
        self.sound_button.setChecked(True)
        self.sound_button.setAccessibleDescription(
            ui_text(
                "播放导航、收纳、重复和拒绝反馈音效",
                "Play sound feedback for navigation, intake, duplicates, and rejection",
            )
        )
        layout.addWidget(self.sound_button)

        self.language_section = self._section_label(ui_text("语言", "Language"), page)
        layout.addWidget(self.language_section)
        language_row = QWidget(page)
        language_row.setStyleSheet("QWidget { background: transparent; border: none; }")
        language_layout = QHBoxLayout(language_row)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_layout.setSpacing(5)
        self.language_button_group = QButtonGroup(self)
        self.language_buttons: dict[str, QPushButton] = {}
        for mode, label in (("auto", ui_text("自动", "Auto")), ("zh", "简体中文"), ("en", "English")):
            button = self._action_button(label, f"language:{mode}", language_row)
            _configure_choice_button(button, self.language_button_group)
            self.language_buttons[mode] = button
            language_layout.addWidget(button)
        layout.addWidget(language_row)

        self.service_section = self._section_label(
            ui_text("服务与诊断", "Service & diagnostics"),
            page,
        )
        layout.addWidget(self.service_section)
        self.service_status_label = QLabel("", page)
        self.service_status_label.setWordWrap(True)
        self.service_status_label.setStyleSheet(
            "QLabel { color: #625B4C; font-size: 11px; padding: 7px 8px; background: #F6F1E4; border-radius: 7px; }"
        )
        layout.addWidget(self.service_status_label)
        self.diagnostics_button = self._action_button(
            ui_text("复制诊断摘要", "Copy diagnostic summary"),
            "diagnostics",
            page,
        )
        self.diagnostics_button.setAccessibleDescription(
            ui_text(
                "复制不含文件名、路径、网址或密钥的运行状态",
                "Copy runtime status without filenames, paths, URLs, or keys",
            )
        )
        layout.addWidget(self.diagnostics_button)
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
        self.ai_provider_button_group = QButtonGroup(self)
        self.ai_provider_buttons: dict[str, QPushButton] = {}
        for mode, label in (
            ("local", ui_text("本地模型", "Local model")),
            ("api", "API"),
            ("off", ui_text("关闭", "Off")),
        ):
            button = self._action_button(label, f"ai_provider:{mode}", provider_row)
            _configure_choice_button(button, self.ai_provider_button_group)
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
            field.setMinimumHeight(28)
            field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            field.setStyleSheet(_LINE_EDIT_STYLE)
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
        button.setMinimumHeight(30)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setStyleSheet(
            _ACTION_BUTTON_STYLE
            if not danger
            else _ACTION_BUTTON_STYLE.replace("color: #4E5F3D", "color: #9B4C37", 1)
        )
        button.clicked.connect(lambda _checked=False, selected=action: self._emit_action(selected))
        self._detail_buttons[action] = button
        return button


    def retranslate(self) -> None:
        button_labels = {
            "mcp": ui_text("复制 MCP 配置", "Copy MCP config"),
            "http": ui_text("复制 HTTP 地址", "Copy HTTP URL"),
            "latest_handoff": ui_text("复制最新批次 handoff", "Copy latest batch handoff"),
            "ready_handoff": ui_text("复制全部可用素材", "Copy all ready assets"),
            "agent_recipe": ui_text("复制 Agent 配方", "Copy Agent recipe"),
            "ai_setup": ui_text("AI 分拣", "AI sorting"),
            "diagnostics": ui_text("复制诊断摘要", "Copy diagnostic summary"),
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
        self.service_section.setText(ui_text("服务与诊断", "Service & diagnostics"))
        self.diagnostics_button.setAccessibleDescription(
            ui_text(
                "复制不含文件名、路径、网址或密钥的运行状态",
                "Copy runtime status without filenames, paths, URLs, or keys",
            )
        )
        self.low_power_button.setText(
            ui_text("低功耗：开", "Low power: on")
            if self._low_power_enabled
            else ui_text("低功耗：关", "Low power: off")
        )
        self.low_power_button.setChecked(self._low_power_enabled)
        self.sound_button.setText(
            ui_text("操作音效：开", "Action sounds: on")
            if self._sound_enabled
            else ui_text("操作音效：关", "Action sounds: off")
        )
        self.sound_button.setChecked(self._sound_enabled)
        self.sound_button.setAccessibleDescription(
            ui_text(
                "播放导航、收纳、重复和拒绝反馈音效",
                "Play sound feedback for navigation, intake, duplicates, and rejection",
            )
        )
        self._update_ai_nav_state(
            ai_enabled=self._ai_enabled,
            low_power=self._low_power_enabled,
            ai_provider=self._ai_provider_mode,
        )
        self.material_panel.retranslate()
        if self._drawer_page:
            self._switch_page(self._drawer_page, animate=False)
        self.refresh_agent_status()
        self.update()


    def refresh_agent_status(self) -> None:
        try:
            from mcp_server import active_mcp_sessions

            sessions = active_mcp_sessions(self.settings.INDEX_DIR)
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
        sound_enabled: bool,
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
        self.low_power_button.setChecked(low_power)
        self._low_power_enabled = bool(low_power)
        self.sound_button.setText(
            ui_text("操作音效：开", "Action sounds: on")
            if sound_enabled
            else ui_text("操作音效：关", "Action sounds: off")
        )
        self.sound_button.setChecked(sound_enabled)
        self._sound_enabled = bool(sound_enabled)
        self._language_mode = language
        self._ai_provider_mode = ai_provider
        self._update_ai_nav_state(
            ai_enabled=ai_enabled,
            low_power=low_power,
            ai_provider=ai_provider,
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
            button.setChecked(mode == ai_provider)
        self.service_status_label.setText(service_status)
        for mode, button in self.language_buttons.items():
            button.setChecked(mode == language)


    def _update_ai_nav_state(
        self,
        *,
        ai_enabled: bool,
        low_power: bool,
        ai_provider: str,
    ) -> None:
        if ai_enabled:
            label = ui_text("已开启", "On")
            foreground, background = _UI_MOSS, "#F3EDDA"
        elif low_power or ai_provider == "off":
            label = ui_text("已关闭", "Off")
            foreground, background = "#625B4C", "#E9E4D4"
        else:
            label = ui_text("需配置", "Setup needed")
            foreground, background = _UI_GOLD_TEXT, "#FFF2C4"
        self.ai_state_badge.setText(label)
        self.ai_state_badge.setStyleSheet(
            "QLabel { "
            f"color: {foreground}; background: {background}; "
            "border: 1px solid #DDD3BB; border-radius: 7px; "
            "padding: 4px 7px; font-size: 10px; font-weight: bold; }"
        )
        self.ai_settings_button.setAccessibleDescription(
            ui_text(
                f"打开 AI 分拣设置，当前{label}",
                f"Open AI sorting settings, currently {label}",
            )
        )




class DesktopIntakeCard(QWidget):
    """One opaque native rectangle, independent of the grass pile."""
    def __init__(self, ball) -> None:
        super().__init__(ball)
        self.ball = ball
        self.mode = ""
        self.candidates = []
        self._candidate_groups = {}
        self._candidate_positions = {}
        self.index = 0
        self.bundle = None
        self._drag_origin = None
        self._dragging = False
        self._exporting = False
        self.font_preview_worker = None
        self._preview_generation = 0
        self._preview_request = None
        self._click_counter = None
        self._escape_state = None
        if sys.platform == "darwin" and os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            try:
                framework = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
                self._click_counter = framework.CGEventSourceCounterForEventType
                self._click_counter.argtypes = [ctypes.c_int32, ctypes.c_uint32]
                self._click_counter.restype = ctypes.c_uint32
                self._escape_state = framework.CGEventSourceKeyState
                self._escape_state.argtypes = [ctypes.c_int32, ctypes.c_uint16]
                self._escape_state.restype = ctypes.c_bool
            except (AttributeError, OSError):
                logger.debug("Desktop input state is unavailable")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(ui_text("Haypile 收纳", "Haypile intake"))
        self.setObjectName("intakeCard")
        self.setStyleSheet(
            "QWidget#intakeCard { background: #FFF9EA; border: 1px solid #C4CCB8; }"
            "QLabel { color: #443F33; background: transparent; border: none; font-size: 12px; }"
            "QPushButton { color: #4E5F3D; background: transparent; border: 1px solid transparent; "
            "border-radius: 5px; padding: 0 4px; font-size: 12px; }"
            "QPushButton:hover, QPushButton:checked { background: #E9ECDD; }"
            "QPushButton:pressed { background: #DDE3D1; }"
            "QPushButton:focus { border: 2px solid #A67624; }"
            "QPushButton:disabled { color: #A9A08A; background: transparent; }"
            "QPushButton#cardGlyph { font-size: 20px; padding: 0; }"
            "QPushButton#cardPrimary { color: #FFF9EA; background: #4E5F3D; font-weight: 600; }"
            "QPushButton#cardPrimary:hover { background: #405032; }"
            "QPushButton#cardPrimary:pressed { background: #334128; }"
            "QPushButton#cardPrimary:disabled { color: #6B6656; background: #E9E4D4; }"
            "QPushButton#recentAsset { background: #F6F1E4; text-align: left; padding: 0 8px; }"
            "QPushButton#recentAsset:hover { background: #E9ECDD; }"
            "QTextEdit { color: #443F33; background: #F6F1E4; border: 1px solid transparent; "
            "border-radius: 5px; font-size: 12px; }"
            "QTextEdit:focus { border: 1px solid #A67624; }"
            "QScrollBar:vertical { width: 5px; background: transparent; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: #A9B49A; border-radius: 2px; min-height: 18px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setFixedHeight(24)
        self.title.setStyleSheet("font-size: 14px; font-weight: bold; color: #4E5F3D;")
        header.addWidget(self.title, 1)
        self.kind_button = self._button("", self._switch_candidate_kind)
        self.kind_button.setFixedSize(64, 24)
        header.addWidget(self.kind_button)
        self.close_button = self._button("×", lambda: self.dismiss("close"))
        self.close_button.setObjectName("cardGlyph")
        self.close_button.setFixedSize(24, 24)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self.preview = QLabel()
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(84)
        self.preview.setStyleSheet("background: #F6F1E4; border-radius: 5px;")
        self.preview.setAccessibleName(ui_text("原文件样张", "Original file sample"))
        layout.addWidget(self.preview)
        self.recent_rows = QWidget()
        self.recent_layout = QVBoxLayout(self.recent_rows)
        self.recent_layout.setContentsMargins(0, 0, 0, 0)
        self.recent_layout.setSpacing(6)
        layout.addWidget(self.recent_rows)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setFixedHeight(32)
        layout.addWidget(self.status)
        navigation = QHBoxLayout()
        navigation.setSpacing(4)
        self.previous_button = self._button("‹", lambda: self._navigate(-1))
        self.next_button = self._button("›", lambda: self._navigate(1))
        for button in (self.previous_button, self.next_button):
            button.setObjectName("cardGlyph")
            button.setFixedWidth(28)
        self.position = QLabel()
        self.position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position.setFixedWidth(44)
        self.position.setStyleSheet("color: #6B6656;")
        self.info_button = self._button(ui_text("信息", "Info"))
        self.info_button.setFixedWidth(44)
        self.info_button.setCheckable(True)
        self.info_button.toggled.connect(self._toggle_info)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.position)
        navigation.addWidget(self.next_button)
        navigation.addStretch()
        self.export_button = self._button(ui_text("导出", "Export"), self._export_bundle_font)
        self.export_button.setFixedWidth(52)
        self.export_button.setAccessibleName(ui_text("导出字体原文件", "Export original font"))
        self.export_button.setToolTip(self.export_button.accessibleName())
        navigation.addWidget(self.export_button)
        navigation.addWidget(self.info_button)
        layout.addLayout(navigation)
        self.information = QWidget()
        info_layout = QVBoxLayout(self.information)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(4)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setFixedHeight(75)
        self.details.document().setDocumentMargin(2)
        self.details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details.setAccessibleName(ui_text("来源、缺字与许可", "Source, missing glyphs and license"))
        info_layout.addWidget(self.details)
        self.usage_row = QWidget()
        usage_layout = QHBoxLayout(self.usage_row)
        usage_layout.setContentsMargins(0, 0, 0, 0)
        usage_layout.setSpacing(4)
        self.usage_buttons = {}
        self.usage_button_group = QButtonGroup(self)
        for usage, zh, en in [("heading", "标题", "Heading"), ("body", "正文", "Body"), ("decorative", "装饰", "Decorative")]:
            button = self._button(ui_text(zh, en))
            button.setFixedHeight(24)
            button.setCheckable(True)
            self.usage_button_group.addButton(button)
            button.toggled.connect(lambda checked, target=usage: self._set_usage(target) if checked else None)
            button.setAccessibleName(ui_text("字体用途：", "Font usage: ") + ui_text(zh, en))
            self.usage_buttons[usage] = button
            usage_layout.addWidget(button)
        info_layout.addWidget(self.usage_row)
        layout.addWidget(self.information)
        layout.addStretch()
        self.store_button = self._button(ui_text("收纳这款", "Store this font"), self._primary_action)
        self.store_button.setObjectName("cardPrimary")
        self.store_button.setFixedHeight(34)
        layout.addWidget(self.store_button)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.timeout.connect(lambda: self.store_button.setText(
            ui_text("复制原文件", "Copy original file")) if self.mode == "bundle" else None)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(lambda: self.dismiss("timeout"))
        self._outside_timer = QTimer(self)
        self._outside_timer.setInterval(50)
        self._outside_timer.timeout.connect(self._poll_outside)
        self._last_buttons = ()
        self._last_escape = False
        QApplication.instance().installEventFilter(self)
        QApplication.instance().applicationStateChanged.connect(self._application_state_changed)
        self._reset("", "")

    def _button(self, text: str, callback=None) -> QPushButton:
        button = _MaterialItemButton(text)
        button.setFixedHeight(26)
        if callback is not None:
            button.clicked.connect(lambda _checked=False: callback())
        return button

    def _reset(self, mode: str, title: str) -> None:
        self._copy_feedback_timer.stop()
        self._hide_timer.stop()
        self._cancel_preview()
        self.mode = mode
        self._drag_origin = None
        self._candidate_groups = {}
        self._candidate_positions = {}
        self.kind_button.hide()
        self.kind_button.setToolTip("")
        self.setWindowTitle(ui_text("Haypile 收纳", "Haypile intake"))
        self.close_button.setAccessibleName(ui_text("关闭", "Close"))
        self.close_button.setToolTip(ui_text("关闭 · Esc", "Close · Esc"))
        self.previous_button.setText("‹")
        self.previous_button.setStyleSheet("")
        self.previous_button.setFixedWidth(28)
        self.previous_button.setAccessibleName(ui_text("上一款", "Previous font"))
        self.previous_button.setToolTip(self.previous_button.accessibleName())
        self.next_button.setAccessibleName(ui_text("下一款", "Next font"))
        self.next_button.setToolTip(self.next_button.accessibleName())
        self.info_button.setText(ui_text("信息", "Info"))
        self.preview.setAccessibleName(ui_text("原文件样张", "Original file sample"))
        self.details.setAccessibleName(ui_text("来源、缺字与许可", "Source, missing glyphs and license"))
        for usage, button in self.usage_buttons.items():
            label = MaterialPanelWindow._font_usage_label(usage)
            button.setText(label)
            button.setAccessibleName(ui_text("字体用途：", "Font usage: ") + label)
        self.title.setText(title)
        self.title.setToolTip(_plain_tooltip(title))
        self.title.setAccessibleName(title)
        self.preview.clear()
        self.preview.unsetCursor()
        self.preview.setToolTip("")
        self.preview.setAccessibleName(ui_text("原文件样张", "Original file sample"))
        self.export_button.hide()
        self.store_button.setToolTip("")
        self.preview.setFixedHeight(84)
        self.status.setFixedHeight(32)
        self.preview.setVisible(mode in {"fonts", "images", "bundle"})
        self.recent_rows.setVisible(mode == "recent")
        self.previous_button.setVisible(mode in {"fonts", "images", "bundle"})
        self.next_button.setVisible(mode in {"fonts", "images"})
        self.position.setVisible(mode in {"fonts", "images"})
        self.info_button.setVisible(mode in {"fonts", "images", "bundle"})
        self.info_button.blockSignals(True)
        self.info_button.setChecked(False)
        self.info_button.blockSignals(False)
        self.information.hide()
        self.usage_row.hide()
        self.status.clear()
        self.status.setToolTip("")
        self.status.setStyleSheet("")
        self.status.show()
        self.store_button.setEnabled(True)
        self.store_button.show()

    def _show(self) -> None:
        self.details.setFixedHeight(47 if self.usage_row.isVisibleTo(self.information) else 75)
        height = 320 if self.information.isVisibleTo(self) else 240
        if self.mode == "message":
            height = 140 if self.store_button.isVisibleTo(self) else 100
        self.setFixedSize(280, height)
        self.reposition()
        self._last_buttons = self._desktop_buttons()
        self._last_escape = self._desktop_escape()
        self.show()
        self.raise_()
        self._outside_timer.start()

    def reposition(self) -> None:
        anchor = self.ball._ball_anchor_rect()
        available = self.ball._available_geometry().adjusted(8, 8, -8, -8)
        width, height = self.width(), self.height()
        options = [QPoint(anchor.right() + 12, anchor.center().y() - height // 2),
                   QPoint(anchor.left() - width - 12, anchor.center().y() - height // 2),
                   QPoint(anchor.center().x() - width // 2, anchor.top() - height - 12),
                   QPoint(anchor.center().x() - width // 2, anchor.bottom() + 12)]
        for point in options:
            rect = QRect(point, self.size())
            if available.contains(rect):
                self.move(point)
                return
        point = options[0] if anchor.center().x() < available.center().x() else options[1]
        self.move(max(available.left(), min(point.x(), available.right() - width + 1)),
                  max(available.top(), min(point.y(), available.bottom() - height + 1)))

    def dismiss(self, reason: str) -> None:
        if reason in {"close", "escape"} and self.mode == "message" and self.ball.remote_worker is not None:
            self.ball._cancel_remote_download()
        if self.mode in {"fonts", "images"} and reason != "store":
            self.ball._discard_web_candidates()
        self.candidates = []
        self._candidate_groups = {}
        self._candidate_positions = {}
        self._cancel_preview()
        self._copy_feedback_timer.stop()
        self.mode = ""
        self._drag_origin = None
        self.hide()
        self._hide_timer.stop()
        self._outside_timer.stop()

    def closeEvent(self, event) -> None:
        self.dismiss("close")
        event.accept()

    def show_message(self, message: str, tone: str) -> None:
        if self.ball._closing:
            return
        if self.isVisible() and self.mode in {"fonts", "images", "bundle", "recent"}:
            return
        self._reset("message", "Haypile")
        self.status.setText(message)
        self.status.setToolTip(message)
        foreground, background = _FEEDBACK_TONES.get(tone, _FEEDBACK_TONES["success"])
        if tone in {"progress", "pending"}:
            foreground = "#7D581B"
        self.status.setStyleSheet(f"color: {foreground}; background: {background}; border-radius: 5px; padding: 0 6px;")
        self.status.setProperty("feedbackTone", tone)
        self.store_button.setVisible(tone == "progress" and self.ball.remote_worker is not None or tone == "error" and bool(self.ball._last_remote_urls))
        self.store_button.setText(ui_text("取消查找", "Cancel search") if tone == "progress" else ui_text("重试", "Retry"))
        self._show()
        if tone != "progress":
            self._hide_timer.start(4500 if tone == "error" else 1700)

    def show_candidates(self, fonts: list[WebFontCandidate], images: list[WebImageCandidate] | None = None, note: str = "") -> None:
        self._reset("fonts" if fonts else "images", "")
        self._candidate_groups = {"fonts": list(fonts), "images": list(images or ())}
        self.candidates = self._candidate_groups[self.mode]
        self.kind_button.setVisible(bool(fonts and images))
        self.index = 0
        self._discovery_note = note
        self._show_candidate()
        self._show()

    def _switch_candidate_kind(self) -> None:
        target = "images" if self.mode == "fonts" else "fonts"
        if not self._candidate_groups.get(target):
            return
        self._candidate_positions[self.mode] = self.index
        self.mode = target
        self.candidates = self._candidate_groups[target]
        self.index = self._candidate_positions.get(target, 0)
        self._show_candidate()
        self._show()

    def _show_candidate(self) -> None:
        if not self.candidates:
            return
        candidate = self.candidates[self.index]
        metadata = candidate.metadata
        is_image = self.mode == "images"
        self.preview.setFixedHeight(100 if is_image else 84)
        self.status.setFixedHeight(16 if is_image else 32)
        self.previous_button.setAccessibleName(ui_text("上一张" if is_image else "上一款", "Previous image" if is_image else "Previous font"))
        self.next_button.setAccessibleName(ui_text("下一张" if is_image else "下一款", "Next image" if is_image else "Next font"))
        self.previous_button.setToolTip(self.previous_button.accessibleName())
        self.next_button.setToolTip(self.next_button.accessibleName())
        self.kind_button.setText(ui_text("字体" if is_image else "图片", "Fonts" if is_image else "Images"))
        count = len(self._candidate_groups.get("fonts" if is_image else "images", []))
        kind_hint = ui_text(f"查看网页中的{'字体' if is_image else '图片'}（{count}）", f"Show page {'fonts' if is_image else 'images'} ({count})")
        self.kind_button.setToolTip(kind_hint)
        self.kind_button.setAccessibleName(kind_hint)
        self.details.setAccessibleName(ui_text("图片来源与尺寸", "Image source and dimensions") if is_image else ui_text("来源、缺字与许可", "Source, missing glyphs and license"))
        if is_image:
            name = candidate.label or {"cover": ui_text("网页封面", "Page cover"), "image": ui_text("网页图片", "Page image"), "icon": ui_text("站点图标", "Site icon")}[candidate.hint]
        else:
            name = f"{metadata.get('family') or candidate.path.stem} · {metadata.get('style') or 'Regular'}"
        self.title.setText(self.title.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 154 if self.kind_button.isVisibleTo(self) else 224))
        self.title.setToolTip(_plain_tooltip(name))
        self.title.setAccessibleName(name)
        self._set_preview(QImage(candidate.pixels, candidate.width, candidate.height, QImage.Format.Format_RGBA8888).copy())
        self.position.setText(f"{self.index + 1} / {len(self.candidates)}")
        self.position.setAccessibleName(ui_text(
            f"第 {self.index + 1} 张，共 {len(self.candidates)} 张" if is_image else f"第 {self.index + 1} 款，共 {len(self.candidates)} 款",
            f"{'Image' if is_image else 'Font'} {self.index + 1} of {len(self.candidates)}",
        ))
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index + 1 < len(self.candidates))
        self.store_button.setText(ui_text("收纳这张", "Store this image") if is_image else ui_text("收纳这款", "Store this font"))
        self.store_button.setEnabled(self.ball.worker is None)
        if is_image:
            dimensions = f"{metadata['width']} × {metadata['height']}"
            self.status.setText(dimensions + " · " + metadata["mime_type"].split("/")[-1].replace("svg+xml", "svg").upper())
            self.status.setToolTip(self.status.text())
            self.details.setPlainText(name + "\n" + ui_text("来源：", "Source: ") + candidate.origin + "\n" + dimensions + " · " + metadata["mime_type"]
                                      + "\n" + ui_text("许可未核实", "License unverified")
                                      + ("\n" + ui_text("GIF 仅预览首帧，收纳保留动图原文件", "GIF preview uses the first frame; the original animation is preserved") if metadata["mime_type"] == "image/gif" else "")
                                      + ("\n" + self._discovery_note if self._discovery_note else ""))
        else:
            self._font_details(metadata, candidate.origin, candidate.path.suffix, candidate.missing, candidate.hint)

    def _set_preview(self, image: QImage) -> None:
        # ponytail: retain 2× pixels across displays; raise density only for higher-DPI targets.
        pixmap = QPixmap.fromImage(image).scaled(508, self.preview.height() * 2,
                    Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(2)
        self.preview.setPixmap(pixmap)

    def _font_details(self, metadata, origin: str, suffix: str, missing: str = "", hint: str = "") -> None:
        license_text = str(metadata.get("license_description") or metadata.get("license_url") or "")
        status = ui_text("缺字：", "Missing: ") + missing if missing else ui_text("样张字符齐全", "Sample glyphs covered")
        status += " · " + ui_text("内嵌许可已记录" if license_text else "许可未知", "Embedded license recorded" if license_text else "License unknown")
        self.status.setText(status)
        self.status.setToolTip(status)
        self.details.setPlainText(str(metadata.get("family") or "") + " · " + str(metadata.get("style") or "Regular")
                                  + "\n" + ui_text("来源：", "Source: ") + (origin or ui_text("未记录", "Unrecorded"))
                                  + "\n" + suffix.lstrip('.').upper() + (" · " + hint if hint else "")
                                  + "\n" + status
                                  + "\n" + ui_text("内嵌许可：", "Embedded license: ") + (license_text[:8192] or ui_text("未知", "Unknown"))
                                  + ("\n" + self._discovery_note if self.mode == "fonts" and self._discovery_note else ""))

    def _navigate(self, step: int) -> None:
        if self.mode == "bundle":
            self.show_recent()
        elif self.mode in {"fonts", "images"}:
            self.index = max(0, min(self.index + step, len(self.candidates) - 1))
            self._show_candidate()

    def _toggle_info(self, checked: bool) -> None:
        if self.mode not in {"fonts", "images", "bundle"}:
            return
        self.information.setVisible(checked)
        self._show()

    def _primary_action(self) -> None:
        if self.mode in {"fonts", "images"} and self.candidates:
            self.ball._collect_web_asset(self.candidates[self.index])
        elif self.mode == "message":
            if self.ball.remote_worker is not None:
                self.ball._cancel_remote_download()
            elif self.ball._last_remote_urls and not self.ball._ingest_busy():
                self.ball._start_remote_download_worker(self.ball._last_remote_urls)
        elif self.mode == "bundle":
            self._copy_bundle_original()
        elif self.mode == "recent":
            self.ball._handle_quick_menu_action("assets")

    def show_recent(self) -> None:
        self._reset("recent", ui_text("最近收纳", "Recent assets"))
        while self.recent_layout.count():
            item = self.recent_layout.takeAt(0)
            item.widget().deleteLater()
        try:
            service = self.ball._bundle_service()
            bundles = service.list_recent_bundles()
            for bundle in bundles[:3]:
                metadata = bundle.get("font_metadata") or {}
                name = str(metadata.get("family") or Path(str(bundle["source_key"])).name)
                if metadata.get("family") and metadata.get("style"):
                    name += " · " + str(metadata["style"])
                label = self.title.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 240)
                button = self._button(label.replace("&", "&&"), lambda target=bundle: self.show_bundle(target))
                if bundle.get("type") == "image" and bundle.get("status") != "missing":
                    path = service._asset_path(str(bundle["source_key"]))
                    if path is not None:
                        reader = QImageReader(str(path))
                        if reader.size().isValid():
                            reader.setScaledSize(reader.size().scaled(QSize(56, 56), Qt.AspectRatioMode.KeepAspectRatio))
                        thumbnail = reader.read()
                        if not thumbnail.isNull():
                            button.setIcon(QIcon(QPixmap.fromImage(thumbnail)))
                            button.setIconSize(QSize(28, 28))
                            label = self.title.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 202)
                            button.setText(label.replace("&", "&&"))
                button.setObjectName("recentAsset")
                button.setToolTip(_plain_tooltip(name))
                button.setAccessibleName(name)
                button.setFixedHeight(38)
                self.recent_layout.addWidget(button)
            self.status.setVisible(not bundles)
            self.status.setText("" if bundles else ui_text("拖入文件或网页链接开始收纳", "Drop a file or page link to start"))
        except (ManifestReadinessError, OSError, ValueError):
            self.status.setText(ui_text("素材目录待恢复，请稍后重试", "Catalog is recovering; try again shortly"))
        self.store_button.setText(ui_text("全部素材", "All assets"))
        self._show()

    def show_bundle(self, bundle) -> None:
        self._reset("bundle", "")
        self.bundle = bundle
        self.previous_button.setText(ui_text("返回", "Back"))
        self.previous_button.setStyleSheet("font-size: 12px;")
        self.previous_button.setFixedWidth(58)
        self.previous_button.setAccessibleName(ui_text("返回最近收纳", "Back to recent assets"))
        self.previous_button.setToolTip(self.previous_button.accessibleName())
        self.previous_button.setEnabled(True)
        self.store_button.setText(ui_text("复制原文件", "Copy original file"))
        self.store_button.setToolTip(ui_text("粘贴到支持文件的应用或文件夹", "Paste into an app or folder that accepts files"))
        path = self.ball._bundle_service()._asset_path(str(bundle["source_key"]))
        metadata = bundle.get("font_metadata") or {}
        name = str(metadata.get("family") or Path(str(bundle["source_key"])).name)
        self.title.setText(self.title.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 224))
        self.title.setToolTip(_plain_tooltip(name))
        self.title.setAccessibleName(name)
        if bundle.get("type") == "font":
            self.export_button.setText(ui_text("导出", "Export"))
            self.export_button.setAccessibleName(ui_text("导出字体原文件", "Export original font"))
            self.export_button.setToolTip(self.export_button.accessibleName())
            self.export_button.show()
            self._font_details(metadata, str(bundle.get("origin_url") or ""), path.suffix if path is not None else "")
            self.status.setText(ui_text("正在检查样张…", "Checking sample…"))
            self.status.setToolTip(self.status.text())
            self._refresh_usage()
            self.usage_row.show()
            if path is not None and bundle.get("status") != "missing":
                self._preview_request = (self._preview_generation, bundle["id"], path, WEB_FONT_PREVIEW_TEXT, 64, bundle["sha256"])
                self.preview.setText(ui_text("正在预览…", "Rendering…"))
                self._start_preview()
        else:
            self.status.setText(ui_text("待确认" if bundle.get("status") == "pending" else "已收纳", "Pending" if bundle.get("status") == "pending" else "Stored"))
            self.details.setPlainText(str(bundle.get("origin_url") or ui_text("未记录来源", "Source unrecorded")))
            if path is not None and bundle.get("type") == "image":
                reader = QImageReader(str(path))
                if reader.size().isValid():
                    reader.setScaledSize(reader.size().scaled(QSize(508, 168), Qt.AspectRatioMode.KeepAspectRatio))
                self._set_preview(reader.read())
            else:
                self.preview.setText(ui_text("音频素材", "Audio asset"))
        if bundle.get("status") == "missing":
            self.preview.setText(ui_text("素材副本缺失", "Asset copy missing"))
            self.status.setText(ui_text("素材副本缺失", "Asset copy missing"))
        available = path is not None and path.is_file() and bundle.get("status") != "missing"
        self.store_button.setEnabled(available)
        self.export_button.setEnabled(available)
        if available:
            self.preview.setCursor(Qt.CursorShape.OpenHandCursor)
            hint = ui_text("拖出原文件，或点击下方复制", "Drag out the original file, or copy it below")
            self.preview.setToolTip(hint)
            self.preview.setAccessibleName(hint)
        self._show()

    def _stored_bundle_source(self) -> Path | None:
        if self.mode != "bundle" or self.ball._closing:
            return None
        try:
            service = self.ball._bundle_service()
            current = service.get_bundle(str(self.bundle["id"]))
            path = service._asset_path(str(current["source_key"])) if current else None
            if current is None or current["status"] == "missing" or path is None or not path.is_file():
                raise ValueError("asset_unavailable")
            return path
        except (ManifestReadinessError, OSError, ValueError):
            self.status.setText(ui_text("原文件不可用，请返回最近收纳后重试", "Original file unavailable; return to recent assets and retry"))
            self.status.setToolTip(self.status.text())
            return None

    def _copy_bundle_original(self) -> None:
        path = self._stored_bundle_source()
        if path is None:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        QApplication.clipboard().setMimeData(mime)
        self.store_button.setText(ui_text("已复制原文件", "Original file copied"))
        self._copy_feedback_timer.start(1400)

    def _drag_bundle_original(self) -> None:
        path = self._stored_bundle_source()
        if path is None:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        drag = QDrag(self.preview)
        drag.setMimeData(mime)
        pixmap = self.preview.pixmap()
        if pixmap and not pixmap.isNull():
            drag.setPixmap(pixmap.scaled(160, 100, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        card_generation = self._preview_generation
        self._dragging = True
        try:
            copied = execute_copy_drag(drag, self)
        finally:
            self._dragging = False
            self._last_buttons, self._last_escape = self._desktop_buttons(), self._desktop_escape()
        if copied and card_generation == self._preview_generation:
            self.dismiss("drag")

    def _export_bundle_font(self) -> None:
        if self.mode != "bundle" or self.bundle.get("type") != "font":
            return
        source = self._stored_bundle_source()
        if source is None:
            return
        self._exporting = True
        try:
            target, _filter = QFileDialog.getSaveFileName(
                self, ui_text("导出字体原文件", "Export original font"), source.name,
                f"{source.suffix.upper()[1:]} (*{source.suffix})",
            )
            if not target:
                return
            settings = self.ball.settings
            export_font_original(source, Path(target), sha256=str(self.bundle["sha256"]), storage_dir=settings.STORAGE_DIR,
                                 protected_paths=(settings.ASSETS_DIR, settings.INDEX_DIR, settings.THEMES_DIR, settings.MANIFEST_PATH))
        except (OSError, ValueError):
            self.status.setText(ui_text("导出失败，目标原文件已保留；请选择素材库外的位置", "Export failed; destination retained. Choose a location outside the library."))
        else:
            self.status.setText(ui_text("已导出字体原文件", "Original font exported"))
        finally:
            self._exporting = False
            self._last_buttons, self._last_escape = self._desktop_buttons(), self._desktop_escape()
            self.status.setToolTip(self.status.text())

    def _refresh_usage(self) -> None:
        self.usage_button_group.setExclusive(False)
        for button in self.usage_buttons.values():
            button.blockSignals(True)
        for usage, button in self.usage_buttons.items():
            button.setChecked(self.bundle.get("font_usage") == usage)
            button.setEnabled(self.bundle.get("status") != "missing")
        for button in self.usage_buttons.values():
            button.blockSignals(False)
        self.usage_button_group.setExclusive(True)

    def _set_usage(self, usage: str) -> None:
        if self.mode != "bundle" or self.bundle.get("type") != "font":
            return
        try:
            updated = self.ball._bundle_service().update_bundle_font(self.bundle["id"], font_usage=usage)
        except (OSError, TimeoutError, ValueError, ManifestReadinessError):
            updated = None
        if updated is None:
            self.status.setText(ui_text("用途保存失败，请重试", "Usage could not be saved; retry"))
        else:
            self.bundle = updated
            self.status.setText(ui_text("用途已确认：", "Usage confirmed: ") + MaterialPanelWindow._font_usage_label(usage))
            self.ball._refresh_pending_badge()
        self.status.setToolTip(self.status.text())
        self._refresh_usage()

    def _cancel_preview(self) -> None:
        self._preview_generation += 1
        self._preview_request = None
        if self.font_preview_worker is not None:
            self.font_preview_worker.requestInterruption()

    def _start_preview(self) -> None:
        if self.font_preview_worker is not None or self._preview_request is None:
            return
        self.font_preview_worker = FontPreviewWorker(self._preview_request, self, preview_width=508)
        self._preview_request = None
        self.font_preview_worker.finished.connect(self._finish_preview)
        self.font_preview_worker.start()

    def _finish_preview(self) -> None:
        worker = self.font_preview_worker
        self.font_preview_worker = None
        if worker.request[0] == self._preview_generation and self.mode == "bundle":
            if worker.result is not None:
                pixels, width, height, missing = worker.result
                self._set_preview(QImage(pixels, width, height, QImage.Format.Format_RGBA8888).copy())
                self._font_details(self.bundle.get("font_metadata") or {}, str(self.bundle.get("origin_url") or ""), worker.request[2].suffix, missing)
            elif worker.error:
                self.preview.setText(ui_text("字体预览失败", "Font preview failed"))
                self.status.setText(ui_text("无法检查样张字符", "Sample glyphs could not be checked"))
                self.status.setToolTip(self.status.text())
        worker.deleteLater()
        self._start_preview()

    def _desktop_buttons(self):
        if self._click_counter is not None:
            return tuple(self._click_counter(0, event_type) for event_type in (1, 3, 25))
        if sys.platform.startswith("win"):
            return tuple(ctypes.windll.user32.GetAsyncKeyState(key) for key in (1, 2, 4))
        return ()

    def _desktop_escape(self) -> bool:
        return bool(self._escape_state and self._escape_state(0, 53))

    def _poll_outside(self) -> None:
        if self._exporting or self._dragging:
            return
        # ponytail: poll native counters only while visible; no event tap or input-capturing overlay.
        buttons, escape = self._desktop_buttons(), self._desktop_escape()
        outside = not self.frameGeometry().contains(QCursor.pos()) and not self.ball.frameGeometry().contains(QCursor.pos())
        if escape and not self._last_escape:
            self.dismiss("escape")
        elif buttons != self._last_buttons and outside:
            self.dismiss("outside")
        self._last_buttons, self._last_escape = buttons, escape
        if self.mode in {"fonts", "images"}:
            self.store_button.setEnabled(self.ball.worker is None and not self.ball._closing)

    def _application_state_changed(self, state) -> None:
        if not (self._exporting or self._dragging) and state == Qt.ApplicationState.ApplicationInactive and self.isVisible():
            self.dismiss("outside")

    def eventFilter(self, watched, event) -> bool:
        if self.isVisible() and not (self._exporting or self._dragging):
            if watched is self.preview and self.mode == "bundle":
                if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                    self._drag_origin = event.position().toPoint()
                elif event.type() == QEvent.Type.MouseMove and self._drag_origin is not None:
                    if event.buttons() & Qt.MouseButton.LeftButton and (event.position().toPoint() - self._drag_origin).manhattanLength() >= QApplication.startDragDistance():
                        self._drag_origin = None
                        self._drag_bundle_original()
                        return True
                elif event.type() == QEvent.Type.MouseButtonRelease:
                    self._drag_origin = None
            if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.dismiss("escape")
            elif event.type() == QEvent.Type.MouseButtonPress and isinstance(watched, QWidget):
                if watched.window() not in {self, self.ball}:
                    self.dismiss("outside")
        return False


class HaypileFloatingBall(QWidget):
    shutdown_requested = Signal()
    shutdown_ready = Signal()

    COLLAPSED_SIZE = 72
    EXPANDED_SIZE = 300
    GIF_TICK_SECONDS = INTAKE_GIF_TICK_SECONDS
    GIF_FRAME_STAGGER_SECONDS = INTAKE_GIF_FRAME_STAGGER_SECONDS
    GIF_EXPAND_SECONDS = INTAKE_GIF_EXPAND_SECONDS
    GIF_OPEN_MS = 210
    GIF_SUCTION_MS = round(INTAKE_GIF_SUCTION_SECONDS * 1000)
    GIF_CLOSE_MS = 170

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        initial_state: dict[str, object] | None = None,
        deferred_runtime: bool = False,
        managed_shutdown: bool = False,
    ) -> None:
        super().__init__()
        self._managed_shutdown = bool(managed_shutdown)
        self.settings = settings or get_settings()
        self.project_root: Path = self.settings.BASE_DIR
        self.assets_dir: Path = self.settings.ASSETS_DIR
        self.themes_dir: Path = self.settings.THEMES_DIR
        self.manifest_path: Path = self.settings.MANIFEST_PATH
        self._desktop_runtime_applied = not deferred_runtime
        self._storage_ready = not deferred_runtime
        self._storage_error_code = ""
        self._backend_phase = "idle"
        self._service_status_override = (
            ui_text("正在准备素材库", "Preparing asset library")
            if deferred_runtime
            else ""
        )
        self.haypile_icon = QPixmap(str(self.project_root / "ui_assets" / "haypile-icon.png"))
        self._haypile_alpha_image = self.haypile_icon.toImage()
        self._haypile_glow_pixmap = self._tinted_haypile_pixmap(QColor("#FFD66D"))
        self._haypile_direction_glow_pixmap = self._tinted_haypile_pixmap(QColor("#FFF1B2"))
        self._haypile_exit_glow_pixmap = self._tinted_haypile_pixmap(QColor("#9B4C37"))
        self._intake_renderer = IntakeEntryRenderer(self.project_root / "ui_assets")
        self._gui_state_path = self.settings.INDEX_DIR / "gui_state.json"

        stored_state = dict(initial_state or {})
        self.language_mode = str(stored_state.get("language") or "auto")
        if self.language_mode not in {"auto", "zh", "en"}:
            self.language_mode = "auto"
        set_ui_language(self.language_mode)
        self.low_power_enabled = bool(
            stored_state.get("low_power_enabled", self.settings.HAYPILE_LOW_POWER_MODE)
        )
        stored_sound_enabled = stored_state.get("sound_enabled", True)
        self.sound_enabled = (
            stored_sound_enabled
            if isinstance(stored_sound_enabled, bool)
            else False
        )
        self._sound_feedback = SoundFeedbackController(
            self.project_root / "ui_assets" / "sounds",
            enabled=self.sound_enabled,
            parent=self,
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
        self.ai_api_key_present = False
        self._session_api_key = ""

        self.worker: IngestWorker | None = None
        self.remote_worker: RemoteDownloadWorker | None = None
        self._web_candidates: list[WebFontCandidate | WebImageCandidate] = []
        self._remote_request_cancelled = False
        self._last_remote_urls: list[str] = []
        self._remote_ingest_paths: set[Path] = set()
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
        self._drop_feedback_anchor_global: QRect | None = None
        self._drop_visual_kind = "leaf"
        self._audio_suction_progress = 0.0
        self._audio_suction_animation: QVariantAnimation | None = None
        self._gif_suction_progress = 0.0
        self._gif_suction_animation: QVariantAnimation | None = None
        self._gif_expand_started_at = 0.0
        self._active_ingest_visual_kind = "leaf"
        self._ingest_feedback_not_before = 0.0
        self._pending_ingest_finish: tuple[str, bool, IngestResult | None] | None = None
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
        self._shutdown_ready_emitted = False
        self._shutdown_started_at = 0.0
        self._shutdown_wait_notice_shown = False
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
        self._gif_open_timer = QTimer(self)
        self._gif_open_timer.setSingleShot(True)
        self._gif_open_timer.setInterval(self.GIF_OPEN_MS)
        self._gif_open_timer.timeout.connect(self._animate_gif_suction)
        self._ingest_feedback_timer = QTimer(self)
        self._ingest_feedback_timer.setSingleShot(True)
        self._ingest_feedback_timer.timeout.connect(self._flush_pending_ingest_finish)
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
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(100)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)

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
        self.setAcceptDrops(self._storage_ready)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.resize(self.COLLAPSED_SIZE, self.COLLAPSED_SIZE)
        self.move(self._window_position_from_state(stored_state))
        self._update_window_mask()

        self.quick_menu = QuickMenuWindow(self.settings)
        self.quick_menu.setParent(self, self.quick_menu.windowFlags())
        self.intake_card = DesktopIntakeCard(self)
        self.quick_menu.feedback_card = self.intake_card
        self.material_panel = self.quick_menu.material_panel
        self.material_panel.set_toast_handler(self.show_toast)
        self.material_panel.set_low_power_enabled(self.low_power_enabled)
        self.material_panel.set_ai_handlers(
            provider_factory=self._current_ai_provider_config,
            enabled_callback=lambda: self.ai_enabled and not self.low_power_enabled,
            retry_batch_callback=self._retry_latest_ai_batch,
        )
        self.quick_menu.set_action_handler(self._handle_quick_menu_action)
        self.quick_menu.set_close_handler(self._close_attached_ui)
        self._refresh_ai_menu_status(
            service_status=(
                self._service_status_override
                or ui_text("素材状态待刷新", "Asset status pending refresh")
            )
        )

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

            gif_elapsed = (
                max(0.0, time.monotonic() - self._gif_expand_started_at)
                if self._gif_expand_started_at > 0.0
                else 0.0
            )
            self._intake_renderer.paint(
                painter,
                QRectF(self.rect()),
                panel_rect,
                IntakeVisualState(
                    kind=self._drop_visual_kind,
                    open_progress=progress,
                    audio_suction_progress=self._audio_suction_progress,
                    gif_suction_progress=self._gif_suction_progress,
                    gif_elapsed_seconds=gif_elapsed,
                    direction_angle=self._drag_awareness_angle,
                    has_direction=self._drag_awareness_has_direction,
                    low_power=self.low_power_enabled,
                ),
                device_pixel_ratio=self.devicePixelRatioF(),
            )

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
        self._drop_feedback_anchor_global = None
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_window_mask()
        self._reposition_quick_menu()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if hasattr(self, "quick_menu"):
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
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()
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
        if not self._storage_ready:
            event.ignore()
            return
        files = self._extract_local_files(event.mimeData())
        remote_urls = self._extract_remote_media_urls(event.mimeData())
        if files or remote_urls:
            self._close_attached_ui()
            self._cancel_audio_suction()
            self._cancel_gif_suction()
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
        self._abandon_drag_hover_drop()

    def _abandon_drag_hover_drop(self) -> None:
        self._drag_hover = False
        self._drag_prepare_active = False
        self._drag_prepare_timer.stop()
        self._cancel_audio_suction()
        self._cancel_gif_suction()
        self._close_drop_target()
        self._sync_visual_timer()
        self.update()

    def dropEvent(self, event: QDropEvent) -> None:
        if not self._ensure_storage_ready():
            event.ignore()
            return
        mime_data = event.mimeData()
        route = resolve_intake_route(
            local_files=self._extract_local_files(mime_data),
            remote_urls=self._extract_remote_media_urls(mime_data),
            raw_gif=None,
            has_image=False,
        )
        self._drop_visual_kind = self._drop_visual_kind_for_mime_data(mime_data)
        self._close_attached_ui()
        self._drag_hover = False
        self._clear_external_drag_candidate()
        self._drag_prepare_active = False
        self._drag_prepare_timer.stop()
        self._collapse_timer.stop()
        event.acceptProposedAction()
        if self._drop_visual_kind == "audio" and self._drop_open_progress > 0.05:
            self._animate_audio_suction()
        elif self._drop_visual_kind == "gif" and self._drop_open_progress > 0.05:
            self._animate_gif_suction()
        else:
            self._close_drop_target()
        self._sync_visual_timer()

        if route.name == ROUTE_UNSUPPORTED:
            self._show_intake_error(
                ui_text(
                    "没有找到可收纳的图片、音频或字体",
                    "No images, audio, or fonts to store",
                )
            )
            return
        if self._ingest_busy():
            self.show_toast(ui_text("正在入库中，请稍后", "Import in progress"), tone="progress")
            return
        if route.has_remote:
            self._start_remote_download_worker(list(route.remote_urls), list(route.local_files))
            return
        self._drop_feedback_until = time.monotonic() + 0.65
        self._sync_visual_timer()
        self.update()
        self._start_worker(list(route.local_files))

    def _open_drop_target(self) -> None:
        if not self._drag_hover:
            return
        self._drag_prepare_active = False
        if self._drop_visual_kind == "gif":
            self._gif_expand_started_at = time.monotonic()
        self._capture_drop_anchors()
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
            # macOS/Qt can skip dragLeave when the drag ends outside the widget.
            self._force_end_drag_hover()
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

    def _force_end_drag_hover(self) -> None:
        """Close an open drop face if the pointer drag ended without dragLeave."""
        if not (self._drag_hover or self._drag_prepare_active):
            return
        self._abandon_drag_hover_drop()

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
        if not self._cleanup_done:
            event.ignore()
            self.shutdown()
            return
        event.accept()
        super().closeEvent(event)

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
            urls.extend(value for value in (parser.urls or parser.link_urls) if HaypileFloatingBall._is_http_url(value))
        if mime_data.hasText():
            urls.extend(value for value in mime_data.text().split() if HaypileFloatingBall._is_http_url(value))
        return RemoteDownloadWorker._dedupe_urls(urls)

    @staticmethod
    def _clipboard_gif_payload(mime_data) -> bytes | None:
        found = False
        # ponytail: add app-private clipboard formats only after observing a real sample.
        for mime_type in ("image/gif", "com.compuserve.gif"):
            if not mime_data.hasFormat(mime_type):
                continue
            found = True
            payload = mime_data.data(mime_type)
            if payload:
                return bytes(payload)
        return b"" if found else None

    def _new_clipboard_temp(self, suffix: str) -> tuple[Path, int]:
        incoming_dir = self.settings.STORAGE_DIR / "incoming" / "browser"
        incoming_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            incoming_dir.chmod(0o700)
        path = incoming_dir / f"clipboard_{uuid4().hex}{suffix}"
        return path, os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def _write_clipboard_bytes(self, payload: bytes, suffix: str) -> Path:
        path, fd = self._new_clipboard_temp(suffix)
        try:
            with os.fdopen(fd, "wb") as target:
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            path.unlink(missing_ok=True)
            raise
        return path

    def _write_clipboard_image(self, image) -> Path:
        path, fd = self._new_clipboard_temp(".png")
        os.close(fd)
        try:
            if not image.save(str(path), "PNG"):
                raise OSError("clipboard_image_save_failed")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def _write_clipboard_mime_image(self, mime_data) -> Path:
        image = mime_data.imageData()
        width = int(image.width())
        height = int(image.height())
        if (
            image.isNull()
            or width <= 0
            or height <= 0
            or width > MAX_RASTER_DIMENSION
            or height > MAX_RASTER_DIMENSION
            or width * height > MAX_RASTER_PIXELS
        ):
            raise ValueError("clipboard_image_dimensions")
        return self._write_clipboard_image(image)

    def _ingest_clipboard(self) -> None:
        self._ingest_clipboard_data(QApplication.clipboard().mimeData())

    def _ingest_clipboard_data(self, mime_data) -> None:
        if self._closing:
            return
        if not self._ensure_storage_ready():
            return
        if self._ingest_busy():
            self.show_toast(ui_text("正在入库中，请稍后", "Import in progress"), tone="progress")
            return

        route = resolve_intake_route(
            local_files=self._extract_local_files(mime_data),
            remote_urls=self._extract_remote_media_urls(mime_data),
            raw_gif=self._clipboard_gif_payload(mime_data),
            has_image=bool(mime_data.hasImage()),
        )
        if route.name in {ROUTE_LOCAL_FILES, ROUTE_LOCAL_FILES_AND_REMOTE_URLS}:
            if route.has_remote:
                self._start_remote_download_worker(
                    list(route.remote_urls),
                    list(route.local_files),
                )
            else:
                self._start_worker(list(route.local_files))
            return

        if route.name == ROUTE_RAW_GIF:
            gif_payload = route.raw_gif or b""
            if len(gif_payload) > MAX_GIF_BYTES:
                self._show_intake_error(
                    ui_text("GIF 素材超过 50MB", "GIF asset is over 50MB")
                )
                return
            try:
                path = self._write_clipboard_bytes(gif_payload, ".gif")
            except Exception:
                logger.exception("Failed to materialize clipboard GIF")
                self._show_intake_error(
                    ui_text(
                        "无法保存剪贴板 GIF",
                        "Clipboard GIF could not be saved",
                    )
                )
                return
            self._remote_ingest_paths.add(path)
            self._start_worker([path])
            return

        if route.name == ROUTE_EMPTY_GIF:
            self._show_intake_error(
                ui_text("剪贴板中的 GIF 为空", "Clipboard GIF is empty")
            )
            return

        if route.name in {ROUTE_REMOTE_URL, ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK}:
            fallback_file = None
            if route.name == ROUTE_REMOTE_URL_WITH_STATIC_PNG_FALLBACK:
                try:
                    fallback_file = self._write_clipboard_mime_image(mime_data)
                except ValueError:
                    pass
                except Exception:
                    logger.exception("Failed to materialize clipboard image fallback")
                if fallback_file is not None:
                    self._remote_ingest_paths.add(fallback_file)
            if fallback_file is None:
                self._start_remote_download_worker(list(route.remote_urls))
            else:
                self._start_remote_download_worker(
                    list(route.remote_urls),
                    fallback_file=fallback_file,
                )
            return

        if route.name == ROUTE_STATIC_PNG:
            try:
                path = self._write_clipboard_mime_image(mime_data)
            except ValueError:
                self._show_intake_error(
                    ui_text(
                        "剪贴板图片尺寸无效或过大",
                        "Clipboard image dimensions are invalid or too large",
                    )
                )
                return
            except Exception:
                logger.exception("Failed to materialize clipboard image")
                self._show_intake_error(
                    ui_text(
                        "无法保存剪贴板图片",
                        "Clipboard image could not be saved",
                    )
                )
                return
            self._remote_ingest_paths.add(path)
            self._start_worker([path])
            self.show_toast(
                ui_text(
                    "剪贴板只提供静态画面，将按 PNG 收纳",
                    "Clipboard exposed a still image; it will be imported as PNG",
                ),
                tone="progress",
            )
            return

        self._show_intake_error(
            ui_text(
                "剪贴板里没有可收纳的文件、图片或网页链接",
                "No supported file, image, or web link found on the clipboard",
            )
        )

    @classmethod
    def _drop_visual_kind_for_mime_data(cls, mime_data) -> str:
        local_files = [
            Path(url.toLocalFile())
            for url in mime_data.urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_file()
        ]
        local_kind = cls._visual_kind_for_files(local_files)
        if local_files and local_kind == "leaf":
            return "leaf"

        parser = DroppedMediaHTMLParser()
        if mime_data.hasHtml():
            parser.feed(mime_data.html())
        if local_kind == "gif":
            has_remote_candidate = bool(parser.urls) or any(
                not url.isLocalFile() and cls._is_http_url(url.toString().strip())
                for url in mime_data.urls()
            )
            if mime_data.hasText():
                has_remote_candidate = has_remote_candidate or any(
                    cls._is_http_url(value) for value in mime_data.text().split()
                )
            return "leaf" if has_remote_candidate else "gif"
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
    def _visual_kind_for_files(files: list[Path]) -> str:
        if not files:
            return "leaf"
        suffixes = [path.suffix.lower() for path in files]
        if all(suffix == ".gif" for suffix in suffixes):
            return "gif"
        if all(suffix in SUPPORTED_AUDIO_EXTENSIONS for suffix in suffixes):
            return "audio"
        return "leaf"

    @staticmethod
    def _is_http_url(value: str) -> bool:
        try:
            return urlparse(value).scheme.lower() in {"http", "https"}
        except ValueError:
            return False

    @staticmethod
    def _is_audio_url(value: str) -> bool:
        return Path(urlparse(value).path).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS

    def _ingest_busy(self) -> bool:
        return bool(
            self.worker is not None
            or self.remote_worker is not None
            or bool(self._web_candidates)
            or self._pending_ingest_finish is not None
        )

    def _ensure_storage_ready(self) -> bool:
        if self._storage_ready:
            return True
        message = (
            ui_text(
                "素材目录不可用，请检查存储权限",
                "Asset storage is unavailable; check storage permissions",
            )
            if self._storage_error_code == "storage_unavailable"
            else ui_text("正在准备素材库", "Preparing asset library")
        )
        self._show_intake_error(message)
        return False

    def _start_remote_download_worker(
        self,
        urls: list[str],
        local_files: list[Path] | None = None,
        *,
        fallback_file: Path | None = None,
    ) -> None:
        if self._closing or not self._ensure_storage_ready():
            return
        self._remote_request_cancelled = False
        self._last_remote_urls = list(urls)
        self.intake_card.dismiss("new_request")
        self.remote_worker = RemoteDownloadWorker(urls, self.settings.STORAGE_DIR / "incoming" / "browser")
        self.remote_worker.setParent(self)
        self.remote_worker.progress_signal.connect(self._on_ingest_progress)
        self.remote_worker.finished_signal.connect(
            lambda downloaded, message, success: self._on_remote_download_finished(
                downloaded,
                message,
                success,
                local_files or [],
                fallback_file,
            )
        )
        self.remote_worker.start()
        self.show_toast(ui_text("正在获取网页素材...", "Fetching web assets..."), tone="progress")
        self.quick_menu.begin_progress(
            self._toast_anchor(),
            self._available_geometry(),
            ui_text("正在获取网页素材...", "Fetching web assets..."),
        )
        self.quick_menu.set_progress(5, ui_text("正在获取网页素材...", "Fetching web assets..."))

    @staticmethod
    def _delete_remote_temp(path: Path) -> None:
        path.unlink(missing_ok=True)
        provenance_path_for(path).unlink(missing_ok=True)

    def _cleanup_remote_ingest_paths(self) -> None:
        for path in self._remote_ingest_paths:
            self._delete_remote_temp(path)
        self._remote_ingest_paths.clear()

    def _on_remote_download_finished(
        self,
        downloaded_files: list[Path],
        message: str,
        success: bool,
        local_files: list[Path],
        fallback_file: Path | None = None,
    ) -> None:
        # Keep the owner until QThread stops, even when its result signal arrives first.
        if self.remote_worker is not None and self.remote_worker.isRunning():
            QTimer.singleShot(10, self, lambda: self._on_remote_download_finished(downloaded_files, message, success, local_files, fallback_file))
            return
        if self._closing:
            for path in downloaded_files:
                self._delete_remote_temp(path)
            return
        worker = self.remote_worker
        fonts = worker.font_candidates if worker is not None else []
        images = worker.image_candidates if worker is not None else []
        candidates = [*fonts, *images]
        note = worker.discovery_note if worker is not None else ""
        if worker is not None:
            worker.deleteLater()
        self.remote_worker = None
        self._remote_ingest_paths.update(downloaded_files)
        if self._remote_request_cancelled:
            self._cleanup_remote_ingest_paths()
            self.quick_menu.complete_progress("pending", "")
            return
        if candidates:
            self._web_candidates = list(candidates)
            candidate_paths = {candidate.path for candidate in candidates}
            self._remote_ingest_paths.difference_update(candidate_paths)
            downloaded_files = [path for path in downloaded_files if path not in candidate_paths]
            self.intake_card.show_candidates(fonts, images, note)
        if (downloaded_files or candidates) and fallback_file is not None:
            self._delete_remote_temp(fallback_file)
            self._remote_ingest_paths.discard(fallback_file)
        if self._closing:
            self._cleanup_remote_ingest_paths()
            return
        files = [*local_files, *downloaded_files]
        if not downloaded_files and fallback_file is not None and fallback_file.is_file():
            files.append(fallback_file)
        if not files:
            if candidates:
                self.quick_menu.complete_progress("pending", "")
                return
            failure_message = (
                message
                if not success
                else ui_text("没有找到可收纳的图片、音频或字体", "No images, audio, or fonts to store")
            )
            self._show_intake_error(failure_message)
            self.quick_menu.complete_progress("error", failure_message)
            return
        self._remote_ingest_paths.update(downloaded_files)
        self._drop_feedback_until = time.monotonic() + 0.65
        self._sync_visual_timer()
        self.update()
        self._start_worker(files)

    def _start_worker(self, files: list[Path]) -> None:
        if self._closing or not self._ensure_storage_ready():
            return
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
            self._cleanup_remote_ingest_paths()
            self._show_intake_error(
                ui_text("没有可收纳的文件", "No files to store")
            )
            return

        self._active_ingest_visual_kind = self._visual_kind_for_files(merged_files)
        self._ingest_feedback_not_before = 0.0
        self._sound_feedback.play("intake")
        if self._active_ingest_visual_kind == "gif":
            visual_already_started = bool(
                self._gif_open_timer.isActive()
                or self._gif_suction_animation is not None
            )
            was_open = self._drop_open_progress > 0.05
            self._begin_programmatic_gif_intake()
            visual_ms = self.GIF_SUCTION_MS + self.GIF_CLOSE_MS
            if not visual_already_started and not was_open:
                visual_ms += self.GIF_OPEN_MS
            self._ingest_feedback_not_before = time.monotonic() + visual_ms / 1000.0

        self.worker = IngestWorker(merged_files, self.assets_dir, settings=self.settings)
        self.worker.setParent(self)
        self.worker.finished_signal.connect(self._on_ingest_finished)
        self.worker.progress_signal.connect(self._on_ingest_progress)
        self.worker.batch_signal.connect(self._on_ingest_batch)
        self.worker.start()
        self._sync_visual_timer()
        initial_message = (
            ui_text("正在校验 GIF…", "Checking GIF…")
            if self._active_ingest_visual_kind == "gif"
            else ui_text(
                f"已接收 {len(merged_files)} 个文件，正在收纳...",
                f"Received {len(merged_files)} files, storing...",
            )
        )
        self.show_toast(initial_message, tone="progress")
        self.quick_menu.begin_progress(
            self._toast_anchor(),
            self._available_geometry(),
            initial_message,
        )
        if self.intake_card.mode in {"fonts", "images"}:
            self.intake_card.store_button.setEnabled(False)

    def _on_ingest_finished(self, message: str, success: bool) -> None:
        if self.worker is not None and self.worker.isRunning():
            QTimer.singleShot(10, self, lambda: self._on_ingest_finished(message, success))
            return
        self._cleanup_remote_ingest_paths()
        if self._closing:
            return
        ingest_result = getattr(self.worker, "result", None)
        blocked_without_new = bool(
            isinstance(ingest_result, IngestResult)
            and ingest_result.blocked_without_new
        )
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None
        if self._active_ingest_visual_kind == "gif" and (
            not success or blocked_without_new
        ):
            self._ingest_feedback_timer.stop()
            self._pending_ingest_finish = None
            self._reset_drop_visual_state()
            self._close_drop_target()
            self._finalize_ingest_finish(message, False, ingest_result)
            return
        if (
            self._active_ingest_visual_kind == "gif"
            and time.monotonic() < self._ingest_feedback_not_before
        ):
            self._pending_ingest_finish = (message, success, ingest_result)
            remaining_ms = max(
                1,
                int((self._ingest_feedback_not_before - time.monotonic()) * 1000),
            )
            self._ingest_feedback_timer.start(remaining_ms)
            return
        self._finalize_ingest_finish(message, success, ingest_result)

    def _flush_pending_ingest_finish(self) -> None:
        pending = self._pending_ingest_finish
        self._pending_ingest_finish = None
        if pending is None or self._closing:
            return
        self._finalize_ingest_finish(*pending)

    def _finalize_ingest_finish(
        self,
        message: str,
        success: bool,
        ingest_result: IngestResult | None,
    ) -> None:
        duplicate_only = bool(
            isinstance(ingest_result, IngestResult) and ingest_result.duplicate_only
        )
        blocked_without_new = bool(
            isinstance(ingest_result, IngestResult)
            and ingest_result.blocked_without_new
        )
        has_rejections = bool(
            isinstance(ingest_result, IngestResult)
            and ingest_result.rejected_count > 0
        )
        clean_gif_success = bool(
            success
            and not duplicate_only
            and not blocked_without_new
            and self._active_ingest_visual_kind == "gif"
            and (
                ingest_result is None
                or (
                    ingest_result.accepted_count > 0
                    and ingest_result.rejected_count == 0
                )
            )
        )
        if clean_gif_success:
            message = ui_text(
                "GIF 已收纳 · 请选择用途",
                "GIF stored · choose a role",
            )
        if success and not blocked_without_new:
            now = time.monotonic()
            if duplicate_only:
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
        tone = (
            "duplicate"
            if success and duplicate_only
            else "pending"
            if success and not blocked_without_new
            else "error"
        )
        if duplicate_only:
            self._sound_feedback.play("duplicate")
        elif not success or has_rejections:
            self._sound_feedback.play("error")
        self.intake_card.store_button.setEnabled(True)
        self.show_toast(message, tone=tone)
        self.quick_menu.complete_progress(tone, message)
        if self.quick_menu.current_page() == "assets":
            self.material_panel.refresh()
        self._refresh_pending_badge()
        self._active_ingest_visual_kind = "leaf"
        self._ingest_feedback_not_before = 0.0

    def _on_ingest_batch(self, batch_id: str, _summary: object) -> None:
        self.latest_batch_id = batch_id
        if self._closing:
            return
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
        if (
            self._closing
            or self.ai_batch_worker is not None
            or not self.ai_enabled
            or self.low_power_enabled
        ):
            return
        while self._ai_batch_queue:
            batch_id = self._ai_batch_queue.pop(0)
            try:
                bundles = self._bundle_service().list_bundles(
                    status="pending",
                    asset_type="image",
                    batch_id=batch_id,
                )
                bundles = [
                    bundle
                    for bundle in bundles
                    if str(bundle.get("content_type") or "").lower() != "image/gif"
                ]
            except ManifestReadinessError:
                self._ai_batch_queue.insert(0, batch_id)
                QTimer.singleShot(1000, self._start_next_ai_batch)
                return
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
        if self._closing:
            return
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
                tone="success"
                if result_status == "success"
                else "pending"
                if result_status == "partial_success"
                else "error",
            )
        self._sync_visual_timer()
        self._start_next_ai_batch()

    def _retry_latest_ai_batch(self) -> None:
        latest = self._bundle_service().get_latest_batch()
        if latest is None:
            self.show_toast(ui_text("还没有可重试的批次", "No batch to retry"), tone="error")
            return
        if not self.ai_enabled or self.low_power_enabled:
            self.show_toast(ui_text("请先开启 AI 整理", "Enable AI sorting first"), tone="error")
            return
        self._enqueue_ai_batch(str(latest["id"]))
        self.show_toast(ui_text("已加入 AI 整理队列", "Added to AI sorting queue"), tone="progress")

    def _on_ingest_progress(self, percent: int, text: str) -> None:
        if self._active_ingest_visual_kind == "gif":
            text = (
                ui_text("正在校验 GIF…", "Checking GIF…")
                if percent < 50
                else ui_text("正在收纳…", "Storing…")
            )
        self.quick_menu.set_progress(percent, text)

    def show_toast(self, message: str, *, tone: str) -> None:
        self.quick_menu.show_feedback(
            message,
            tone,
            self._toast_anchor(),
            self._available_geometry(),
        )

    def _show_intake_error(self, message: str) -> None:
        self._sound_feedback.play("error")
        self.show_toast(message, tone="error")

    def _reposition_toast(self) -> None:
        self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=False)

    def _toast_anchor(self) -> QRect:
        if self._drop_feedback_anchor_global is not None:
            return QRect(self._drop_feedback_anchor_global)
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
                self._drop_feedback_anchor_global = None
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
                self._drop_feedback_anchor_global = None
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
            self._drop_feedback_anchor_global = None
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

    def _capture_drop_anchors(self) -> None:
        if self._drop_anchor_global is None:
            self._drop_anchor_global = self.mapToGlobal(
                self._get_collapsed_circle_rect().center()
            )
        if self._drop_feedback_anchor_global is None:
            target_center = self._clamped_geometry_for_size(self.EXPANDED_SIZE).center()
            self._drop_feedback_anchor_global = QRect(
                target_center.x() - 33,
                target_center.y() - 33,
                68,
                68,
            )

    def _clamped_window_point(self, point: QPoint) -> QPoint:
        x, y = self._clamp_window_position(point.x(), point.y(), self.width(), self.height())
        return QPoint(x, y)

    def prepare_sound_feedback(self) -> int:
        if self._closing:
            return 0
        return self._sound_feedback.prepare()

    def apply_desktop_runtime(self, result: DesktopStartupResult) -> bool:
        if self._desktop_runtime_applied:
            return self._storage_ready
        self._desktop_runtime_applied = True
        self._storage_ready = bool(result.storage_ready)
        self._storage_error_code = str(result.error_code or "")
        self.setAcceptDrops(self._storage_ready)

        if not self._storage_ready:
            self._service_status_override = ui_text(
                "素材目录不可用",
                "Asset storage unavailable",
            )
            self._refresh_ai_menu_status(
                service_status=self._service_status_override,
            )
            self.show_toast(
                ui_text(
                    "素材目录不可用，请检查存储权限",
                    "Asset storage is unavailable; check storage permissions",
                ),
                tone="error",
            )
            return False

        self._session_api_key = str(result.session_api_key or "")
        self.ai_api_key_present = bool(self._session_api_key)
        self.ai_enabled = self._restore_ai_enabled()
        self._service_status_override = ""
        self._refresh_ai_menu_status()
        self._refresh_pending_badge()
        return True

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
        return self._window_position_from_state(self._read_gui_state())

    def _window_position_from_state(
        self,
        payload: dict[str, object],
    ) -> QPoint:
        try:
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
        self.material_panel.set_low_power_enabled(self.low_power_enabled)
        if self.low_power_enabled:
            self._ai_preference = bool(self.ai_enabled or self._ai_preference)
            self.ai_enabled = False
            self._drag_awareness_timer.stop()
            self._clear_external_drag_candidate()
            if self._gif_open_timer.isActive() or self._gif_suction_animation is not None:
                self._cancel_gif_suction()
                self._close_drop_target()
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
            tone="success",
        )

    def _set_sound_enabled(self, enabled: bool) -> None:
        if self._closing:
            return
        self.sound_enabled = bool(enabled)
        self._sound_feedback.set_enabled(self.sound_enabled)
        if self.sound_enabled:
            self._sound_feedback.prepare()
            self._sound_feedback.play("nav")
        try:
            self._save_gui_state({"sound_enabled": self.sound_enabled})
        except OSError:
            logger.debug("Failed to save Haypile sound setting")
        self._refresh_ai_menu_status()
        self.show_toast(
            ui_text("操作音效已开启", "Action sounds enabled")
            if self.sound_enabled
            else ui_text("操作音效已关闭", "Action sounds disabled"),
            tone="success",
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
            self.show_toast(ui_text("填写 API 配置后保存授权", "Enter API settings and authorize"), tone="pending")
        else:
            self.show_toast(self._ai_status_text(), tone="success")

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
                tone="error",
            )
            return
        if not model:
            self.show_toast(ui_text("请填写模型名称", "Enter a model name"), tone="error")
            return
        host_changed = bool(self.ai_api_authorized_host and host != self.ai_api_authorized_host)
        if host_changed and not entered_key:
            self.show_toast(ui_text("更换域名后请重新填写密钥授权", "Enter the key again for the new domain"), tone="error")
            return
        key = entered_key
        if not key and host == self.ai_api_authorized_host:
            key = self._session_api_key or SystemCredentialStore.get(host)
        if not key:
            self.show_toast(ui_text("请填写 API 密钥", "Enter an API key"), tone="error")
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
            tone="success",
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
        self.show_toast(ui_text("语言已更新", "Language updated"), tone="success")

    def _clear_exit_armed(self) -> None:
        self._exit_armed = False
        self._sync_visual_timer()
        self.update()

    def _shutdown_worker(self) -> None:
        if self.worker is None:
            return
        if self.worker.isRunning():
            self.worker.requestInterruption()
            return
        self.worker.deleteLater()
        self.worker = None

    def _shutdown_remote_worker(self) -> None:
        if self.remote_worker is None:
            return
        if self.remote_worker.isRunning():
            self.remote_worker.requestInterruption()
            return
        self.remote_worker.deleteLater()
        self.remote_worker = None

    def _shutdown_ai_batch_worker(self) -> None:
        if self.ai_batch_worker is None:
            return
        if self.ai_batch_worker.isRunning():
            self.ai_batch_worker.requestInterruption()
            return
        self.ai_batch_worker.deleteLater()
        self.ai_batch_worker = None

    def _shutdown_ai_refresh_worker(self) -> None:
        worker = self.material_panel.ai_refresh_worker
        if worker is None:
            return
        if worker.isRunning():
            worker.requestInterruption()
            return
        worker.deleteLater()
        self.material_panel.ai_refresh_worker = None

    def shutdown(self) -> None:
        if self._cleanup_done or self._closing:
            return
        self._closing = True
        self.intake_card.dismiss("shutdown")
        self.intake_card._cancel_preview()
        self._shutdown_started_at = time.monotonic()
        self._shutdown_wait_notice_shown = False
        self._collapse_timer.stop()
        self._drag_prepare_timer.stop()
        self._gif_open_timer.stop()
        self._ingest_feedback_timer.stop()
        self._pending_ingest_finish = None
        self._exit_timer.stop()
        self._visual_timer.stop()
        self._drag_awareness_timer.stop()
        self._sound_feedback.set_enabled(False)
        if self._drop_open_animation is not None:
            self._drop_open_animation.stop()
        if self._audio_suction_animation is not None:
            self._audio_suction_animation.stop()
        if self._gif_suction_animation is not None:
            self._gif_suction_animation.stop()
        self._shutdown_remote_worker()
        self._shutdown_worker()
        self._shutdown_ai_batch_worker()
        self._shutdown_ai_refresh_worker()
        self.material_panel._font_preview_closing = True
        self.material_panel._cancel_font_preview()
        self.shutdown_requested.emit()
        self.show_toast(
            ui_text("正在安全结束当前操作", "Finishing the current operation safely"),
            tone="progress",
        )
        self._shutdown_timer.start()
        self._poll_shutdown()

    def _poll_shutdown(self) -> None:
        self._shutdown_remote_worker()
        self._shutdown_worker()
        self._shutdown_ai_batch_worker()
        self._shutdown_ai_refresh_worker()
        workers_done = all(
            worker is None
            for worker in (
                self.remote_worker,
                self.worker,
                self.ai_batch_worker,
                self.material_panel.ai_refresh_worker,
                self.material_panel.font_preview_worker,
                self.intake_card.font_preview_worker,
            )
        )
        if workers_done:
            self._shutdown_timer.stop()
            if not self._shutdown_ready_emitted:
                self._shutdown_ready_emitted = True
                self.shutdown_ready.emit()
            if not self._managed_shutdown:
                self.complete_shutdown()
            return
        if (
            time.monotonic() - self._shutdown_started_at >= 30.0
            and not self._shutdown_wait_notice_shown
        ):
            self._shutdown_wait_notice_shown = True
            self.show_toast(
                ui_text(
                    "仍在安全结束；如必须立即退出，请使用系统强制退出",
                    "Still finishing safely; use the system Force Quit only if necessary",
                ),
                tone="error",
            )

    def complete_shutdown(self) -> None:
        if self._cleanup_done or not self._shutdown_ready_emitted:
            return
        self._cleanup_remote_ingest_paths()
        self._cleanup_done = True
        self._shutdown_timer.stop()
        self.intake_card.hide()
        self.intake_card.deleteLater()
        self.quick_menu.hide()
        self.quick_menu.close()
        self.quick_menu.deleteLater()
        QTimer.singleShot(0, self.close)

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
        self.intake_card.dismiss("close")
        self.quick_menu.hide_menu()
        QTimer.singleShot(0, self._refresh_pending_badge)

    def _toggle_quick_menu(self) -> None:
        if self.quick_menu.isVisible() or self.intake_card.isVisible():
            self._close_attached_ui()
            return
        if self.remote_worker is not None:
            self.intake_card.show_message(ui_text("正在查找网页素材…", "Finding page assets…"), "progress")
        else:
            self.intake_card.show_recent()
        self.intake_card.activateWindow()
        self.intake_card.close_button.setFocus(Qt.FocusReason.MouseFocusReason)
        self._sound_feedback.play("nav")

    def _show_context_menu(self, point: QPoint) -> None:
        self._close_attached_ui()
        menu = QMenu(self)
        for text, action in [(ui_text("全部素材", "All assets"), "assets"), ("Agent", "agent"),
                             (ui_text("设置", "Settings"), "settings"), (ui_text("退出", "Quit"), "quit")]:
            item = menu.addAction(text)
            item.triggered.connect(lambda _checked=False, target=action: self.shutdown() if target == "quit" else self._handle_quick_menu_action(target))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(point)

    def _discard_web_candidates(self) -> None:
        for candidate in self._web_candidates:
            self._delete_remote_temp(candidate.path)
        self._web_candidates.clear()

    def _collect_web_asset(self, candidate: WebFontCandidate | WebImageCandidate) -> None:
        if self.worker is not None or self._closing or candidate not in self._web_candidates:
            return
        self._web_candidates.remove(candidate)
        self._discard_web_candidates()
        self._remote_ingest_paths.add(candidate.path)
        self.intake_card.dismiss("store")
        self._start_worker([candidate.path])

    def _cancel_remote_download(self) -> None:
        self._remote_request_cancelled = True
        if self.remote_worker is not None:
            self.remote_worker.requestInterruption()
        self.intake_card.dismiss("cancel")

    def _reposition_quick_menu(self) -> None:
        if hasattr(self, "intake_card") and self.intake_card.isVisible():
            self.intake_card.reposition()
        self.quick_menu.reposition(self._ball_anchor_rect(), self._available_geometry(), allow_flip=False)

    def _align_quick_menu_track_to_ball(self) -> None:
        self._reposition_quick_menu()

    def _handle_quick_menu_action(self, action: str) -> None:
        if action == "assets":
            if self.quick_menu.current_page() != action:
                self._sound_feedback.play("nav")
            self._toggle_material_panel()
            return
        if action in {"agent", "settings"}:
            if self.quick_menu.current_page() != action:
                self._sound_feedback.play("nav")
            self.quick_menu.open_drawer(action, self._ball_anchor_rect(), self._available_geometry())
            if action == "settings":
                QTimer.singleShot(0, self._refresh_ai_menu_status)
            return
        if action == "paste_ingest":
            self._ingest_clipboard()
            return
        if action == "mcp":
            QApplication.clipboard().setText(self._mcp_config_text())
            self.show_toast(ui_text("已复制 MCP 配置", "MCP config copied"), tone="success")
            return
        if action == "http":
            base_url = self._base_url()
            QApplication.clipboard().setText(base_url)
            self.show_toast(ui_text(f"已复制 HTTP 地址 {base_url}", f"HTTP URL copied {base_url}"), tone="success")
            return
        if action == "latest_handoff":
            service = self._bundle_service()
            latest = service.get_latest_batch()
            if latest is None:
                self.show_toast(ui_text("还没有最新批次", "No latest batch"), tone="error")
                return
            batch_id = str(latest["id"])
            try:
                bundles = service.list_bundles(status="ready", batch_id=batch_id)
            except ManifestReadinessError:
                self.show_toast(
                    ui_text(
                        "素材已保存，Agent 接口待恢复",
                        "Assets are saved; Agent access is pending recovery",
                    ),
                    tone="error",
                )
                return
            if not bundles:
                self.show_toast(ui_text("最新批次还没有可用素材", "Latest batch has no ready assets"), tone="error")
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
                tone="success",
            )
            return
        if action == "ready_handoff":
            try:
                bundles = self._bundle_service().list_bundles(status="ready")
            except ManifestReadinessError:
                self.show_toast(
                    ui_text(
                        "素材已保存，Agent 接口待恢复",
                        "Assets are saved; Agent access is pending recovery",
                    ),
                    tone="error",
                )
                return
            if not bundles:
                self.show_toast(ui_text("没有可用素材", "No ready assets"), tone="error")
                return
            QApplication.clipboard().setText(
                json.dumps(self.material_panel._handoff_for_bundles(bundles), ensure_ascii=False, indent=2)
            )
            self.show_toast(ui_text(f"已复制 {len(bundles)} 个可用素材", f"Copied {len(bundles)} ready assets"), tone="success")
            return
        if action == "agent_recipe":
            QApplication.clipboard().setText(self.material_panel._agent_recipe_text())
            self.show_toast(ui_text("已复制 Agent 配方", "Agent recipe copied"), tone="success")
            return
        if action == "ai_setup":
            if self.quick_menu.current_page() != "ai":
                self._sound_feedback.play("nav")
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
            self.show_toast(ui_text("已复制模型安装命令", "Model install command copied"), tone="success")
            return
        if action == "ai_recheck":
            self._recheck_ai_setup()
            return
        if action == "low_power":
            self._set_low_power_enabled(not self.low_power_enabled)
            return
        if action == "sound":
            self._set_sound_enabled(not self.sound_enabled)
            return
        if action.startswith("language:"):
            self._set_language_mode(action.partition(":")[2])
            return
        if action == "diagnostics":
            QApplication.clipboard().setText(
                json.dumps(
                    self._desktop_diagnostic_payload(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            self.show_toast(
                ui_text("诊断摘要已复制", "Diagnostic summary copied"),
                tone="success",
            )
            return
        if action == "logs":
            self.settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.LOG_DIR)))
            self.show_toast(
                ui_text("已打开日志目录", "Logs folder opened"),
                tone="success" if opened else "error",
            )
            return
        if action == "exit":
            if self._ingest_busy():
                if getattr(self, "_busy_exit_armed", False):
                    self.close()
                    return
                self._busy_exit_armed = True
                self.quick_menu.exit_button.setText(ui_text("再次点击以退出", "Click again to quit"))
                self.show_toast(ui_text("仍在收纳，退出会中断任务", "Import is active; quitting will stop it"), tone="error")
                QTimer.singleShot(2200, self._clear_busy_exit_armed)
                return
            self.close()

    def _refresh_ai_menu_status(
        self,
        *,
        service_status: str | None = None,
    ) -> None:
        if not hasattr(self, "quick_menu"):
            return
        ai_status = self._ai_status_text()
        self.quick_menu.set_ai_enabled(self.ai_enabled, ai_status)
        resolved_service_status = (
            service_status
            if service_status is not None
            else self._service_status_override or self._status_text()
        )
        self.quick_menu.update_settings_state(
            ai_enabled=self.ai_enabled,
            ai_status=ai_status,
            low_power=self.low_power_enabled,
            sound_enabled=self.sound_enabled,
            language=self.language_mode,
            service_status=resolved_service_status,
            ai_provider=self.ai_provider_mode,
            api_base_url=self.ai_api_base_url,
            api_model=self.ai_api_model,
            api_key_present=self.ai_api_key_present,
        )

    def _on_backend_phase_changed(self, phase: str) -> None:
        self._backend_phase = str(phase or "idle").strip().lower()
        if phase == "ready":
            self._refresh_pending_badge()
            self._refresh_ai_menu_status()

    def _on_backend_notice(self, code: str, details: object) -> None:
        payload = details if isinstance(details, dict) else {}
        if code == "port_conflict":
            port = payload.get("port", self.settings.PORT)
            message = ui_text(
                f"端口 {port} 已被其他程序占用",
                f"Port {port} is used by another program",
            )
            success = False
        elif code == "auto_start_disabled":
            message = ui_text(
                "Haypile 后台未启动，当前配置禁止界面自动启动",
                "Haypile backend is not running; auto-start is disabled",
            )
            success = False
        elif code in {"launch_failed", "start_failed"}:
            message = ui_text(
                "后台服务启动失败",
                "Backend failed to start",
            )
            success = False
        elif code == "start_slow":
            message = ui_text(
                "后台仍在准备素材库",
                "Backend is still preparing the asset library",
            )
            success = True
        else:
            return
        self.show_toast(
            message,
            tone="progress" if code == "start_slow" else "success" if success else "error",
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
            self.show_toast(ui_text("先安装本地视觉模型", "Install the local vision model first"), tone="pending")

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
            self.show_toast(self._ai_status_text(), tone="success")
            return
        self._show_ai_setup_panel(status_text)

    def _clear_busy_exit_armed(self) -> None:
        self._busy_exit_armed = False
        if hasattr(self, "quick_menu"):
            self.quick_menu.exit_button.setText(ui_text("退出 Haypile", "Quit Haypile"))

    def _status_text(self) -> str:
        try:
            bundles = self._bundle_service().list_bundles()
        except ManifestReadinessError:
            return ui_text(
                "素材已保存 · Agent 接口待恢复",
                "Assets saved · Agent access pending recovery",
            )
        summary = build_material_panel_summary(
            bundles,
            assets_dir=self.settings.ASSETS_DIR,
        )
        return ui_text(
            f"运行中 · 可用 {summary.recognized_count} · 待确认 {summary.pending_count}",
            f"Running · ready {summary.recognized_count} · pending {summary.pending_count}",
        )

    def _desktop_diagnostic_payload(self) -> dict[str, object]:
        bundles: list[dict[str, object]] = []
        manifest_state = "unavailable"
        if self._storage_ready:
            try:
                bundles = self._bundle_service().list_bundles()
                manifest_state = "ready"
            except ManifestReadinessError as exc:
                manifest_state = {
                    "catalog_projection_dirty": "dirty",
                    "catalog_projection_missing": "missing",
                    "catalog_projection_unreadable": "unreadable",
                }.get(exc.code, "unavailable")
            except (OSError, ValueError):
                manifest_state = "unavailable"
        return build_desktop_diagnostic_summary(
            app_version=APP_VERSION,
            build_commit=read_build_commit(self.project_root),
            runtime="packaged" if is_packaged_app() else "source",
            platform_name=sys.platform,
            python_version=(
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
            qt_version=qVersion(),
            language=self.language_mode,
            low_power_enabled=self.low_power_enabled,
            storage_ready=self._storage_ready,
            backend_phase=self._backend_phase,
            manifest_state=manifest_state,
            bundles=bundles,
            ai_provider=self.ai_provider_mode,
            ai_enabled=self.ai_enabled,
        )

    def _bundle_service(self) -> BundleService:
        return BundleService(
            assets_dir=self.settings.ASSETS_DIR,
            manifest_path=self.settings.MANIFEST_PATH,
            themes_dir=self.settings.THEMES_DIR,
            runtime_db_path=self.settings.INDEX_DIR / "storage_runtime.db",
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
        if hasattr(self, "quick_menu") and self.quick_menu.isVisible():
            self._reposition_quick_menu()

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

    def _set_gif_suction_progress(self, value: float) -> None:
        self._gif_suction_progress = max(0.0, min(float(value), 1.0))
        self.update()

    def _cancel_gif_suction(self) -> None:
        self._gif_open_timer.stop()
        if self._gif_suction_animation is not None:
            self._gif_suction_animation.stop()
            self._gif_suction_animation = None
        self._set_gif_suction_progress(0.0)

    def _animate_gif_suction(self) -> None:
        self._cancel_gif_suction()
        if self._gif_expand_started_at <= 0.0:
            self._gif_expand_started_at = time.monotonic() - self.GIF_EXPAND_SECONDS
        if self.low_power_enabled:
            self._set_gif_suction_progress(1.0)
            self._finish_gif_suction()
            return
        animation = QVariantAnimation(self)
        self._gif_suction_animation = animation
        animation.valueChanged.connect(self._set_gif_suction_progress)
        animation.setDuration(self.GIF_SUCTION_MS)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.finished.connect(self._finish_gif_suction)
        animation.start()

    def _finish_gif_suction(self) -> None:
        self._gif_suction_animation = None
        self._close_drop_target()

    def _begin_programmatic_gif_intake(self) -> None:
        if self._gif_open_timer.isActive() or self._gif_suction_animation is not None:
            return
        self._drop_visual_kind = "gif"
        self._gif_expand_started_at = time.monotonic()
        if self._drop_open_progress > 0.05:
            self._animate_gif_suction()
            return
        self._capture_drop_anchors()
        self._collapse_timer.stop()
        self._animate_drop_open(True)
        self._animate_size(self.EXPANDED_SIZE)
        self._gif_open_timer.start()
        self._sync_visual_timer()

    def _close_drop_target(self) -> None:
        self._animate_drop_open(False)
        self._collapse_timer.start()

    def _reset_drop_visual_state(self) -> None:
        self._cancel_audio_suction()
        self._cancel_gif_suction()
        self._gif_expand_started_at = 0.0
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

    def _refresh_pending_badge(self) -> None:
        try:
            self._has_pending_assets = bool(
                self._storage_ready
                and self._bundle_service().list_bundles(status="pending", limit=1)
            )
        except (ManifestReadinessError, OSError, ValueError):
            logger.debug("Failed to refresh Haypile pending badge")
            self._has_pending_assets = False
        if not self._has_pending_assets and self.quick_menu._attention_action == "assets":
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

def clipboard_mime_diagnostics(mime_data) -> dict[str, object]:
    formats: list[dict[str, object]] = []
    for name in sorted({str(value) for value in mime_data.formats()}):
        try:
            byte_length: int | None = len(mime_data.data(name))
        except Exception:
            byte_length = None
        formats.append({"name": name, "byte_length": byte_length})

    route = resolve_intake_route(
        local_files=HaypileFloatingBall._extract_local_files(mime_data),
        remote_urls=HaypileFloatingBall._extract_remote_media_urls(mime_data),
        raw_gif=HaypileFloatingBall._clipboard_gif_payload(mime_data),
        has_image=bool(mime_data.hasImage()),
    )

    return {
        "diagnostic_version": "haypile.clipboard-diagnostics.v1",
        "platform": sys.platform,
        "format_count": len(formats),
        "formats": formats,
        "has_urls": bool(mime_data.hasUrls()),
        "has_html": bool(mime_data.hasHtml()),
        "has_text": bool(mime_data.hasText()),
        "has_image": route.has_image,
        "local_file_count": len(route.local_files),
        "remote_url_count": len(route.remote_urls),
        "raw_gif_byte_count": len(route.raw_gif or b""),
        "ingest_route": route.name,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--clipboard-diagnostics"]:
        app = QApplication.instance() or QApplication([sys.argv[0], *args])
        print(
            json.dumps(
                clipboard_mime_diagnostics(app.clipboard().mimeData()),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
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
    instance_lock = InterProcessFileLock(settings.INDEX_DIR / "gui.instance.lock")
    instance_lock_acquired = False
    startup_error = ""
    try:
        instance_lock_acquired = instance_lock.acquire(timeout=0.1)
    except OSError as exc:
        startup_error = "storage_unavailable"
        logger.error(
            "GUI instance lock unavailable error_type=%s",
            type(exc).__name__,
        )
    if not instance_lock_acquired and not startup_error:
        logger.info("Haypile GUI is already running")
        return 0
    initial_state = read_desktop_gui_state(settings)
    app = QApplication([sys.argv[0], *args])
    app.setQuitOnLastWindowClosed(False)
    widget = HaypileFloatingBall(
        settings=settings,
        initial_state=initial_state,
        deferred_runtime=True,
        managed_shutdown=True,
    )
    backend = BackendRuntimeController(settings, settings.BASE_DIR, parent=app)
    shutdown_state = {
        "requested": False,
        "window_ready": False,
        "completed": False,
    }

    def maybe_complete_shutdown() -> None:
        if (
            shutdown_state["completed"]
            or not shutdown_state["window_ready"]
            or not backend.is_stopped
        ):
            return
        shutdown_state["completed"] = True
        widget.complete_shutdown()
        QTimer.singleShot(0, QCoreApplication.quit)

    def request_shutdown() -> None:
        shutdown_state["requested"] = True
        backend.stop()
        maybe_complete_shutdown()

    def mark_window_ready() -> None:
        shutdown_state["window_ready"] = True
        maybe_complete_shutdown()

    def hydrate_desktop() -> None:
        if shutdown_state["requested"]:
            return
        widget.prepare_sound_feedback()
        configure_packaged_logging("gui", settings.LOG_DIR)
        result = (
            DesktopStartupResult(
                storage_ready=False,
                error_code=startup_error,
            )
            if startup_error
            else prepare_desktop_runtime(settings, initial_state)
        )
        if widget.apply_desktop_runtime(result) and not shutdown_state["requested"]:
            backend.start()

    widget.shutdown_requested.connect(request_shutdown)
    widget.shutdown_ready.connect(mark_window_ready)
    backend.stopped.connect(maybe_complete_shutdown)
    backend.notice.connect(widget._on_backend_notice)
    backend.phase_changed.connect(widget._on_backend_phase_changed)
    app.aboutToQuit.connect(widget.shutdown)
    widget.show()
    QTimer.singleShot(0, hydrate_desktop)
    try:
        return app.exec()
    finally:
        if instance_lock_acquired:
            instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
