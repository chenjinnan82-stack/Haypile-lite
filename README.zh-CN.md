<div align="center">

<img src="assets/logo.png" alt="Haypile 冬藏" width="220">

# Haypile · 冬藏

**丢进草堆，整理后交给 Agent。**

为 AI 创作者和独立开发者准备的本地优先素材入口。

[English](README.md)

![Local first](https://img.shields.io/badge/local--first-yes-2f855a)
![MCP](https://img.shields.io/badge/MCP-read--only-6F7F5A)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-MIT-blue)
[![CI](https://github.com/chenjinnan82-stack/Haypile-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/chenjinnan82-stack/Haypile-lite/actions/workflows/ci.yml)

</div>

## 桌面版下载

`v0.3.0-alpha.4` 是面向知情测试用户的安全种子预览版。

| 平台 | 下载 | 校验 |
| --- | --- | --- |
| macOS Apple Silicon | [Haypile.app.zip](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.3.0-alpha.4/Haypile-v0.3.0-alpha.4-macos-arm64.app.zip) | [SHA-256](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.3.0-alpha.4/Haypile-v0.3.0-alpha.4-macos-arm64.app.zip.sha256) |
| Windows x64 | [便携 ZIP](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.3.0-alpha.4/Haypile-v0.3.0-alpha.4-windows-x64.zip) | [SHA-256](https://github.com/chenjinnan82-stack/Haypile-lite/releases/download/v0.3.0-alpha.4/Haypile-v0.3.0-alpha.4-windows-x64.zip.sha256) |

[版本说明](https://github.com/chenjinnan82-stack/Haypile-lite/releases/tag/v0.3.0-alpha.4)

macOS 包采用临时签名且尚未公证，首次启动请右键应用并选择“打开”。Windows
包尚未签名，请先解压再运行 `Haypile.exe`。Haypile 会保存受控的本地副本，但
Alpha 版本不应成为不可替代素材的唯一备份。

## 看它怎么工作

![Haypile 桌面工作流](docs/haypile-demo.gif)

草堆始终固定在桌面。左键展开附着式“素材、Agent、设置”抽屉；把文件拖到草堆
上方即可入库。
演示使用当前 Qt 界面与仓库自有示例素材渲染，没有读取用户素材。

## 三步使用

1. **拖入：** 从 Finder、资源管理器或浏览器拖入图片；音频仍可正常入库。
2. **整理：** Haypile 对最新批次做 hash、去重和安全登记，并可用本地模型或经
   授权的 API 提供图片用途建议。
3. **交付：** 把最新一批 ready 素材通过 HTTP、MCP 或 `asset-handoff.v1`
   交给 Codex 或其他 Agent。

```text
浏览器 / 桌面 -> Haypile -> 最新可用批次 -> HTTP / MCP -> Agent
```

## 安全边界

Haypile 的本地优先不是一句口号：

- 服务只绑定 `127.0.0.1`，静态文件必须先进入 manifest。
- Agent 得到登记后的 URL 和 provenance，不获得文件系统访问权。
- HTTP 与 MCP 只读，不提供 Agent 写入或删除素材。
- 远程 AI 必须明确授权域名；非本机地址强制 HTTPS。
- API 密钥写入 macOS Keychain 或 Windows Credential Manager，不进入
  `gui_state.json`、日志、provenance 或 handoff。
- 云端视觉请求不发送原始文件名和本地绝对路径。

安全边界问题请按 [SECURITY.md](SECURITY.md) 或通过 [GitHub 私密漏洞报告](https://github.com/chenjinnan82-stack/Haypile-lite/security/advisories/new) 提交。

## v0.3 增加了什么

- 入库先完成、AI 后整理；模型离线、超时或限流不会阻止素材保存。
- 每次 Drop 都有稳定批次 ID，重复素材仍会加入本批次。
- 图片用途包括背景、主视觉、Logo、图标、内容图和纹理。
- 技术质量由本地确定性规则判断，不让模型评价审美。
- Agent 页默认复制最新批次，而不是整个长期素材仓库。
- AI 可选择“本地模型 / API / 关闭”。音频元数据和手动用途确认不依赖 AI。

## Agent 接入

本地后端默认地址：`http://127.0.0.1:8010`。

```text
GET /healthz
GET /readyz
GET /api/v1/batches/latest
GET /api/v1/bundles?status=ready&batch_id=latest
GET /api/v1/bundles/{bundle_id}
```

源码模式 MCP 配置：

```json
{
  "mcpServers": {
    "haypile": {
      "command": "python3",
      "args": ["/absolute/path/to/Haypile-lite/mcp_server.py"],
      "env": {"HAYPILE_BASE_URL": "http://127.0.0.1:8010"}
    }
  }
}
```

打包应用直接调用 Haypile 可执行文件：

```json
{
  "mcpServers": {
    "haypile": {
      "command": "/Applications/Haypile.app/Contents/MacOS/Haypile",
      "args": ["--mcp"]
    }
  }
}
```

Windows 把 `command` 改为 `Haypile.exe` 的绝对路径，继续使用 `--mcp`。Agent
抽屉可以直接复制当前平台的正确配置。

完整说明见 [HTTP 合约](docs/AGENT_HTTP_CONTRACT.md) 与
[Agent 配方](docs/AGENT_RECIPES.md)。

## 从源码运行

```bash
git clone https://github.com/chenjinnan82-stack/Haypile-lite.git
cd Haypile-lite
python3 -m pip install -r requirements-desktop.txt
python3 app_gui.py
```

无界面 smoke demo：

```bash
python3 -m pip install -r requirements-core.txt
python3 examples/public_smoke_demo.py --out /tmp/haypile-demo
```

运行测试：

```bash
python3 -m unittest discover -s tests
```

macOS 构建说明见 [MACOS_INTERNAL_BUILD.md](docs/MACOS_INTERNAL_BUILD.md)，平台脚本位于
`scripts/`。私有评估与发布门槛见 [AI_EVALUATION.md](docs/AI_EVALUATION.md)。

## 项目边界

- 本仓库及其 Git tag / Release 是 Haypile 唯一公开源头。
- Haypile 不是云端 DAM、多用户同步服务或 Agent 写入平台。
- 相同 SHA-256 内容仍视为同一个逻辑素材；当前不支持项目级身份、多角色上下文或 workspace 隔离。
- Haypile 保存受控副本，但不能作为重要原始素材的唯一备份。
- 实验性的真实项目投放/撤回 helper 默认关闭，不属于公开 Agent 接入面。

问题和可复现的体验反馈请提交到
[GitHub Issues](https://github.com/chenjinnan82-stack/Haypile-lite/issues)。欢迎小而清晰的
改动，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

MIT 许可，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
