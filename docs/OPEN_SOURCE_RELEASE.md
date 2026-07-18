# Open Source Release Copy

Use this page as copy-paste material for GitHub and launch posts.

## GitHub About

Feed agents local assets without letting them rummage through your disk.
Drop images/audio, expose ready bundles through HTTP and MCP, and keep storage
manifest-gated.

## Suggested Topics

```text
local-first
agent-tools
mcp
fastapi
pyside6
asset-management
ollama
python
desktop-app
local-ai
provenance
```

## Release Title

Haypile Lite v0.2.0-test.8 customer package hygiene

## GitHub Release Body

Haypile Lite is a local asset haypile for agent workflows.

Drop images or audio onto the desktop pile. Haypile hashes, dedupes, registers,
and serves ready assets through a small local HTTP API and MCP adapter. The pile,
three-entry C-ring, and attached Assets, Agent, and Settings drawers now behave
as one desktop component. Agents use `resolved_url` and provenance from Haypile
instead of scanning your local disk. Tiny pile, strict boundary.

### Highlights

- Fixed desktop drop target with attached Assets, Agent, and Settings drawers.
- Distinct image and audio intake feedback with stable edge placement.
- Local asset manifest with hash and dedupe.
- Read-only HTTP API and MCP adapter.
- Private MCP session heartbeat without asset paths or handoff content.
- `asset-handoff.json` examples for downstream agents.
- Optional local Ollama vision sorting.
- Low-power mode without AI.
- Windows package guard that rejects build-time storage, logs, and IPC secrets.
- Nuitka standalone detection that keeps packaged Windows data in Local AppData.
- 210 automated tests plus packaged backend/MCP smoke checks.

### Install

Download the macOS Apple Silicon or Windows x64 test build from:

https://github.com/chenjinnan82-stack/Haypile-lite/releases/tag/v0.2.0-test.8

Source users can still run `python3 app_gui.py` after installing
`requirements-desktop.txt`.

### Agent Access

```text
GET http://127.0.0.1:8010/api/v1/bundles?status=ready
```

MCP hosts can run `mcp_server.py` with `HAYPILE_BASE_URL=http://127.0.0.1:8010`.

### Notes

This remains a test release. The macOS Apple Silicon app is ad-hoc signed and
not notarized. The Windows x64 portable build is unsigned. Verify the published
SHA-256 before running either package.

## Short Launch Post

I open-sourced Haypile Lite, a small local asset haypile for agent workflows.

You drop images/audio into a desktop pile; Haypile hashes, dedupes, registers,
and exposes ready assets through HTTP/MCP. Agents get stable `id`, `sha256`,
`source_key`, `resolved_url`, and provenance instead of rummaging through your
filesystem.

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## 中文首发文案

我把 Haypile Lite 开源了。

它是一个给本地 agent 工作流用的素材草堆：给 agent 喂素材，但不让它翻你的硬盘。把图片或音频拖进去，Haypile 会做 hash、去重、登记和本地访问边界。其他 agent 通过 HTTP/MCP 读取 ready assets。

核心点：

- 本地优先的素材入库
- manifest-gated `/static` 访问
- HTTP/MCP 读取 ready bundles
- `asset-handoff.json` 保留来源和 hash
- 可选本地 Ollama AI 分拣

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## README Badge Snippets

```md
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-ready-6F7F5A)
```
