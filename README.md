<div align="center">

<img src="assets/logo.png" alt="Haypile Lite" width="260">

# Haypile Lite

**Feed your agents local assets without letting them rummage through your disk.**

Scattered files -> Haypile -> Ready bundles -> HTTP/MCP -> Agents

[简体中文](README.zh-CN.md)

![Local first](https://img.shields.io/badge/local--first-yes-2f855a)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![MCP](https://img.shields.io/badge/MCP-ready-6F7F5A)
![Agent writes](https://img.shields.io/badge/agent%20writes-off-1f2937)
![License](https://img.shields.io/badge/license-MIT-blue)
![Desktop](https://img.shields.io/badge/app-desktop-334155)
[![CI](https://github.com/chenjinnan82-stack/Haypile-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/chenjinnan82-stack/Haypile-lite/actions/workflows/ci.yml)

</div>

## Source of Truth

This repository and its tagged releases are the only public Haypile source.
Copies embedded in larger integration workspaces are compatibility snapshots;
do not publish them or copy them back into this repository.

## Desktop Test Builds

No Python required.

| Platform | Download | Status |
| --- | --- | --- |
| macOS Apple Silicon | [App ZIP](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.7/Haypile-v0.2.0-macos-arm64.app.zip) · [SHA-256](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.7/Haypile-v0.2.0-macos-arm64.app.zip.sha256) | Ad-hoc signed, not notarized |
| Windows x64 | [Portable ZIP](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.7/Haypile-v0.2.0-windows-x64.zip) · [SHA-256](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.7/Haypile-v0.2.0-windows-x64.zip.sha256) | Unsigned test build |

### macOS

This limited test build is ad-hoc signed and not notarized. After unzipping,
drag `Haypile.app` into **Applications**, then right-click it and choose
**Open**. Later, launch Haypile from Spotlight, Launchpad, or the Dock. If macOS still blocks it, use
**System Settings -> Privacy & Security -> Open Anyway**.

### Windows

Unzip the portable build and run `Haypile\Haypile.exe`. This x64 test build is
unsigned, so Windows may show a Microsoft Defender SmartScreen warning. Verify
the SHA-256 before running it. Automated Windows tests, packaged MCP/backend
smoke checks, and artifact validation pass; real-machine desktop testing is
still in progress.

## 30-Second Demo

Run the headless demo:

```bash
python3 -m pip install -r requirements-core.txt
python3 examples/public_smoke_demo.py --out /tmp/haypile-demo
```

It creates a sample asset registry and prints `asset-handoff` JSON with stable
`id`, `sha256`, `source_key`, `url`, `resolved_url`, and provenance fields.

Then try the desktop pile:

```bash
python3 -m pip install -r requirements-desktop.txt
python3 app_gui.py
```

Drop images or audio onto it, then ask the running backend what is ready:

```bash
python3 examples/use_haypile_http.py
```

**Boundary:** agents read registered assets through HTTP or MCP. They should not
scan or mutate `storage/assets` directly.

![Haypile agent workflow demo](docs/haypile-demo.gif)

The desktop UI now unfolds from one fixed pile: a three-entry C-ring opens
attached Assets, Agent, and Settings drawers without moving the drop target.

## Why

Agents are much better when they can use the user's real images, audio, and
theme fragments. Raw folders are the problem: assets are scattered, names are
unreliable, duplicates pile up, and filesystem access is too much power for a
simple generation task.

Haypile is a local asset pile with a gate. Drop files in; Haypile hashes,
dedupes, registers, and serves only manifest-approved assets. Agents get clean
ready bundles instead of a shovel and a disk path.

The metaphor is a pika haypile: gathered local material, stored safely, ready
for later.

## What It Does Today

- Provides a small desktop drop target for images and audio (`mp3`, `wav`, `ogg`, `m4a`, `flac`, `aac`).
- Keeps Assets, Agent access, and Settings in one attached desktop component.
- Hashes, dedupes, renames, and stores assets locally.
- Builds a manifest and serves only registered files through `/static`.
- Exposes ready bundles through a read-only HTTP API.
- Provides a thin MCP adapter over the same HTTP API.
- Emits agent handoff data with provenance.
- Preserves audio duration, basic technical metadata, and existing title/artist/album tags; users can confirm music, voice, ambience, sound effect, or loop usage.
- Optionally uses a local Ollama vision model directly or through a local Sophon gateway.
- Keeps low-power mode available when AI sorting is not wanted.

## Quick Start

Install from source:

```bash
git clone https://github.com/chenjinnan82-stack/Haypile-lite.git
cd Haypile-lite
python3 -m pip install -r requirements-desktop.txt
```

Run Haypile:

```bash
python3 app_gui.py
```

Direct Ollama remains the default. To route vision classification through an
already-running local Sophon gateway:

```bash
VISION_CLASSIFIER_TRANSPORT=sophon \
SOPHON_BASE_URL=http://127.0.0.1:8030 \
HAYPILE_SOPHON_API_KEY_FILE=/path/to/admin_api_key \
python3 app_gui.py
```

Manual backend smoke test:

```bash
HAYPILE_BACKEND_HOST_ALLOW_START=1 python3 backend_host.py
```

Run public checks:

```bash
python3 -m unittest tests/test_agent_examples.py tests/test_mcp_server.py
```

Run the headless public demo:

```bash
python3 examples/public_smoke_demo.py --out /tmp/haypile-demo
```

Run the full suite:

```bash
python3 -m unittest discover -s tests
```

### Building the macOS app

Haypile can now be frozen into a standalone Apple Silicon app that does not
require Python at runtime:

```bash
./scripts/build_macos_app.sh
open dist/Haypile.app
```

The packaged app keeps its assets under
`~/Library/Application Support/Haypile/storage` and logs under
`~/Library/Logs/Haypile`. It does not migrate or modify the source checkout's
`storage/` directory.

The GitHub test build is ad-hoc signed for limited testing and is not
notarized. Broad public distribution still requires Developer ID signing and
Apple notarization. See [macOS Test Build](docs/MACOS_INTERNAL_BUILD.md).

## Agent Access

Default backend:

```text
http://127.0.0.1:8010
```

Useful endpoints:

```text
GET /healthz
GET /readyz
GET /api/v1/bundles
GET /api/v1/bundles?status=ready
GET /api/v1/bundles?status=ready&type=image&role=hero_image
GET /api/v1/bundles/{bundle_id}
GET /api/v1/vault
```

MCP host config:

```json
{
  "mcpServers": {
    "haypile": {
      "command": "python3",
      "args": ["/absolute/path/to/Haypile-lite/mcp_server.py"],
      "env": {
        "HAYPILE_BASE_URL": "http://127.0.0.1:8010"
      }
    }
  }
}
```

For a packaged app, use its bundled executable instead of Python:

```json
{
  "mcpServers": {
    "haypile": {
      "command": "/absolute/path/to/Haypile.app/Contents/MacOS/Haypile",
      "args": ["--mcp"]
    }
  }
}
```

See [Agent HTTP Contract](docs/AGENT_HTTP_CONTRACT.md) and
[Agent Recipes](docs/AGENT_RECIPES.md) for the full handoff shape.

## Local AI

AI sorting is optional. Haypile still works as a local registry without it.

To force no-AI mode:

```bash
HAYPILE_LOW_POWER_MODE=1 python3 app_gui.py
```

For local model setup, see [Local AI Setup](docs/LOCAL_AI.md).

## Boundaries

Haypile Lite is not a cloud asset manager or a full DAM.

It does **not** currently publish a signed/notarized installer or promise
multi-user sync, remote hosting, destructive asset mutation through agents, or
production-grade asset approval workflows.

The public v0.1 surface is intentionally small: local intake, local registry,
manifest-gated static access, read-only HTTP, read-only MCP, and explicit
handoff data for agents.

Experimental real-project apply/rollback helpers are disabled by default and
are not part of the public agent-access surface.

## Project Shape

```text
Desktop drop target                 app_gui.py
FastAPI backend                     app/main.py
Backend launcher                    backend_host.py
HTTP bundle API                     app/api/v1/bundles.py
Theme vault API                     app/api/v1/theme.py
Manifest scanner                    app/services/scanner.py
Bundle service                      app/services/bundle_service.py
Theme registry                      app/services/theme_registry.py
Optional vision sorting             app/services/style_classifier.py
MCP adapter                         mcp_server.py
Agent examples                      examples/
Public docs                         docs/
Tests                               tests/
Runtime storage                     storage/
```

## Roadmap

- Developer ID signing, notarization, and a public macOS DMG.
- More public agent recipes.
- Clearer desktop onboarding.
- Cross-platform startup notes.
- More stable optional AI sorting.

## Contributing

Small, focused changes are welcome. See [Contributing](CONTRIBUTING.md).

For vulnerability reports, see [Security Policy](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

Third-party notices are listed in [NOTICE](NOTICE).
