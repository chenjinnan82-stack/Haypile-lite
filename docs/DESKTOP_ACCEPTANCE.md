# 桌面验收记录 · 2026-10-14 至 15

最新源码 `95436a0` 的 [Linux / Windows CI](https://github.com/chenjinnan82-stack/Haypile-lite/actions/runs/38600944584) 全部通过：各运行 605 项，Linux 604 通过、1 项平台跳过；Windows 600 通过、5 项平台跳过。本机 macOS 全量 604 通过、1 项平台跳过，另有 25 项 Cocoa 原生自动检查通过。旧版存储兼容检查已实际执行。

结论：核心功能及可选 AI 进入小范围实机试用，保留当前功能范围。真实鼠标跨应用点击、悬停观感、Windows 实机交互及最终发行包仍须验收，目前尚未公开发布新版本。本轮没有制作安装包、启用用户 AI、下载模型或修改真实素材库。下文保留历史阶段记录，最新结果见末节；既有下载包不包含本次源码改动。

## 桌面交互阶段记录

最后补查发现：拖动草堆期间隐藏窗口，鼠标松开可能不再送达，残留的拖动状态会干扰重新显示后的下一次外部拖入。隐藏时现在一并清除拖动状态、移动标记和速度；回归先复现失败，再验证下一次外部悬停不被错误取消。只增加这三处状态重置，没有增加界面或依赖。

| 最终检查 | 结果 |
| --- | --- |
| 全量回归 | **566 项：565 通过，1 项 Windows 专用跳过**；真实素材库写入保护零触发。 |
| 原生生命周期 | Cocoa 普通/音频/GIF 共 30 轮展开、收起、隐藏和再次显示通过；60 次窗口收起均在动画进度归零后发生。新增“拖动中隐藏 → 再显示 → 外部悬停”检查通过；安全退出完成、退出码 0。输入为程序内事件。 |
| 完整取用流程 | 两个独立进程验证四类文件收纳、去重、字体用途与备注、组合、重启后最近列表及搜索；四类原文件取用后 SHA-256 相同。 |
| 真实网络提取 | 对公开网页实际请求，取得 8 个字体及 4 个图片候选；取消不入库、临时文件清空。再次提取并收纳一项后，其他候选清理完成。网页内容可能随时间变化；此项不代表任意网站都能提取。 |
| 操作边界 | C 环入口、键盘操作、卡片互斥、忙碌时拒绝重复拖入、取消后的晚到结果及退出清理由全量回归覆盖；本地 HTML 提示明确引导拖入原网站链接。 |

最终证据集中在本机 `build/final-acceptance-20261014/`：`hidden-drag-before.log`、`hidden-drag-after.log`、`full-tests-final.log`、`guarded-tests.json`、`native-result.json`、`collect.json`、`reopen.json`、`workflow-proof-result.json` 与 `integrity.json`。原生验收首次启动时，夹具错误地解析了虚拟环境 Python 的符号链接，导致缺少 PySide6；修正启动器后完成上述检查，失败日志保留为 `native-launcher-first.log`，不计作产品缺陷。

桌面交互检查在该轮收尾，下面的人工发布条件仍保留。随后按用户授权完成备份恢复，见文末的新阶段记录。

## 固定日常流程

1. **收进来**：把文件、浏览器地址栏网址或页面链接拖到草堆。网页只出现一张候选卡片，翻到需要的字体或图片再收纳；取消不会保存候选。
2. **找回来**：点击草堆展开 C 型环，再选最近收纳。更多素材从全部素材进入，用名称、字体家族、来源或用途搜索；组合保留跨批次的素材引用。
3. **拿去使用**：打开条目，复制原文件或拖出预览；字体也可以导出。确认用途与原文件取用是两个动作，待确认项不会混入 Agent 的可用交接。

环、卡片互斥；完整管理只在主动选择后打开，不新增常驻窗口或网页浏览器。

## 本轮修复与复核

此前已修复 C 环角落按钮互相抢点击、悬停入口位移、短暂离开后重入等待、反向动画瞬间跳变，以及本地 HTML 的模糊拦截提示。本轮继续发现并修复：

- 音频/GIF 每次吸入新建的动画对象在结束或取消后仍留在窗口下，长时间操作会累积。改用 Qt 的停止后自动释放策略，回归覆盖两类动画的完成与取消。
- 剪贴板触发的 GIF 动画仍先启动动画再扩展窗口。现与拖入路径一致，先准备窗口再启动动画，已展开时不重复调整。
- 中英文 README 的旧说明仍写着点击草堆直接打开最近卡片、管理仅在右键。已与当前 C 环流程统一。

两个新增回归在修复前失败，修复后通过。音频/GIF 四组各连续操作 20 次，动画对象数量恢复到基线；这里验证对象生命周期，不声称测得整机长期内存曲线。

## 验收证据

| 检查 | 结果与边界 |
| --- | --- |
| 完整源码回归 | 562 项，561 通过、1 项 Windows 专用跳过；真实库写入保护零触发。 |
| 新进程重启 | 进程一收纳 PNG、GIF、WAV、TTF，重复导入 PNG，确认字体标题用途与备注并创建组合，然后安全退出；进程二重新加载，四个素材、用途、备注、组合与最近排序保持一致。 |
| 搜索与交接 | 重开后以字体家族和用途组合搜索得到目标字体；混合可用/待确认成员的组合只交付可用项，并报告省略数。 |
| 原文件取用 | 四种格式均通过卡片提供原文件剪贴板 URL，复制结果逐一比对 SHA-256 一致，库内文件保留。此轮复制目标由脚本写入，不能冒充 Finder 原生粘贴；既往 Finder 实测见桌面收纳文档。 |
| 原生动画 | Cocoa 窗口中普通/音频/GIF × 中央/边缘六组，反复离开重入后进度不回跳、完全展开，收起回到原矩形；另有 80 次吸入结束/取消检查。输入由程序内事件产生。 |
| 原生导航 | CUA 实际操作 C 环最近收纳入口、空库卡片、全部素材管理页、Esc 返回及退出；窗口内操作与互斥状态正常。 |
| 浏览器直接拖入 | 用户在前一轮实际拖入公开网站并确认候选卡片出现，原生观察器记录到 DragEnter/DragMove/Drop。 |
| 退出与隔离 | 本轮原生进程退出码 0，清理完成、候选数 0；恢复验收启动器，真实 GUI 设置及原安装包摘要不变。 |

本机详细证据在 `build/overnight-acceptance-20261014/`：`collect.json`、`reopen.json`、`roundtrip.py`、`roundtrip.log`、`animation-result.json`、`full-tests.log`、`guarded-tests.json`、`integrity.json`、`exit.json`。该目录是本地验收产物，不随源码发布。

首次离屏验收脚本在业务流程完成后，因 offscreen 插件持有 Python 的 QMimeData 到解释器析构而崩溃，堆栈位于 QMimeDataWrapper 析构；脚本增加退出前清理自己的测试剪贴板后，两个独立进程均正常退出。此项属于测试夹具修正，未把它写成产品缺陷，也没有通过忽略非零退出码掩盖失败。

## 发布前仍需签收

- 真实鼠标下，C 环中央/旁边及卡片外第一次点击是否同时送达原网页并正确收起界面。
- 用户所见“刚出现时停顿或闪烁”，在修复后的源码版本是否仍出现。自动采样已经覆盖可复现的动画跳变，但不能排除系统窗口合成或特定显示器上的观感问题。
- Windows 原生交互、最终发行包启动与平台签名流程；本轮没有新打包。
- 独立用户隔天找回素材、理解步骤所需提示次数。跨进程重开验证了持久化，不能代替真实使用反馈。

这些项目完成前，不以测试数量或一次成功拖入作整体上线保证。种子测试继续沿用 [独立任务与判断规则](ALPHA9_PILOT.md)，不为本轮技术验收编造用户样本。

## 后续复核：透明角落误触

同日继续检查发现：草堆透明角落的按下会被忽略，但旧的移动/松开处理没有检查这次按下是否被草堆接收，会导致意外拖动或打开 C 环。现在只有草堆已接收的左键按下才能继续拖动和触发松开操作。回归先复现了窗口从 `(180, 260)` 意外移动至 `(210, 290)`，修复后位置保持不变、C 环不打开。两项旧测试也补齐了真实操作必需的按下事件，不再凭空发送移动或松开。

CUA 在独立 Cocoa 窗口中验证透明角落点击、从角落拖向草堆、中心正常点击和 Esc：角落操作不改变草堆位置或打开环，中心点击仍正常展开四个入口，退出清理完成。测量以实际窗口位置为基线，不假定 macOS 启动后位置与脚本请求位置相同。该结果证明 Haypile 的事件归属已修正，不等同于跨应用点击穿透签收。

该轮完整回归为 **563 项，562 通过、1 项 Windows 专用跳过**，真实库写入保护零触发。证据在本机 `build/pointer-acceptance-20261014/` 的 `corner-before.json`、`corner-result.json`、`exit.json`、`full-tests.log`、`guarded-tests.json` 与 `integrity.json`。

窗口形状遮罩仍保留，但不把它单独当作首次点击必然送达其他应用的证据：[Qt 的 QWidget 文档](https://doc.qt.io/qt-6/qwidget.html#setMask-1)将它描述为向窗口系统提供的鼠标区域提示；[Qt 6.11.1 Cocoa 实现](https://github.com/qt/qtbase/blob/v6.11.1/src/plugins/platforms/cocoa/qnsview_mouse.mm)还涉及系统响应链。因此上面的人工发布检查继续保留。

## 后续复核：动画收尾、隐藏与退出

本轮复现并修正三条生命周期路径：

- 上轮引入的动画自动释放没有同步到退出处理。音频/GIF 动画停止后，后台仍在结束时，隐藏清理可能访问已经被 Qt 删除的对象。现在退出调用已有取消方法，同时清空引用；新增回归覆盖先删除动画、稍后再清理窗口的顺序。
- 固定 170ms 收起定时器可能早于动画最后一帧。30 次离屏采样中确实出现过窗口已经缩至 72 点、动画进度仍为 0.213 的状态。现在定时器不再强制缩小，动画归零后才恢复原窗口。
- 隐藏时原先只重置 GIF 状态，没有取消整体展开。现在隐藏会停止悬停计时和动画、清空展开进度；再次显示时，在系统完成显示后恢复原矩形。Cocoa 测试发现同步恢复会被系统定位覆盖，已改为下一轮事件处理恢复，不靠固定延时猜测窗口就绪。

新回归和扩展的隐藏测试均先失败后通过。macOS Cocoa 中普通/音频/GIF 各 10 轮，共 30 轮展开、收起、隐藏和再次显示验证通过，60 次恢复窗口时进度均为 0，位置与大小保持一致。另验证了 GIF 动画中退出、等待 300ms 后再次清理，最终退出码 0、清理完成。原生检查使用程序内事件，不冒充人工拖拽或跨应用点击验收。

本机证据在 `build/lifecycle-acceptance-20261014/`：`collapse-before.json`、`native-first-result.json`、`native-position-before.json` 保留失败记录，`native-result.json` 是最终 30 轮通过结果；`targeted-final.log` 为 151 项针对性检查。完整回归及隔离摘要见同目录的 `full-tests-final.log`、`guarded-tests.json` 和 `integrity.json`。

最新全量检查 **565 项，564 通过、1 项 Windows 专用跳过**。真实素材库写入保护零触发，用户设置、原安装包与验收启动器摘要核对通过；未新增安装包。跨应用首次点击和物理鼠标下的观感仍按发布前检查保留。

## 后续已实现：素材库备份与恢复

用户确认后，设置页已接入完整的备份、校验、恢复确认及空库恢复流程。备份记录相对路径，恢复时重建本机索引，未修改现有库格式；已有数据不覆盖。实现范围、保护措施、历史批次边界及原生验收见[备份恢复说明](LIBRARY_BACKUP.md)。

本阶段全量回归 **576 项，575 通过、1 项 Windows 专用跳过**；真实库写入保护零触发。最终回归与隔离结果以 `build/backup-acceptance-20261014/full-tests-final.log`、`guarded-tests.json` 和 `integrity.json` 为准。桌面跨应用首次点击、物理鼠标观感及 Windows 原生发布签收仍保留，不用备份恢复通过替代这些项目。

## 2026-10-14 窗口依赖整理

- C 环不再持有草堆业务对象或调用素材卡片的私有输入方法；位置通过两个几何回调获取，四个入口通过 Qt 信号交给草堆处理。原生输入读取移至共用的 `desktop_input.py`，保留原来的平台实现和轮询频率。
- 素材卡片通过信号通知打开和请求跳转，不再直接操作 C 环；卡片与 C 环的几何计算都通过回调获取边界。样式、遮罩、按钮大小和操作步骤不变。
- 卡片提供 `begin_shutdown`、`shutdown_pending` 和 `native_operation_active`，草堆退出或启动备份时不再检查卡片私有拖拽/导出字段，也不直接取消卡片的预览线程。卡片的收纳业务仍与草堆协调，未将整个桌面层宣称为完全解耦。

新增回归将 C 环挂在普通 QWidget 上，独立验证四个动作、完成后的外部点击、Esc 和停止轮询。85 项专项检查通过；全量 **578 项，577 通过、1 项 Windows 专用跳过**。

macOS Cocoa 下另通过 6 项检查，覆盖独立 C 环、外部按钮点击、最近卡片打开，以及拖拽/导出嵌套事件循环中的安全退出；原生输入函数加载与读取也通过。原生检查使用 Qt 程序事件，不代替物理鼠标和跨应用点击穿透签收。

证据在 `build/ui-coupling-20261014/` 的 `targeted.log`、`full-tests.log`、`native-tests.log` 及相应 `*-guard.json` / `guarded-tests.json`。均使用隔离素材库，真实库写入保护零触发，未制作新安装包。

## 解耦收尾验收

查询与修复、数据库读写、窗口命令和后台任务退出边界已整理完成，详见[模块边界与解耦验收](ARCHITECTURE_BOUNDARIES.md)。修正了 AI 整理结果信号早于线程结束时的释放时序，实际延迟退出的线程测试通过。损坏主题现在只在明确修改时修复，浏览列表会提示读取异常并保留原文件。

本轮最终全量 **583 项，582 通过、1 项 Windows 专用跳过**；macOS 原生自动检查 **8 项通过**。证据以 `build/decoupling-final-20261014/` 为准，真实库写入保护零触发。之前列出的物理鼠标与 Windows 发布验收边界保持不变。

### 后续任务时序复核

修复两个遗漏：单项 AI 线程停止后、完成通知尚未处理时，重试曾可能覆盖旧任务引用；批量 AI 曾在结果信号到达时提前释放线程并启动下一批。现在以完成清理为重试条件，批量任务等待 QThread 的 `finished`，并拒绝旧任务信号。关闭中的面板不会启动新的 AI 重试。

新增真实 QThread 回归覆盖排队通知、旧任务信号、关闭期间重试及批量串行执行。最新全量 **585 项，584 通过、1 项 Windows 专用跳过**，macOS Cocoa 原生自动检查 **10 项通过**。证据位于 `build/decoupling-followup-20261014/`，真实库写入保护零触发，GUI 设置及原安装包摘要未变；人工发布验收边界保持不变。

### 临时文件清理异常复核

复现并修复网页临时副本或来源记录删除失败时，中断入库完成处理及安全退出的问题。现在逐个尝试删除，记录失败的异常类型后继续；不会将已完成的入库任务留在忙碌状态。无法删除的临时文件可能保留，本次未增加后台清理服务或更改权限，也不会删除用户原文件。

新增两项回归，分别覆盖入库结束和完整退出流程；文件及来源记录分别模拟权限错误，并验证其他临时文件仍被清理、原文件保留。专项 **181 项通过**；全量 **587 项，586 通过、1 项 Windows 专用跳过**；macOS Cocoa 原生自动检查 **12 项通过**。证据位于 `build/desktop-cleanup-review-20261014/` 的 `before-fix.log`、`targeted.log`、`full-tests.log`、`native-tests.log` 和保护报告。真实库写入保护零触发，GUI 设置及既有安装包摘要未变。Windows 和物理鼠标人工验收边界仍保留。

## 最后一轮深度审查

检查范围：备份与恢复的空库保护、暂存校验和提交回退，查询/修改及锁边界，C 环输入和窗口生命周期，网页下载、入库、AI、字体预览与退出清理。修复两项已复现问题：

- **P1，恢复覆盖保护遗漏**：原空库判断仅检查主题目录顶层 JSON，会遗漏笔记、旧副本及子目录中的内容。恢复提交会整体替换主题目录，导致这些文件丢失。现在遇到额外文件、子目录或符号链接时拒绝恢复；仅允许已验证的空主题 JSON 和已知系统目录元数据文件。新增测试验证三种额外内容被保留，清单未修改，未开始恢复事务。
- **P2，下载完成通知被清理异常截断**：下载线程取消或异常后删除临时文件失败，会跳过完成通知。现在下载线程与界面共用原有的容错清理函数，重复候选清理也使用同一规则；单个清理失败不会中断其他清理或完成通知。残留临时副本可能保留，错误类型写入日志。

修复前测试分别复现三种误放行与两种清理异常。修复后专项 **194 项通过**；全量 **589 项：588 通过、1 项 Windows 专用跳过**；macOS Cocoa 原生自动检查 **15 项通过**。独立进程在恢复切换中直接退出（77），新进程回退、再次恢复及第三进程文件摘要验证通过。绝对本地导入图无循环、服务层无 GUI/Qt 导入。

证据位于 `build/release-review-20261014/`：`before-fix.log`、`targeted.log`、`full-tests.log`、`native-tests.log`、`process-result.json`、`boundaries.json` 及保护报告。真实库写入保护零触发，GUI 设置和原安装包摘要未变，没有制作安装包。

结论：本轮未发现剩余开发阻塞项，可结束此次解耦与稳定性修改，进入小范围试用。自动检查不替代物理鼠标跨应用首次点击、拖拽及闪烁观感确认；Windows 原生操作与大规模库性能仍未完成验收，不作跨平台正式发布保证。
## Optional AI boundary — 2026-10-14

AI now defaults to Off; existing saved choices and explicit configuration opt-in remain valid. Image retry actions disappear while AI is off, and switching off requests cancellation without releasing a running worker early. Regular asset refresh and opening AI settings no longer probe a model. The explicit installation check is read-only and never enables AI. Installed/configured states do not claim verified vision capability; actual sorting responses remain validated advisory results. Credential-store exceptions fall back to the existing session-only key behavior or prompt for a key, without plaintext persistence.

The isolated no-AI workflow denies socket connections and model construction while exercising new intake, duplicate intake, keyword search, preview/original copying, manual confirmation, real MCP dispatch through an in-process HTTP route, and library backup/restore. Model connection failures, timeouts, and invalid results preserve bytes and manual roles. AI failures now say that assets are saved and only assistance is unavailable. Existing HTTP/MCP access remains read-only and independent of Haypile AI; metadata/URL access is not advertised as universal description-based image search.

Final regression: **598 tests, 597 passed and one Windows-only skip**. The intermediate targeted run passed 279 tests; additional credential-store coverage is included in the final total. Chinese/English × Off/Local/API layout checks passed all six combinations at the existing panel size, and generated screenshots were visually reviewed. Real-library write protection recorded zero attempts; GUI settings and the existing installer hashes are unchanged. Evidence: `build/optional-ai-20261014/full-tests-final.log`, `guarded-tests.json`, `ui-result.json`, screenshots, protected-file hashes, and the current-turn diff.

No model was downloaded or called, no package was built, and no release was published. These checks establish offline/core independence and error handling, not accuracy or compatibility of an arbitrary model. Native OCR and semantic image search remain future optional work; Windows native acceptance and previously recorded physical-pointer checks retain their existing boundaries.

## 可选 AI 后续复核 — 2026-10-14

修复两处边界：切换本地/API 来源或保存新的 API 授权时，旧批次与单项整理会收到取消请求，旧队列清空，线程保留至正常结束；AI 工作线程初始化遇到无效配置或读取异常时，恢复重试入口，并提示素材已保存。重试批次启动失败不会再被“已加入队列”的提示覆盖。无效 API 表单输入不取消原任务。复制到外部 Agent 的配方也明确：MCP 列表提供元数据和地址，读取图片仍需工具环境访问图片地址以及 Agent 自身的视觉能力。

修复前测试复现了上述取消遗漏和未捕获异常。新增真实 QThread 测试使用受控的延迟模型响应：切换来源后只调用第一张图片，第二张与旧队列均不执行，迟到结果未写入素材，手动确认流程保持可用。该测试没有调用真实模型。完整回归 **603 项，602 通过、1 项 Windows 专用跳过**；真实库写入保护零触发，GUI 设置与现有安装包摘要未变。

证据位于 `build/optional-ai-review-20261014/`：`before-fix.log`、`targeted-corrected.log`、`full-tests.log`、`guarded-tests.json`、`changes.diff` 及保护文件摘要。两次中间专项运行暴露的是新增单项测试夹具缺少目录字段，补齐真实 bundle 必备字段后通过。本轮没有安装模型、制作安装包或发布版本，也没有新增 AI 功能；已有 Windows 原生与物理鼠标人工验收边界保持不变。

## 可选 AI 实际进程验收 — 2026-10-14

在独立测试库中连续运行三次真实 `app_gui.main()` 启动与退出；GUI 使用不可见的 offscreen 平台，后台服务、IPC 和 MCP 均为真实独立进程，不替代原生桌面鼠标验收。

- 首次启动未设置 AI 启用环境变量，确认默认关闭。通过桌面入库线程收纳 PNG 与 TTF，手动确认用途后，真实 HTTP 静态下载逐字节哈希一致；独立 MCP stdio 进程列出六个工具并交付两项素材。
- 第二次启动恢复已保存的本地模型偏好和低功耗暂停状态；退出低功耗恢复原选择，关闭 AI 后完成两项素材的备份与恢复。前两次运行没有任何模型请求。
- 第三次启动确认 AI 关闭状态和素材保留。用户动作启用本地增强后，向仅在本机运行的模拟 Ollama 服务发出一个真实 HTTP 请求；请求尚未返回时关闭 AI 并退出。旧批次第二张图片未发送，迟到建议未写入，四项原文件哈希全部一致。

三次 GUI 均正常退出且完成清理，三个所属后台进程全部退出、端口释放。共七个 Python 进程启用真实素材库写入保护和网络目的地限制，零违规记录；真实 GUI 设置与既有安装包摘要未变。本轮没有修改产品代码，沿用上一轮 **603 项回归，602 通过、1 项 Windows 专用跳过** 的结果，不重复计算测试数。没有调用真实模型、安装依赖、制作安装包或发布版本。

证据：`build/optional-ai-process-20261014/process_proof.py`、`run.log`、三个 `phase-*.json`、七份 `audit-*.json`、`result.json`（含源代码摘要）。本轮未发现新的阻塞项；这些结果验证进程集成和取消行为，不代表模型识别准确率、Windows 原生交互或大规模素材库性能已经验收。


## 发布准备：双平台 CI 与原生 AI 复核 — 2026-10-14

现有测试 CI 增加 Windows，与 Linux 使用同一 Python 3.12.10、依赖约束和全量测试；保留独立的打包工作流。测试产物改用 runner 临时目录，不生成安装包。Windows 远程结果待本次源码提交后的 CI 确认，不能将工作流配置视为已通过验收。

当前源码在 macOS Cocoa 下通过 **25 项原生自动检查**，覆盖 C 环、原生输入读取、窗口及任务退出、备份确认，以及默认关闭 AI、设置不探测模型、切换取消、初始化异常恢复、低功耗偏好和迟到结果丢弃。使用隔离素材库，真实库写入保护零触发；GUI 设置与原安装包摘要未变。证据在 `build/cross-platform-acceptance-20261014/native-tests.log`、`native-guard.json` 与保护文件摘要。物理鼠标和 Windows 人工检查仍保留。

GitHub 实际已发布版本核验为 Alpha.8，README 中不存在的 Alpha.9 下载及视频链接已移除；源码候选与旧包能力明确区分。`OPEN_SOURCE_RELEASE.md` 更新为 Alpha.10 发布草稿，不代表新版本已经发布。

## 跨平台复核与修复 — 2026-10-14 至 15

测试 CI 已与打包拆开：Linux、Windows 使用 Python 3.12.10 和发布依赖约束，获取完整仓库历史以实际执行 Alpha.9 存储兼容检查。此轮只推送开发分支，没有创建版本标签、GitHub Release、网站发布或安装包。

Windows 实际执行及随后的定向复核发现并修复了五处问题：

- **文件身份校验**：CPython 3.12 的 Windows 路径 `stat` 与句柄 `fstat` 的 `ctime` 含义不同，导致正常素材被误判无效、清单为空。跨接口比较改用共同的创建时间，Windows 仍逐次计算文件摘要；读取前后另比较句柄变更时间，保留中途改写保护。实现依据见 [CPython 3.12.10 路径转换](https://github.com/python/cpython/blob/v3.12.10/Modules/posixmodule.c#L2009)与[句柄时间转换](https://github.com/python/cpython/blob/v3.12.10/Python/fileutils.c#L1026)，回归先复现失败后通过。
- **备份持久化**：Windows 不接受在只读句柄上执行文件同步，导致备份生成失败。备份临时文件和恢复暂存文件改用可写句柄同步；原始素材读取仍只读，事务、校验和覆盖保护保持不变。
- **备份编码**：中文备注在非 UTF-8 默认系统编码下可能触发读取异常。备份与恢复的 JSON 元数据明确按 UTF-8 读取；回归模拟 cp1252 默认编码并核对中文备注完整保留。
- **字体 HTTP 类型**：Windows 系统 MIME 映射可能将 TTF 识别为通用二进制。受清单保护的字体响应现在明确设置字体 MIME，不依赖注册表映射。回归模拟错误映射，核对原字节与 `font/ttf`。
- **TLS 取消延迟**：Windows 的套接字 shutdown 未立即唤醒阻塞中的 TLS 握手。改用非阻塞握手与最长 50ms 的就绪等待，检查同一个绝对截止时间与取消请求；握手成功后恢复有超时的读取。真实 TLS 测试覆盖超时和主动取消，并模拟 shutdown 不唤醒的情况；修复前取消耗时约 2 秒，修复后须在 350ms 验收阈值内结束。该阈值是测试条件，不作为所有机器上的实时响应保证。

同时修正两处测试夹具：断网测试只放行 Windows asyncio 创建内部 socketpair 的回环连接，其余连接及模型构造仍拒绝；TLS 测试从无超时的模拟 recv 改为真实 SSL 握手。第三轮 Windows 仅剩 TLS 主动取消失败，证明前述备份、字体和无 AI 流程已通过，不将夹具差异混记为产品缺陷。

本机专项检查分别通过 65 项备份/字体/取用/网络检查及 59 项网络与网页检查。备份与字体修复后的 Cocoa 原生检查 25 项通过；真实库写入保护零触发，GUI 设置和既有安装包摘要保持不变。网络成功路径实际请求 `www.python.org` 与 `example.com`，均返回 200；没有使用真实素材库或调用模型。

最终源码 `95436a027e1ca6799d01ae03216cd94be377cdc6` 的 [CI 38600944584](https://github.com/chenjinnan82-stack/Haypile-lite/actions/runs/38600944584) 双平台通过：Windows **605 项，600 通过、5 项预期平台跳过**；Linux **605 项，604 通过、1 项 Windows 专用跳过**。本机最终全量同为 **605 项，604 通过、1 项跳过**，真实库写入保护零触发。另有 25 项 Cocoa 原生自动检查通过。没有使用增加跳过项或放宽时间阈值来通过 Windows 检查。

验收源码与结果固定在上述提交；本节仅补写验收记录。最终证据包括 `windows-final.log`、`linux-final.log`、`ci-result.json`、`full-tests-tls-final.log`、`native-tests-final.log`、`guarded-tests.json`、`native-guard.json`、`tls-live-smoke.json` 和 `result.json`。源码自动验收通过后，下一步是实机操作签收，再从最终源码一次性构建发行包；本轮不继续扩展功能。

证据目录为 `build/cross-platform-acceptance-20261014/`，保留首次及后续 Windows 失败日志、本地修复前复现、专项检查和完整回归；目录日期沿用此次验收启动日。
