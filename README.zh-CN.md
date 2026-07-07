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

</div>

## 30 秒演示

启动桌面草堆：

```bash
python3 app_gui.py
```

把图片或音频拖到草堆上，然后读取 ready assets：

```bash
python3 examples/use_haypile_http.py
```

预期输出是一份 `asset-handoff` JSON，包含稳定的 `id`、`sha256`、
`source_key`、`url`、`resolved_url` 和 provenance。

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
python3 -m pip install -r requirements.txt
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

运行完整测试：

```bash
python3 -m unittest discover -s tests
```

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

它目前**不承诺**打包安装器、多用户同步、远程托管、agent 破坏性修改素材、生产级素材审批流。

v0.1 的公开边界很小：本地入库、本地登记、manifest-gated 静态访问、只读 HTTP、只读 MCP，以及可追溯的 agent handoff。

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

- 更好的 macOS 打包。
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
