# Changelog

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
