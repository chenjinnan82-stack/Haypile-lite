# Changelog

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
