# 真实公共网站字体检查

`scripts/check_web_font_sites.py` 是手动联网检查，不是固定结果的单元测试。它直接调用当前 `download_web_fonts`，记录实际收到的 HTML/CSS 字体声明、每次请求的状态或错误、文件哈希和元数据、样张缺字、声明中的 `unicode-range` 提示、耗时及额度。字体和来源临时文件只进入 `TemporaryDirectory`，结束后整体删除；JSON 不包含字体二进制、网页正文或预览位图。

```sh
.build-venv/bin/python scripts/check_web_font_sites.py \
  --out build/web-font-validation-20261013
```

也可在命令末尾指定公共 HTML/CSS 地址。默认每个输入最多 6 个候选、字体总量 32 MiB、每个请求 8 秒、每个输入 45 秒；产品原有的页面、样式表和字体地址上限继续生效。`should_stop` 与产品 socket watchdog 共同执行取消；平台 DNS 阻塞只能在解析返回后检查取消。脚本不使用代理、重定向、登录状态或 JavaScript，也不替换公开地址检查。

每个地址单独输出 JSON，`results.json` 汇总结果并记录 Git HEAD 和三个服务文件的 SHA-256。`candidates_found` 表示得到至少一个候选；`no_static_candidates` 表示读取到的静态输入没有解析出可下载来源。声明存在而没有收集到文件时是 `declared_but_not_collected`，获取失败导致无法判断时是 `unresolved_resource_failures`；它们与顶层错误、取消一样返回非零退出码。已有候选时仍必须查看 `failed_resources` 与 `truncated`，不能将部分成功理解为完整。

## 2026-10-13 实测

检查基于 `ed5d1f6` 及本次工作区修改，详细 JSON 位于忽略目录 `build/web-font-validation-20261013/`；修复后的完整复测位于其 `after-woff2-fix/` 子目录。四个真实站点共五个输入；CDN 示例同时检查首页和它实际链接的中文 CSS，用来区分页面扫描额度与单个子集声明的行为。下表为修复后的实际结果。

| 输入 | 复测结果 | 样张及声明证据 |
| --- | --- | --- |
| [Rasmus Andersson 个人站](https://rsms.me/) | 6 个候选，达到候选额度，0 个资源失败；5.914 秒 | Inter Variable 正体、斜体及若干静态字重；均缺“冬藏”。[Inter CSS](https://rsms.me/inter/inter.css) 与 [Inter Display CSS](https://rsms.me/inter/inter-display.css) 声明多个字重和样式。 |
| [Simon Willison 个人博客](https://simonwillison.net/) | 0 个候选，0 个资源失败；1.137 秒 | [实际 CSS](https://simonwillison.net/static/css/all.d8050490c5db.css) 没有远程 `@font-face`，有 Helvetica Neue、Georgia、Courier 等字体栈。静态输入没有候选符合该声明；不能据此确定读者机器的实际字体。 |
| [京都大学日文首页](https://www.kyoto-u.ac.jp/ja) | 4 个候选，未截断，0 个资源失败；12.230 秒 | 两个 Noto Sans JP TTF（400、500）覆盖样张；Lora 缺“冬藏”，Material Icons 缺“冬藏 A”。页面实际链接的 [Google CSS](https://fonts.googleapis.com/css2?family=Lora:wght@500&family=Noto+Sans+JP:wght@400;500&display=swap) 对 Haypile 用户代理返回完整 TTF 声明，没有 `unicode-range`；不能把另一个浏览器的 CSS 响应当成这次响应。 |
| [Giantaccel 公共字体示例首页](https://static.giantaccel.com/) | 6 个候选，达到额度，0 个资源失败；17.939 秒 | 得到 Fugaz One 与五个 Font Awesome 字体。首页还有 Noto Sans SC 等字体声明，不能由前六个候选代表全部字体。修复前同一输入有七个 Font Awesome 失败资源；见下文。 |
| 首页实际链接的 [Noto Sans SC 子集 CSS](https://static.giantaccel.com/fonts/noto-sans-sc/result.css) | 6 个候选，达到额度，0 个资源失败；3.265 秒 | 六个不同 WOFF2 均有 `unicode-range` 声明，均缺样张中的“冬藏Aa0123”。内嵌家族/样式为 Noto Sans SC Thin / Thin，CSS 声明为 Noto Sans SC / 400，两者分别记录。 |

全部五个输入的临时目录均已删除，输出目录没有 TTF、OTF、WOFF 或 WOFF2 文件。来源 CSS 是从上述公共站点直接获取，网页浏览工具也核对了站点身份；浏览工具不支持部分 `text/css` 响应，因此 CSS 证据以脚本收到的原始响应哈希和解析摘要为准。

这些结果证明能够发现、下载、校验并预览部分真实声明，不能证明网站每段文字实际使用哪个字体。脚本没有运行 CSS 级联、浏览器排版、`local()` 选择或脚本加载。中文子集应按每个文件的实际字符表判断缺字；同一家族多个文件不自动合并。这里没有准确率、网站覆盖率或完整性百分比。

## 实际兼容性问题与边界

初始检查中，真实 [Font Awesome Brands WOFF2](https://static.giantaccel.com/libs/fontawesome/7.2.0/webfonts/fa-brands-400.woff2)（110,076 字节）和 [Solid WOFF2](https://static.giantaccel.com/libs/fontawesome/7.2.0/webfonts/fa-solid-900.woff2)（265,848 字节）均能被 FontTools 与 FreeType 解码，但 Haypile 以 `font_size_limit` 拒绝。Brands 的 `totalSfntSize` 为 181,124，按表目录计算与 FontTools 重建后的大小均为 181,288；Solid 对应为 835,123 与 835,288。原检查把 WOFF2 的参考头大小当成必须精确相等的值；[W3C WOFF2 §3.2](https://www.w3.org/TR/WOFF2/#woff20Header) 说明该值仅供参考，正确解码的字体不应仅因大小不匹配而拒绝。两个文件的无二进制诊断保存在 `fontawesome-diagnostics.json`。

修复后保留头部、目录计算与解压流的体积上限，去掉 WOFF2 参考值的精确相等要求。对原来的 [Font Awesome CSS](https://static.giantaccel.com/libs/fontawesome/7.2.0/css/all.min.css) 不作修改，以 7 个候选为额度复测，实际得到七个候选、0 个资源失败、未截断，耗时 14.107 秒。修复前相同 CSS 得到 0 个候选、7 个资源失败，状态为 `declared_but_not_collected` 并返回退出码 1。对比证据分别位于 `declared-uncollected/` 与 `after-woff2-fix/fontawesome-css/`，均未保留字体文件。

额外边界检查保存在同目录的子目录：

- `redirect-boundary/`：`http://simonwillison.net/` 返回 `redirect_not_allowed`，未跟随重定向；临时目录已清理。
- `cancellation-boundary/`：将 Noto Sans SC CSS 的每输入预算设为 0.02 秒，实际 0.023 秒返回 `interrupted`，状态为 `cancelled`；临时目录已清理。

结果会随站点、用户代理响应、网络及源码变化。重新执行时使用新的输出目录，核对服务哈希与时间，再解释失败或缺字。
