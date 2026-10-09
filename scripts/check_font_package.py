"""Exercise compiled font decoding, HTTP, static bytes, and MCP in isolated storage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from app.services.asset_provenance import provenance_path_for
from test_font_support import write_test_font


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    if (output / "storage").exists():
        parser.error("Use a fresh output directory for isolated acceptance.")
    samples = output / "storage/assets/generic/fonts"
    samples.mkdir(parents=True)
    hashes = {}
    for name, variable in (("static.ttf", False), ("outline.otf", False), ("variable.ttf", True), ("truetype.otf", False), ("symbol.ttf", False)):
        path = samples / name
        if name == "truetype.otf":
            original = output / "truetype.ttf"
            write_test_font(original, width=250)
            original.replace(path)
        else:
            write_test_font(path, variable=variable, symbol=name == "symbol.ttf")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Leave the synthetic URL raw to exercise sanitization in the compiled reader.
        provenance_path_for(path).write_text(json.dumps({"font_usage": "body", "font_annotation": {
            "license_note": "Original MIT test outlines", "source_note": "https://reader(test):private@example.com/font.ttf?token=private",
        }}), encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ, STORAGE_DIR=str(output / "storage"), LOG_DIR=str(output / "logs"), PORT=str(port),
               IPC_CHANNEL=f"haypile_font_package_{os.getpid()}", HAYPILE_IPC_AUTHKEY_FILE=str(output / "ipc_authkey"),
               VISION_CLASSIFIER_ENABLED="false", HAYPILE_BASE_URL=f"http://127.0.0.1:{port}")
    for key in ("ASSETS_DIR", "INDEX_DIR", "THEMES_DIR", "MANIFEST_PATH"):
        env.pop(key, None)
    base = env["HAYPILE_BASE_URL"]
    # No proxies or external service participates in this local acceptance.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(path):
        with opener.open(base + path, timeout=3) as response:
            return response.read(), response.headers
    with (output / "backend.log").open("w") as log:
        backend = subprocess.Popen([str(args.binary.resolve()), "--backend"], env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    ready = json.loads(get("/readyz")[0])
                    break
                except OSError:
                    if backend.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Packaged backend did not become ready; inspect backend.log")
                    time.sleep(.1)
            fonts = json.loads(get("/api/v1/bundles?type=font&font_usage=body&status=ready")[0])
            assert len(fonts) == len(hashes), fonts
            assert sum(font["font_metadata"]["is_variable"] for font in fonts) == 1
            for font in fonts:
                name = Path(font["source_key"]).name
                data, headers = get(font["url"])
                assert hashlib.sha256(data).hexdigest() == hashes[name] == font["sha256"]
                assert headers["Content-Type"] == "font/" + Path(name).suffix[1:]
                assert font["role"] == "font" and font["font_usage"] == "body"
                assert "MIT" in font["font_metadata"]["license_description"]
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "font-acceptance", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "haypile_copy_handoff", "arguments": {"type": "font", "font_usage": "body"}}},
            ]
            result = subprocess.run([str(args.binary.resolve()), "--mcp"], env=env,
                input="".join(json.dumps(message) + "\n" for message in messages), text=True, capture_output=True, timeout=20, check=True)
            responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            tool = next(response["result"] for response in responses if response.get("id") == 2)
            assert not tool.get("isError"), tool
            handoff = json.loads(tool["content"][0]["text"])
            assert handoff["asset_count"] == len(hashes) and handoff["complete"]
            assert handoff["manifest_generation"] == ready["manifest_generation"]
            assert {asset["sha256"] for asset in handoff["assets"]} == set(hashes.values())
            assert all(asset["font_annotation"]["license_note"] == "Original MIT test outlines" for asset in handoff["assets"])
            assert all(asset["font_annotation"]["source_note"] == "https://example.com" for asset in handoff["assets"])
            report = {"result": "passed", "formats": ["TTF", "CFF OTF", "variable TTF default", "TrueType OTF", "symbol TTF"], "hashes": hashes,
                      "manifest_generation": ready["manifest_generation"], "mcp_handoff": handoff}
            (output / "font-package-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print("PASS: packaged TTF / CFF OTF / TrueType OTF / variable default / symbol TTF, static hashes, HTTP and MCP handoff")
        finally:
            backend.terminate()
            try:
                backend.wait(timeout=15)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
