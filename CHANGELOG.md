# Changelog

## v0.3.0-alpha.6 (seed safety candidate)

- Keep the `alpha.5` source tag immutable after both desktop package gates
  exposed the same timing-sensitive GUI debounce assertion.
- Wait for the real Qt timer signal in search tests instead of assuming a
  20-millisecond scheduling margin on loaded macOS and Windows runners.
- Rebuild both desktop platforms from one corrected source commit without
  changing the 180-millisecond product interaction.

## v0.3.0-alpha.5 (seed safety candidate)

- Reject malformed or non-finite SVG geometry and AI confidence data before it
  reaches media metadata, readiness decisions, or persisted JSON.
- Preserve immutable ingest history, record per-occurrence browser origins, and
  enforce SQLite foreign keys without changing storage format v2.
- Serialize manifest projection and fail the latest-batch Agent view closed when
  the current projection is unavailable.
- Keep media roles type-safe, distinguish missing controlled copies in the UI,
  and prevent missing references from counting as identified assets.
- Return MCP tool failures as bounded tool results, retain the legacy `asset`
  filter meaning, and keep protocol errors for malformed requests.
- Clean owned browser-import temporary files, debounce material search, and
  refresh cached model availability without adding background polling.

## v0.3.0-alpha.4 (seed safety candidate)

- Keep the published `alpha.3` tag immutable after its Windows package smoke
  check exposed a stale `alpha.2` version matcher.
- Use one Windows release-version variable for archive naming, build metadata,
  MCP package validation, and future prerelease updates.
- Rebuild both desktop platforms from one corrected source commit before
  restoring seed-user downloads.

## v0.3.0-alpha.3 (seed safety candidate)

- Replace forced Qt thread termination with cooperative cancellation and a
  visible, event-driven safe shutdown flow.
- Identify the local backend over authenticated IPC before treating an open
  port as Haypile; allow slow startup and retain private backend logs.
- Create the IPC secret under a cross-process lock with fsync and atomic replace.
- Restrict MCP to loopback by default; remote endpoints require explicit opt-in
  and HTTPS, with credentials, redirects, paths, queries, and fragments rejected.
- Fail bundle and vault reads closed while the manifest projection is dirty,
  missing, or unreadable, and expose the manifest generation on successful reads.
- Report manifest-registered files that lost their physical copy as `missing`
  and exclude them from ready handoffs.
- Reject oversized local drops or insufficient disk space before creating a
  batch or changing the manifest projection.
- Add same-origin resource policy to manifest-gated static responses.

## v0.3.0-alpha.2 (hardening candidate)

- Pause the older desktop test download path while both platforms are rebuilt.
- Pin browser imports to verified public IP addresses without ambient proxies or redirects.
- Stage, validate, fsync, atomically commit, and recover asset intake with storage format v2.
- Reject decompression bombs, unsafe SVG content, and malformed audio before commit.
- Quarantine damaged theme contracts and add locked schema revisions.
- Use full SHA-256 bundle identities with unique legacy aliases during migration.
- Add per-instance locks, per-connection IPC timeouts, stricter MCP lifecycle handling,
  and rollback conflict preservation.
- Fail readiness and static serving closed while the manifest projection is dirty,
  missing, or corrupt; expose its generation and asset count after recovery.
- Make AI sorting role-only, bounded by the configured total timeout, and honest
  about partial, failed, and cancelled batches.
- Add complete handoff pagination metadata and whitelist public provenance while
  treating AI and asset metadata as untrusted advisory data.
- Keep IPC authentication independent from administrative credentials and run
  synchronous disk-heavy API reads in FastAPI's thread pool.
- Make attached-drawer closure deterministic after rapid page switches without
  allowing stale animation callbacks to close a newly reopened drawer.
- Upload prerelease assets with explicit repository context from checkout-free
  release jobs.
- Mark Alpha storage as a controlled copy, not a user's only backup.

## v0.3.0-alpha.1 (candidate)

- Store and manifest assets before optional AI classification.
- Group every valid drop into a stable ingest batch, including duplicates.
- Add latest-batch filtering to HTTP, MCP, the Assets drawer, and handoff output.
- Add deterministic image quality gates and conservative automatic readiness.
- Add Local model, OpenAI-compatible API, and Off modes without a new SDK.
- Store remote API credentials in Keychain or Credential Manager; redact
  secrets, request data, and absolute paths from public metadata and logs.
- Make the Agent drawer hand off the latest ready batch by default.
- Keep audio intake, metadata, and manual usage confirmation unchanged.

## v0.2.0-test.8

Customer package hygiene release.

- Prevent Windows portable archives from carrying build-time storage, logs, or
  IPC secrets.
- Isolate packaged MCP smoke state under a temporary `LOCALAPPDATA` directory
  and fail the build if runtime data appears beside `Haypile.exe`.
- Use Nuitka's official standalone runtime marker so frozen Windows builds
  resolve resources and user data independently of the executable filename.
- Emit portable LF-terminated checksum files.
- Document the macOS extraction path required to preserve ad-hoc signature
  extended attributes.
- Verify the source release with 210 tests and packaged backend/MCP smoke checks.

## v0.2.0-test.7

Attached desktop container release.

- Keep the grass pile fixed while the C-ring and drawers unfold around it.
- Consolidate first-level navigation into Assets, Agent, and Settings.
- Embed asset review, explicit handoff copying, AI setup, language, low-power,
  service, and log controls in one attached shell.
- Add private MCP session heartbeats for a real connection indicator without
  storing asset paths or handoff content.
- Refine drag awareness, image and audio intake visuals, edge placement, and
  open/close timing without changing the HTTP, MCP, storage, or handoff contracts.
- Replace the old independent-panel demo with the current attached UI render.
- Verify the release with 208 tests and packaged backend/MCP smoke checks.

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
