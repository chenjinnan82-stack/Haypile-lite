# Open Source Release Copy

Use this page as copy-paste material for GitHub and launch posts.

## GitHub About

Local asset haypile for agents. Drop images/audio, expose ready assets through
HTTP and MCP, and keep agents out of your filesystem.

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
```

## Release Title

Haypile Lite v0.1.0

## GitHub Release Body

Haypile Lite is a local asset haypile for agent workflows.

Drop images or audio onto the desktop pile. Haypile hashes, dedupes, registers,
and serves ready assets through a small local HTTP API and MCP adapter. Agents
use `resolved_url` and provenance from Haypile instead of scanning your local
disk.

### Highlights

- Desktop drop target for images and audio.
- Local asset manifest with hash and dedupe.
- Read-only HTTP API and MCP adapter.
- `asset-handoff.json` examples for downstream agents.
- Optional local Ollama vision sorting.
- Low-power mode without AI.

### Install

```bash
git clone https://github.com/chenjinnan82-stack/Haypile-lite.git
cd Haypile-lite
python3 -m pip install -r requirements.txt
python3 app_gui.py
```

### Agent Access

```text
GET http://127.0.0.1:8010/api/v1/bundles?status=ready
```

MCP hosts can run `mcp_server.py` with `HAYPILE_BASE_URL=http://127.0.0.1:8010`.

### Notes

This is a v0.1 local-first release. macOS is the primary polished desktop target.
Windows and Linux can run from source, but packaged installers are not included
yet.

## Short Launch Post

I open-sourced Haypile Lite, a small local asset haypile for agent workflows.

You drop images/audio into a desktop pile; Haypile hashes, dedupes, registers,
and exposes ready assets through HTTP/MCP. Agents get stable `id`, `sha256`,
`source_key`, `resolved_url`, and provenance instead of rummaging through your
filesystem.

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## 中文首发文案

我把 Haypile Lite 开源了。

它是一个给本地 agent 工作流用的素材草堆：把图片或音频拖进去，Haypile 会做 hash、去重、登记和本地访问边界。其他 agent 通过 HTTP/MCP 读取 ready assets，不需要直接翻你的硬盘目录。

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
