from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.core.config import get_settings
from app.services.json_io import atomic_write_json


STORAGE_FORMAT_VERSION = 1


class StorageRuntimeDB:
    """
    Runtime metadata store for VFS ingest.

    Notes:
    - Force WAL mode to improve concurrent read/write behavior.
    - Keep schema intentionally small and append-friendly.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        settings = get_settings()
        self.db_path: Path = db_path or (settings.INDEX_DIR / "storage_runtime.db")
        self.format_path = self.db_path.parent / "storage_format.json"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = Lock()
        self._initialized = False

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
            with closing(self.get_connection()) as conn:
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
                        accepted_count INTEGER NOT NULL DEFAULT 0,
                        duplicate_count INTEGER NOT NULL DEFAULT 0,
                        rejected_count INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
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
                    CREATE INDEX IF NOT EXISTS idx_ingest_batch_assets_sha256
                    ON ingest_batch_assets(sha256);
                    """
                )
                conn.commit()
            self._ensure_storage_format()
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
                    stat.st_size,
                    stat.st_mtime_ns,
                ),
            )
            conn.commit()

    def begin_batch(self) -> str:
        self.ensure_ready()
        batch_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self.get_connection()) as conn:
            conn.execute(
                "INSERT INTO ingest_batches (id, created_at) VALUES (?, ?)",
                (batch_id, created_at),
            )
            conn.commit()
        return batch_id

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
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                UPDATE ingest_batches
                SET completed_at = ?, accepted_count = ?, duplicate_count = ?, rejected_count = ?
                WHERE id = ?
                """,
                (completed_at, accepted_count, duplicate_count, rejected_count, batch_id),
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
                WHERE b.completed_at IS NOT NULL
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
                "SELECT id FROM ingest_batches WHERE id = ? AND completed_at IS NOT NULL",
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
            # ponytail: v1 adds only a marker; add explicit transforms when v2 changes stored data.
            atomic_write_json(self.format_path, {"format_version": STORAGE_FORMAT_VERSION})
            return
        if not isinstance(version, int):
            raise RuntimeError("Haypile storage format marker is invalid")
        if version > STORAGE_FORMAT_VERSION:
            raise RuntimeError("Haypile storage was created by a newer version")
        if version < STORAGE_FORMAT_VERSION:
            atomic_write_json(self.format_path, {"format_version": STORAGE_FORMAT_VERSION})

    @staticmethod
    def _ensure_vfs_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vfs_asset_links)")}
        if "dst_size" not in columns:
            conn.execute("ALTER TABLE vfs_asset_links ADD COLUMN dst_size INTEGER")
        if "dst_mtime_ns" not in columns:
            conn.execute("ALTER TABLE vfs_asset_links ADD COLUMN dst_mtime_ns INTEGER")
