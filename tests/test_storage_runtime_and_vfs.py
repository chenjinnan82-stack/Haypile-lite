from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.services.storage_runtime import StorageRuntimeDB
from app.services.vfs_storage import VFSStorage


class StorageRuntimeDBTests(unittest.TestCase):
    def test_sqlite_forces_wal_and_normal(self) -> None:
        tmpdir = tempfile.mkdtemp()
        try:
            db = StorageRuntimeDB(db_path=Path(tmpdir) / "runtime.db")
            db.ensure_ready()

            with closing(db.get_connection()) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()
                synchronous = conn.execute("PRAGMA synchronous;").fetchone()

            self.assertIsNotNone(journal_mode)
            self.assertEqual(str(journal_mode[0]).lower(), "wal")
            self.assertIsNotNone(synchronous)
            # NORMAL maps to 1.
            self.assertEqual(int(synchronous[0]), 1)
        finally:
            # On Windows, WAL side files may be released slightly later.
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_record_link_upserts_by_sha256(self) -> None:
        tmpdir = tempfile.mkdtemp()
        try:
            db = StorageRuntimeDB(db_path=Path(tmpdir) / "runtime.db")
            db.record_link(
                sha256_hex="abc",
                src_path=Path("one.png"),
                dst_path=Path("out/one.png"),
                strategy="copy",
            )
            db.record_link(
                sha256_hex="abc",
                src_path=Path("two.png"),
                dst_path=Path("out/two.png"),
                strategy="hardlink",
            )

            with closing(db.get_connection()) as conn:
                rows = conn.execute("SELECT sha256, src_path, dst_path, strategy FROM vfs_asset_links").fetchall()

            self.assertEqual(rows, [("abc", "two.png", "out/two.png", "hardlink")])
        finally:
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)


class VFSStorageTests(unittest.TestCase):
    def test_materialize_uses_copy(self) -> None:
        storage = VFSStorage(copy_max_retries=3, copy_base_delay=0.01)
        tmpdir = Path(tempfile.mkdtemp())
        source = tmpdir / "source.png"
        destination = tmpdir / "out" / "source.png"

        try:
            with patch("app.services.vfs_storage.shutil.copy2") as copy_mock:
                strategy = storage.materialize(source, destination)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(strategy, "copy")
        copy_mock.assert_called_once()

    def test_copy_retries_on_permission_error_with_backoff(self) -> None:
        storage = VFSStorage(copy_max_retries=3, copy_base_delay=0.01)
        tmpdir = Path(tempfile.mkdtemp())
        source = tmpdir / "source.png"
        destination = tmpdir / "out" / "source.png"

        try:
            with patch(
                "app.services.vfs_storage.shutil.copy2",
                side_effect=[PermissionError("locked"), PermissionError("locked"), None],
            ) as copy_mock, patch("app.services.vfs_storage.time.sleep") as sleep_mock:
                strategy = storage.materialize(source, destination)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(strategy, "copy")
        self.assertEqual(copy_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
