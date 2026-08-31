# v0.3.0-alpha.9 Open Source Release Notes

Release copy for `v0.3.0-alpha.9`. This is a cleanup-only prerelease: it adds
no product capability and keeps the Alpha.8 storage, HTTP, MCP, handoff, image,
true-GIF, clipboard, and audio behavior unchanged. Publish only after both tag
workflows, package checks, and compatibility smoke tests pass.

## Release procedure

The tag workflows upload assets to an existing GitHub Release; they do not
create one. Create a **draft prerelease** for the exact Alpha.9 tag before its
release jobs reach the upload step, and keep that draft private while the two
platform builds run. A tag without the draft Release is not sufficient.

Publish only after CI, macOS, and Windows are green; both archives and both
checksum files are present; their embedded commit matches the tag; package
sizes and SHA-256 values are recorded; and the project owner accepts the
release. Do not announce or open the pilot before that acceptance.

### Local package comparison

A same-environment rebuild at cleanup commit `68b16b6` produced:

| Package | ZIP bytes | Expanded bytes | ZIP entries |
| --- | ---: | ---: | ---: |
| Alpha.8 comparison build | 70,582,478 | 192,201,111 | 285 |
| Alpha.9 candidate | 70,582,503 | 192,201,111 | 285 |

The 25-byte ZIP increase came from archive metadata; the package tree and
expanded size did not grow. Both builds still contain the same QtDBus runtime
because QtGui links it, so the spec change removes only a redundant explicit
request. The published Alpha.8 ZIP is 70,057,790 bytes; its difference from the
same-source local Alpha.8 rebuild is build-environment drift, not an Alpha.9
source addition. Repeat and record the comparison using the official Alpha.9
workflow assets before publishing, and explain any unexplained growth.

## GitHub About

Collect local images, true GIFs, and audio, organize the latest batch, and hand
ready assets to agents without exposing your disk.

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

Haypile v0.3.0-alpha.9 · Cleanup release for local Agent asset handoff

## GitHub Release Body

Haypile is a local-first asset intake for AI creators and independent
developers.

Alpha.9 is deliberately small. It synchronizes the English and Chinese release
pages and build metadata, removes the unused `filetype` dependency, stops
making a redundant explicit Qt DBus request in the macOS deployment spec, and
documents an exact Python 3.12.10 local build path.

There are no storage, API, MCP, handoff, or user-flow changes. Images, true GIFs,
clipboard intake, audio, manual role confirmation, optional image suggestions,
and read-only Agent access behave as in Alpha.8. Experimental real-project
selection, apply, and rollback helpers remain disabled and outside the normal
desktop product path.

### Compatibility and safety

- Storage format and `haypile.asset-handoff.v1` are unchanged.
- HTTP and MCP remain read-only and loopback-only by default.
- Static files remain manifest-gated; missing or untrusted projections fail closed.
- True GIFs retain original bytes and verified animation metadata.
- Pixel-only clipboard images remain explicit static-PNG fallbacks.
- App-private animated emoji formats remain unsupported.
- The full Python 3.12 regression suite and packaged backend/MCP checks remain
  release gates.

### Install

Attach these files only after both platform builds pass:

```text
Haypile-v0.3.0-alpha.9-macos-arm64.app.zip
Haypile-v0.3.0-alpha.9-windows-x64.zip
matching .sha256 files
```

Source users can run `python3 app_gui.py` after installing
`requirements-desktop.txt`.

### Agent access

```text
GET http://127.0.0.1:8010/api/v1/bundles?status=ready&batch_id=latest
```

MCP hosts can run `mcp_server.py` with
`HAYPILE_BASE_URL=http://127.0.0.1:8010`.

### Distribution notes

This is a prerelease for informed testing. The macOS Apple Silicon app is
ad-hoc signed and not notarized. Right-click it and choose **Open** on first
launch. The Windows x64 portable build is unsigned; unzip it before running
`Haypile.exe`. Verify the published SHA-256 before running either package.

Haypile keeps controlled local copies, but an Alpha build must not be the only
backup of material that cannot be replaced.

## Short Launch Post

Haypile Alpha.9 is a cleanup release for the local-first asset intake.

It adds no feature: image, true-GIF, clipboard, audio, HTTP, MCP, and
`asset-handoff.v1` behavior stay unchanged. The release aligns bilingual
downloads and build metadata, removes one unused dependency and one redundant
macOS Qt module request, and preserves the existing local trust boundary.

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## 中文发布文案

Haypile · 冬藏 Alpha.9 是一次不增加功能的清理版发布。

本版统一中英文下载信息与构建元数据，删除一个未使用的媒体识别依赖和 macOS
部署配置中冗余的 Qt DBus 显式请求，并补齐精确 Python 3.12.10 的本地构建
路径。图片、原字节 GIF、剪贴板、音频、HTTP、MCP 与 `asset-handoff.v1`
行为均保持不变。

HTTP 与 MCP 继续只读，素材文件继续受 manifest 限制。实验性的真实项目选择、
投放和回滚能力仍默认关闭，也不进入正常桌面产品路径。

macOS Apple Silicon 包采用临时签名且尚未公证；Windows x64 便携包尚未签名。
运行前请核对发布页提供的 SHA-256。

Repo: https://github.com/chenjinnan82-stack/Haypile-lite

## Pilot boundary

After release, invite 6–8 people through the maintainer's private network and
aim for four completed sessions, with three as the minimum usable sample.
Include at least one AI coding-tool user and one design or content creator.
Participants work from a short task card before a follow-up interview. Keep raw
notes private; publish only anonymized findings and reproducible defects. Use
the [Alpha.9 pilot task card](ALPHA9_PILOT.md) for the session.
