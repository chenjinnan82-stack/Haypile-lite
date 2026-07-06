from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock

from app.core.config import get_settings


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
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
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
        with closing(self.get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO vfs_asset_links (sha256, src_path, dst_path, strategy)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(sha256) DO UPDATE SET
                    src_path = excluded.src_path,
                    dst_path = excluded.dst_path,
                    strategy = excluded.strategy,
                    created_at = CURRENT_TIMESTAMP;
                """,
                (
                    sha256_hex,
                    str(src_path),
                    str(dst_path),
                    strategy,
                ),
            )
            conn.commit()
