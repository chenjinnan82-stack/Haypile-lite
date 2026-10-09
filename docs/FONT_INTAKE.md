# Alpha.10 字体收纳与验收

字体与图片、GIF、音频共用入库事务、SHA-256 去重、批次、恢复和受清单控制的静态入口。字体存放在 `generic/fonts`，存储格式仍为 v2，字体不会进入图片主题契约。

## 使用范围

支持单文件 TTF / OTF / WOFF / WOFF2，单文件上限 50 MiB。OTF 可包含 CFF/CFF2 或 TrueType 轮廓，按实际 SFNT 签名与轮廓表校验。支持 Unicode 与 Microsoft Symbol 字符表；Symbol 预览明确使用符号编码，并将有对应映射的单字节字符转换至 F000 符号区，缺字提示按实际映射判断。输入来自本地拖入、剪贴板文件或公共 HTTP(S) 字体直链。直链 `application/octet-stream` 必须有支持的字体 URL 后缀；网页 CSS 明确声明 `format()` 时也可用于无后缀字体端点。所有字体均校验实际结构、表数据与 FreeType 解码。沿用禁止私网、重定向、代理和下载超限的远程边界。

通用 `font/sfnt` 与旧 `application/font-sfnt` 也可用于字体直链：有 TTF/OTF 后缀时保留后缀，无后缀时保存为 `.otf`，再校验实际内容，原始字节不变。取消下载或触发流式体积限制时，先关闭目标文件再删除临时副本，兼容 Windows 文件占用规则。

字体预览通过原文件绘制，不依赖系统同名字体。文字上限 120 字，字号为 12–72 像素，默认 32；默认文字为“冬藏 Haypile 0123456789”。会话内切换字体保留文字与字号，重启恢复默认值。预览使用原生滚动区域展示原尺寸位图，cmap 未覆盖的字符显示缺字提示。可变字体使用默认实例。预览画布高度与单次解码字形边界最多为 2048 像素；异常尺寸会被拦截或显示预览失败。

新字体为待确认状态。用途可选标题、正文、装饰，确认后变为可用，也可恢复为待确认。内嵌许可只读，来源备注最多 512 字、许可备注最多 2048 字；保存与取消独立，备注不会修改内嵌信息。备注中的 HTTP(S) URL 不区分大小写，先清洗完整 URL 再限制保存长度，均仅保留站点；长度边界落在 URL 中时舍弃该 URL，避免保存不完整地址。含绝对本地路径或 `file://` 的备注字段不保存、不公开。许可缺失仍显示未知，不自动判断商用授权。收纳事务占用锁时，用途和备注保存立即失败并可重试，原记录不变，不等待事务锁阻塞界面。

导出使用原生保存对话框，按原后缀导出原始字节。先写目标目录临时文件，校验 SHA-256，再原子替换；校验或替换失败不覆盖目标原文件，取消不写文件。素材库目录及其符号链接目标不可作为导出位置。副本缺失时禁用用途、备注和导出操作。

HTTP / MCP 保持只读。使用 `type=font`、`role=font` 和可选 `font_usage` 筛选；默认可用交接排除待确认字体。`font_metadata` 和 `font_annotation` 为 `haypile.asset-handoff.v1` 的新增字段。MCP 的旧 `type=asset` 仍表示全部媒体类型。

WOFF/WOFF2 的编码文件、声明的 SFNT 大小与字体表展开体积均不超过 50 MiB；在 FontTools / FreeType 解码前先对同一字节快照做有输出限制的 zlib/Brotli 检查。可选 WOFF XML 与私有块不用于元数据或预览，预览快照移除这些块，收纳和导出保留原始文件全部字节。许可信息来自 SFNT name 表。

暂不包含 TTC / OTC、内嵌 SVG 字形的字体、可变轴编辑、系统字体安装。SVG 字形表在解析和解压前拒绝；其余字体只解析必要元数据表，由 FreeType 验证实际字形解码，避免提前展开无关的可选表。Windows 源码配置随版本对齐，需单独构建和验收。

## 网页字体流程

拖入地址栏网址、页面链接或字体下载链接，也可复制链接后点击“收纳剪贴板”。图片、GIF、音频和字体直链沿用原来的直接收纳流程。普通网页与 CSS 链接则扫描字体：HTML 字体下载链接、字体预加载链接、内联 style、外部样式表与 CSS `@import` / `@font-face src: url()`。相对地址按页面 base 或样式表地址解析；CSS 转义、备用格式和循环引入会受控处理。同一个声明优先使用第一个成功的远程字体来源；原始字节相同的候选去重。

找到候选后显示选择框，预览通过实际字体文件生成，并显示文件家族、样式、网页声明、来源站点、缺字和内嵌许可。默认不勾选；只有勾选并确认的文件才进入原有入库事务。取消或退出会清除候选与来源临时文件。收纳后仍为待确认状态，按原流程确认用途。网页中找到的是声明的字体候选，不能据此认定每段文字实际使用了哪个字体，也不自动合并字符子集。默认预览覆盖较多的候选优先展示。

单次网页收纳最多 20 个候选、字体总量最多 100 MiB；每个 HTML/CSS 响应最多 2 MiB，每个页面最多获取 8 个样式表、尝试 40 个字体地址。达到上限或资源部分不可获取时显示提示。所有资源都经过相同的公开地址与下载限制检查，来源只保存站点，不保存页面路径、查询参数或凭据。

不执行网页 JavaScript、不携带登录状态、不扫描网页全部图片或音频，也不保存网页正文、标题、截图和链接卡片。`local()` 系统字体、data/blob 字体、只在脚本运行后出现的字体、仅在浏览器内登录可见的资源无法按此流程下载。单独拖一段文字如果未携带网页地址，就没有可靠的来源；此时拖入该页面链接即可。

## 可重复的检查

在项目根目录执行，Python 与依赖使用 `constraints-release.txt` 固定版本：

```sh
QT_QPA_PLATFORM=offscreen .build-venv/bin/python -m unittest discover -s tests -p test_font_support.py -v
QT_QPA_PLATFORM=offscreen .build-venv/bin/python -m unittest tests.test_web_font_intake -v
QT_QPA_PLATFORM=offscreen .build-venv/bin/python -m unittest discover -s tests
.build-venv/bin/python examples/public_smoke_demo.py --out /tmp/haypile-public-demo
QT_QPA_PLATFORM=offscreen .build-venv/bin/python scripts/render_intake_visuals.py --out /tmp/haypile-intake
QT_QPA_PLATFORM=offscreen .build-venv/bin/python scripts/render_hub_visuals.py --out /tmp/haypile-hub
```

字体测试自行生成原创 TTF、CFF OTF、可变 TTF 及 WOFF/WOFF2，覆盖有效和损坏文件、伪装格式、展开体积限制、精确文件预览、缺字、用途和备注持久化、重复批次、恢复、缺失状态、原子导出、远程 MIME、安全清理、HTTP / MCP / 静态字节、200 条之外筛选、异步结果失效与协作退出。网页测试覆盖链接、CSS、相对路径、转义、备用来源、循环引入、私网拒绝、候选去重与取消清理。Alpha.9 兼容检查使用历史提交 `4b198d2`；浅克隆缺少该提交时仅跳过这项历史检查。

macOS 构建要求干净提交，打包显式包含 fontTools 动态表模块与 Pillow FreeType 扩展，并自动运行字体包检查：

```sh
PYTHON=/path/to/python3.12 ./scripts/build_macos_app.sh
.build-venv/bin/python scripts/check_font_package.py \
  --binary dist/Haypile.app/Contents/MacOS/Haypile \
  --out /tmp/haypile-font-package-check
```

包检查输出目录必须是新的隔离目录。检查会启动自己的后台，验证 TTF、CFF OTF、TrueType OTF、可变默认实例、Symbol TTF，以及 TrueType/CFF/可变 WOFF 与 WOFF2，核对静态 SHA-256 与 MIME、HTTP 筛选、备注清洗和 MCP 交接，最后停止该后台。实际发布前还应在隔离素材库中完成原生界面操作，核对 `BUILD_INFO.json` 的提交、版本、包校验和及退出后无残留后台；本次工作不自动发布。
