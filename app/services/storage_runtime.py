from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.core.config import get_settings
from app.services.json_io import atomic_write_json
from app.services.media_types import SUPPORTED_AUDIO_EXTENSIONS


STORAGE_FORMAT_VERSION = 2


class StorageRuntimeDB:
    """
    Runtime metadata store for VFS ingest.

    Notes:
    - Force WAL mode to improve concurrent read/write behavior.
    - Keep schema intentionally small and append-friendly.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (get_settings().INDEX_DIR / "storage_runtime.db")
        self.format_path = self.db_path.parent / "storage_format.json"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = Lock()
        self._initialized = False
        self._upgraded_from_version: int | None = None

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=8.0,
            check_same_thread=False,
        )
        # Concurrency unlock for mixed read/write traffic.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def ensure_ready(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            database_existed = self.db_path.exists()
            previous_version = self._assert_compatible_storage_format()
            with closing(self.get_connection()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vfs_asset_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sha256 TEXT NOT NULL,
                        src_path TEXT NOT NULL,
                        dst_path TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        dst_size INTEGER,
                        dst_mtime_ns INTEGER,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                self._ensure_vfs_columns(conn)
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_vfs_asset_links_sha256
                    ON vfs_asset_links(sha256);
                    """
                )
                conn.execute(
                    """
                    DELETE FROM vfs_asset_links
                    WHERE id NOT IN (
                        SELECT MIN(id) FROM vfs_asset_links GROUP BY sha256
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_vfs_asset_links_sha256
                    ON vfs_asset_links(sha256);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_vfs_asset_links_dst_path
                    ON vfs_asset_links(dst_path);
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingest_batches (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        completed_at TEXT,
                        state TEXT NOT NULL DEFAULT 'open',
                        accepted_count INTEGER NOT NULL DEFAULT 0,
                        duplicate_count INTEGER NOT NULL DEFAULT 0,
                        rejected_count INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                self._ensure_batch_columns(conn)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingest_batch_assets (
                        batch_id TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        PRIMARY KEY (batch_id, sha256)
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ingest_batch_items (
                        batch_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        source_name TEXT NOT NULL,
                        media_kind TEXT,
                        state TEXT NOT NULL,
                        reason TEXT,
                        sha256 TEXT,
                        staging_path TEXT,
                        destination_path TEXT,
                        is_duplicate INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (batch_id, ordinal),
                        FOREIGN KEY (batch_id) REFERENCES ingest_batches(id)
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ingest_batch_items_state
                    ON ingest_batch_items(state);
                    """
                )
                conn.execute(
                    """
                    UPDATE ingest_batches
                    SET state = CASE
                        WHEN completed_at IS NOT NULL THEN 'completed'
                        ELSE state
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ingest_batch_assets_sha256
                    ON ingest_batch_assets(sha256);
                    """
                )
                conn.commit()
            self._ensure_storage_format()
            if previous_version is not None:
                self._upgraded_from_version = previous_version
            elif database_existed:
                self._upgraded_from_version = 1
            self._initialized = True

    def record_link(
        self,
        *,
        sha256_hex: str,
        src_path: Path,
        dst_path: Path,
        strategy: str,
    ) -> None:
        self.ensure_ready()
        stat = dst_path.stat()
        with closing(self.get_connection()) as conn:
            self._upsert_link(
                conn,
                sha256_hex=sha256_hex,
                src_path=src_path,
                dst_path=dst_path,
                strategy=strategy,
                dst_size=stat.st_size,
                dst_mtime_ns=stat.st_mtime_ns,
            )
            conn.commit()

    def begin_batch(self) -> str:
        self.ensure_ready()
        batch_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute(
                "INSERT INTO ingest_batches (id, created_at, state) VALUES (?, ?, 'open')",
                (batch_id, created_at),
            )
            conn.commit()
        return batch_id

    def record_item_discovered(
        self,
        batch_id: str,
        ordinal: int,
        source_name: str,
        media_kind: str | None = None,
    ) -> None:
        self.ensure_ready()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO ingest_batch_items (
                    batch_id, ordinal, source_name, media_kind, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'discovered', ?, ?)
                ON CONFLICT(batch_id, ordinal) DO UPDATE SET
                    source_name = excluded.source_name,
                    media_kind = COALESCE(excluded.media_kind, ingest_batch_items.media_kind),
                    updated_at = excluded.updated_at
                """,
                (batch_id, int(ordinal), Path(source_name).name, media_kind, now, now),
            )
            conn.commit()

    def record_item_staged(
        self,
        batch_id: str,
        ordinal: int,
        *,
        media_kind: str,
        sha256_hex: str,
        staging_path: Path,
        destination_path: Path,
    ) -> None:
        self.ensure_ready()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                UPDATE ingest_batch_items
                SET media_kind = ?, state = 'staged', reason = NULL, sha256 = ?,
                    staging_path = ?, destination_path = ?, updated_at = ?
                WHERE batch_id = ? AND ordinal = ?
                """,
                (
                    media_kind,
                    sha256_hex,
                    staging_path.as_posix(),
                    destination_path.as_posix(),
                    now,
                    batch_id,
                    int(ordinal),
                ),
            )
            conn.commit()

    def commit_item(
        self,
        batch_id: str,
        ordinal: int,
        *,
        sha256_hex: str,
        src_path: Path,
        dst_path: Path,
        strategy: str,
        duplicate: bool = False,
    ) -> None:
        self.ensure_ready()
        stat = dst_path.stat()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not duplicate:
                self._upsert_link(
                    conn,
                    sha256_hex=sha256_hex,
                    src_path=src_path,
                    dst_path=dst_path,
                    strategy=strategy,
                    dst_size=stat.st_size,
                    dst_mtime_ns=stat.st_mtime_ns,
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO ingest_batch_assets (batch_id, sha256, ordinal)
                VALUES (?, ?, ?)
                """,
                (batch_id, sha256_hex, int(ordinal)),
            )
            conn.execute(
                """
                UPDATE ingest_batch_items
                SET state = 'committed', reason = NULL, sha256 = ?, staging_path = NULL,
                    destination_path = ?, is_duplicate = ?, updated_at = ?
                WHERE batch_id = ? AND ordinal = ?
                """,
                (
                    sha256_hex,
                    dst_path.as_posix(),
                    1 if duplicate else 0,
                    now,
                    batch_id,
                    int(ordinal),
                ),
            )
            conn.commit()

    def reject_item(self, batch_id: str, ordinal: int, reason: str) -> None:
        self._set_item_state(batch_id, ordinal, "rejected", reason)

    def interrupt_item(self, batch_id: str, ordinal: int, reason: str) -> None:
        self._set_item_state(batch_id, ordinal, "interrupted", reason)

    def _set_item_state(self, batch_id: str, ordinal: int, state: str, reason: str) -> None:
        self.ensure_ready()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                UPDATE ingest_batch_items
                SET state = ?, reason = ?, updated_at = ?
                WHERE batch_id = ? AND ordinal = ?
                """,
                (state, reason[:160], now, batch_id, int(ordinal)),
            )
            conn.commit()

    def record_batch_asset(self, batch_id: str, sha256_hex: str, ordinal: int) -> None:
        self.ensure_ready()
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO ingest_batch_assets (batch_id, sha256, ordinal)
                VALUES (?, ?, ?)
                """,
                (batch_id, sha256_hex, int(ordinal)),
            )
            conn.commit()

    def complete_batch(
        self,
        batch_id: str,
        *,
        accepted_count: int,
        duplicate_count: int,
        rejected_count: int,
    ) -> None:
        self.ensure_ready()
        completed_at = datetime.now(timezone.utc).isoformat()
        state = "completed" if accepted_count + duplicate_count > 0 else "empty"
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                UPDATE ingest_batches
                SET completed_at = ?, state = ?, accepted_count = ?, duplicate_count = ?, rejected_count = ?
                WHERE id = ?
                """,
                (completed_at, state, accepted_count, duplicate_count, rejected_count, batch_id),
            )
            conn.commit()

    def interrupt_batch(self, batch_id: str) -> None:
        self.ensure_ready()
        completed_at = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                UPDATE ingest_batches
                SET state = 'interrupted', completed_at = COALESCE(completed_at, ?)
                WHERE id = ? AND state = 'open'
                """,
                (completed_at, batch_id),
            )
            conn.commit()

    def latest_batch(self) -> dict[str, object] | None:
        self.ensure_ready()
        with closing(self.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT b.id, b.created_at, b.completed_at, b.accepted_count,
                       b.duplicate_count, b.rejected_count, COUNT(a.sha256)
                FROM ingest_batches AS b
                JOIN ingest_batch_assets AS a ON a.batch_id = b.id
                WHERE b.state = 'completed' AND b.completed_at IS NOT NULL
                GROUP BY b.id
                ORDER BY b.completed_at DESC, b.created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "created_at": str(row[1]),
            "completed_at": str(row[2]),
            "accepted_count": int(row[3]),
            "duplicate_count": int(row[4]),
            "rejected_count": int(row[5]),
            "asset_count": int(row[6]),
        }

    def resolve_batch_id(self, value: str | None) -> str | None:
        requested = str(value or "").strip()
        if not requested:
            return None
        if requested == "latest":
            latest = self.latest_batch()
            return str(latest["id"]) if latest else ""
        self.ensure_ready()
        with closing(self.get_connection()) as conn:
            row = conn.execute(
                "SELECT id FROM ingest_batches WHERE id = ? AND state = 'completed'",
                (requested,),
            ).fetchone()
        return requested if row else ""

    def batch_hashes(self, batch_id: str) -> list[str]:
        self.ensure_ready()
        with closing(self.get_connection()) as conn:
            rows = conn.execute(
                """
                SELECT sha256 FROM ingest_batch_assets
                WHERE batch_id = ?
                ORDER BY ordinal, sha256
                """,
                (batch_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def asset_hash_index(self, assets_dir: Path) -> dict[str, Path]:
        """Return verified hash entries for Haypile-owned asset copies."""
        self.ensure_ready()
        return self.read_asset_hash_index(self.db_path, assets_dir)

    def committed_asset_paths(self, assets_dir: Path, *, verify_hashes: bool = False) -> set[Path]:
        index = self.asset_hash_index(assets_dir)
        if not verify_hashes:
            return {path.resolve(strict=False) for path in index.values()}
        verified: set[Path] = set()
        for expected_hash, path in index.items():
            try:
                if self._sha256_file(path) == expected_hash:
                    verified.add(path.resolve(strict=False))
            except OSError:
                continue
        return verified

    def register_legacy_assets(self, assets_dir: Path) -> int:
        """Register pre-v2 files without renaming, deleting, or trusting old names as identity."""
        self.ensure_ready()
        root = assets_dir.resolve(strict=False)
        with closing(self.get_connection()) as conn:
            known: dict[Path, tuple[int, str, int | None, int | None]] = {}
            for row_id, sha256_hex, dst_path, dst_size, dst_mtime_ns in conn.execute(
                "SELECT id, sha256, dst_path, dst_size, dst_mtime_ns FROM vfs_asset_links"
            ).fetchall():
                try:
                    resolved = Path(str(dst_path)).resolve(strict=False)
                    resolved.relative_to(root)
                except ValueError:
                    continue
                known[resolved] = (
                    int(row_id),
                    str(sha256_hex),
                    int(dst_size) if dst_size is not None else None,
                    int(dst_mtime_ns) if dst_mtime_ns is not None else None,
                )
        registered = 0
        supported = {".png", ".jpg", ".jpeg", ".webp", ".svg", *SUPPORTED_AUDIO_EXTENSIONS}
        for candidate in sorted(assets_dir.rglob("*")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.suffix.lower() not in supported:
                continue
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            try:
                digest = self._sha256_file(resolved)
                stat = resolved.stat()
            except OSError:
                continue
            existing = known.get(resolved)
            if existing is not None and existing[1:] == (digest, stat.st_size, stat.st_mtime_ns):
                continue
            with closing(self.get_connection()) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if existing is not None:
                    conn.execute("DELETE FROM vfs_asset_links WHERE id = ?", (existing[0],))
                duplicate = conn.execute(
                    "SELECT id FROM vfs_asset_links WHERE sha256 = ? LIMIT 1",
                    (digest,),
                ).fetchone()
                cursor = None
                if duplicate is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO vfs_asset_links (
                            sha256, src_path, dst_path, strategy, dst_size, dst_mtime_ns
                        ) VALUES (?, ?, ?, 'legacy-v2-migration', ?, ?)
                        """,
                        (
                            digest,
                            resolved.name,
                            resolved.as_posix(),
                            stat.st_size,
                            stat.st_mtime_ns,
                        ),
                    )
                conn.commit()
                if cursor is not None and cursor.rowcount:
                    known[resolved] = (int(cursor.lastrowid), digest, stat.st_size, stat.st_mtime_ns)
                    registered += 1
        return registered

    def recover_incomplete_ingest(
        self,
        *,
        assets_dir: Path,
        staging_dir: Path,
        quarantine_dir: Path,
    ) -> dict[str, int]:
        self.ensure_ready()
        assets_root = assets_dir.resolve(strict=False)
        staging_root = staging_dir.resolve(strict=False)
        quarantine_root = quarantine_dir.resolve(strict=False)
        staging_root.mkdir(parents=True, exist_ok=True)
        quarantine_root.mkdir(parents=True, exist_ok=True)
        recovered = 0
        quarantined = self._audit_committed_assets(assets_root, quarantine_root)
        tracked_staging: set[Path] = set()
        affected_batches: set[str] = set()

        with closing(self.get_connection()) as conn:
            affected_batches.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM ingest_batches WHERE state = 'open'"
                ).fetchall()
            )
            rows = conn.execute(
                """
                SELECT batch_id, ordinal, source_name, sha256, staging_path, destination_path
                FROM ingest_batch_items
                WHERE state IN ('discovered', 'staged')
                ORDER BY created_at, ordinal
                """
            ).fetchall()

        for batch_id, ordinal, source_name, expected_hash, staged_raw, destination_raw in rows:
            batch_id = str(batch_id)
            affected_batches.add(batch_id)
            if not expected_hash or not destination_raw:
                self.interrupt_item(batch_id, int(ordinal), "incomplete_before_staging")
                continue
            try:
                destination = self._inside_root(Path(str(destination_raw)), assets_root)
                staged = self._inside_root(Path(str(staged_raw)), staging_root) if staged_raw else None
            except ValueError:
                self.interrupt_item(batch_id, int(ordinal), "unsafe_recovery_path")
                continue
            if staged is not None:
                tracked_staging.add(staged)

            candidate = destination if destination.is_file() else staged
            if candidate is None or not candidate.is_file():
                self.interrupt_item(batch_id, int(ordinal), "staged_file_missing")
                continue
            try:
                actual_hash = self._sha256_file(candidate)
            except OSError:
                self.interrupt_item(batch_id, int(ordinal), "staged_file_unreadable")
                continue
            if actual_hash != str(expected_hash):
                quarantine_target = quarantine_root / (
                    f"{batch_id}-{ordinal}-{uuid4().hex[:8]}-{Path(str(source_name)).name}"
                )
                quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(candidate), str(quarantine_target))
                self._fsync_directory(quarantine_root)
                self.interrupt_item(batch_id, int(ordinal), "staged_hash_mismatch")
                quarantined += 1
                continue
            if candidate != destination:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(candidate, destination)
                self._fsync_directory(destination.parent)
            self.commit_item(
                batch_id,
                int(ordinal),
                sha256_hex=str(expected_hash),
                src_path=Path(str(source_name)),
                dst_path=destination,
                strategy="recovered-atomic-copy",
            )
            recovered += 1

        removed_partials = 0
        for partial in staging_root.glob("*.partial"):
            if partial.resolve(strict=False) in tracked_staging:
                continue
            partial.unlink(missing_ok=True)
            removed_partials += 1

        for orphan in staging_root.glob("*.staged*"):
            resolved_orphan = orphan.resolve(strict=False)
            if resolved_orphan in tracked_staging or not orphan.is_file():
                continue
            quarantine_target = quarantine_root / f"orphan-{uuid4().hex[:8]}-{orphan.name}"
            os.replace(orphan, quarantine_target)
            self._fsync_directory(quarantine_root)
            quarantined += 1

        for batch_id in affected_batches:
            self._finalize_recovered_batch(batch_id)
        return {
            "recovered": recovered,
            "quarantined": quarantined,
            "removed_partials": removed_partials,
        }

    def _audit_committed_assets(self, assets_root: Path, quarantine_root: Path) -> int:
        if self._upgraded_from_version is not None and self._upgraded_from_version < STORAGE_FORMAT_VERSION:
            return 0
        with closing(self.get_connection()) as conn:
            rows = conn.execute(
                "SELECT sha256, dst_path FROM vfs_asset_links ORDER BY id"
            ).fetchall()

        quarantined = 0
        for expected_hash, destination_raw in rows:
            candidate = Path(str(destination_raw))
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(assets_root)
            except ValueError:
                self._invalidate_committed_asset(str(expected_hash), "unsafe_committed_path")
                continue
            if candidate.is_symlink():
                quarantine_target = quarantine_root / f"committed-{uuid4().hex[:8]}-{candidate.name}"
                os.replace(candidate, quarantine_target)
                self._fsync_directory(quarantine_root)
                self._invalidate_committed_asset(str(expected_hash), "committed_symlink")
                quarantined += 1
                continue
            if not resolved.is_file():
                self._invalidate_committed_asset(str(expected_hash), "committed_file_missing")
                continue
            try:
                actual_hash = self._sha256_file(resolved)
                stat = resolved.stat()
            except OSError:
                self._invalidate_committed_asset(str(expected_hash), "committed_file_unreadable")
                continue
            if actual_hash != str(expected_hash):
                quarantine_target = quarantine_root / f"committed-{uuid4().hex[:8]}-{resolved.name}"
                os.replace(resolved, quarantine_target)
                self._fsync_directory(quarantine_root)
                self._invalidate_committed_asset(str(expected_hash), "committed_hash_mismatch")
                quarantined += 1
                continue
            with closing(self.get_connection()) as conn:
                conn.execute(
                    """
                    UPDATE vfs_asset_links
                    SET dst_path = ?, dst_size = ?, dst_mtime_ns = ?
                    WHERE sha256 = ?
                    """,
                    (resolved.as_posix(), stat.st_size, stat.st_mtime_ns, str(expected_hash)),
                )
                conn.commit()
        return quarantined

    def _invalidate_committed_asset(self, sha256_hex: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch_ids = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT DISTINCT batch_id FROM ingest_batch_items
                    WHERE sha256 = ? AND state = 'committed'
                    """,
                    (sha256_hex,),
                ).fetchall()
            ]
            conn.execute("DELETE FROM vfs_asset_links WHERE sha256 = ?", (sha256_hex,))
            conn.execute(
                """
                UPDATE ingest_batch_items
                SET state = 'interrupted', reason = ?, updated_at = ?
                WHERE sha256 = ? AND state = 'committed'
                """,
                (reason, now, sha256_hex),
            )
            for batch_id in batch_ids:
                conn.execute(
                    """
                    UPDATE ingest_batches
                    SET state = 'interrupted', completed_at = COALESCE(completed_at, ?)
                    WHERE id = ?
                    """,
                    (now, batch_id),
                )
            conn.commit()

    def _finalize_recovered_batch(self, batch_id: str) -> None:
        with closing(self.get_connection()) as conn:
            rows = conn.execute(
                "SELECT state, is_duplicate FROM ingest_batch_items WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        if not rows:
            self.interrupt_batch(batch_id)
            return
        accepted = sum(1 for state, duplicate in rows if state == "committed" and not duplicate)
        duplicates = sum(1 for state, duplicate in rows if state == "committed" and duplicate)
        rejected = sum(1 for state, _duplicate in rows if state == "rejected")
        if all(state in {"committed", "rejected"} for state, _duplicate in rows):
            self.complete_batch(
                batch_id,
                accepted_count=accepted,
                duplicate_count=duplicates,
                rejected_count=rejected,
            )
        else:
            self.interrupt_batch(batch_id)

    @classmethod
    def read_asset_hash_index(cls, db_path: Path, assets_dir: Path) -> dict[str, Path]:
        """Read verified cache entries without initializing or changing storage."""
        if not db_path.is_file():
            return {}
        try:
            with closing(sqlite3.connect(str(db_path))) as conn:
                rows = conn.execute(
                    "SELECT sha256, dst_path, dst_size, dst_mtime_ns FROM vfs_asset_links"
                ).fetchall()
        except sqlite3.Error:
            return {}
        return cls._verified_asset_hash_index(rows, assets_dir)

    @classmethod
    def read_recorded_asset_hash_index(cls, db_path: Path, assets_dir: Path) -> dict[str, Path]:
        """Read recorded identities for reporting missing committed assets."""
        if not db_path.is_file():
            return {}
        try:
            with closing(sqlite3.connect(str(db_path))) as conn:
                rows = conn.execute("SELECT sha256, dst_path FROM vfs_asset_links").fetchall()
        except sqlite3.Error:
            return {}
        root = assets_dir.resolve(strict=False)
        index: dict[str, Path] = {}
        for sha256_hex, dst_path in rows:
            if not sha256_hex or not dst_path:
                continue
            candidate = Path(str(dst_path))
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            index[str(sha256_hex)] = resolved
        return index

    @staticmethod
    def _verified_asset_hash_index(rows: list[tuple[object, ...]], assets_dir: Path) -> dict[str, Path]:
        root = assets_dir.resolve(strict=False)
        index: dict[str, Path] = {}
        for sha256_hex, dst_path, dst_size, dst_mtime_ns in rows:
            if not sha256_hex or not dst_path or dst_size is None or dst_mtime_ns is None:
                continue
            candidate = Path(dst_path)
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            try:
                stat = resolved.stat()
            except OSError:
                continue
            if stat.st_size == int(dst_size) and stat.st_mtime_ns == int(dst_mtime_ns):
                index[str(sha256_hex)] = resolved
        return index

    def _ensure_storage_format(self) -> None:
        try:
            payload = json.loads(self.format_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("Haypile storage format marker is unreadable") from None

        version = payload.get("format_version") if isinstance(payload, dict) else None
        if version is None:
            atomic_write_json(self.format_path, {"format_version": STORAGE_FORMAT_VERSION})
            return
        if not isinstance(version, int):
            raise RuntimeError("Haypile storage format marker is invalid")
        if version > STORAGE_FORMAT_VERSION:
            raise RuntimeError("Haypile storage was created by a newer version")
        if version < STORAGE_FORMAT_VERSION:
            atomic_write_json(self.format_path, {"format_version": STORAGE_FORMAT_VERSION})

    def _assert_compatible_storage_format(self) -> int | None:
        try:
            payload = json.loads(self.format_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("Haypile storage format marker is unreadable") from None
        version = payload.get("format_version") if isinstance(payload, dict) else None
        if not isinstance(version, int):
            raise RuntimeError("Haypile storage format marker is invalid")
        if version > STORAGE_FORMAT_VERSION:
            raise RuntimeError("Haypile storage was created by a newer version")
        return version

    @staticmethod
    def _ensure_vfs_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vfs_asset_links)")}
        if "dst_size" not in columns:
            conn.execute("ALTER TABLE vfs_asset_links ADD COLUMN dst_size INTEGER")
        if "dst_mtime_ns" not in columns:
            conn.execute("ALTER TABLE vfs_asset_links ADD COLUMN dst_mtime_ns INTEGER")

    @staticmethod
    def _ensure_batch_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ingest_batches)")}
        if "state" not in columns:
            conn.execute("ALTER TABLE ingest_batches ADD COLUMN state TEXT NOT NULL DEFAULT 'open'")

    @staticmethod
    def _upsert_link(
        conn: sqlite3.Connection,
        *,
        sha256_hex: str,
        src_path: Path,
        dst_path: Path,
        strategy: str,
        dst_size: int,
        dst_mtime_ns: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO vfs_asset_links (sha256, src_path, dst_path, strategy, dst_size, dst_mtime_ns)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                src_path = excluded.src_path,
                dst_path = excluded.dst_path,
                strategy = excluded.strategy,
                dst_size = excluded.dst_size,
                dst_mtime_ns = excluded.dst_mtime_ns,
                created_at = CURRENT_TIMESTAMP;
            """,
            (
                sha256_hex,
                src_path.as_posix(),
                dst_path.as_posix(),
                strategy,
                int(dst_size),
                int(dst_mtime_ns),
            ),
        )

    @staticmethod
    def _inside_root(path: Path, root: Path) -> Path:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
        return resolved

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
