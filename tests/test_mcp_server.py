from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import call, patch

import mcp_server

ROOT = Path(__file__).resolve().parents[1]


class McpServerTests(unittest.TestCase):
    def test_mcp_heartbeat_is_private_and_contains_no_asset_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            heartbeat = mcp_server.McpSessionHeartbeat(index_dir).start()
            try:
                sessions = mcp_server.active_mcp_sessions(index_dir)
                self.assertEqual(len(sessions), 1)
                self.assertNotIn("client", sessions[0])
                self.assertNotIn("source_key", json.dumps(sessions[0]))
                self.assertNotIn("handoff", json.dumps(sessions[0]))
                if os.name != "nt":
                    self.assertEqual(heartbeat.directory.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(heartbeat.path.stat().st_mode & 0o777, 0o600)
                heartbeat.path.unlink()
                heartbeat.touch()
                self.assertFalse(heartbeat.path.exists())
            finally:
                heartbeat.stop()
            self.assertEqual(mcp_server.active_mcp_sessions(index_dir), [])

    def test_mcp_session_reader_times_out_and_cleans_old_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            directory = index_dir / "mcp_sessions"
            directory.mkdir(parents=True)
            stale = directory / "101.json"
            expired = directory / "102.json"
            for path, pid in ((stale, 101), (expired, 102)):
                path.write_text(json.dumps({"pid": pid, "client": {"name": "Codex"}}), encoding="utf-8")
            now = time.time()
            os.utime(stale, (now - 13, now - 13))
            os.utime(expired, (now - 61, now - 61))

            self.assertEqual(mcp_server.active_mcp_sessions(index_dir, now=now), [])
            self.assertTrue(stale.exists())
            self.assertFalse(expired.exists())

    @unittest.skipIf(os.name == "nt", "Windows symlink permissions vary")
    def test_mcp_session_storage_rejects_symlink_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index_dir = Path(tmp) / "index"
            target = Path(tmp) / "redirected"
            target.mkdir()
            index_dir.mkdir()
            (index_dir / "mcp_sessions").symlink_to(target, target_is_directory=True)

            with self.assertRaises(OSError):
                mcp_server.McpSessionHeartbeat(index_dir).start()
            self.assertEqual(mcp_server.active_mcp_sessions(index_dir), [])

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
        self.assertEqual(response["result"]["serverInfo"]["version"], "0.3.0-alpha.2")
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

        get_json.assert_called_once_with("/api/v1/bundles?status=ready&type=image&role=hero_image&limit=100")
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
        with patch.object(mcp_server, "list_bundles", return_value=[bundle]) as list_bundles, patch.object(
            mcp_server,
            "get_json",
            return_value={"status": "ok", "manifest_generation": "generation-1"},
        ):
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
            status="ready", asset_type="image", role=None, theme_id=None, audio_usage=None, batch_id=None
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["source"], "haypile")
        self.assertEqual(payload["handoff_version"], "haypile.asset-handoff.v1")
        self.assertEqual(payload["assets"][0]["role"], "hero_image")
        self.assertEqual(payload["assets"][0]["status"], "ready")
        self.assertEqual(payload["assets"][0]["resolved_url"], "http://127.0.0.1:8010/static/generic/images/hero.png")
        self.assertEqual(payload["assets"][0]["provenance"]["source_key"], "generic/images/hero.png")
        self.assertEqual(payload["assets"][0]["provenance"]["sha256"], "sha")
        self.assertEqual(payload["manifest_generation"], "generation-1")
        self.assertEqual(payload["asset_count"], 1)
        self.assertEqual(payload["total_matching"], 1)
        self.assertTrue(payload["complete"])
        self.assertIsNone(payload["next_cursor"])
        self.assertNotIn("storage/assets", json.dumps(payload))

    def test_copy_handoff_resolves_latest_batch(self) -> None:
        with patch.object(
            mcp_server,
            "get_json",
            side_effect=[
                {"id": "batch-1"},
                {"status": "ok", "manifest_generation": "generation-1"},
                {"status": "ok", "manifest_generation": "generation-1"},
            ],
        ) as get_json, patch.object(
            mcp_server, "list_bundles", return_value=[]
        ) as list_bundles:
            payload = mcp_server.call_tool("haypile_copy_handoff", {"batch_id": "latest"})

        self.assertEqual(
            get_json.call_args_list,
            [
                call("/api/v1/batches/latest"),
                call("/readyz"),
                call("/readyz"),
            ],
        )
        list_bundles.assert_called_once_with(
            status="ready",
            asset_type=None,
            role=None,
            theme_id=None,
            audio_usage=None,
            batch_id="batch-1",
        )
        self.assertEqual(payload["batch_id"], "batch-1")

    def test_copy_handoff_paginates_complete_match_set_and_rejects_unknown_cursor(self) -> None:
        bundles = [
            {
                "id": str(index),
                "theme_id": "generic",
                "type": "image",
                "role": "hero_image",
                "status": "ready",
                "sha256": str(index),
                "source_key": f"generic/images/{index}.png",
                "url": f"/static/generic/images/{index}.png",
                "access": "manifest_static",
            }
            for index in range(3)
        ]
        with patch.object(mcp_server, "list_bundles", return_value=bundles), patch.object(
            mcp_server,
            "get_json",
            return_value={"manifest_generation": "generation-2"},
        ):
            first = mcp_server.call_tool("haypile_copy_handoff", {"limit": 2})
            second = mcp_server.call_tool(
                "haypile_copy_handoff",
                {"limit": 2, "cursor": first["next_cursor"]},
            )
            with self.assertRaisesRegex(ValueError, "invalid cursor"):
                mcp_server.call_tool(
                    "haypile_copy_handoff",
                    {"limit": 2, "cursor": "missing.png"},
                )

        self.assertEqual(first["asset_count"], 2)
        self.assertEqual(first["total_matching"], 3)
        self.assertFalse(first["complete"])
        self.assertEqual(second["asset_count"], 1)
        self.assertTrue(second["complete"])

    def test_copy_handoff_rejects_manifest_generation_race(self) -> None:
        with patch.object(mcp_server, "list_bundles", return_value=[]), patch.object(
            mcp_server,
            "get_json",
            side_effect=[
                {"manifest_generation": "old"},
                {"manifest_generation": "new"},
            ],
        ):
            with self.assertRaisesRegex(ValueError, "changed during handoff"):
                mcp_server.call_tool("haypile_copy_handoff", {})

    def test_list_bundles_passes_batch_id_through(self) -> None:
        with patch.object(mcp_server, "list_bundles", return_value=[]) as list_bundles:
            payload = mcp_server.call_tool(
                "haypile_list_bundles",
                {"status": "pending", "batch_id": "batch-2"},
            )

        self.assertEqual(payload, [])
        list_bundles.assert_called_once_with(
            status="pending",
            asset_type=None,
            role=None,
            theme_id=None,
            audio_usage=None,
            batch_id="batch-2",
            limit=100,
            cursor=None,
        )

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

    def test_handoff_redacts_untrusted_runtime_metadata(self) -> None:
        asset = mcp_server._handoff_asset(
            {
                "id": "hero",
                "theme_id": "generic",
                "type": "image",
                "role": "hero_image",
                "status": "ready",
                "sha256": "sha",
                "source_key": "generic/images/hero.png",
                "url": "/static/generic/images/hero.png",
                "access": "manifest_static",
                "origin_url": "https://cdn.example.com/private/path.png?token=secret",
                "ai_suggestions": {
                    "source": "model",
                    "agent_summary": "useful",
                    "runtime_receipt": {"request_body": "secret"},
                    "local_path": "/Users/tester/private.png",
                },
            }
        )
        encoded = json.dumps(asset)

        self.assertEqual(asset["provenance"]["origin_url"], "https://cdn.example.com")
        self.assertNotIn("runtime_receipt", encoded)
        self.assertNotIn("request_body", encoded)
        self.assertNotIn("/Users/", encoded)

    def test_mcp_server_starts_and_lists_tools_over_stdio(self) -> None:
        server_path = Path(mcp_server.__file__)
        process = subprocess.run(
            [sys.executable, str(server_path)],
            input=(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            ),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )

        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "haypile")
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.3.0-alpha.2")
        names = [tool["name"] for tool in responses[1]["result"]["tools"]]
        self.assertIn("haypile_copy_handoff", names)

    def test_malformed_and_oversized_messages_do_not_end_stdio_session(self) -> None:
        server_path = Path(mcp_server.__file__)
        oversized = "x" * (mcp_server.MAX_LINE_BYTES + 1)
        process = subprocess.run(
            [sys.executable, str(server_path)],
            input=(
                oversized + "\n"
                + "{not-json}\n"
                + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            ),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )

        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([responses[0]["error"]["code"], responses[1]["error"]["code"]], [-32700, -32700])
        self.assertEqual(responses[2]["result"]["serverInfo"]["version"], "0.3.0-alpha.2")
        self.assertIn("tools", responses[3]["result"])


if __name__ == "__main__":
    unittest.main()
