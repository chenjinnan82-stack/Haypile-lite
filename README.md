# Haypile

A local asset haypile for agents.

Haypile is a local asset bundle registry. Images, audio, and theme fragments are
stored under `storage/assets`, registered in a manifest, and exposed to local
tools through a small FastAPI backend plus the PySide6 desktop drop target.

Drop files onto the desktop pile; agents read the resulting `ready` assets
through HTTP or MCP instead of scanning your disk directly.

![Haypile agent workflow demo](docs/haypile-demo.gif)

## What it does

- Drag images and audio into a small desktop drop target.
- Hash, dedupe, rename, and register assets locally.
- Serve only manifest-registered files through `/static`.
- Expose ready bundles through HTTP and MCP.
- Optionally use a local Ollama vision model for image sorting.
- Keep project reapply/rollback compatibility behind local GUI confirmation.

## Install

Python 3.12 or newer is recommended.

```bash
python3 -m pip install -r requirements.txt
```

## Start

```bash
python3 app_gui.py
```

From PimOS/MeowBus:

```powershell
meowbus-control.ps1 -Action start -Target haypile
```

The desktop app starts the local FastAPI backend when needed. For a manual
backend smoke test, run:

```bash
HAYPILE_BACKEND_HOST_ALLOW_START=1 python3 backend_host.py
```

## Configuration

```text
HAYPILE_BACKEND_HOST_ALLOW_START=1       allow manual backend_host.py startup
HAYPILE_GUI_ALLOW_BACKEND_START=0        stop the desktop app from auto-starting the backend
HAYPILE_BASE_URL=http://127.0.0.1:8010   MCP/examples backend URL
HAYPILE_REAL_PROJECT_ROOT=/path/project  optional project binding for投放/撤回兼容流程
HAYPILE_IPC_AUTHKEY_FILE=/path/key       optional local IPC auth key file
HAYPILE_LOW_POWER_MODE=1                 skip vision classification for battery use
VISION_CLASSIFIER_KEEP_ALIVE=30s         Ollama model keep-alive after classification
PIMOS_HAYPILE_DIR=/path/to/haypile       optional PimOS/MeowBus service directory override
```

## Agent API

Agents should read registered bundles through HTTP instead of scanning local
folders. The first supported integration contract is documented in
`docs/AGENT_HTTP_CONTRACT.md`; practical usage recipes are in
`docs/AGENT_RECIPES.md`.

```text
GET /healthz
GET /readyz
GET /api/v1/vault
GET /api/v1/bundles
GET /api/v1/bundles?status=ready&type=image&role=hero_image
GET /api/v1/bundles/{bundle_id}
```

For MCP hosts, run `mcp_server.py` as a stdio adapter over the same HTTP API.
Runnable agent examples live in `examples/`.

## Smoke Test

From the Haypile directory:

```bash
python3 -m unittest tests/test_agent_examples.py tests/test_mcp_server.py
```

The smoke test uses only the standard library. After installing requirements,
run the full test suite with:

```bash
python3 -m unittest discover -s tests
```

MCP config:

```json
{
  "mcpServers": {
    "haypile": {
      "command": "python3",
      "args": ["/absolute/path/to/haypile/mcp_server.py"],
      "env": {
        "HAYPILE_BASE_URL": "http://127.0.0.1:8010"
      }
    }
  }
}
```

Handoff shape:

```json
{
  "source": "haypile",
  "base_url": "http://127.0.0.1:8010",
  "assets": [
    {
      "id": "generic_img_hero_image_abcd1234",
      "theme_id": "generic",
      "type": "image",
      "role": "hero_image",
      "status": "ready",
      "sha256": "abcd...",
      "source_key": "generic/images/generic_img_hero_image_abcd1234.png",
      "url": "/static/generic/images/generic_img_hero_image_abcd1234.png",
      "access": "manifest_static",
      "resolved_url": "http://127.0.0.1:8010/static/generic/images/generic_img_hero_image_abcd1234.png"
    }
  ]
}
```

Bundle status:

- `ready`: registered and classified for use
- `pending`: registered but still `unknown`
- `missing`: referenced by a theme contract but absent from the manifest

## Local Data Boundary

Haypile is local-first. It stores imported assets and runtime indexes under
`storage/`, serves only manifest-registered assets through `/static`, and does
not require agents to read local asset paths directly.

## Safety Boundary

HTTP and MCP access is read-only for agents. Desktop compatibility actions that
can reapply or roll back a bound project are local GUI actions and require
explicit human confirmation; they are not exposed through HTTP or MCP.

Public commands, environment variables, docs, and agent contracts use Haypile
as the product name. Internal compatibility identifiers may remain only for
migration.

## License

MIT. See `LICENSE`.

Third-party notices are listed in `NOTICE`.

## Release Notes

From the repository root:

```bash
python3 sync_haypile_dist.py --sync --zip
python3 verify_haypile_release.py
```

Ship `dist/haypile.zip` after both commands pass.

Do not package local state:

```text
.pydeps_user/
__pycache__/
.pytest_cache/
storage/assets/
storage/index/assets_manifest.json
storage/ipc_authkey
*.log
```
