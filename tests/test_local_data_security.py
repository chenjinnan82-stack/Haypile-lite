from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.config import Settings, _ensure_private_directory
from app.core.ipc import cleanup_unix_socket, start_ipc_listener
from app.main import ManifestStaticFiles, app
from app.services.asset_provenance import public_origin_url
from app.services.json_io import atomic_write_json
from app.services.vfs_storage import VFSStorage
from app_gui import RemoteDownloadWorker


class LocalDataSecurityTests(unittest.TestCase):
    def test_network_defaults_remain_local_only(self) -> None:
        settings = Settings(
            _env_file=None,
            HOST="0.0.0.0",
            CORS_ORIGINS=["*", "https://evil.example", "http://127.0.0.1:5173"],
            IPC_AUTHKEY="test-key",
        )

        self.assertEqual(settings.HOST, "127.0.0.1")
        self.assertEqual(settings.CORS_ORIGINS, ["http://127.0.0.1:5173"])
        self.assertEqual(Settings(_env_file=None, IPC_AUTHKEY="test-key").CORS_ORIGINS, [])

    def test_vision_model_endpoint_cannot_exfiltrate_assets(self) -> None:
        settings = Settings(
            _env_file=None,
            IPC_AUTHKEY="test-key",
            VISION_CLASSIFIER_BASE_URL="https://models.evil.example",
        )

        self.assertEqual(settings.VISION_CLASSIFIER_BASE_URL, "http://127.0.0.1:11434")

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_generated_ipc_secret_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            secret = Path(tmpdir) / "private" / "ipc_authkey"
            with patch.dict(
                os.environ,
                {"HAYPILE_IPC_AUTHKEY_FILE": str(secret), "IPC_AUTHKEY": ""},
                clear=False,
            ):
                settings = Settings(_env_file=None, IPC_AUTHKEY="")

            self.assertEqual(len(settings.IPC_AUTHKEY), 64)
            self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_local_files_and_directories_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "storage"
            _ensure_private_directory(root)
            payload_path = root / "state.json"
            atomic_write_json(payload_path, {"private": True})
            source = Path(tmpdir) / "source.bin"
            source.write_bytes(b"asset")
            destination = root / "assets" / "asset.bin"
            VFSStorage(copy_base_delay=0.1).materialize(source, destination)

            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(payload_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "Unix socket permissions only")
    def test_ipc_socket_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            address = str(Path(tmpdir) / "haypile.sock")
            listener = start_ipc_listener(address=address, authkey=b"test-secret")
            try:
                self.assertEqual(stat.S_IMODE(Path(address).stat().st_mode), 0o600)
            finally:
                listener.close()
                cleanup_unix_socket(address)

    def test_public_origin_strips_credentials_and_query_secrets(self) -> None:
        self.assertEqual(
            public_origin_url("https://user:secret@cdn.example.com:8443/a.png?token=private#part"),
            "https://cdn.example.com:8443/a.png",
        )

    def test_static_assets_send_private_sandbox_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assets = root / "assets"
            manifest = root / "manifest.json"
            assets.mkdir()
            (assets / "active.svg").write_text("<svg><script>alert(1)</script></svg>", encoding="utf-8")
            manifest.write_text(json.dumps({"active.svg": {"type": "image"}}), encoding="utf-8")
            static = ManifestStaticFiles(directory=str(assets), manifest_path=manifest)
            local_app = Starlette(routes=[Mount("/static", app=static)])

            response = TestClient(local_app, base_url="http://127.0.0.1").get("/static/active.svg")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(response.headers["content-security-policy"], "default-src 'none'; sandbox")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_untrusted_host_is_rejected(self) -> None:
        response = TestClient(app, base_url="http://127.0.0.1").get(
            "/healthz",
            headers={"host": "evil.example"},
        )
        self.assertEqual(response.status_code, 400)

    def test_remote_drop_has_a_bounded_url_count(self) -> None:
        urls = [f"https://cdn.example.com/{index}.png" for index in range(100)]
        self.assertEqual(len(RemoteDownloadWorker._dedupe_urls(urls)), RemoteDownloadWorker.MAX_URLS)


if __name__ == "__main__":
    unittest.main()
