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

</div>

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

- Provides a small desktop drop target for images and audio.
- Hashes, dedupes, renames, and stores assets locally.
- Builds a manifest and serves only registered files through `/static`.
- Exposes ready bundles through a read-only HTTP API.
- Provides a thin MCP adapter over the same HTTP API.
- Emits agent handoff data with provenance.
- Optionally uses a local Ollama vision model for image sorting.
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

It does **not** currently promise packaged installers, multi-user sync, remote
hosting, destructive asset mutation through agents, or production-grade asset
approval workflows.

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

- Better macOS packaging.
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
