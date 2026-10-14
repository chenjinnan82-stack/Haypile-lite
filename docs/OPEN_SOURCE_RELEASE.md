# v0.3.0-alpha.10 release draft

This is release preparation, not a published release. GitHub currently provides
Alpha.8 packages; README download links must keep pointing to those verified
assets until Alpha.10 is published. Existing local Alpha.10 archives predate
the final fixes and must not be uploaded as the accepted build.

## Release title

Haypile v0.3.0-alpha.10 · Fonts, compact webpage intake, and optional AI

## Release body

Haypile collects local assets through a small desktop pile. Core intake,
preview, keyword search, manual organization, original-file reuse, and library
backup/restore work without AI or an installed model.

- **Fonts:** collect TTF, OTF, WOFF, and WOFF2; preview the actual file, inspect
  character coverage and embedded license information, compare family members,
  and confirm heading/body/decorative usage before Agent handoff.
- **Webpage intake:** drag a browser address or page link onto the pile. One
  compact card lets you browse font and image candidates and save the selected
  original. Static HTML/CSS discovery does not identify every rendered font,
  execute JavaScript, or access signed-in pages.
- **Simple reuse:** click the pile for the C-shaped ring, open recent assets,
  then copy or drag an original file. Cross-batch groups stay in All assets;
  ready-group handoff excludes pending members. Images expose approximate main
  colors; audio supports manual preview.
- **Portable backups:** Settings can save a `.haypile.zip` archive and restore
  it into an empty library after validation. Originals, uses, notes, groups,
  and recent order are retained. GUI preferences, AI credentials, and historical
  batch IDs are excluded; this is manual backup, not cloud sync or library merge.
- **Optional AI:** AI defaults to Off. Existing explicit choices are retained;
  local and authorized API modes provide advisory image suggestions. Intake
  completes first, and unavailable AI does not prevent core operations. Switching
  it off cancels queued work and discards late results. No model is installed
  automatically. An installed/configured model is not a verified vision model.
- **External Agents:** six read-only MCP tools and the existing HTTP endpoints
  provide metadata and controlled URLs. Agents use their own capabilities;
  inspecting images requires both image access and vision support. Arbitrary
  models are not promised description-based image search.

Storage remains version 2 and handoff remains `haypile.asset-handoff.v1`, with
additive font metadata. Local files stay manifest-gated and HTTP binds to
loopback by default. Experimental real-project apply/rollback stays disabled.
See [font limits](FONT_INTAKE.md), [backup scope](LIBRARY_BACKUP.md),
[AI boundaries](LOCAL_AI.md), and [desktop acceptance](DESKTOP_ACCEPTANCE.md).

## 中文发布文案

草堆 Alpha.10 将字体、网页候选和素材库备份接入原来的桌面流程：拖入草堆，
在一张小卡片中预览与收纳，之后通过 C 型环找回并复制或拖出原文件。

支持 TTF / OTF / WOFF / WOFF2 字体、静态网页中的字体与图片候选、跨批次素材
组合，以及设置页的手动备份和空库恢复。网页候选不等于具体文字的实际用字，
内嵌许可与个人备注也不等于商用授权；详细范围见上述说明。

核心功能完整独立于 AI。AI 默认关闭，本地模型和经授权的 API 都是可选增强，
不会自动安装模型；关闭或不可用时仍能正常收纳、搜索、手动整理和取用素材。
外部 Agent 通过只读工具使用自己的能力，不要求绑定草堆模型，也不承诺任意
模型都能描述找图。

## Publication gates

1. Commit the accepted source. The test-only CI matrix runs Linux and Windows
   with Python 3.12.10 and `constraints-release.txt`; it does not produce packages.
   Both jobs must pass for that commit. Keep native macOS and physical-pointer
   results separate from offscreen CI; outstanding checks are recorded in
   [desktop acceptance](DESKTOP_ACCEPTANCE.md).
2. Complete the outstanding native Windows and physical-pointer checks before
   public desktop release. Keep the scope a prerelease; large-library performance
   and arbitrary-model accuracy have not been certified.
3. Build once from the final clean commit. Create a draft prerelease for
   `v0.3.0-alpha.10` before the tag workflows reach their upload step. Both build
   workflows must finish; they validate that the tag matches the build commit.
   The workflows upload assets but do not create the Release.
4. Verify both archives and checksum files, embedded `BUILD_INFO.json` commit,
   package startup, backend/MCP/font smoke checks, and clean shutdown in isolated
   libraries. Record sizes and SHA-256 values from the actual final archives.
5. Publish the draft, then update README and both website download entries to
   the published files. Do not advertise draft or nonexistent download URLs.

Expected release assets:

```text
Haypile-v0.3.0-alpha.10-macos-arm64.app.zip
Haypile-v0.3.0-alpha.10-macos-arm64.app.zip.sha256
Haypile-v0.3.0-alpha.10-windows-x64.zip
Haypile-v0.3.0-alpha.10-windows-x64.zip.sha256
```

The macOS Apple Silicon app is ad-hoc signed and not notarized; Windows x64 is
an unsigned portable build. State the actual signing status in the release and
verify package checksums. An Alpha library should not be the only copy of
irreplaceable material.
