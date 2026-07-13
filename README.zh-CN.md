<div align="center">

<img src="assets/logo.png" alt="Haypile Lite" width="260">

# Haypile Lite

**给 agent 喂本地素材，但不让它翻你的硬盘。**

散落文件 -> Haypile -> 可用 bundles -> HTTP/MCP -> Agents

[English](README.md)

![Local first](https://img.shields.io/badge/local--first-yes-2f855a)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![MCP](https://img.shields.io/badge/MCP-ready-6F7F5A)
![Agent writes](https://img.shields.io/badge/agent%20writes-off-1f2937)
![License](https://img.shields.io/badge/license-MIT-blue)
![Desktop](https://img.shields.io/badge/app-desktop-334155)
[![CI](https://github.com/chenjinnan82-stack/Haypile-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/chenjinnan82-stack/Haypile-lite/actions/workflows/ci.yml)

</div>

## 唯一公开源头

本仓库及其 Git tag / Release 是 Haypile 唯一的公开源头。大型集成工作区中嵌入的
Haypile 副本仅用于兼容，不得作为公开发行包，也不得反向覆盖本仓库。

## 桌面测试版

无需安装 Python。

| 平台 | 下载 | 状态 |
| --- | --- | --- |
| macOS Apple Silicon | [应用 ZIP](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.1/Haypile-v0.2.0-macos-arm64.app.zip) · [SHA-256](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.1/Haypile-v0.2.0-macos-arm64.app.zip.sha256) | Ad-hoc 签名，未公证 |
| Windows x64 | [便携 ZIP](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.2/Haypile-v0.2.0-windows-x64.zip) · [SHA-256](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.2.0-test.2/Haypile-v0.2.0-windows-x64.zip.sha256) | 未签名测试版 |

### macOS

这是供少量用户测试的 ad-hoc 签名版本，尚未经过 Apple 公证。解压后右键点击
`Haypile.app` 前先将它拖入“应用程序”，再右键选择“打开”。以后可从 Spotlight、
启动台或 Dock 打开冬藏；如果仍被拦截，请到“系统设置 -> 隐私与安全性”点击
“仍要打开”。

### Windows

解压便携包后运行 `Haypile\Haypile.exe`。首个 x64 测试包尚未签名，Windows 可能
显示 Microsoft Defender SmartScreen 提示；运行前请先核对 SHA-256。Windows
自动测试、打包后 MCP/后端冒烟检查及产物校验均已通过，真实 Windows 桌面交互
仍待外部测试。

## 30 秒演示

运行无界面 demo：

```bash
python3 -m pip install -r requirements-core.txt
python3 examples/public_smoke_demo.py --out /tmp/haypile-demo
```

它会创建一个样本素材仓库，并输出包含稳定 `id`、`sha256`、`source_key`、
`url`、`resolved_url` 和 provenance 的 `asset-handoff` JSON。

然后体验桌面草堆：

```bash
python3 -m pip install -r requirements-desktop.txt
python3 app_gui.py
```

把图片或音频拖到草堆上，再读取运行中的后端：

```bash
python3 examples/use_haypile_http.py
```

**边界：** agent 通过 HTTP 或 MCP 读取已登记素材，不应该直接扫描或修改
`storage/assets`。

![Haypile agent workflow demo](docs/haypile-demo.gif)

## 为什么

agent 如果能拿到用户真实的图片、音频和主题碎片，生成效果会好很多。问题是原始文件夹太乱：素材散落、命名不可靠、重复文件越来越多，而且简单生成任务不应该获得整个硬盘目录的访问权。

Haypile 是一个带边界的本地素材草堆。你把文件拖进去，它负责 hash、去重、登记，并且只通过 manifest 允许的 `/static` 暴露素材。agent 拿到的是干净的 ready bundle，不是一个本地路径和一把铲子。

这个隐喻来自鼠兔的越冬草堆：把本地材料收集起来，放在一个安全的位置，之后再取用。

## 现在能做什么

- 提供一个小型桌面拖拽入口，接收图片和音频。
- 对素材做 hash、去重、重命名，并存入本地仓库。
- 生成 manifest，只通过 `/static` 服务已登记文件。
- 通过只读 HTTP API 暴露 ready bundles。
- 提供一层很薄的 MCP 适配器。
- 输出带 provenance 的 agent handoff。
- 可选使用本地 Ollama 视觉模型进行图片分拣。
- 不需要 AI 时可以使用低功耗模式。

## 快速开始

从源码安装：

```bash
git clone https://github.com/chenjinnan82-stack/Haypile-lite.git
cd Haypile-lite
python3 -m pip install -r requirements-desktop.txt
```

运行 Haypile：

```bash
python3 app_gui.py
```

手动后端 smoke test：

```bash
HAYPILE_BACKEND_HOST_ALLOW_START=1 python3 backend_host.py
```

运行公开检查：

```bash
python3 -m unittest tests/test_agent_examples.py tests/test_mcp_server.py
```

运行无界面公开 demo：

```bash
python3 examples/public_smoke_demo.py --out /tmp/haypile-demo
```

运行完整测试：

```bash
python3 -m unittest discover -s tests
```

### 构建 macOS 应用

Haypile 现在可以构建为独立的 Apple Silicon 应用，运行时不需要安装 Python：

```bash
./scripts/build_macos_app.sh
open dist/Haypile.app
```

打包应用的数据写入 `~/Library/Application Support/Haypile/storage`，日志写入
`~/Library/Logs/Haypile`。它不会迁移或修改源码目录中的 `storage/`。

GitHub 测试版只使用 ad-hoc 签名，供少量用户测试，尚未经过 Apple 公证。大规模
公开分发仍需 Developer ID 签名和 Apple 公证。详见
[macOS 测试版说明](docs/MACOS_INTERNAL_BUILD.md)。

## Agent 接入

默认后端地址：

```text
http://127.0.0.1:8010
```

常用端点：

```text
GET /healthz
GET /readyz
GET /api/v1/bundles
GET /api/v1/bundles?status=ready
GET /api/v1/bundles?status=ready&type=image&role=hero_image
GET /api/v1/bundles/{bundle_id}
GET /api/v1/vault
```

MCP host 配置：

```json
{
  "mcpServers": {
    "haypile": {
      "command": "python3",
      "args": ["/absolute/path/to/Haypile-lite/mcp_server.py"],
      "env": {
        "HAYPILE_BASE_URL": "http://127.0.0.1:8010"
      }
    }
  }
}
```

打包应用不需要 Python，MCP 配置直接调用包内可执行文件：

```json
{
  "mcpServers": {
    "haypile": {
      "command": "/absolute/path/to/Haypile.app/Contents/MacOS/Haypile",
      "args": ["--mcp"]
    }
  }
}
```

完整 handoff 结构见 [Agent HTTP Contract](docs/AGENT_HTTP_CONTRACT.md) 和
[Agent Recipes](docs/AGENT_RECIPES.md)。

## 本地 AI

AI 分拣是可选能力。没有 AI，Haypile 依然可以作为本地素材仓库使用。

强制无 AI 模式：

```bash
HAYPILE_LOW_POWER_MODE=1 python3 app_gui.py
```

本地模型设置见 [Local AI Setup](docs/LOCAL_AI.md)。

## 边界

Haypile Lite 不是云端素材管理系统，也不是完整 DAM。

它目前**不提供**已签名和公证的公开安装器，也不承诺多用户同步、远程托管、
agent 破坏性修改素材或生产级素材审批流。

v0.1 的公开边界很小：本地入库、本地登记、manifest-gated 静态访问、只读 HTTP、只读 MCP，以及可追溯的 agent handoff。

实验性的真实项目投放/撤回 helper 默认关闭，不属于公开 agent 接入面。

## 项目结构

```text
桌面拖拽入口                     app_gui.py
FastAPI 后端                     app/main.py
后端启动器                       backend_host.py
HTTP bundle API                  app/api/v1/bundles.py
Theme vault API                  app/api/v1/theme.py
Manifest scanner                 app/services/scanner.py
Bundle service                   app/services/bundle_service.py
Theme registry                   app/services/theme_registry.py
可选视觉分拣                     app/services/style_classifier.py
MCP 适配器                       mcp_server.py
Agent 示例                       examples/
公开文档                         docs/
测试                             tests/
运行时存储                       storage/
```

## Roadmap

- Developer ID 签名、公证和公开 macOS DMG。
- 更多公开 agent 配方。
- 更清楚的桌面新手引导。
- 跨平台启动说明。
- 更稳定的可选 AI 分拣。

## 贡献

欢迎小而清晰的改动。见 [Contributing](CONTRIBUTING.md)。

漏洞报告见 [Security Policy](SECURITY.md)。

## License

MIT. See [LICENSE](LICENSE).

第三方声明见 [NOTICE](NOTICE)。
