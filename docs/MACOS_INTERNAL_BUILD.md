# macOS Test Build

Haypile v0.2 can be built as a standalone Apple Silicon utility app. This is
an unsigned limited-test artifact, not a notarized public installer.

## Requirements

- Apple Silicon Mac
- Python 3.12
- Xcode Command Line Tools
- Network access on the first build for Python packages and Nuitka tooling

## Build

From the repository root:

```bash
./scripts/build_macos_app.sh
```

The script creates an isolated `.build-venv`, generates `build/Haypile.icns`,
runs `pyside6-deploy` in standalone mode, ad-hoc signs the app, and executes
packaged MCP and backend smoke tests.

Outputs:

```text
dist/Haypile.app
dist/Haypile-v0.2.0-macos-arm64.app.zip
dist/Haypile-v0.2.0-macos-arm64.app.zip.sha256
```

Build outputs are intentionally ignored by Git.

## Install and open

Unzip the test build and drag `Haypile.app` into `/Applications`. Open it from
Spotlight, Launchpad, Finder's Applications folder, or keep it in the Dock.
On the first launch, right-click the app and choose **Open** because the test
build is not notarized.

## Runtime modes

The same frozen executable provides all three entry points:

```bash
open dist/Haypile.app
dist/Haypile.app/Contents/MacOS/Haypile --backend
dist/Haypile.app/Contents/MacOS/Haypile --mcp
```

The app launches `--backend` itself during normal desktop use. Codex and other
MCP hosts should call the same executable with `--mcp`.

## Data boundary

Source mode continues to use the repository `storage/` directory. Packaged
mode uses:

```text
~/Library/Application Support/Haypile/storage/
~/Library/Logs/Haypile/gui.log
~/Library/Logs/Haypile/backend.log
```

The first packaged run starts with a separate asset registry. There is no
automatic migration from source mode.

## Codex registration

```bash
codex mcp add haypile -- \
  "/absolute/path/to/Haypile.app/Contents/MacOS/Haypile" --mcp
```

Start Haypile before asking Codex to call `haypile_health`,
`haypile_list_bundles`, `haypile_copy_handoff`, or `haypile_get_bundle`.

## Distribution boundary

The internal app uses an ad-hoc signature so its bundle structure can be
verified locally:

```bash
codesign --verify --deep --strict dist/Haypile.app
```

This archive may be attached only to an explicitly labeled unsigned GitHub
pre-release for informed testers. Broad public distribution requires a valid
Developer ID Application identity, hardened-runtime signing, Apple
notarization, stapling, and a DMG.
