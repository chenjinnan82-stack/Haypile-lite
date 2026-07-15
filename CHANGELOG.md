# Changelog

## v0.2.0-test.6

Desktop package synchronization release.

- Rebuild macOS Apple Silicon and Windows x64 packages from the latest hardened main branch.
- Add audio metadata and usage fields to bundles and agent handoffs.
- Add cursor pagination for larger bundle collections.
- Add local Sophon vision transport and receipt support.
- Reject decompression-bomb images and manifest paths outside the asset root.
- Prevent local filesystem paths from leaking through internal HTTP errors.
- Bypass ambient proxies for local model payloads, MCP, and example HTTP calls.
- Verify the release with 184 tests, packaged backend/MCP smoke checks, and SHA-256 files.

## v0.2.0-test.5

Local data and dependency security hardening.

- Force the HTTP backend to bind to loopback and reject untrusted Host headers.
- Disable browser CORS access by default and allow only explicit loopback origins.
- Keep local AI model endpoints on loopback so assets are not silently uploaded.
- Store Haypile data, logs, IPC secrets, sockets, JSON, and copied assets with private permissions.
- Sandbox static responses, disable caching, and prevent MIME sniffing.
- Strip credentials, query parameters, and fragments from browser-import provenance.
- Bound remote imports to 20 URLs and 1 GiB per drop.
- Bypass ambient proxies for local model, MCP, and example HTTP calls.
- Reject decompression-bomb images and manifest paths outside the asset root.
- Redact local filesystem paths from internal HTTP errors.
- Require Pillow 12.3.x, the current security release line.
- Rebuild both desktop packages after dependency auditing.

## v0.2.0-test.1

Apple Silicon test app.

- Run the desktop GUI, FastAPI backend, and stdio MCP server from one frozen executable.
- Store packaged runtime data under `~/Library/Application Support/Haypile/storage`.
- Store packaged GUI and backend logs under `~/Library/Logs/Haypile`.
- Launch packaged backend and MCP modes without a Python installation.
- Avoid loading the Qt GUI stack in packaged backend and MCP processes.
- Build a standalone `arm64` `Haypile.app` with the bundled UI assets and macOS icon.
- Improve leaf-drop visibility on dark desktop backgrounds.
- Add repeatable ad-hoc signing, package smoke tests, zip creation, and SHA-256 output.
- Update MCP `serverInfo.version` to `0.2.0`.

Public distribution remains gated on Developer ID signing and Apple notarization.

## v0.1.3

Public smoke demo fix.

- Let `examples/public_smoke_demo.py` run directly from the repository root without setting `PYTHONPATH`.

## v0.1.2

Public alpha polish.

- Add a fully headless public smoke demo.
- Split install requirements into core, desktop, and dev files.
- Add minimal Python project metadata.
- Expand security boundary reporting guidance.

## v0.1.1

Hardening release.

- Add GitHub Actions CI.
- Copy assets into Haypile storage instead of hardlinking.
- Skip symlink escapes in scanner/static serving.
- Block loopback/private/link-local remote media imports.
- Disable experimental real-project apply/rollback helpers by default.
- Align the default local vision model with the setup docs.

## v0.1.0

Initial Haypile Lite public release.

- Desktop drop target for local image and audio intake.
- Hash, dedupe, rename, and manifest-registered local storage.
- Read-only HTTP API for ready bundles and theme contracts.
- Thin MCP adapter over the same HTTP API.
- Agent handoff examples with `id`, `sha256`, `source_key`, `url`, and provenance.
- Optional local Ollama vision sorting.
- Low-power mode for running without local AI.

Known limits:

- macOS is the primary polished desktop target for this release.
- No packaged `.app` or installer yet.
- Asset mutation and deletion are intentionally not exposed through HTTP or MCP.
