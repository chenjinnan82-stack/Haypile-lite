# v0.3 Open Source Release Copy

Draft copy for `v0.3.0-alpha.4`. Do not publish it until the security regression,
private image evaluation, and macOS/Windows package checks have passed. After
publication, limit it to a 3–5 user pilot.

## GitHub About

Drop local images, organize the latest batch, and hand ready assets to agents
without exposing your disk.

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

Haypile v0.3.0-alpha.4 · Safety seed build for local Agent asset handoff

## GitHub Release Body

Haypile is a local-first asset intake for AI creators and independent developers.

Drop images from the browser or desktop. Haypile stores and registers them first,
then optionally suggests roles with a local model or an explicitly authorized
API. Review the latest batch and hand its ready assets to Codex through HTTP,
MCP, or `asset-handoff.v1`. Audio intake, metadata, and manual usage confirmation
remain supported.

### Highlights

- Cooperative cancellation replaces forced Qt thread termination; shutdown waits
  for intake, download, AI, manifest, and the owned backend to finish safely.
- Authenticated IPC identifies the Haypile process, port, PID, protocol, and
  readiness before the GUI trusts an open local port.
- IPC secrets are created under a cross-process lock with fsync and atomic replace.
- MCP is loopback-only by default; explicit remote use requires HTTPS opt-in.
- Bundle and vault reads fail closed while the manifest projection is not trusted.
- Missing physical copies are never ready and are excluded from ready handoffs.
- Local drops are rejected before persistence when count, total size, or free-space
  limits are exceeded.
- 293 Python 3.12 tests plus packaged backend/MCP smoke checks.
- Keeps the incomplete `alpha.3` tag immutable and corrects its stale Windows
  package-smoke version gate.

### Install

Attach these files only after both platform builds pass:

```text
Haypile-v0.3.0-alpha.4-macos-arm64.app.zip
Haypile-v0.3.0-alpha.4-windows-x64.zip
matching .sha256 files
```

Source users can still run `python3 app_gui.py` after installing
`requirements-desktop.txt`.

### Agent Access

```text
GET http://127.0.0.1:8010/api/v1/bundles?status=ready&batch_id=latest
```

MCP hosts can run `mcp_server.py` with `HAYPILE_BASE_URL=http://127.0.0.1:8010`.

### Notes

This is a prerelease. The macOS Apple Silicon app is ad-hoc signed and not
notarized. The Windows x64 portable build is unsigned. Verify the published
SHA-256 before running either package.

## Short Launch Post

Haypile v0.3 turns the desktop pile into a current-project asset intake for agents.

Drop images in, review the latest organized batch, and let Codex read only the
registered assets through HTTP/MCP. Storage happens before optional AI sorting,
and the agent never receives a disk path.

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## 中文首发文案

Haypile v0.3 把桌面草堆收口成了“当前项目的 AI 素材入口”。

把图片拖进去，Haypile 会先保存、去重和安全登记，再由本地模型或经授权的 API
提供用途建议。确认后，Codex 只读取最新一批 ready 素材，不需要翻你的硬盘。

核心点：

- 入库不被 AI 离线、超时或限流阻断
- 每次投放都有批次 ID，重复素材仍属于本批次
- 本地规则控制自动 ready 门槛
- HTTP/MCP 默认交付最新批次
- 本地模型、授权 API 或完全关闭 AI

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## README Badge Snippets

```md
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-read--only-6F7F5A)
```
