from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.core.file_lock import InterProcessFileLock
from app.services.asset_provenance import (
    read_asset_provenance,
    write_asset_provenance,
)
from app.services.bundle_service import BundleService
from app.services.ingest_service import (
    IngestCandidate,
    IngestService,
)
from app.services.scanner import AssetScanner, manifest_dirty_path
from app.services.storage_runtime import StorageRuntimeDB


class IngestServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_dir = self.root / "storage"
        self.assets_dir = self.storage_dir / "assets"
        self.index_dir = self.storage_dir / "index"
        self.themes_dir = self.storage_dir / "themes"
        self.manifest_path = self.index_dir / "assets_manifest.json"
        self.runtime_db_path = self.index_dir / "storage_runtime.db"
        self.storage_dir.mkdir()
        self.service = IngestService(
            storage_dir=self.storage_dir,
            assets_dir=self.assets_dir,
            index_dir=self.index_dir,
            themes_dir=self.themes_dir,
            manifest_path=self.manifest_path,
            fallback_theme="generic",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_service_has_no_gui_network_or_ai_dependency(self) -> None:
        source = Path(__file__).parents[1] / "app/services/ingest_service.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("PySide6", text)
        self.assertNotIn("app_gui", text)
        self.assertNotIn("safe_remote", text)
        self.assertNotIn("classifier", text)
        self.assertNotIn("get_settings", text)

    def test_add_duplicate_reject_progress_and_provenance(self) -> None:
        first = self.root / "first.svg"
        duplicate = self.root / "duplicate.svg"
        invalid = self.root / "invalid.png"
        payload = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>'
        )
        first.write_text(payload, encoding="utf-8")
        duplicate.write_text(payload, encoding="utf-8")
        invalid.write_bytes(b"not-an-image")
        write_asset_provenance(
            first,
            {
                "origin_url": "https://cdn.example.com/path/first.svg",
                "content_type": "image/svg+xml",
                "downloaded_at": "2026-07-26T00:00:00+00:00",
            },
        )
        write_asset_provenance(
            duplicate,
            {"origin_url": "https://chat.example.com/path/duplicate.svg"},
        )
        progress: list[tuple[str, dict[str, int]]] = []

        result = self.service.ingest(
            [IngestCandidate(first), IngestCandidate(duplicate), IngestCandidate(invalid)],
            progress=lambda stage, data: progress.append((stage, data)),
        )

        self.assertTrue(result.success)
        self.assertIsNotNone(result.batch_id)
        self.assertEqual(result.accepted_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.renamed_count, 1)
        self.assertEqual(progress[0][0], "build_hash_index")
        self.assertEqual(progress[-1][0], "complete")
        self.assertTrue(all(set(data) == {"percent", "index", "total"} for _, data in progress))

        stored = next(self.assets_dir.rglob("*.svg"))
        self.assertEqual(stored.read_text(encoding="utf-8"), payload)
        expected_hash = hashlib.sha256(first.read_bytes()).hexdigest()
        provenance = read_asset_provenance(stored)
        self.assertEqual(provenance["origin_url"], "https://cdn.example.com")
        self.assertEqual(provenance["sha256"], expected_hash)
        self.assertEqual(
            provenance["source_key"],
            stored.relative_to(self.assets_dir).as_posix(),
        )
        runtime = StorageRuntimeDB(self.runtime_db_path)
        with closing(runtime.get_connection()) as connection:
            origins = [
                str(row[0])
                for row in connection.execute(
                    "SELECT origin_url FROM ingest_batch_items ORDER BY ordinal"
                ).fetchall()
            ]
        self.assertEqual(
            origins,
            ["https://cdn.example.com", "https://chat.example.com", ""],
        )

    def test_gif_bytes_hash_metadata_restart_and_pending_status(self) -> None:
        source = self.root / "transparent.gif"
        first = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        second = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        first.putpixel((1, 1), (255, 0, 0, 255))
        second.putpixel((2, 2), (0, 255, 0, 255))
        first.save(
            source,
            save_all=True,
            append_images=[second],
            duration=[400, 600],
            loop=0,
            disposal=2,
            optimize=False,
        )
        original = source.read_bytes()
        expected_hash = hashlib.sha256(original).hexdigest()

        result = self.service.ingest([IngestCandidate(source)])

        self.assertTrue(result.success)
        stored = next(self.assets_dir.rglob("*.gif"))
        self.assertEqual(stored.read_bytes(), original)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        item = next(iter(manifest.values()))
        self.assertEqual(item["content_type"], "image/gif")
        self.assertEqual(item["frame_count"], 2)
        self.assertEqual(item["duration_seconds"], 1.0)
        self.assertEqual(item["loop_count"], 0)
        with Image.open(stored) as preview:
            self.assertEqual(preview.n_frames, 2)
            preview.seek(1)
            preview.load()

        bundles = BundleService(
            assets_dir=self.assets_dir,
            manifest_path=self.manifest_path,
            themes_dir=self.themes_dir,
            runtime_db_path=self.runtime_db_path,
        ).list_bundles(batch_id=result.batch_id)
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["sha256"], expected_hash)
        self.assertEqual(bundles[0]["status"], "pending")
        self.assertEqual(bundles[0]["role"], "unknown")

        restarted = self.service.recover_and_project()
        self.assertTrue(restarted.success)
        rebuilt = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt, manifest)

    def test_cancel_releases_lock_and_recovery_can_run(self) -> None:
        source = self.root / "cancel.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>',
            encoding="utf-8",
        )
        cancelled = threading.Event()

        def stop_after_batch_started(stage: str, _data: dict[str, int]) -> None:
            if stage == "build_hash_index":
                cancelled.set()

        result = self.service.ingest(
            [IngestCandidate(source)],
            should_stop=cancelled.is_set,
            progress=stop_after_batch_started,
        )

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.error_code, "ingest_cancelled")
        lock = InterProcessFileLock(self.service.transaction_lock_path)
        self.assertTrue(lock.acquire(timeout=0.0))
        lock.release()
        self.assertTrue(manifest_dirty_path(self.manifest_path).exists())
        self.assertTrue(self.service.recover_and_project().success)
        self.assertFalse(manifest_dirty_path(self.manifest_path).exists())

    def test_commit_failure_is_recovered_after_restart(self) -> None:
        source = self.root / "recover.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>',
            encoding="utf-8",
        )
        expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        original_commit = StorageRuntimeDB.commit_item
        failed = False

        def fail_first_durable_commit(runtime, *args, **kwargs):
            nonlocal failed
            if not failed and kwargs.get("strategy") == "atomic-copy":
                failed = True
                raise sqlite3.OperationalError("simulated commit failure")
            return original_commit(runtime, *args, **kwargs)

        with patch.object(StorageRuntimeDB, "commit_item", new=fail_first_durable_commit):
            result = self.service.ingest([IngestCandidate(source)])

        self.assertEqual(result.error_code, "durable_commit_failed")
        self.assertTrue(manifest_dirty_path(self.manifest_path).exists())
        self.assertEqual(len(list(self.assets_dir.rglob("*.svg"))), 1)
        self.assertTrue(self.service.recover_and_project().success)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        stored = self.assets_dir / next(iter(manifest))
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), expected_hash)
        self.assertEqual(
            StorageRuntimeDB(self.runtime_db_path).asset_hash_index(self.assets_dir)[
                expected_hash
            ],
            stored.resolve(),
        )

    def test_journal_failure_does_not_claim_asset_is_recoverable(self) -> None:
        source = self.root / "journal.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>',
            encoding="utf-8",
        )
        with patch.object(
            StorageRuntimeDB,
            "record_item_staged",
            side_effect=sqlite3.OperationalError("simulated journal failure"),
        ):
            result = self.service.ingest([IngestCandidate(source)])

        self.assertEqual(result.error_code, "ingest_journal_failed")
        self.assertFalse(list(self.storage_dir.rglob("*.staged*")))
        self.assertFalse(list(self.assets_dir.rglob("*.svg")))
        self.assertTrue(self.service.recover_and_project().success)
        self.assertFalse(list(self.assets_dir.rglob("*.svg")))

    def test_duplicate_journal_failure_requires_retry(self) -> None:
        first = self.root / "first.svg"
        duplicate = self.root / "duplicate.svg"
        payload = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>'
        )
        first.write_text(payload, encoding="utf-8")
        duplicate.write_text(payload, encoding="utf-8")
        self.assertTrue(self.service.ingest([IngestCandidate(first)]).success)
        original_commit = StorageRuntimeDB.commit_item

        def fail_duplicate_commit(runtime, *args, **kwargs):
            if kwargs.get("duplicate"):
                raise sqlite3.OperationalError("simulated duplicate journal failure")
            return original_commit(runtime, *args, **kwargs)

        with patch.object(StorageRuntimeDB, "commit_item", new=fail_duplicate_commit):
            failed = self.service.ingest([IngestCandidate(duplicate)])

        self.assertEqual(failed.error_code, "ingest_journal_failed")
        self.assertEqual(failed.duplicate_count, 0)
        self.assertTrue(self.service.recover_and_project().success)
        retried = self.service.ingest([IngestCandidate(duplicate)])
        self.assertTrue(retried.success)
        self.assertEqual(retried.duplicate_count, 1)

    def test_projection_failure_keeps_dirty_and_releases_lock(self) -> None:
        source = self.root / "projection.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>',
            encoding="utf-8",
        )

        async def fail_projection(_scanner, *, should_stop=None):
            raise OSError("simulated projection failure")

        with patch.object(
            AssetScanner,
            "scan_assets_directory",
            new=fail_projection,
        ):
            result = self.service.ingest([IngestCandidate(source)])

        self.assertEqual(result.error_code, "manifest_projection_failed")
        self.assertEqual(result.accepted_count, 1)
        self.assertTrue(manifest_dirty_path(self.manifest_path).exists())
        self.assertEqual(len(list(self.assets_dir.rglob("*.svg"))), 1)
        lock = InterProcessFileLock(self.service.transaction_lock_path)
        self.assertTrue(lock.acquire(timeout=0.0))
        lock.release()
        self.assertTrue(self.service.recover_and_project().success)

    def test_busy_does_not_recover_begin_batch_or_mark_dirty(self) -> None:
        source = self.root / "busy.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>',
            encoding="utf-8",
        )
        holder = InterProcessFileLock(self.service.transaction_lock_path)
        self.assertTrue(holder.acquire(timeout=0.0))
        try:
            with patch(
                "app.services.ingest_service.mark_manifest_dirty"
            ) as mark_dirty, patch.object(
                StorageRuntimeDB, "recover_incomplete_ingest"
            ) as recover, patch.object(
                StorageRuntimeDB, "begin_batch"
            ) as begin:
                result = self.service.ingest([IngestCandidate(source)])
        finally:
            holder.release()

        self.assertEqual(result.error_code, "ingest_busy")
        mark_dirty.assert_not_called()
        recover.assert_not_called()
        begin.assert_not_called()
        self.assertFalse(manifest_dirty_path(self.manifest_path).exists())

    def test_active_batch_cannot_be_recovered_by_second_entry(self) -> None:
        source = self.root / "active.svg"
        source.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="8"></svg>',
            encoding="utf-8",
        )
        active = threading.Event()
        release = threading.Event()
        results = []

        def pause_after_begin(stage: str, _data: dict[str, int]) -> None:
            if stage == "build_hash_index":
                active.set()
                release.wait(timeout=5.0)

        thread = threading.Thread(
            target=lambda: results.append(
                self.service.ingest(
                    [IngestCandidate(source)],
                    progress=pause_after_begin,
                )
            )
        )
        thread.start()
        try:
            self.assertTrue(active.wait(timeout=5.0))
            runtime = StorageRuntimeDB(self.runtime_db_path)
            with closing(runtime.get_connection()) as connection:
                state_before = connection.execute(
                    "SELECT state FROM ingest_batches"
                ).fetchone()[0]
            second = self.service.recover_and_project(lock_timeout=0.0)
            with closing(runtime.get_connection()) as connection:
                state_after = connection.execute(
                    "SELECT state FROM ingest_batches"
                ).fetchone()[0]
            self.assertEqual(second.error_code, "ingest_busy")
            self.assertEqual(state_before, "open")
            self.assertEqual(state_after, "open")
        finally:
            release.set()
            thread.join(timeout=10.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)


if __name__ == "__main__":
    unittest.main()
