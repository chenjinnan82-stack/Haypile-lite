from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.v1.bundles import get_bundle_service, router as bundles_router
from app.core.config import get_settings
from app.models.bundle import BundlePayload
from app.services.asset_provenance import write_asset_provenance
from app.services.bundle_service import BundleService
from app.services.ingest_service import IngestService
from app.services.media_validator import MediaValidationError, validate_media
from app.services.safe_remote_fetcher import download_remote_media
from app.services.scanner import AssetScanner
from app.services.storage_runtime import StorageRuntimeDB
from app.services.vfs_storage import VFSStorage
import mcp_server


def _write_gif(
    path: Path,
    *,
    durations: list[int] | None = None,
    loop: int | None = 0,
    size: tuple[int, int] = (8, 8),
    frame_count: int = 2,
    transparent: bool = False,
) -> None:
    frames: list[Image.Image] = []
    for index in range(frame_count):
        frame = Image.new(
            "RGBA",
            size,
            (0, 0, 0, 0) if transparent else (index * 50, 120, 200, 255),
        )
        if transparent:
            frame.putpixel(
                (index % size[0], index % size[1]),
                (index * 50, 120, 200, 255),
            )
        frames.append(frame)
    options: dict[str, object] = {
        "save_all": True,
        "append_images": frames[1:],
        "optimize": False,
        "disposal": 2,
    }
    if durations is not None:
        options["duration"] = durations
    if loop is not None:
        options["loop"] = loop
    frames[0].save(path, **options)


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


class _RemoteResponse:
    def __init__(self, content_type: str, payload: bytes) -> None:
        self.headers = {
            "content-type": content_type,
            "content-length": str(len(payload)),
        }
        self.payload = payload

    def iter_bytes(self):
        yield self.payload

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class GifValidationTests(unittest.TestCase):
    def test_reports_declared_animation_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "reaction.gif"
            _write_gif(path, durations=[10, 30], loop=0)

            result = validate_media(path)

            self.assertEqual(result.kind, "image")
            self.assertEqual(result.mime_type, "image/gif")
            self.assertEqual((result.width, result.height), (8, 8))
            self.assertEqual(result.frame_count, 2)
            self.assertAlmostEqual(result.duration_seconds or 0, 0.04)
            self.assertEqual(result.loop_count, 0)

    def test_single_frame_positive_delay_has_null_duration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "still.gif"
            _write_gif(path, durations=[500], loop=0, frame_count=1)

            result = validate_media(path)

            self.assertEqual(result.frame_count, 1)
            self.assertIsNone(result.duration_seconds)
            self.assertEqual(result.loop_count, 0)

    def test_rejects_multiframe_gif_without_any_positive_delay(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "zero-delay.gif"
            _write_gif(path, durations=[0, 0])

            with self.assertRaisesRegex(MediaValidationError, "gif_missing_frame_delay"):
                validate_media(path)

    def test_limits_use_effective_delay_and_patchable_constants(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "bounded.gif"
            _write_gif(path, durations=[10, 10])

            with patch(
                "app.services.media_validator.MAX_GIF_EFFECTIVE_DURATION_MS",
                199,
            ), self.assertRaisesRegex(MediaValidationError, "gif_duration_limit"):
                validate_media(path)
            with patch(
                "app.services.media_validator.MAX_GIF_FRAMES",
                1,
            ), self.assertRaisesRegex(MediaValidationError, "gif_frame_limit"):
                validate_media(path)
            with patch(
                "app.services.media_validator.MAX_GIF_DIMENSION",
                7,
            ), self.assertRaisesRegex(MediaValidationError, "gif_dimension_limit"):
                validate_media(path)
            with patch(
                "app.services.media_validator.MAX_RASTER_TOTAL_PIXELS",
                127,
            ), self.assertRaisesRegex(MediaValidationError, "raster_total_pixel_limit"):
                validate_media(path)
            with patch(
                "app.services.media_validator.MAX_GIF_BYTES",
                path.stat().st_size - 1,
            ), self.assertRaisesRegex(MediaValidationError, "gif_size_limit"):
                validate_media(path)

    def test_rejects_corrupt_and_extension_mismatched_gif(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid = root / "valid.gif"
            mismatch = root / "wrong.png"
            corrupt = root / "corrupt.gif"
            _write_gif(valid, durations=[100, 100])
            mismatch.write_bytes(valid.read_bytes())
            corrupt.write_bytes(b"GIF89a")

            with self.assertRaisesRegex(MediaValidationError, "extension_mismatch"):
                validate_media(mismatch)
            with self.assertRaisesRegex(MediaValidationError, "invalid_raster"):
                validate_media(corrupt)


class GifIntakeAndProjectionTests(unittest.TestCase):
    def test_scanner_projects_image_mime_and_gif_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            assets.mkdir()
            gif = assets / "motion.gif"
            single_gif = assets / "single.gif"
            png = assets / "still.png"
            _write_gif(gif, durations=[40, 60], loop=2)
            _write_gif(single_gif, durations=[500], loop=0, frame_count=1)
            Image.new("RGB", (6, 4), "green").save(png)
            scanner = AssetScanner(
                assets_dir=assets,
                manifest_path=root / "index/assets_manifest.json",
                runtime_db_path=root / "index/missing.db",
            )

            manifest = asyncio.run(scanner.scan_assets_directory())

            self.assertEqual(manifest["motion.gif"]["content_type"], "image/gif")
            self.assertEqual(manifest["motion.gif"]["frame_count"], 2)
            self.assertEqual(manifest["motion.gif"]["duration_seconds"], 0.1)
            self.assertEqual(manifest["motion.gif"]["loop_count"], 2)
            self.assertIsNone(manifest["single.gif"]["duration_seconds"])
            self.assertEqual(manifest["still.png"]["content_type"], "image/png")
            self.assertNotIn("frame_count", manifest["still.png"])

            service = BundleService(
                assets_dir=assets,
                manifest_path=root / "index/assets_manifest.json",
                themes_dir=root / "themes",
                runtime_db_path=root / "index/missing.db",
            )
            single_bundle = next(
                bundle
                for bundle in service.list_bundles()
                if bundle["source_key"] == "single.gif"
            )
            self.assertIsNone(single_bundle["duration_seconds"])
            self.assertIsNone(
                mcp_server._handoff_asset(single_bundle)["duration_seconds"]
            )

    def test_legacy_registration_keeps_invalid_gif_unregistered_and_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            assets.mkdir()
            valid = assets / "valid.gif"
            invalid = assets / "invalid.gif"
            _write_gif(valid, durations=[100, 100])
            invalid.write_bytes(b"GIF89a")
            runtime = StorageRuntimeDB(root / "index/storage_runtime.db")

            registered = runtime.register_legacy_assets(assets)

            digest = hashlib.sha256(valid.read_bytes()).hexdigest()
            self.assertEqual(registered, 1)
            self.assertEqual(runtime.asset_hash_index(assets), {digest: valid.resolve()})
            self.assertTrue(invalid.exists())

    def test_recovers_original_gif_after_rename_before_database_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "reaction.gif"
            assets = root / "assets"
            staging = root / "staging"
            _write_gif(source, durations=[40, 60])
            original = source.read_bytes()
            runtime = StorageRuntimeDB(root / "index/storage_runtime.db")
            storage = VFSStorage()
            batch_id = runtime.begin_batch()
            runtime.record_item_discovered(batch_id, 1, source.name)
            staged = storage.stage(source, staging, f"{batch_id}-1")
            destination = assets / "generic/images/reaction.gif"
            runtime.record_item_staged(
                batch_id,
                1,
                media_kind="image",
                sha256_hex=staged.sha256,
                staging_path=staged.path,
                destination_path=destination,
            )
            storage.commit_staged(staged.path, destination)

            result = runtime.recover_incomplete_ingest(
                assets_dir=assets,
                staging_dir=staging,
                quarantine_dir=root / "quarantine",
            )

            self.assertEqual(result["recovered"], 1)
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(validate_media(destination).mime_type, "image/gif")

    def test_direct_gif_download_is_bounded_and_signature_is_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            incoming = Path(raw)
            gif_buffer = io.BytesIO()
            frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
            frames[0].save(
                gif_buffer,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=[100, 100],
                loop=0,
            )
            payload = gif_buffer.getvalue()

            path, reason = download_remote_media(
                "https://cdn.example/reaction.gif",
                incoming,
                1,
                max_bytes=1024 * 1024,
                timeout=1,
                opener=lambda *_args, **_kwargs: _RemoteResponse("image/gif", payload),
            )

            self.assertEqual(reason, "")
            self.assertIsNotNone(path)
            self.assertEqual(validate_media(path).mime_type, "image/gif")

            fake_path, fake_reason = download_remote_media(
                "https://cdn.example/fake.gif",
                incoming,
                2,
                max_bytes=1024 * 1024,
                timeout=1,
                opener=lambda *_args, **_kwargs: _RemoteResponse("image/gif", _png_bytes()),
            )
            self.assertEqual(fake_reason, "")
            with self.assertRaisesRegex(MediaValidationError, "extension_mismatch"):
                validate_media(fake_path)

    def test_static_url_serves_original_gif_bytes_and_mime(self) -> None:
        from app.main import ManifestStaticFiles

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            gif = assets / "generic/images/transparent.gif"
            gif.parent.mkdir(parents=True)
            _write_gif(gif, durations=[40, 60], transparent=True)
            original = gif.read_bytes()
            manifest_path = root / "index/assets_manifest.json"
            scanner = AssetScanner(
                assets_dir=assets,
                manifest_path=manifest_path,
                runtime_db_path=root / "index/missing.db",
            )
            asyncio.run(scanner.scan_assets_directory())
            local_app = FastAPI()
            local_app.mount(
                "/static",
                ManifestStaticFiles(
                    directory=str(assets),
                    manifest_path=manifest_path,
                ),
            )

            with TestClient(local_app, base_url="http://127.0.0.1") as client:
                response = client.get("/static/generic/images/transparent.gif")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, original)
            self.assertEqual(response.headers["content-type"], "image/gif")

    def test_remote_video_and_gif_size_limit_fail_before_storage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            incoming = Path(raw)
            video, video_reason = download_remote_media(
                "https://cdn.example/clip.mp4",
                incoming,
                1,
                max_bytes=1024,
                timeout=1,
                opener=lambda *_args, **_kwargs: _RemoteResponse("video/mp4", b"video"),
            )
            self.assertIsNone(video)
            self.assertEqual(video_reason, "video_not_supported")

            with patch("app.services.safe_remote_fetcher.MAX_GIF_BYTES", 3):
                oversized, reason = download_remote_media(
                    "https://cdn.example/large.gif",
                    incoming,
                    2,
                    max_bytes=1024,
                    timeout=1,
                    opener=lambda *_args, **_kwargs: _RemoteResponse("image/gif", b"GIF89a"),
                )
            self.assertIsNone(oversized)
            self.assertEqual(reason, "gif_too_large")


class GifBundleContractTests(unittest.TestCase):
    def test_bundle_prefers_manifest_mime_enforces_roles_and_handoffs_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            themes = root / "themes"
            manifest_path = root / "index/assets_manifest.json"
            gif = assets / "generic/images/reaction.gif"
            png = assets / "generic/images/still.png"
            gif.parent.mkdir(parents=True)
            themes.mkdir()
            manifest_path.parent.mkdir()
            _write_gif(gif, durations=[40, 60], loop=0)
            Image.new("RGB", (8, 8), "green").save(png)
            write_asset_provenance(gif, {"content_type": "image/png"})
            manifest_path.write_text(
                json.dumps(
                    {
                        "generic/images/reaction.gif": {
                            "type": "image",
                            "content_type": "image/gif",
                            "duration_seconds": 0.1,
                            "frame_count": 2,
                            "loop_count": 0,
                            "url_path": "/static/generic/images/reaction.gif",
                        },
                        "generic/images/still.png": {
                            "type": "image",
                            "content_type": "image/png",
                            "url_path": "/static/generic/images/still.png",
                        },
                    }
                ),
                encoding="utf-8",
            )
            service = BundleService(
                assets_dir=assets,
                manifest_path=manifest_path,
                themes_dir=themes,
                runtime_db_path=root / "index/storage_runtime.db",
            )

            gif_bundle = service.get_bundle(hashlib.sha256(gif.read_bytes()).hexdigest())
            self.assertIsNotNone(gif_bundle)
            self.assertEqual(gif_bundle["content_type"], "image/gif")
            self.assertEqual(gif_bundle["frame_count"], 2)
            self.assertEqual(gif_bundle["loop_count"], 0)
            BundlePayload.model_validate(gif_bundle)

            updated = service.set_bundle_role(gif_bundle["id"], "sticker")
            self.assertEqual(updated["status"], "ready")
            self.assertEqual(updated["role"], "sticker")

            png_bundle = service.get_bundle(hashlib.sha256(png.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ValueError, "validated GIF"):
                service.set_bundle_role(png_bundle["id"], "reaction")

            mcp_asset = mcp_server._handoff_asset(updated)
            self.assertEqual(mcp_asset["content_type"], "image/gif")
            self.assertEqual(mcp_asset["frame_count"], 2)
            self.assertEqual(mcp_asset["loop_count"], 0)
            self.assertEqual(mcp_asset["provenance"]["content_type"], "image/gif")

            app = FastAPI()
            app.include_router(bundles_router, prefix="/api/v1")
            app.dependency_overrides[get_bundle_service] = lambda: service
            with TestClient(app) as client:
                http_asset = client.get(f"/api/v1/bundles/{updated['id']}").json()
            self.assertEqual(http_asset["content_type"], "image/gif")
            self.assertEqual(http_asset["frame_count"], 2)
            self.assertEqual(http_asset["loop_count"], 0)


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtGui import QCloseEvent, QHideEvent, QImageReader, QMovie
    from PySide6.QtWidgets import QApplication

    from app_gui import (
        IngestWorker,
        MaterialPanelWindow,
        RemoteDownloadWorker,
        _classify_registered_bundle,
    )
except (ImportError, OSError):
    QApplication = None
    MaterialPanelWindow = None
    IngestWorker = None
    QImageReader = None
    QMovie = None
    _classify_registered_bundle = None


@unittest.skipIf(QApplication is None, "PySide6 is unavailable")
class GifGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_gif_roles_are_visible_and_ai_retry_is_hidden(self) -> None:
        panel = MaterialPanelWindow()
        self.addCleanup(panel.close)
        bundle = {
            "id": "a" * 64,
            "theme_id": "generic",
            "type": "image",
            "role": "unknown",
            "status": "pending",
            "sha256": "a" * 64,
            "source_key": "generic/images/reaction.gif",
            "url": "/static/generic/images/reaction.gif",
            "access": "manifest_static",
            "content_type": "image/gif",
            "frame_count": 2,
            "loop_count": 0,
            "duration_seconds": 0.2,
            "ai_suggestions": {},
        }

        panel._show_detail_for_bundle(bundle)

        self.assertFalse(panel.role_row.isHidden())
        self.assertFalse(panel.gif_role_row.isHidden())
        self.assertEqual(set(panel.gif_role_buttons), {"reaction", "sticker", "ui_animation"})
        self.assertTrue(panel.retry_ai_button.isHidden())
        handoff = panel._handoff_asset(bundle, "http://127.0.0.1:8010")
        self.assertEqual(handoff["content_type"], "image/gif")
        self.assertEqual(handoff["frame_count"], 2)
        self.assertEqual(handoff["loop_count"], 0)
        self.assertEqual(handoff["provenance"]["content_type"], "image/gif")

    def test_copy_selected_handoff_uses_keyword_toast_contract(self) -> None:
        panel = MaterialPanelWindow()
        self.addCleanup(panel.close)
        bundle = {
            "id": "a" * 64,
            "theme_id": "generic",
            "type": "image",
            "role": "reaction",
            "status": "ready",
            "sha256": "a" * 64,
            "source_key": "generic/images/reaction.gif",
            "url": "/static/generic/images/reaction.gif",
            "access": "manifest_static",
            "content_type": "image/gif",
            "duration_seconds": 0.2,
            "frame_count": 2,
            "loop_count": 0,
            "ai_suggestions": {},
        }
        toasts: list[tuple[str, bool]] = []

        def keyword_only_toast(message: str, *, success: bool) -> None:
            toasts.append((message, success))

        panel.set_toast_handler(keyword_only_toast)
        panel._selected_bundle_id = bundle["id"]
        with patch.object(panel, "_get_bundle_safely", return_value=bundle):
            panel._copy_selected_handoff()

        self.assertEqual(toasts[-1][1], True)

    def test_transparent_gif_preserves_bytes_hash_rescan_and_preview(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / "storage"
            assets = storage / "assets"
            index = storage / "index"
            themes = storage / "themes"
            first = root / "chat-reaction.gif"
            duplicate = root / "same-reaction.gif"
            _write_gif(first, durations=[40, 60], loop=0, transparent=True)
            original = first.read_bytes()
            expected_hash = hashlib.sha256(original).hexdigest()
            duplicate.write_bytes(first.read_bytes())
            environment = {
                "STORAGE_DIR": storage.as_posix(),
                "ASSETS_DIR": assets.as_posix(),
                "INDEX_DIR": index.as_posix(),
                "THEMES_DIR": themes.as_posix(),
                "MANIFEST_PATH": (index / "assets_manifest.json").as_posix(),
                "VISION_CLASSIFIER_ENABLED": "1",
                "VISION_FALLBACK_THEME": "generic",
                "HAYPILE_UI_LANG": "en",
            }
            with patch.dict(os.environ, environment, clear=False):
                get_settings.cache_clear()
                try:
                    worker = IngestWorker([first, duplicate], assets, ai_enabled=True)
                    service = IngestService(
                        storage_dir=storage,
                        assets_dir=assets,
                        index_dir=index,
                        themes_dir=themes,
                        manifest_path=index / "assets_manifest.json",
                    )
                    with patch(
                        "app.services.ingest_service.MAX_GIF_BYTES",
                        first.stat().st_size - 1,
                    ):
                        self.assertEqual(
                            service._candidate_rejection_code(first),
                            "gif_file_too_large",
                        )
                    finished: list[tuple[str, bool]] = []
                    worker.finished_signal.connect(
                        lambda message, success: finished.append((message, success))
                    )
                    worker.run()

                    self.assertTrue(finished[-1][1])
                    manifest_path = index / "assets_manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    self.assertEqual(len(manifest), 1)
                    item = next(iter(manifest.values()))
                    self.assertEqual(item["content_type"], "image/gif")
                    self.assertEqual(item["frame_count"], 2)
                    source_key = next(iter(manifest))
                    stored = assets / source_key
                    self.assertEqual(stored.read_bytes(), original)
                    with Image.open(stored) as image:
                        self.assertEqual(
                            image.convert("RGBA").getchannel("A").getextrema(),
                            (0, 255),
                        )

                    scanner = AssetScanner(
                        assets_dir=assets,
                        manifest_path=manifest_path,
                        runtime_db_path=index / "storage_runtime.db",
                    )
                    rescanned = asyncio.run(scanner.scan_assets_directory())
                    self.assertEqual(rescanned, manifest)

                    service = BundleService(
                        assets_dir=assets,
                        manifest_path=manifest_path,
                        themes_dir=themes,
                        runtime_db_path=index / "storage_runtime.db",
                    )
                    bundles = service.list_bundles()
                    self.assertEqual(len(bundles), 1)
                    self.assertEqual(bundles[0]["sha256"], expected_hash)
                    self.assertEqual(bundles[0]["status"], "pending")
                    self.assertEqual(bundles[0]["ai_suggestions"], {})
                    movie = QMovie(str(stored))
                    self.assertTrue(movie.isValid())
                    self.assertEqual(movie.frameCount(), 2)
                    self.assertTrue(movie.jumpToFrame(0))
                    self.assertFalse(movie.currentPixmap().isNull())
                    self.assertTrue(movie.currentPixmap().hasAlphaChannel())
                    ready = service.set_bundle_role(bundles[0]["id"], "reaction")
                    self.assertEqual(ready["status"], "ready")
                finally:
                    get_settings.cache_clear()

    def test_preview_controller_holds_last_frame_then_resets(self) -> None:
        class FakeMovie:
            def __init__(self) -> None:
                self.paused = False
                self.stopped = 0
                self.jumps: list[int] = []

            def setPaused(self, paused: bool) -> None:
                self.paused = paused

            def nextFrameDelay(self) -> int:
                return 5

            def stop(self) -> None:
                self.stopped += 1

            def jumpToFrame(self, frame: int) -> bool:
                self.jumps.append(frame)
                return True

        class FakeTimer:
            def __init__(self) -> None:
                self.started_with: int | None = None
                self.stopped = 0

            def start(self, delay: int) -> None:
                self.started_with = delay

            def stop(self) -> None:
                self.stopped += 1

        panel = MaterialPanelWindow()
        self.addCleanup(panel.close)
        movie = FakeMovie()
        timer = FakeTimer()
        panel._gif_movie = movie
        panel._gif_frame_count = 2
        panel._gif_stop_timer = timer

        panel._on_gif_frame_changed(1)

        self.assertTrue(movie.paused)
        self.assertEqual(timer.started_with, 20)
        panel._finish_gif_preview()
        self.assertEqual(movie.stopped, 1)
        self.assertEqual(movie.jumps, [0])

        panel.set_low_power_enabled(True)
        self.assertEqual(movie.stopped, 2)
        self.assertEqual(movie.jumps, [0, 0])
        panel._gif_movie = None

    def test_preview_lifecycle_paths_use_the_same_reset(self) -> None:
        class StopCalled(Exception):
            pass

        panel = MaterialPanelWindow()
        self.addCleanup(panel.close)
        triggers = (
            panel.refresh,
            lambda: panel._show_preview_for_item(
                SimpleNamespace(asset_type="audio", source_key=""),
                {},
            ),
            lambda: panel.hideEvent(QHideEvent()),
            lambda: panel.closeEvent(QCloseEvent()),
        )
        for trigger in triggers:
            with self.subTest(trigger=trigger), patch.object(
                panel,
                "_stop_gif_preview",
                side_effect=StopCalled,
            ):
                with self.assertRaises(StopCalled):
                    trigger()

    def test_qmovie_gif_plugin_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "smoke.gif"
            _write_gif(path, durations=[10, 20], loop=0)
            formats = {
                bytes(value).decode("ascii", errors="ignore").lower()
                for value in QImageReader.supportedImageFormats()
            }
            movie = QMovie(str(path))

            self.assertIn("gif", formats)
            self.assertTrue(movie.isValid())
            self.assertEqual(movie.frameCount(), 2)
            self.assertEqual(movie.loopCount(), -1)
            self.assertTrue(movie.jumpToFrame(0))
            self.assertEqual(movie.nextFrameDelay(), 100)

    def test_panel_uses_qmovie_only_for_multiframe_gif(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "motion.gif"
            _write_gif(path, durations=[40, 60], loop=0)
            panel = MaterialPanelWindow()
            self.addCleanup(panel.close)
            item = SimpleNamespace(
                asset_type="image",
                source_key="motion.gif",
            )
            with patch(
                "app_gui.get_settings",
                return_value=SimpleNamespace(ASSETS_DIR=root),
            ):
                panel._show_preview_for_item(
                    item,
                    {"content_type": "image/gif", "frame_count": 2},
                )
                self.assertIsNone(panel._gif_movie)
                with patch.object(panel, "isVisible", return_value=True):
                    panel._show_preview_for_item(
                        item,
                        {"content_type": "image/gif", "frame_count": 2},
                    )

            self.assertIsNotNone(panel._gif_movie)
            self.assertTrue(panel._gif_movie.isValid())
            panel._stop_gif_preview(reset_to_first=True, discard=True)

    def test_remote_video_rejection_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            worker = RemoteDownloadWorker(
                ["https://cdn.example/clip.mp4"],
                Path(raw),
            )
            finished: list[tuple[list[Path], str, bool]] = []
            worker.finished_signal.connect(
                lambda files, message, success: finished.append(
                    (files, message, success)
                )
            )
            with patch.object(
                worker,
                "_download_one",
                return_value=(None, "video_not_supported"),
            ):
                worker._run_downloads([])

            self.assertFalse(finished[-1][2])
            self.assertTrue(
                "视频" in finished[-1][1] or "Video" in finished[-1][1]
            )

    def test_local_mp4_and_webm_rejection_is_explicit_and_leaves_no_media(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            storage = root / "storage"
            assets = storage / "assets"
            mp4 = root / "clip.mp4"
            webm = root / "clip.webm"
            mp4.write_bytes(b"not-a-video")
            webm.write_bytes(b"not-a-video")
            environment = {
                "STORAGE_DIR": storage.as_posix(),
                "ASSETS_DIR": assets.as_posix(),
                "INDEX_DIR": (storage / "index").as_posix(),
                "THEMES_DIR": (storage / "themes").as_posix(),
                "MANIFEST_PATH": (storage / "index/assets_manifest.json").as_posix(),
                "VISION_FALLBACK_THEME": "generic",
                "HAYPILE_UI_LANG": "en",
            }
            with patch.dict(os.environ, environment, clear=False):
                get_settings.cache_clear()
                try:
                    worker = IngestWorker([mp4, webm], assets, ai_enabled=False)
                    finished: list[tuple[str, bool]] = []
                    worker.finished_signal.connect(
                        lambda message, success: finished.append((message, success))
                    )

                    worker.run()

                    self.assertFalse(finished[-1][1])
                    self.assertIn("Video", finished[-1][0])
                    self.assertFalse(any(assets.rglob("*")) if assets.exists() else False)
                    self.assertFalse(list(storage.rglob("*.provenance.json")))
                    self.assertFalse(list(storage.rglob("*.part")))
                finally:
                    get_settings.cache_clear()

    def test_gif_never_reaches_classifier(self) -> None:
        class FakeClassifier:
            called = False

            async def classify_image(self, *_args, **_kwargs):
                self.called = True
                raise AssertionError("GIF reached the classifier")

        classifier = FakeClassifier()
        bundle = {
            "type": "image",
            "content_type": "image/gif",
            "source_key": "generic/images/reaction.gif",
        }

        with self.assertRaisesRegex(ValueError, "unsupported_bundle"):
            asyncio.run(
                _classify_registered_bundle(
                    bundle,
                    Path("/unused"),
                    classifier,
                    [],
                    SimpleNamespace(),
                )
            )
        self.assertFalse(classifier.called)


if __name__ == "__main__":
    unittest.main()
