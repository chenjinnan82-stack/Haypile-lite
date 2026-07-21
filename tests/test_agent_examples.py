from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


class AgentExampleTests(unittest.TestCase):
    def test_http_example_builds_reviewable_handoff(self) -> None:
        module = _load_http_example()
        handoff = module.build_handoff(
            [
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
                    "ai_suggestions": {
                        "tags": ["主视觉"],
                        "usage": "hero_image",
                        "quality": "high",
                        "agent_summary": "适合作为主视觉。",
                    },
                }
            ]
        )

        self.assertEqual(handoff["source"], "haypile")
        self.assertEqual(handoff["handoff_version"], "haypile.asset-handoff.v1")
        self.assertEqual(handoff["assets"][0]["role"], "hero_image")
        self.assertEqual(handoff["assets"][0]["status"], "ready")
        self.assertEqual(handoff["assets"][0]["resolved_url"], "http://127.0.0.1:8010/static/generic/images/hero.png")
        self.assertEqual(handoff["assets"][0]["provenance"]["sha256"], "sha")
        self.assertEqual(handoff["assets"][0]["provenance"]["source_key"], "generic/images/hero.png")
        self.assertEqual(handoff["assets"][0]["ai_suggestions"]["quality"], "high")
        self.assertIsNone(handoff["assets"][0]["duration_seconds"])
        self.assertEqual(handoff["assets"][0]["audio_metadata"], {})
        self.assertEqual(handoff["assets"][0]["audio_tags"], {})
        self.assertEqual(handoff["assets"][0]["audio_usage"], "unknown")
        self.assertNotIn("storage/assets", json.dumps(handoff))

    def test_http_example_runs_against_http_endpoint(self) -> None:
        module = _load_http_example()
        previous_base_url = module.BASE_URL
        server = HTTPServer(("127.0.0.1", 0), _HaypileExampleHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            module.BASE_URL = f"http://127.0.0.1:{server.server_address[1]}"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main()
        finally:
            module.BASE_URL = previous_base_url
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(payload["batch_id"], "batch-latest")
        self.assertEqual(payload["assets"][0]["provenance"]["source"], "haypile")
        self.assertNotIn("storage/assets", json.dumps(payload))

    def test_http_example_explains_empty_ready_assets(self) -> None:
        module = _load_http_example()
        previous_get_json = module.get_json
        previous_ready_images = module.ready_images
        try:
            module.get_json = lambda path: {"id": "batch-latest"} if path.endswith("/batches/latest") else {"status": "ok"}
            module.ready_images = lambda role=None, batch_id="latest": []

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main()

            self.assertEqual(exit_code, 1)
            self.assertIn('"assets": []', stdout.getvalue())
            self.assertIn("latest batch has no ready images", stderr.getvalue())
        finally:
            module.get_json = previous_get_json
            module.ready_images = previous_ready_images

    def test_http_example_treats_missing_latest_batch_as_empty(self) -> None:
        module = _load_http_example()
        previous_get_json = module.get_json
        previous_ready_images = module.ready_images
        calls: list[str] = []

        def fake_get_json(path: str):
            if path.endswith("/batches/latest"):
                raise module.HTTPError(path, 404, "Not Found", {}, None)
            return {"status": "ok"}

        try:
            module.get_json = fake_get_json
            module.ready_images = lambda **_kwargs: calls.append("ready") or []
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main()

            self.assertEqual(exit_code, 1)
            self.assertEqual(calls, [])
            self.assertIn('"assets": []', stdout.getvalue())
            self.assertIn("latest batch has no ready images", stderr.getvalue())
        finally:
            module.get_json = previous_get_json
            module.ready_images = previous_ready_images

    def test_http_example_explains_unreachable_backend(self) -> None:
        module = _load_http_example()
        previous_get_json = module.get_json
        try:
            module.get_json = lambda _path: (_ for _ in ()).throw(module.URLError("offline"))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = module.main()

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("Cannot reach Haypile", stderr.getvalue())
            self.assertIn("HAYPILE_BASE_URL", stderr.getvalue())
        finally:
            module.get_json = previous_get_json

    def test_handoff_sample_has_required_provenance(self) -> None:
        payload = json.loads((EXAMPLES_DIR / "asset-handoff.json").read_text(encoding="utf-8"))
        asset = payload["assets"][0]

        self.assertEqual(payload["handoff_version"], "haypile.asset-handoff.v1")
        for key in ("id", "role", "status", "sha256", "source_key", "url", "provenance"):
            self.assertTrue(asset[key])
        self.assertTrue(asset["url"].startswith("/static/"))
        self.assertEqual(asset["provenance"]["source"], "haypile")

    def test_review_agent_can_validate_handoff_without_local_storage(self) -> None:
        payload = json.loads((EXAMPLES_DIR / "asset-handoff.json").read_text(encoding="utf-8"))
        required = {"id", "sha256", "source_key", "url", "resolved_url", "provenance"}

        for asset in payload["assets"]:
            self.assertFalse(required - set(asset))
            self.assertEqual(asset["provenance"]["source"], "haypile")
            self.assertTrue(asset["resolved_url"].startswith(payload["base_url"]))
            self.assertNotIn("storage/assets", json.dumps(asset))

    def test_agent_recipes_cover_design_review_generation_without_local_assets(self) -> None:
        text = (EXAMPLES_DIR / "agent_recipes.md").read_text(encoding="utf-8")

        self.assertIn("## Design Agent", text)
        self.assertIn("## Review Agent", text)
        self.assertIn("## Generation Agent", text)
        self.assertIn("## Codex Agent", text)
        self.assertIn("python3 examples/use_haypile_http.py", text)
        self.assertIn("id", text)
        self.assertIn("role", text)
        self.assertIn("status", text)
        self.assertIn("sha256", text)
        self.assertIn("source_key", text)
        self.assertIn("resolved_url", text)
        self.assertIn("provenance", text)
        self.assertIn("audio_usage", text)
        self.assertIn("audio_tags", text)
        self.assertIn("Never inspect Haypile's local storage directory.", text)

    def test_public_smoke_demo_creates_headless_handoff(self) -> None:
        module = _load_example("public_smoke_demo.py", "public_smoke_demo")
        tmpdir = Path(tempfile.mkdtemp())
        old_argv = sys.argv
        try:
            sys.argv = ["public_smoke_demo.py", "--out", str(tmpdir)]
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = module.main()
        finally:
            sys.argv = old_argv
            shutil.rmtree(tmpdir, ignore_errors=True)

        payload = json.loads(stdout.getvalue())
        asset = payload["assets"][0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["handoff_version"], "haypile.asset-handoff.v1")
        self.assertEqual(asset["role"], "hero_image")
        self.assertEqual(asset["status"], "ready")
        self.assertTrue(asset["url"].startswith("/static/"))
        self.assertNotIn("storage/assets", json.dumps(payload))


def _load_http_example():
    return _load_example("use_haypile_http.py", "use_haypile_http_example")


def _load_example(filename: str, module_name: str):
    path = EXAMPLES_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _HaypileExampleHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            self._send({"status": "ok"})
            return
        if self.path == "/api/v1/batches/latest":
            self._send({"id": "batch-latest"})
            return
        if self.path.startswith("/api/v1/bundles?"):
            self._send(
                [
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
                    }
                ]
            )
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send(self, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    unittest.main()
