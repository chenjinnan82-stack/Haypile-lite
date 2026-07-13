from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server

ROOT = Path(__file__).resolve().parents[1]


class McpServerTests(unittest.TestCase):
    def test_app_entry_mcp_mode_does_not_load_qt(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        process = subprocess.run(
            [sys.executable, "-X", "importtime", str(ROOT / "app_gui.py"), "--mcp"],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            check=True,
            cwd=ROOT,
        )

        response = json.loads(process.stdout)
        self.assertEqual(response["result"]["serverInfo"]["version"], "0.2.0")
        self.assertNotIn("PySide6", process.stderr)

    def test_lists_haypile_tools(self) -> None:
        response = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertIn("haypile_list_bundles", names)
        self.assertIn("haypile_copy_handoff", names)
        self.assertIn("haypile_get_theme", names)

    def test_list_bundles_calls_http_contract(self) -> None:
        with patch.object(mcp_server, "get_json", return_value=[{"id": "hero"}]) as get_json:
            response = mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "haypile_list_bundles",
                        "arguments": {"status": "ready", "type": "image", "role": "hero_image"},
                    },
                }
            )

        get_json.assert_called_once_with("/api/v1/bundles?status=ready&type=image&role=hero_image")
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload, [{"id": "hero"}])

    def test_list_bundles_passes_limit_and_cursor(self) -> None:
        with patch.object(mcp_server, "get_json", return_value=[{"id": "next"}]) as get_json:
            response = mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        "name": "haypile_list_bundles",
                        "arguments": {"limit": 2, "cursor": "generic/images/hero.png"},
                    },
                }
            )

        get_json.assert_called_once_with(
            "/api/v1/bundles?status=ready&limit=2&cursor=generic%2Fimages%2Fhero.png"
        )
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), [{"id": "next"}])

    def test_health_keeps_ready_status(self) -> None:
        with patch.object(
            mcp_server,
            "get_status_json",
            side_effect=[
                {"status_code": 200, "body": {"status": "ok"}},
                {"status_code": 503, "body": {"detail": "not ready"}},
            ],
        ):
            response = mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "haypile_health", "arguments": {}},
                }
            )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["ready"]["status_code"], 503)

    def test_copy_handoff_returns_mcp_handoff_payload(self) -> None:
        bundle = {
            "id": "hero",
            "theme_id": "generic",
            "type": "image",
            "role": "hero_image",
            "status": "ready",
            "sha256": "sha",
            "source_key": "generic/images/hero.png",
            "url": "/static/generic/images/hero.png",
            "access": "manifest_static",
        }
        with patch.object(mcp_server, "list_bundles", return_value=[bundle]) as list_bundles:
            response = mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "haypile_copy_handoff",
                        "arguments": {"status": "ready", "type": "image"},
                    },
                }
            )

        list_bundles.assert_called_once_with(
            status="ready", asset_type="image", role=None, theme_id=None, audio_usage=None, limit=None, cursor=None
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["source"], "haypile")
        self.assertEqual(payload["handoff_version"], "haypile.asset-handoff.v1")
        self.assertEqual(payload["assets"][0]["role"], "hero_image")
        self.assertEqual(payload["assets"][0]["status"], "ready")
        self.assertEqual(payload["assets"][0]["resolved_url"], "http://127.0.0.1:8010/static/generic/images/hero.png")
        self.assertEqual(payload["assets"][0]["provenance"]["source_key"], "generic/images/hero.png")
        self.assertEqual(payload["assets"][0]["provenance"]["sha256"], "sha")
        self.assertNotIn("storage/assets", json.dumps(payload))

    def test_handoff_preserves_audio_metadata(self) -> None:
        asset = mcp_server._handoff_asset(
            {
                "id": "voice",
                "theme_id": "generic",
                "type": "audio",
                "role": "audio",
                "status": "ready",
                "sha256": "sha",
                "source_key": "generic/audio/voice.m4a",
                "url": "/static/generic/audio/voice.m4a",
                "access": "manifest_static",
                "duration_seconds": 12.5,
                "audio_metadata": {"sample_rate_hz": 48000, "channels": 2},
                "audio_tags": {"title": "Pika Call", "artist": "Winter Ridge"},
                "audio_usage": "voice",
            }
        )

        self.assertEqual(asset["duration_seconds"], 12.5)
        self.assertEqual(asset["audio_metadata"]["channels"], 2)
        self.assertEqual(asset["audio_tags"]["title"], "Pika Call")
        self.assertEqual(asset["audio_usage"], "voice")

    def test_mcp_server_starts_and_lists_tools_over_stdio(self) -> None:
        server_path = Path(mcp_server.__file__)
        process = subprocess.run(
            [sys.executable, str(server_path)],
            input=(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            ),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )

        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "haypile")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.2.0")
        names = [tool["name"] for tool in responses[1]["result"]["tools"]]
        self.assertIn("haypile_copy_handoff", names)


if __name__ == "__main__":
    unittest.main()
