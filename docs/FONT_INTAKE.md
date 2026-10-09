# Alpha.10 字体收纳与验收

字体与图片、GIF、音频共用入库事务、SHA-256 去重、批次、恢复和受清单控制的静态入口。字体存放在 `generic/fonts`，存储格式仍为 v2，字体不会进入图片主题契约。

## 使用范围

支持单文件 TTF / OTF，单文件上限 50 MiB。OTF 可包含 CFF/CFF2 或 TrueType 轮廓，按实际 SFNT 签名与轮廓表校验。输入来自本地拖入、剪贴板文件或公共 HTTP(S) 字体直链。`application/octet-stream` 必须有 `.ttf` / `.otf` URL 后缀；所有字体均校验实际 SFNT 结构、表数据与 FreeType 解码。沿用禁止私网、重定向、代理和下载超限的远程边界。

通用 `font/sfnt` 与旧 `application/font-sfnt` 也可用于字体直链：有 TTF/OTF 后缀时保留后缀，无后缀时保存为 `.otf`，再校验实际内容，原始字节不变。取消下载或触发流式体积限制时，先关闭目标文件再删除临时副本，兼容 Windows 文件占用规则。

字体预览通过原文件绘制，不依赖系统同名字体。文字上限 120 字，字号为 12–72 像素，默认 32；默认文字为“冬藏 Haypile 0123456789”。会话内切换字体保留文字与字号，重启恢复默认值。预览使用原生滚动区域展示原尺寸位图，cmap 未覆盖的字符显示缺字提示。可变字体使用默认实例。预览画布高度与单次解码字形边界最多为 2048 像素；异常尺寸会被拦截或显示预览失败。

新字体为待确认状态。用途可选标题、正文、装饰，确认后变为可用，也可恢复为待确认。内嵌许可只读，来源备注最多 512 字、许可备注最多 2048 字；保存与取消独立，备注不会修改内嵌信息。备注中的 HTTP(S) URL 不区分大小写，均仅保留站点；含绝对本地路径或 `file://` 的备注字段不保存、不公开。许可缺失仍显示未知，不自动判断商用授权。

导出使用原生保存对话框，按原后缀导出原始字节。先写目标目录临时文件，校验 SHA-256，再原子替换；校验或替换失败不覆盖目标原文件，取消不写文件。素材库目录及其符号链接目标不可作为导出位置。副本缺失时禁用用途、备注和导出操作。

HTTP / MCP 保持只读。使用 `type=font`、`role=font` 和可选 `font_usage` 筛选；默认可用交接排除待确认字体。`font_metadata` 和 `font_annotation` 为 `haypile.asset-handoff.v1` 的新增字段。MCP 的旧 `type=asset` 仍表示全部媒体类型。

暂不包含 TTC / OTC / WOFF / WOFF2、CSS 或网页解析、可变轴编辑、系统字体安装。Windows 源码配置随版本对齐，需单独构建和验收。

## 可重复的检查

在项目根目录执行，Python 与依赖使用 `constraints-release.txt` 固定版本：

```sh
QT_QPA_PLATFORM=offscreen .build-venv/bin/python -m unittest discover -s tests -p test_font_support.py -v
QT_QPA_PLATFORM=offscreen .build-venv/bin/python -m unittest discover -s tests
.build-venv/bin/python examples/public_smoke_demo.py --out /tmp/haypile-public-demo
QT_QPA_PLATFORM=offscreen .build-venv/bin/python scripts/render_intake_visuals.py --out /tmp/haypile-intake
QT_QPA_PLATFORM=offscreen .build-venv/bin/python scripts/render_hub_visuals.py --out /tmp/haypile-hub
```

字体测试自行生成原创 TTF、CFF OTF、可变 TTF，覆盖有效和损坏文件、伪装格式、体积限制、精确文件预览、缺字、用途和备注持久化、重复批次、恢复、缺失状态、原子导出、远程 MIME、安全清理、HTTP / MCP / 静态字节、200 条之外筛选、异步结果失效与协作退出。Alpha.9 兼容检查使用历史提交 `4b198d2`；浅克隆缺少该提交时仅跳过这项历史检查。

macOS 构建要求干净提交，打包显式包含 fontTools 动态表模块与 Pillow FreeType 扩展，并自动运行字体包检查：

```sh
PYTHON=/path/to/python3.12 ./scripts/build_macos_app.sh
.build-venv/bin/python scripts/check_font_package.py \
  --binary dist/Haypile.app/Contents/MacOS/Haypile \
  --out /tmp/haypile-font-package-check
```

包检查输出目录必须是新的隔离目录。检查会启动自己的后台，验证 TTF、CFF OTF、可变默认实例、静态 SHA-256 与 MIME、HTTP 筛选和 MCP 交接，最后停止该后台。实际发布前还应在隔离素材库中完成原生界面操作，核对 `BUILD_INFO.json` 的提交、版本、包校验和及退出后无残留后台；本次工作不自动发布。
