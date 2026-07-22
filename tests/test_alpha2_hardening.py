from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import socket
import tempfile
import threading
import unittest
import re
from multiprocessing import Pipe
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import mcp_server
from app.core.file_lock import InterProcessFileLock
from app.core.ipc import authenticate_ipc_connection, send_ipc_request
from app.services.bundle_service import BundleService
from app.services.media_validator import (
    MediaValidationError,
    _validate_raster_limits,
    validate_media,
    validate_svg,
)
from app.services.real_project_operations import execute_haypile_minimal_real_project_rollback
from app.services.safe_remote_fetcher import SafeFetchError, open_safe_remote, validate_remote_url
from app.services.scanner import AssetScanner
from app.services.storage_runtime import STORAGE_FORMAT_VERSION, StorageRuntimeDB
from app.services.theme_registry import ThemeRegistry
from app.services.vfs_storage import VFSStorage


def _resolver(*addresses: str):
    def resolve(_host: str, port: int, **_kwargs):
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in addresses
        ]

    return resolve


class SafeRemoteFetcherTests(unittest.TestCase):
    def test_rejects_mixed_private_dns_and_metadata_addresses(self) -> None:
        with self.assertRaisesRegex(SafeFetchError, "non_public_address"):
            validate_remote_url(
                "https://assets.example/pika.png",
                resolver=_resolver("8.8.8.8", "10.0.0.2"),
            )
        for url in (
            "http://169.254.169.254/latest/meta-data",
            "http://[::ffff:169.254.169.254]/latest/meta-data",
            "http://127.0.0.1/secret",
        ):
            with self.subTest(url=url), self.assertRaises(SafeFetchError):
                validate_remote_url(url)

    def test_rejects_userinfo_invalid_ports_dns_failure_and_controls(self) -> None:
        failures = (
            "https://user:pass@example.com/image.png",
            "https://example.com:99999/image.png",
            "https://example.com:0/image.png",
            "https://example.com/image.png#fragment",
            "https://example.com\\@127.0.0.1/image.png",
            "https://example.com/image.png\nHost: localhost",
        )
        for url in failures:
            with self.subTest(url=url), self.assertRaises(SafeFetchError):
                validate_remote_url(url, resolver=_resolver("8.8.8.8"))

        def failed_resolver(*_args, **_kwargs):
            raise socket.gaierror("not found")

        with self.assertRaisesRegex(SafeFetchError, "dns_failed"):
            validate_remote_url("https://missing.example/image.png", resolver=failed_resolver)

    def test_pins_verified_ip_ignores_proxy_environment_and_rechecks_peer(self) -> None:
        class FakeSocket:
            def __init__(self, peer: str) -> None:
                self.peer = peer

            def getpeername(self):
                return self.peer, 80

            def settimeout(self, _timeout: float) -> None:
                return None

        class FakeResponse:
            status = 200

            def getheaders(self):
                return [("Content-Type", "image/png"), ("Content-Length", "4")]

            def read(self, _size: int):
                if hasattr(self, "done"):
                    return b""
                self.done = True
                return b"data"

            def close(self):
                return None

        class FakeConnection:
            peer = "8.8.8.8"

            def __init__(self, *_args, **_kwargs) -> None:
                self.sock = FakeSocket(self.peer)

            def request(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return FakeResponse()

            def close(self):
                return None

        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9"}), patch(
            "app.services.safe_remote_fetcher._PinnedHTTPConnection", FakeConnection
        ):
            with open_safe_remote(
                "http://assets.example/pika.png",
                resolver=_resolver("8.8.8.8"),
            ) as response:
                self.assertEqual(b"".join(response.iter_bytes()), b"data")

        class WrongPeerConnection(FakeConnection):
            peer = "1.1.1.1"

        with patch("app.services.safe_remote_fetcher._PinnedHTTPConnection", WrongPeerConnection):
            with self.assertRaisesRegex(SafeFetchError, "peer_address_mismatch"):
                open_safe_remote(
                    "http://assets.example/pika.png",
                    resolver=_resolver("8.8.8.8"),
                )

    def test_dns_and_validation_time_count_against_total_fetch_deadline(self) -> None:
        with patch(
            "app.services.safe_remote_fetcher.time.monotonic",
            side_effect=[100.0, 116.0],
        ), self.assertRaisesRegex(SafeFetchError, "download_timeout"):
            open_safe_remote(
                "http://assets.example/pika.png",
                timeout=15.0,
                resolver=_resolver("8.8.8.8"),
            )


class MediaValidationTests(unittest.TestCase):
    def test_accepts_supported_raster_and_rejects_extension_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid = root / "valid.png"
            mismatch = root / "wrong.jpg"
            Image.new("RGB", (24, 16), "green").save(valid)
            mismatch.write_bytes(valid.read_bytes())
            self.assertEqual(validate_media(valid).mime_type, "image/png")
            with self.assertRaisesRegex(MediaValidationError, "extension_mismatch"):
                validate_media(mismatch)

    def test_rejects_unsafe_svg_features(self) -> None:
        unsafe_payloads = (
            '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject /></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png" /></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><path onclick="run()" /></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><style>.x{fill:url(file:///etc/passwd)}</style></svg>',
        )
        with tempfile.TemporaryDirectory() as raw:
            for index, payload in enumerate(unsafe_payloads):
                path = Path(raw) / f"unsafe-{index}.svg"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(MediaValidationError):
                    validate_svg(path)

    def test_rejects_excessive_total_pixels_across_frames(self) -> None:
        with self.assertRaisesRegex(MediaValidationError, "raster_total_pixel_limit"):
            _validate_raster_limits(2_000, 2_000, 41)


class AtomicIngestRecoveryTests(unittest.TestCase):
    def test_stage_cleans_partial_on_interruption_and_output_open_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.png"
            source.write_bytes(b"asset-bytes")
            staging = root / "staging"
            storage = VFSStorage()

            with self.assertRaises(InterruptedError):
                storage.stage(source, staging, "interrupted", should_stop=lambda: True)
            self.assertEqual(list(staging.glob("*")), [])

            with patch("app.services.vfs_storage.os.fdopen", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    storage.stage(source, staging, "disk-full")
            self.assertEqual(list(staging.glob("*")), [])

    def test_quarantines_unrecorded_staged_file_after_fsync_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            staging = root / "staging"
            quarantine = root / "quarantine"
            runtime = StorageRuntimeDB(root / "index/storage_runtime.db")
            batch_id = runtime.begin_batch()
            runtime.record_item_discovered(batch_id, 1, "source.png")
            source = root / "source.png"
            source.write_bytes(b"durable-staged-bytes")
            VFSStorage().stage(source, staging, f"{batch_id}-1")

            result = runtime.recover_incomplete_ingest(
                assets_dir=assets,
                staging_dir=staging,
                quarantine_dir=quarantine,
            )

            self.assertEqual(result["quarantined"], 1)
            self.assertEqual(list(staging.glob("*")), [])
            self.assertEqual(len(list(quarantine.glob("orphan-*"))), 1)
            self.assertIsNone(runtime.latest_batch())

    def test_recovers_rename_before_database_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            staging = root / "staging" / "ingest"
            quarantine = root / "quarantine" / "ingest"
            source = root / "source.png"
            Image.new("RGB", (20, 12), "green").save(source)
            runtime = StorageRuntimeDB(root / "index" / "storage_runtime.db")
            storage = VFSStorage()
            batch_id = runtime.begin_batch()
            runtime.record_item_discovered(batch_id, 1, source.name)
            staged = storage.stage(source, staging, f"{batch_id}-1")
            destination = assets / "generic" / "images" / "asset.png"
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
                quarantine_dir=quarantine,
            )

            self.assertEqual(result["recovered"], 1)
            self.assertEqual(runtime.asset_hash_index(assets), {staged.sha256: destination.resolve()})
            self.assertEqual(runtime.latest_batch()["id"], batch_id)

    def test_quarantines_hash_mismatch_and_cleans_untracked_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            staging = root / "staging" / "ingest"
            quarantine = root / "quarantine" / "ingest"
            source = root / "source.png"
            Image.new("RGB", (20, 12), "green").save(source)
            runtime = StorageRuntimeDB(root / "index" / "storage_runtime.db")
            storage = VFSStorage()
            batch_id = runtime.begin_batch()
            runtime.record_item_discovered(batch_id, 1, source.name)
            staged = storage.stage(source, staging, f"{batch_id}-1")
            destination = assets / "generic" / "images" / "asset.png"
            runtime.record_item_staged(
                batch_id,
                1,
                media_kind="image",
                sha256_hex=staged.sha256,
                staging_path=staged.path,
                destination_path=destination,
            )
            staged.path.write_bytes(b"changed")
            partial = staging / "orphan.partial"
            partial.write_bytes(b"partial")

            result = runtime.recover_incomplete_ingest(
                assets_dir=assets,
                staging_dir=staging,
                quarantine_dir=quarantine,
            )

            self.assertEqual(result["quarantined"], 1)
            self.assertEqual(result["removed_partials"], 1)
            self.assertFalse(destination.exists())
            self.assertTrue(any(quarantine.iterdir()))

    def test_manifest_only_includes_committed_files_when_runtime_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            index = root / "index"
            committed = assets / "committed.png"
            orphan = assets / "orphan.png"
            assets.mkdir()
            Image.new("RGB", (16, 16), "green").save(committed)
            Image.new("RGB", (16, 16), "orange").save(orphan)
            digest = hashlib.sha256(committed.read_bytes()).hexdigest()
            runtime = StorageRuntimeDB(index / "storage_runtime.db")
            runtime.record_link(
                sha256_hex=digest,
                src_path=Path("committed.png"),
                dst_path=committed,
                strategy="atomic-copy",
            )
            manifest = AssetScanner(
                assets,
                index / "assets_manifest.json",
                index / "storage_runtime.db",
            )._scan_assets_directory_sync()
            self.assertEqual(set(manifest), {"committed.png"})

    def test_committed_asset_survives_projection_failure_and_rebuilds_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            staging = root / "staging"
            destination = assets / "generic/images/asset.png"
            source = root / "source.png"
            Image.new("RGB", (18, 18), "gold").save(source)
            runtime = StorageRuntimeDB(root / "index/storage_runtime.db")
            batch_id = runtime.begin_batch()
            runtime.record_item_discovered(batch_id, 1, source.name)
            staged = VFSStorage().stage(source, staging, f"{batch_id}-1")
            runtime.record_item_staged(
                batch_id,
                1,
                media_kind="image",
                sha256_hex=staged.sha256,
                staging_path=staged.path,
                destination_path=destination,
            )
            VFSStorage().commit_staged(staged.path, destination)
            runtime.commit_item(
                batch_id,
                1,
                sha256_hex=staged.sha256,
                src_path=source,
                dst_path=destination,
                strategy="atomic-copy",
            )
            runtime.complete_batch(
                batch_id,
                accepted_count=1,
                duplicate_count=0,
                rejected_count=0,
            )
            scanner = AssetScanner(
                assets,
                root / "index/assets_manifest.json",
                root / "index/storage_runtime.db",
            )
            with patch("app.services.scanner.atomic_write_json", side_effect=OSError("projection failed")):
                with self.assertRaisesRegex(OSError, "projection failed"):
                    scanner._scan_assets_directory_sync()

            rebuilt = AssetScanner(
                assets,
                root / "index/assets_manifest.json",
                root / "index/storage_runtime.db",
            )._scan_assets_directory_sync()
            self.assertEqual(set(rebuilt), {"generic/images/asset.png"})
            latest = runtime.latest_batch()
            self.assertIsNotNone(latest)
            self.assertEqual(latest["accepted_count"], 1)

    def test_storage_initialization_failure_stops_backend_startup(self) -> None:
        from app.main import app as fastapi_app, lifespan

        async def start_once() -> None:
            async with lifespan(fastapi_app):
                pass

        with patch("app.main.StorageRuntimeDB", side_effect=RuntimeError("damaged database")):
            with self.assertRaisesRegex(RuntimeError, "storage initialization failed"):
                asyncio.run(start_once())

    def test_quarantines_committed_asset_whose_hash_changed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            asset = assets / "generic/images/hero.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"original-bytes")
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            runtime = StorageRuntimeDB(root / "index/storage_runtime.db")
            runtime.record_link(
                sha256_hex=digest,
                src_path=Path("hero.png"),
                dst_path=asset,
                strategy="atomic-copy",
            )
            asset.write_bytes(b"modified-bytes")

            result = runtime.recover_incomplete_ingest(
                assets_dir=assets,
                staging_dir=root / "staging",
                quarantine_dir=root / "quarantine",
            )

            self.assertEqual(result["quarantined"], 1)
            self.assertFalse(asset.exists())
            self.assertEqual(runtime.asset_hash_index(assets), {})
            self.assertEqual(len(list((root / "quarantine").iterdir())), 1)

    def test_legacy_upgrade_recomputes_identity_without_deleting_asset(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            asset = assets / "generic/images/legacy.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"actual-final-bytes")
            db_path = root / "index/storage_runtime.db"
            db_path.parent.mkdir(parents=True)
            (db_path.parent / "storage_format.json").write_text(
                json.dumps({"format_version": 1}),
                encoding="utf-8",
            )
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE vfs_asset_links (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        sha256 TEXT NOT NULL,
                        src_path TEXT NOT NULL,
                        dst_path TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO vfs_asset_links (sha256, src_path, dst_path, strategy)
                    VALUES ('old-untrusted-id', 'legacy.png', ?, 'legacy')
                    """,
                    (asset.as_posix(),),
                )
                conn.commit()

            runtime = StorageRuntimeDB(db_path)
            runtime.recover_incomplete_ingest(
                assets_dir=assets,
                staging_dir=root / "staging",
                quarantine_dir=root / "quarantine",
            )
            self.assertEqual(runtime.register_legacy_assets(assets), 1)

            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.assertTrue(asset.exists())
            self.assertEqual(runtime.asset_hash_index(assets), {digest: asset.resolve()})
            self.assertEqual(
                json.loads((db_path.parent / "storage_format.json").read_text(encoding="utf-8")),
                {"format_version": STORAGE_FORMAT_VERSION},
            )


class ThemeAndIdentityTests(unittest.TestCase):
    def test_quarantines_damaged_theme_and_increments_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            themes = Path(raw) / "themes"
            themes.mkdir()
            path = themes / "generic.json"
            path.write_text("{broken", encoding="utf-8")
            registry = ThemeRegistry(themes)
            payload, _path, created = registry.ensure_theme_contract("generic")
            self.assertTrue(created)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["revision"], 1)
            self.assertTrue(any((Path(raw) / "quarantine" / "themes").iterdir()))

            registry.upsert_image_asset(
                theme_id="generic",
                asset_key="hero",
                asset_url="/static/hero.png",
                role="hero_image",
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreater(saved["revision"], payload["revision"])

    def test_normalized_theme_key_collision_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            themes = Path(raw) / "themes"
            themes.mkdir()
            (themes / "generic.json").write_text(
                json.dumps(
                    {
                        "physical_assets": {
                            "Hero Image": {"url": "/static/a.png"},
                            "hero_image": {"url": "/static/b.png"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "theme_asset_key_collision"):
                ThemeRegistry(themes).ensure_theme_contract("generic")

    def test_filename_role_inference_only_runs_for_legacy_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            themes = Path(raw) / "themes"
            themes.mkdir()
            path = themes / "generic.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "revision": 1,
                        "physical_assets": {
                            "hero_image": {"url": "/static/generic/images/hero_image.png"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            current, _path, _created = ThemeRegistry(themes).ensure_theme_contract("generic")
            self.assertEqual(current["physical_assets"]["hero_image"]["role"], "unknown")

            path.write_text(
                json.dumps(
                    {
                        "physical_assets": {
                            "hero_image": {"url": "/static/generic/images/hero_image.png"}
                        }
                    }
                ),
                encoding="utf-8",
            )
            legacy, _path, _created = ThemeRegistry(themes).ensure_theme_contract("generic")
            self.assertEqual(legacy["physical_assets"]["hero_image"]["role"], "hero_image")

    def test_bundle_id_is_full_hash_and_ambiguous_legacy_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            assets = root / "assets"
            index = root / "index"
            themes = root / "themes"
            first = assets / "one" / "same.png"
            second = assets / "two" / "same.png"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            themes.mkdir()
            Image.new("RGB", (8, 8), "green").save(first)
            Image.new("RGB", (8, 8), "orange").save(second)
            manifest = {
                "one/same.png": {"type": "image", "url_path": "/static/one/same.png"},
                "two/same.png": {"type": "image", "url_path": "/static/two/same.png"},
            }
            index.mkdir()
            (index / "assets_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            service = BundleService(
                assets_dir=assets,
                manifest_path=index / "assets_manifest.json",
                themes_dir=themes,
                runtime_db_path=index / "storage_runtime.db",
            )
            bundles = service.list_bundles()
            self.assertTrue(all(len(bundle["id"]) == 64 for bundle in bundles))
            self.assertIsNone(service.get_bundle("same"))


class IpcMcpAndRollbackTests(unittest.TestCase):
    def test_ipc_authentication_is_bidirectional_and_connection_bounded(self) -> None:
        server, client = Pipe(duplex=True)
        errors: list[Exception] = []

        def authenticate_server() -> None:
            try:
                authenticate_ipc_connection(server, b"shared-secret", timeout=0.5, server=True)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        thread = threading.Thread(target=authenticate_server)
        thread.start()
        try:
            authenticate_ipc_connection(client, b"shared-secret", timeout=0.5, server=False)
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            client.close()
            server.close()

        silent_server, silent_client = Pipe(duplex=True)
        try:
            with self.assertRaises(TimeoutError):
                authenticate_ipc_connection(
                    silent_server,
                    b"shared-secret",
                    timeout=0.01,
                    server=True,
                )
        finally:
            silent_client.close()
            silent_server.close()

    def test_ipc_request_does_not_change_global_socket_timeout(self) -> None:
        before = socket.getdefaulttimeout()
        with tempfile.TemporaryDirectory() as raw:
            result = send_ipc_request(
                {"type": "ping"},
                address=str(Path(raw) / "missing.sock"),
                authkey=b"test-key",
                timeout=0.01,
            )
        self.assertIsNone(result)
        self.assertEqual(socket.getdefaulttimeout(), before)

    def test_interprocess_lock_rejects_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "instance.lock"
            first = InterProcessFileLock(path)
            second = InterProcessFileLock(path)
            self.assertTrue(first.acquire(timeout=0.01))
            try:
                self.assertFalse(second.acquire(timeout=0.01))
            finally:
                first.release()
            self.assertTrue(second.acquire(timeout=0.01))
            second.release()

    def test_mcp_requires_initialized_notification_and_keeps_strict_schemas(self) -> None:
        notification_session = mcp_server.McpProtocolSession()
        self.assertIsNone(
            mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                notification_session,
            )
        )
        self.assertFalse(notification_session.initialize_received)

        session = mcp_server.McpProtocolSession()
        blocked = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            session,
        )
        self.assertEqual(blocked["error"]["code"], -32002)
        initialized = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            session,
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(
            mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, session)["error"]["code"],
            -32002,
        )
        self.assertIsNone(
            mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        )
        self.assertIn(
            "tools",
            mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, session)["result"],
        )
        self.assertIsNone(
            mcp_server.handle({"jsonrpc": "2.0", "method": "tools/list"}, session)
        )
        invalid_arguments = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "haypile_health", "arguments": []},
            },
            session,
        )
        self.assertEqual(invalid_arguments["error"]["code"], -32602)
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in mcp_server.TOOLS))

    def test_rollback_preserves_user_modified_file_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as raw, patch.dict(
            os.environ,
            {"HAYPILE_ENABLE_EXPERIMENTAL_PROJECT_APPLY": "1"},
        ):
            root = Path(raw) / "project"
            source_root = Path(raw) / "source"
            target = root / "generated.txt"
            source = source_root / "generated.txt"
            target.parent.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            source.write_text("applied", encoding="utf-8")
            target.write_text("user changed", encoding="utf-8")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            _write_json(
                root / ".haypile/rollback/haypile-real-project-minimal-apply.json",
                {
                    "source_rehearsal_root": source_root.as_posix(),
                    "entries": [
                        {
                            "path_ref": "generated.txt",
                            "existed_before": False,
                            "source_sha256": expected,
                        }
                    ],
                },
            )
            reports = root / "haypile-rehearsal-reports"
            _write_json(reports / "real-project-minimal-apply-report.json", {"status": "applied", "passed": True})
            _write_json(
                reports / "real-project-minimal-post-apply-verification.json",
                {"status": "verified", "passed": True},
            )
            result = execute_haypile_minimal_real_project_rollback(
                project_root=root,
                human_confirmed=True,
            )
            self.assertEqual(result["status"], "conflict")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "user changed")


class ReleaseWorkflowSafetyTests(unittest.TestCase):
    def test_actions_are_sha_pinned_and_release_source_is_verified(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflows = (
            root / ".github/workflows/ci.yml",
            root / ".github/workflows/macos-build.yml",
            root / ".github/workflows/windows-build.yml",
        )
        uses_pattern = re.compile(r"^\s*-?\s*uses:\s*[^@\s]+@([^\s#]+)", re.MULTILINE)
        for workflow in workflows:
            text = workflow.read_text(encoding="utf-8")
            refs = uses_pattern.findall(text)
            self.assertTrue(refs, workflow.name)
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), workflow.name)
            self.assertNotIn("--clobber", text)
        for workflow in workflows[1:]:
            text = workflow.read_text(encoding="utf-8")
            self.assertIn("source_ref:", text)
            self.assertIn("tag_commit", text.lower())
            self.assertIn("attest-build-provenance@", text)
            self.assertIn('--repo "$GITHUB_REPOSITORY"', text)
        macos_text = workflows[1].read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", macos_text)
        self.assertLess(
            macos_text.index("python -m unittest discover -s tests"),
            macos_text.index("./scripts/build_macos_app.sh"),
        )

    def test_build_scripts_reject_runtime_state_in_packages(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in ("scripts/build_macos_app.sh", "scripts/build_windows_app.ps1"):
            text = (root / relative).read_text(encoding="utf-8")
            for forbidden_name in (
                "ipc_authkey",
                "assets_manifest.json",
                "storage_runtime.db",
                "gui_state.json",
            ):
                self.assertIn(forbidden_name, text, f"{relative}: {forbidden_name}")
            self.assertIn("BUILD_INFO.json", text)

    def test_macos_build_owns_pyside_deployment_cleanup(self) -> None:
        root = Path(__file__).resolve().parents[1]
        text = (root / "scripts/build_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn("--keep-deployment-files", text)
        self.assertIn('DEPLOY_DIR="$ROOT/deployment"', text)
        self.assertIn('rm -rf "$DEPLOY_DIR"', text)
        self.assertIn('DEPLOY_LOG="$BUILD_DIR/pyside6-deploy.log"', text)
        self.assertIn('MACOS_BUILD_VERSION="3002"', text)
        self.assertIn("Add :CFBundleVersion string", text)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
