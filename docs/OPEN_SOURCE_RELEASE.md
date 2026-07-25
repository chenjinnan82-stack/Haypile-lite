# v0.3 Open Source Release Copy

Draft copy for `v0.3.0-alpha.8`. Alpha.7 was not published; alpha.8 supersedes
that candidate after the GIF slice, shared intake transaction, and clipboard
entry landed together. Do not publish until the security regression and
macOS/Windows package checks have passed. After publication, limit it to a
3–5 user pilot.

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

Haypile v0.3.0-alpha.8 · Safe GIF and clipboard intake for local Agent handoff

## GitHub Release Body

Haypile is a local-first asset intake for AI creators and independent developers.

Import images from the desktop or a direct browser media URL, or use the
explicit clipboard action. Haypile stores and registers them first. True GIF
files retain their original bytes when the source exposes a file, direct URL,
or exact GIF payload; they play once in the Assets drawer and become ready
after manual role confirmation. Pixel-only clipboard data is identified and
stored as a static PNG. Static images can still use optional AI suggestions.
Ready assets reach Codex through HTTP, MCP, or `asset-handoff.v1`; audio remains
supported.

### Highlights

- Validate every GIF frame with 50 MiB, 500-frame, 4096-pixel, decoded-pixel,
  and 30-second effective-duration limits.
- Accept local files, direct `image/gif` URLs, and chat attachments exposed as
  actual files without video conversion, page scraping, or derived assets.
- Preview one loop and stop GIF motion when the drawer hides or low-power mode
  is enabled.
- Add manual reaction, sticker, and UI-animation roles while keeping GIFs out
  of AI sorting.
- Route GUI intake and startup recovery through one Qt-free transaction service
  and one cross-process writer lock.
- Add explicit clipboard intake for local files, exact GIF payloads, safe direct
  URLs, and a static-PNG fallback when the source exposes pixels only.
- Expose verified MIME, frame count, declared duration, and loop count through
  the manifest, HTTP, MCP, and handoff.
- Reject package builds from dirty Git worktrees so embedded build metadata maps
  to exact committed source.
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
- Malformed SVG geometry and non-finite model output are rejected before they can
  influence readiness or persisted metadata.
- Ingest history remains immutable when a controlled copy goes missing, while
  manifest projection is serialized and Agent reads fail closed.
- MCP distinguishes protocol errors from bounded tool failures and keeps legacy
  `type=asset` clients compatible.
- Full Python 3.12 regression suite plus packaged backend/MCP and GIF-plugin checks.

### Install

Attach these files only after both platform builds pass:

```text
Haypile-v0.3.0-alpha.8-macos-arm64.app.zip
Haypile-v0.3.0-alpha.8-windows-x64.zip
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

App-private animated emoji formats are not supported. If an app exposes only
decoded pixels, Haypile can store a static PNG but cannot recover the original
animation.

## Short Launch Post

Haypile alpha.8 adds safe, original-byte GIF and explicit clipboard intake to
the desktop pile.

Import a true GIF from the desktop, a direct browser image URL, or a chat
attachment exposed as a file. The clipboard action preserves animation only
when the source exposes a file or exact GIF payload. Preview one loop, choose
its role, and let Codex read only the registered URL and animation metadata.
Pixel-only sources are labeled and stored honestly as static PNGs. GIFs never
enter AI sorting.

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## 中文首发文案

Haypile alpha.8 为桌面草堆加入了安全的原字节 GIF 与明确的剪贴板收纳。

从桌面、浏览器直接图片 URL，或能提供真实文件附件的聊天软件导入 GIF，Haypile
会安全校验并保留原文件，只播放一轮预览。剪贴板来源只有在提供文件或完整 GIF
载荷时才能保留动画；若只提供静态像素，则明确按 PNG 收纳。应用私有动态表情格式
尚不支持。手动确认用途后，Codex 通过登记 URL 和动画元数据使用，不需要翻你的
硬盘。

核心点：

- 核心验证统一限制 50 MiB、500 帧、4096 像素和 30 秒有效时长
- 保留原始 GIF，不转码、不抽帧、不生成派生文件
- 单轮预览，低功耗或隐藏面板立即停止
- 反应、贴纸、界面动画三种手动用途
- GIF 不进入 AI 队列

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## README Badge Snippets

```md
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-read--only-6F7F5A)
```
