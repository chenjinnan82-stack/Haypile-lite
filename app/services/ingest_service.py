from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from app.core.file_lock import InterProcessFileLock
from app.services.asset_provenance import (
    read_asset_provenance,
    write_asset_provenance,
)
from app.services.media_types import SUPPORTED_AUDIO_EXTENSIONS
from app.services.media_validator import (
    MAX_GIF_BYTES,
    MediaValidationError,
    validate_media,
)
from app.services.scanner import AssetScanner, mark_manifest_dirty
from app.services.storage_runtime import StorageRuntimeDB
from app.services.theme_registry import ThemeRegistry
from app.services.vfs_storage import VFSStorage

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str, dict[str, int]], None]


@dataclass(frozen=True, slots=True)
class IngestCandidate:
    path: Path


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str
    batch_id: str | None = None
    error_code: str | None = None
    accepted_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    renamed_count: int = 0
    recovered_theme_count: int = 0
    video_rejected_count: int = 0

    @property
    def success(self) -> bool:
        return self.status == "completed"

    @property
    def duplicate_only(self) -> bool:
        return self.success and self.accepted_count == 0 and self.duplicate_count > 0


class IngestService:
    SUPPORTED_IMAGE_EXTENSIONS = {".png", ".webp", ".svg", ".jpg", ".jpeg", ".gif"}
    UNSUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".webm"}
    DEFAULT_MAX_FILE_BYTES = 500 * 1024 * 1024
    DEFAULT_MAX_FILES = 256
    DEFAULT_MAX_BATCH_BYTES = 2 * 1024 * 1024 * 1024
    DEFAULT_RESERVE_BYTES = 256 * 1024 * 1024
    DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        *,
        storage_dir: Path,
        assets_dir: Path,
        index_dir: Path,
        themes_dir: Path,
        manifest_path: Path,
        fallback_theme: str = "generic",
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
        max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
        reserve_bytes: int = DEFAULT_RESERVE_BYTES,
        hash_chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
    ) -> None:
        self.storage_dir = Path(storage_dir)
        self.assets_dir = Path(assets_dir)
        self.index_dir = Path(index_dir)
        self.themes_dir = Path(themes_dir)
        self.manifest_path = Path(manifest_path)
        self.fallback_theme = fallback_theme
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_batch_bytes = max_batch_bytes
        self.reserve_bytes = reserve_bytes
        self.hash_chunk_size = hash_chunk_size
        self.runtime_db_path = self.index_dir / "storage_runtime.db"
        self.staging_dir = self.storage_dir / "staging" / "ingest"
        self.quarantine_dir = self.storage_dir / "quarantine" / "ingest"
        # ponytail: one writer lock is enough until ingest throughput becomes a real limit.
        self.transaction_lock_path = self.index_dir / "ingest.transaction.lock"

    def ingest(
        self,
        candidates: Iterable[IngestCandidate],
        *,
        should_stop: Callable[[], bool] | None = None,
        progress: ProgressCallback | None = None,
        lock_timeout: float = 0.0,
    ) -> IngestResult:
        files = [Path(candidate.path) for candidate in candidates]
        stop_requested = should_stop or (lambda: False)
        lock = InterProcessFileLock(self.transaction_lock_path)
        if not lock.acquire(timeout=lock_timeout):
            return IngestResult(status="busy", error_code="ingest_busy")

        counts = {
            "accepted_count": 0,
            "duplicate_count": 0,
            "rejected_count": 0,
            "renamed_count": 0,
            "recovered_theme_count": 0,
            "video_rejected_count": 0,
        }
        batch_id: str | None = None
        try:
            if stop_requested():
                return IngestResult(status="cancelled", error_code="ingest_cancelled")
            preflight_error = self._batch_preflight_error(files)
            if preflight_error:
                return IngestResult(status="rejected", error_code=preflight_error)

            mark_manifest_dirty(self.manifest_path)
            runtime = StorageRuntimeDB(self.runtime_db_path)
            try:
                self._recover(runtime)
            except (OSError, RuntimeError, sqlite3.Error):
                logger.warning("Storage recovery failed before ingest")
                return IngestResult(status="failed", error_code="storage_recovery_failed")

            registry = ThemeRegistry(self.themes_dir)
            vfs = VFSStorage(copy_max_retries=3, copy_base_delay=1.0)
            batch_id = runtime.begin_batch()
            total_files = max(len(files), 1)
            self._progress(progress, "build_hash_index", 3, total=total_files)
            hash_index = runtime.asset_hash_index(self.assets_dir)
            if stop_requested():
                runtime.interrupt_batch(batch_id)
                return IngestResult(
                    status="cancelled",
                    batch_id=batch_id,
                    error_code="ingest_cancelled",
                    **counts,
                )

            for index, file_path in enumerate(files, start=1):
                if stop_requested():
                    runtime.interrupt_batch(batch_id)
                    return IngestResult(
                        status="cancelled",
                        batch_id=batch_id,
                        error_code="ingest_cancelled",
                        **counts,
                    )
                provenance = read_asset_provenance(file_path)
                runtime.record_item_discovered(
                    batch_id,
                    index,
                    file_path.name,
                    origin_url=str(provenance.get("origin_url") or ""),
                )
                progress_base = int((index - 1) / total_files * 84)
                self._progress(
                    progress,
                    "validate",
                    progress_base + 8,
                    index=index,
                    total=total_files,
                )
                reason = self._candidate_rejection_code(file_path)
                if reason is not None:
                    counts["rejected_count"] += 1
                    if (
                        reason == "unsupported_extension"
                        and file_path.suffix.lower() in self.UNSUPPORTED_VIDEO_EXTENSIONS
                    ):
                        counts["video_rejected_count"] += 1
                    runtime.reject_item(batch_id, index, reason)
                    continue

                staged = None
                try:
                    self._progress(
                        progress,
                        "stage",
                        progress_base + 28,
                        index=index,
                        total=total_files,
                    )
                    staged = vfs.stage(
                        file_path,
                        self.staging_dir,
                        f"{batch_id}-{index}",
                        should_stop=stop_requested,
                        chunk_size=self.hash_chunk_size,
                    )
                    validated = validate_media(staged.path)
                except InterruptedError:
                    runtime.interrupt_item(batch_id, index, "interrupted")
                    runtime.interrupt_batch(batch_id)
                    return IngestResult(
                        status="cancelled",
                        batch_id=batch_id,
                        error_code="ingest_cancelled",
                        **counts,
                    )
                except (MediaValidationError, OSError) as exc:
                    counts["rejected_count"] += 1
                    if staged is not None:
                        staged.path.unlink(missing_ok=True)
                    reason = (
                        str(exc)
                        if isinstance(exc, MediaValidationError)
                        else type(exc).__name__
                    )
                    runtime.reject_item(batch_id, index, reason)
                    continue

                file_hash = staged.sha256
                if file_hash in hash_index:
                    staged.path.unlink(missing_ok=True)
                    try:
                        runtime.commit_item(
                            batch_id,
                            index,
                            sha256_hex=file_hash,
                            src_path=file_path,
                            dst_path=hash_index[file_hash],
                            strategy="duplicate",
                            duplicate=True,
                        )
                    except (OSError, sqlite3.Error):
                        return IngestResult(
                            status="failed",
                            batch_id=batch_id,
                            error_code="ingest_journal_failed",
                            **counts,
                        )
                    counts["duplicate_count"] += 1
                    continue

                theme_id = self.fallback_theme
                role = "unknown"
                destination = self._resolve_themed_destination(
                    original_name=file_path.name,
                    sha256_hex=file_hash,
                    theme_id=theme_id,
                    media_kind=validated.kind,
                    role=role,
                )
                if destination.name != file_path.name:
                    counts["renamed_count"] += 1

                try:
                    runtime.record_item_staged(
                        batch_id,
                        index,
                        media_kind=validated.kind,
                        sha256_hex=file_hash,
                        staging_path=staged.path,
                        destination_path=destination,
                    )
                except sqlite3.Error:
                    staged.path.unlink(missing_ok=True)
                    return IngestResult(
                        status="failed",
                        batch_id=batch_id,
                        error_code="ingest_journal_failed",
                        **counts,
                    )

                try:
                    self._progress(
                        progress,
                        "commit",
                        progress_base + 58,
                        index=index,
                        total=total_files,
                    )
                    strategy = vfs.commit_staged(staged.path, destination)
                    runtime.commit_item(
                        batch_id,
                        index,
                        sha256_hex=file_hash,
                        src_path=file_path,
                        dst_path=destination,
                        strategy=strategy,
                    )
                except (OSError, sqlite3.Error):
                    # Keep the staged journal row open so the next locked recovery
                    # can finish a move that may already have reached destination.
                    return IngestResult(
                        status="failed",
                        batch_id=batch_id,
                        error_code="durable_commit_failed",
                        **counts,
                    )

                hash_index[file_hash] = destination
                counts["accepted_count"] += 1
                self._persist_asset_provenance(file_path, destination, file_hash)
                if validated.kind == "image":
                    try:
                        self._upsert_theme_contract_for_image(
                            registry,
                            destination,
                            theme_id,
                            role,
                        )
                        if registry.last_recovery is not None:
                            counts["recovered_theme_count"] += 1
                            registry.last_recovery = None
                    except (OSError, ValueError):
                        logger.warning("Theme projection failed: sha256=%s", file_hash)
                self._progress(
                    progress,
                    "item_complete",
                    progress_base + 84,
                    index=index,
                    total=total_files,
                )

            if stop_requested():
                runtime.interrupt_batch(batch_id)
                return IngestResult(
                    status="cancelled",
                    batch_id=batch_id,
                    error_code="ingest_cancelled",
                    **counts,
                )
            runtime.complete_batch(
                batch_id,
                accepted_count=counts["accepted_count"],
                duplicate_count=counts["duplicate_count"],
                rejected_count=counts["rejected_count"],
            )
            self._progress(progress, "project_manifest", 92, total=total_files)
            try:
                self._project_manifest(stop_requested)
            except InterruptedError:
                return IngestResult(
                    status="cancelled",
                    batch_id=batch_id,
                    error_code="ingest_cancelled",
                    **counts,
                )
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "Asset manifest projection failed; Agent access is paused until recovery"
                )
                return IngestResult(
                    status="failed",
                    batch_id=batch_id,
                    error_code="manifest_projection_failed",
                    **counts,
                )

            if counts["accepted_count"] == 0 and counts["duplicate_count"] == 0:
                code = (
                    "video_not_supported"
                    if counts["video_rejected_count"] == counts["rejected_count"]
                    and counts["video_rejected_count"] > 0
                    else "unsupported_media"
                )
                return IngestResult(
                    status="rejected",
                    batch_id=batch_id,
                    error_code=code,
                    **counts,
                )
            self._progress(progress, "complete", 100, total=total_files)
            return IngestResult(status="completed", batch_id=batch_id, **counts)
        except Exception as exc:
            logger.exception(
                "Unexpected ingest service failure error_type=%s", type(exc).__name__
            )
            return IngestResult(
                status="failed",
                batch_id=batch_id,
                error_code="ingest_failed",
                **counts,
            )
        finally:
            lock.release()

    def recover_and_project(self, *, lock_timeout: float = 8.0) -> IngestResult:
        lock = InterProcessFileLock(self.transaction_lock_path)
        if not lock.acquire(timeout=lock_timeout):
            return IngestResult(status="busy", error_code="ingest_busy")
        try:
            mark_manifest_dirty(self.manifest_path)
            runtime = StorageRuntimeDB(self.runtime_db_path)
            try:
                self._recover(runtime)
            except (OSError, RuntimeError, sqlite3.Error):
                logger.warning("Storage recovery failed during initialization")
                return IngestResult(status="failed", error_code="storage_recovery_failed")
            try:
                self._project_manifest(lambda: False)
            except (OSError, RuntimeError, ValueError):
                return IngestResult(
                    status="failed",
                    error_code="manifest_projection_failed",
                )
            return IngestResult(status="completed")
        except Exception as exc:
            logger.exception(
                "Unexpected storage initialization failure error_type=%s",
                type(exc).__name__,
            )
            return IngestResult(status="failed", error_code="storage_recovery_failed")
        finally:
            lock.release()

    def _recover(self, runtime: StorageRuntimeDB) -> None:
        runtime.ensure_ready()
        runtime.recover_incomplete_ingest(
            assets_dir=self.assets_dir,
            staging_dir=self.staging_dir,
            quarantine_dir=self.quarantine_dir,
        )
        runtime.register_legacy_assets(self.assets_dir)

    def _project_manifest(self, should_stop: Callable[[], bool]) -> None:
        scanner = AssetScanner(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            runtime_db_path=self.runtime_db_path,
        )
        asyncio.run(scanner.scan_assets_directory(should_stop=should_stop))

    def _batch_preflight_error(self, files: list[Path]) -> str | None:
        if len(files) > self.max_files:
            return "batch_file_limit"
        total_bytes = 0
        for path in files:
            try:
                if path.is_file() and not path.is_symlink():
                    total_bytes += max(0, path.stat().st_size)
            except OSError:
                continue
        if total_bytes > self.max_batch_bytes:
            return "batch_byte_limit"
        try:
            free_bytes = shutil.disk_usage(self.storage_dir).free
        except OSError:
            return "storage_space_unavailable"
        if free_bytes < total_bytes + self.reserve_bytes:
            return "storage_space_low"
        return None

    def _candidate_rejection_code(self, file_path: Path) -> str | None:
        if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
            return "missing_file"
        try:
            file_size = file_path.stat().st_size
        except OSError:
            return "unreadable_file"
        if file_size <= 0:
            return "empty_file"
        if file_path.suffix.lower() == ".gif" and file_size > MAX_GIF_BYTES:
            return "gif_file_too_large"
        if file_size > self.max_file_bytes:
            return "file_too_large"
        suffix = file_path.suffix.lower()
        if (
            suffix not in self.SUPPORTED_IMAGE_EXTENSIONS
            and suffix not in SUPPORTED_AUDIO_EXTENSIONS
        ):
            return "unsupported_extension"
        return None

    def _resolve_themed_destination(
        self,
        *,
        original_name: str,
        sha256_hex: str,
        theme_id: str,
        media_kind: str,
        role: str,
    ) -> Path:
        safe_theme = self._safe_identifier(theme_id or self.fallback_theme)
        bucket = "images" if media_kind == "image" else "audio"
        extension = Path(original_name).suffix.lower() or ".bin"
        short_hash = sha256_hex[:8]
        safe_role = self._safe_identifier(role or "unknown")
        kind = "img" if media_kind == "image" else "aud"
        prefix = f"{safe_theme}_{kind}_{safe_role}_{short_hash}"
        themed_dir = self.assets_dir / safe_theme / bucket
        candidate = themed_dir / f"{prefix}{extension}"
        counter = 1
        while candidate.exists():
            candidate = themed_dir / f"{prefix}_{counter}{extension}"
            counter += 1
        return candidate

    def _persist_asset_provenance(
        self,
        source_path: Path,
        destination: Path,
        sha256_hex: str,
    ) -> None:
        provenance = read_asset_provenance(source_path)
        if not provenance:
            return
        try:
            source_key = destination.relative_to(self.assets_dir).as_posix()
        except ValueError:
            source_key = destination.name
        provenance.update({"source_key": source_key, "sha256": sha256_hex})
        try:
            write_asset_provenance(destination, provenance)
        except OSError:
            logger.warning("Asset provenance projection failed: sha256=%s", sha256_hex)

    def _upsert_theme_contract_for_image(
        self,
        registry: ThemeRegistry,
        destination: Path,
        theme_id: str,
        role: str,
    ) -> None:
        safe_theme = self._safe_identifier(theme_id or self.fallback_theme)
        safe_role = self._safe_identifier(role or "unknown")
        relative_path = destination.relative_to(self.assets_dir).as_posix()
        registry.upsert_image_asset(
            theme_id=safe_theme,
            asset_key=destination.stem,
            asset_url=f"/static/{relative_path}",
            role=safe_role,
        )

    @staticmethod
    def _safe_identifier(text: str) -> str:
        lowered = (text or "").strip().lower()
        sanitized = "".join(
            character
            if character.isalnum() or character in {"_", "-"}
            else "_"
            for character in lowered
        )
        while "__" in sanitized:
            sanitized = sanitized.replace("__", "_")
        return sanitized.strip("_") or "generic"

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        stage: str,
        percent: int,
        *,
        index: int = 0,
        total: int = 0,
    ) -> None:
        if callback is not None:
            callback(
                stage,
                {"percent": int(percent), "index": int(index), "total": int(total)},
            )
