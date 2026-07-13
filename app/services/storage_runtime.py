from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock

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
