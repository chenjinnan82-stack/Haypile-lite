from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.services.storage_runtime import STORAGE_FORMAT_VERSION, StorageRuntimeDB
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
            root = Path(tmpdir)
            one = root / "out/one.png"
            two = root / "out/two.png"
            one.parent.mkdir(parents=True)
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            db = StorageRuntimeDB(db_path=Path(tmpdir) / "runtime.db")
            db.record_link(
                sha256_hex="abc",
                src_path=Path("one.png"),
                dst_path=one,
                strategy="copy",
            )
            db.record_link(
                sha256_hex="abc",
                src_path=Path("two.png"),
                dst_path=two,
                strategy="hardlink",
            )

            with closing(db.get_connection()) as conn:
                rows = conn.execute("SELECT sha256, src_path, dst_path, strategy FROM vfs_asset_links").fetchall()

            self.assertEqual(rows, [("abc", "two.png", two.as_posix(), "hardlink")])
        finally:
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_asset_hash_index_only_reuses_unchanged_haypile_asset(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        try:
            assets = tmpdir / "assets"
            asset = assets / "generic/images/hero.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"image")
            db = StorageRuntimeDB(db_path=tmpdir / "index/runtime.db")
            db.record_link(sha256_hex="known-sha", src_path=asset, dst_path=asset, strategy="copy")

            self.assertEqual(db.asset_hash_index(assets), {"known-sha": asset.resolve()})
            self.assertEqual(
                StorageRuntimeDB.read_asset_hash_index(db.db_path, assets),
                {"known-sha": asset.resolve()},
            )
            asset.write_bytes(b"changed-image")
            self.assertEqual(db.asset_hash_index(assets), {})
            self.assertEqual(StorageRuntimeDB.read_asset_hash_index(db.db_path, assets), {})
        finally:
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_ingest_batches_keep_order_and_resolve_latest(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        try:
            db = StorageRuntimeDB(db_path=tmpdir / "index/runtime.db")
            batch_id = db.begin_batch()
            db.record_batch_asset(batch_id, "sha-b", 2)
            db.record_batch_asset(batch_id, "sha-a", 1)
            db.record_batch_asset(batch_id, "sha-a", 3)
            db.complete_batch(batch_id, accepted_count=1, duplicate_count=2, rejected_count=1)
            interrupted_batch = db.begin_batch()
            db.record_batch_asset(interrupted_batch, "sha-c", 0)
            empty_batch = db.begin_batch()
            db.complete_batch(empty_batch, accepted_count=0, duplicate_count=0, rejected_count=2)

            latest = db.latest_batch()

            self.assertEqual(latest["id"], batch_id)
            self.assertEqual(latest["asset_count"], 2)
            self.assertEqual(latest["duplicate_count"], 2)
            self.assertEqual(db.resolve_batch_id("latest"), batch_id)
            self.assertEqual(db.resolve_batch_id(batch_id), batch_id)
            self.assertEqual(db.resolve_batch_id("missing"), "")
            self.assertEqual(db.batch_hashes(batch_id), ["sha-a", "sha-b"])
        finally:
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_storage_format_marker_rejects_newer_data(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        try:
            db_path = tmpdir / "index/runtime.db"
            StorageRuntimeDB(db_path=db_path).ensure_ready()
            marker = db_path.parent / "storage_format.json"
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"format_version": STORAGE_FORMAT_VERSION})

            marker.write_text(json.dumps({"format_version": STORAGE_FORMAT_VERSION + 1}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "newer version"):
                StorageRuntimeDB(db_path=db_path).ensure_ready()
        finally:
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_read_asset_hash_index_does_not_create_a_database(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        try:
            db_path = tmpdir / "index/runtime.db"

            self.assertEqual(StorageRuntimeDB.read_asset_hash_index(db_path, tmpdir / "assets"), {})
            self.assertFalse(db_path.exists())
        finally:
            time.sleep(0.05)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_storage_runtime_adds_cache_columns_to_legacy_database(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        try:
            db_path = tmpdir / "index/runtime.db"
            db_path.parent.mkdir(parents=True)
            asset = tmpdir / "assets/generic/images/legacy.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"legacy")
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    "CREATE TABLE vfs_asset_links ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL, src_path TEXT NOT NULL, "
                    "dst_path TEXT NOT NULL, strategy TEXT NOT NULL, "
                    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                conn.execute(
                    "INSERT INTO vfs_asset_links (sha256, src_path, dst_path, strategy) VALUES (?, ?, ?, ?)",
                    ("legacy-sha", "legacy.png", str(asset), "copy"),
                )
                conn.commit()

            db = StorageRuntimeDB(db_path=db_path)
            db.ensure_ready()
            with closing(db.get_connection()) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(vfs_asset_links)")}
            self.assertTrue({"dst_size", "dst_mtime_ns"}.issubset(columns))
            self.assertEqual(db.asset_hash_index(tmpdir / "assets"), {})
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
