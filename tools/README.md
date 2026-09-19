# Tools 目录

项目通用工具脚本的集合。任务专属脚本已迁移到 `workspace/<task_name>/`，本目录只保留不归属单一任务的通用工具。

## 目录结构

```
tools/
├── audit/                # 仓库一致性对账（drift_detector 字符串弱耦合断链检测）
├── browser/              # 浏览器通用工具（调试 Chrome 启动、批量开 tab、tab 泄漏诊断）
├── debug/                # 屏幕操控调试工具（OCR bbox 回归、坐标链路、DPI）
├── disk/                 # 磁盘管理（回收站删除公共模块；扫描/清理见 workspace/disk_manager/）
├── file_classifier/      # 文件分类器（带 GUI）
├── image_organizer/      # 图片整理（Florence-2/Qwen-VL + LLM 分类）
├── llm/                  # LLM 工具（API 测试、批量注释、Token 统计、帖子总结）
├── media_classifier/     # 媒体分类（<data_drive>:\<bilibili_videos>视频、文字篇章、歌词分析）
├── mindforge/            # MindForge 文档转换守护进程
├── network/              # 本机网络/网卡工具（WiFi 掉线一键修复 bat）
├── release/              # 源码分发 profile 只读审计
├── spacesniffer/         # SpaceSniffer 复刻（.sns 快照 treemap 浏览器，带 GUI）
├── wechat_export_organize.py  # 微信收藏导出整理
├── user_message_gui.py   # 用户消息输入 GUI
├── fake_llm_proxy.py     # Fake LLM Proxy（学习/调试用虚拟 API）
├── x_video_dl.py         # x.com(Twitter) 视频下载（savetwitter 免梯子链路）
└── migrate_data_layout.py # 一次性数据迁移脚本（temp/ 和 server/memory_v2/ → data/）
```

> 任务专属脚本（如各游戏抽卡采集、社群爬取、记账、异环模拟器等）已迁移到 `workspace/<task_name>/`，详见 `tools_manifest.json`。

## 各目录详解

### `audit/` — 仓库一致性对账

- `drift_detector.py` — **漂移对账脚本**（提交前机械防线之一，注册表类改动必跑）
  - 检测项目中的"字符串弱耦合断链"，共 8 组：A 运行时 OpenAPI 真源 / B 客户端 HTTP 调用 vs 服务端路由 / C `tools_manifest.json` 路径存在性 / D `GUIDE_REGISTRY` 条目（skill_file 存在性、manifest `[skill].task_type` 一致性、`mcp_tools_priority` 工具名有效性）/ E `mcp_whitelist` 三张表 ⊆ operationIds / F `config.example.toml` ↔ `data/config_descriptions.json` 双向对账 / G `workspace/*/manifest.toml` 声明路径存在性 / H `docs/api-reference.md` ↔ OpenAPI 路由双向对账
  - CLI：`uv run python tools/audit/drift_detector.py`（需 import `server.main`，故必须在仓库根运行）
  - 输出：控制台摘要 + `temp/audit/drift_report.md`。报告是**可再生产物**，所以写 temp/ 不写 `docs/`；脚本本身是工具，故从 temp/ 迁到本目录
  - ⚠️ 本目录不在 `pyrightconfig.json` 的 `include` 范围内，仓库级 `uv run pyright` **从不检查它**，改动后须显式 `uv run pyright tools/audit/drift_detector.py`

### `browser/` — 浏览器通用工具
- `start_debug_browser.py` — 启动独立调试 Chrome 实例（端口 9222，不影响工作浏览器）
- `open_tabs.py` — 批量打开标签页
- `leak_trace_plugin.py` — **pytest tab 泄漏诊断插件**（2026-09-03 从 `temp/` 转正）：逐用例 teardown 后经 `GET /browser/tabs` 统计调试浏览器真实 tab 数，超过基线（默认 2）记录 LEAK 行到 `<项目根>/temp/leak_trace.log`。用法：`env PYTHONPATH="<项目根>/tools/browser" uv run pytest -p leak_trace_plugin tests/browser/... -q`。前提：后端 8766 存活。基线与日志路径可用 `LEAK_TRACE_BASELINE` / `LEAK_TRACE_LOG` 覆盖。定位"测试全绿但浏览器 tab 悄悄变多"类问题用

### `debug/` — 屏幕操控调试
**状态：开发调试用，非常规工具**

排查 OCR 识别、bbox 坐标链路和 DPI 偏移。PaddleOCR/PaddleX 更新后先运行三档 bbox 回归；详见 `debug/README.md`。

### `disk/` — 磁盘管理

- `recycle.py` — **回收站删除公共模块**（项目统一入口）
  - `send_to_recycle(path) -> (ok, err_msg)`；`recycle_batch(paths, on_error=...)`
  - ctypes `SHFileOperationW` + `FOF_ALLOWUNDO`，纯 Python，无子进程依赖
  - CLI：`uv run python tools/disk/recycle.py --list paths.txt`（也支持位置参数 / `--stdin`）
  - ⚠️ 本机 PowerShell `Add-Type` 被安全策略拦截，**不要**改用 `Microsoft.VisualBasic.FileIO` 方案

其余磁盘工具在 `workspace/disk_manager/scripts/`（已登记 `tools_manifest.json` 的 `disk` 分类）：

- `scan_disk.py` — 磁盘空间扫描
- `cleanup.py` — 磁盘清理（dry-run 回执 → 用户勾选 → 回收站删除；内部复用本目录 `recycle.py`）
- `backup_env.py` — 系统环境备份

> `client/core/agent/builtin_tools/file_delete.py` 另有一份等价实现，属**有意为之**：`client/` 是打包分发的产品代码，不能依赖 `tools/`（开发工具不参与发布）。新增工具脚本请一律复用 `tools/disk/recycle.py`。

### `file_classifier/` — 文件分类器
带 PySide6 GUI 的文件分类工具，使用 LLM 预测文件类别（支持文件+文件夹分类、类交互式协议、state.json 状态持久化）。
- `classifier_gui.py` — GUI 主程序
- `predictor.py` — 预测模型（含 classify_folder 类交互式文件夹分类协议）
- `state_manager.py` — 状态持久化管理（分类即已处理、列宽记忆、源目录记忆）
- `type_descriptor.py` — 文件类型描述生成（扩展名→友好中文类型）
- `file_table_widget.py` — 文件表格组件（反选语义交互模型）
- `details_dialog.py` — 详情弹窗（ctrl+enter）
- `folder_info.py` — 文件夹信息聚合（文件数/扩展名分布/嵌套深度）
- `categories.json` — 分类配置
- `file_classifier_guide.md` — 操作指引文档
- 操作指引详见 `tools/file_classifier/file_classifier_guide.md`

### `image_organizer/` — 图片整理
**状态：历史方案/暂停** | **环境：主 `.venv`**

旧版大规模图片自动分类工具。使用 Florence-2 进行图片描述和 OCR，再通过 LLM 池多维度分类。当前不作为 Remote-VL 抽样工作的入口。

- `organize.py` — 主程序（Florence-2 caption+OCR → LLM 分类 → move → SQLite）
- `florence_bench.py` — Florence-2 测速基准
- `test_models.py` — 模型对比测试（Florence-2 vs 其他视觉模型）
- `test_pipeline.py` — 全流程集成测试
- `view_db.py` — 查看分类数据库内容
- `view_failed.py` — 查看失败文件

**运行环境**：`uv run python tools/image_organizer/organize.py`（Florence-2 从 HF cache 加载；OmniParser 移除后仍可运行，主 `.venv` 含 `transformers` + `torch`）

当前 Remote-VL 抽样工作区：`workspace/image_organize_remote_vl_sample/`。恢复前必须阅读该目录的 `RESUME.md`，不要直接对原始手机备份执行扫描或移动。

### `llm/` — LLM 工具
**状态：活跃使用**

通过后端 LLM 池（多 key 并发）执行批量 LLM 任务的工具集。
- `summarize_posts_direct.py` — 批量总结社群帖子（多维评分 + 加权排序）
- `mimo_batch_comment.py` — 批量代码注释生成（Python/C# 源码添加中文注释）
- `test_api_batch.py` — API 端点可用性批量测试
- `test_llm.py` — 快速测试 LLM 池是否可用
- `llm_token_stats.py` — Token 消耗统计报告

### `media_classifier/` — 媒体分类
**状态：活跃使用**

多种媒体内容的 LLM 分类工具。以后可能有文段、视频、歌词等需要整理时复用。
- `bilibili_classifier.py` — <data_drive>:\<bilibili_videos>视频分类 Phase 1（扫描+弹幕提取+LLM 10类分类）
- `bilibili_mover.py` — <data_drive>:\<bilibili_videos>视频分类移动 Phase 2（按分类+置信度分层 move）
- `print_anime.py` — 打印动漫分类结果
- `wenzhang_classifier.py` — 文字篇章整理（txt/docx 解析+LLM 15类分类+特征提取）
- `scan_wenzhang.py` — 扫描文字篇章文件夹统计
- `scan_docx.py` — 读取 docx 段落结构
- `count_segments.py` — 统计文段数量
- `mimo_lrc_analyze.py` — LRC 歌词分析（情感/主题/风格，支持 MuseArc 数据库关联）

### `mindforge/` — MindForge 文档转换
- `converter_daemon.py` — 文档转换守护进程（MineRU + PaddleOCR），通过 stdin/stdout JSON 协议通信

### `network/` — 本机网络/网卡工具

- `wifi_adapter_repair.bat` — **无线网卡掉线一键修复**（双击运行，自动 UAC 提权；桌面 `WiFi网卡修复.bat` 是同一份字节的副本）
  - 症状签名（2026-09-18 实测）：托盘 WiFi 图标消失、`netsh wlan show interfaces` 报"There is no wireless interface on the system."、`Get-NetAdapter` 里 WLAN 的 `Status` 为空白、`Get-PnpDevice` 显示设备 `Error` + `CM_PROB_FAILED_POST_START`，而 `WlanSvc` 服务仍是 `Running`。**关键结论：这种掉线是驱动 miniport 崩了（System 日志里 NDIS `Fatal error: The miniport has detected an internal error` + `Netwtw10` 事件 5002），重启服务/flushdns/重置 TCP 栈都没用，必须重启"设备"**
  - 分级流程：`pnputil /restart-device` → 禁用+启用+`/scan-devices` → `net stop/start WlanSvc` → 提示重启电脑；每级之后每 5 秒轮询体检（网卡 `Status` 非空 **且** PnP 设备 `OK`/`CM_PROB_NONE`）才判定恢复。实测第 1 级命令本身返回成功但驱动没起来，第 2 级才救回——所以必须轮询而不能固定 sleep
  - 设备实例 ID 运行时按 `FriendlyName -like 'Intel*Wi-Fi*'` 解析，不写死（脚本里保留了 AX200 的 fallback 常量）；`[2]` 查看状态是纯只读；`[3]` 一次性加固写 `PnPCapabilities=24`（屏蔽"允许计算机关闭此设备以节约电源"）+ `HiberbootEnabled=0`（关快速启动），先打印当前值再确认，还原命令写在脚本头注释
  - 有意**不做**自动重连：机器上存了一堆 WiFi 配置（含手机热点），脚本只负责让网卡复活，连哪个由人在托盘决定
  - ⚠️ 这是 `.bat`，编码必须是 GBK+CRLF+无 BOM；改它必须走 `.agents/skills/bat_writing/SKILL.md` 的 2b 编辑回路（直接 Edit 会把中文冲坏），改完 `--check` 全绿再同步桌面副本

### 录制器（已迁至 `workspace/recorder/`）
操作录制器（键鼠 + 屏幕截图 + 音频 + 窗口焦点 → 可回放录制包）**不在 `tools/` 下**：
- 启动入口：`python -m workspace.recorder.tools.main`（配置对话框/悬浮条/监控窗口/编辑器等都在 `workspace/recorder/tools/`）
- 分层实现：L0 传感器 `lib/recorder/sensors/`、L1 处理 `lib/recorder/processor/`、L2 时间轴 `lib/recorder/timeline/`、L3 编辑 `lib/recorder/editor/`、L4 agent 消费库 `workspace/recorder/consumer/`（无后端 HTTP 端点，直接 import）
- 用户文档：`docs/recorder-guide.md`（含 L4 消费层章节）

### `release/` — 源码分发审计（v2 compiler 引擎）
- `cli.py` — 单入口多 subcommand CLI（`prepare` / `compute-digest` / `build` / `list-components` / `scan`）。所有发布工作流经此入口；旧的 `audit_profile.py` + `export_release.py` 已删除（2026-07-31，T18）
- `engine/` — 不可变 PreparedRelease 计划模型 + `prepare_release()` + `build_release()` 接口实现（spec-v2-compiler.md）
- `templates/` — Jinja2 模板（`DEPLOYMENT.md.j2` / `RELEASE_NOTES.md.j2`），不含 `zip_size` 字段

### `spacesniffer/` — SpaceSniffer 快照浏览器（带 GUI）
- `main.py` — CLI 入口：`main.py "<快照.sns>"` 直接加载，`--info` 只打印摘要不开窗；`start.bat` 双击启动（GBK+CRLF 编码，勿存成 UTF-8）
- `sns_model.py` / `treemap.py` / `filtering.py` / `filetypes.py` / `tagging.py` / `report.py` / `viewer.py` — 解析 / squarified 布局 / 过滤 DSL / 类型配色 / 四色标记 / 导出 / 主窗口
- **只读快照浏览器，不做扫描**：原版的实时扫描、文件系统事件同步、NTFS ADS 扫描在离线快照上没有意义，刻意不复刻；全部「分析类」操作齐备
- 解析器自包含（不复用 `workspace/disk_manager/scripts/parse_sns.py`，后者属发布产物），两份实现的一致性由 `tests/files_tools/test_spacesniffer.py::test_cross_check_with_disk_manager` 交叉校验守护
- 实测 67.7 MB 全盘快照：解析 ~5.8s、布局 ~100ms、命中测试 ~7.5µs；解析与布局均在后台线程
- 详见 `spacesniffer/README.md`

### `wechat_export_organize.py` — 微信收藏导出整理
将 `temp/微信收藏导出/` 下的内容整理为可读的 Markdown 格式。

### `user_message_gui.py` — 用户消息输入 GUI
Tkinter GUI 窗口，用于向后端发送用户消息。

### `fake_llm_proxy.py` — Fake LLM Proxy
学习用虚拟 API 端点，接收 OpenAI 格式请求返回默认文本，内置实时网页监控。

### `x_video_dl.py` — x.com(Twitter) 视频下载
走 savetwitter.net 免梯子解析链路（解析 API → dl.snapcdn.app 签名直链 → 流式下载），
顺序前台执行、逐条输出进度、条间限速，自动选最高分辨率并校验 MP4 文件头。

- CLI：`uv run python tools/x_video_dl.py <URL> [URL ...] [--max-res 720] [--out DIR] [--seq-start N] [--prefix TAG]`
- 输出默认 `~/<data_drive>:/Downloads/x_videos_<当天日期>/`
- ⚠️ 输出文件名严禁含冒号（Windows 会静默写进 NTFS ADS：主文件 0 字节、播放器打不开），
  脚本已做白名单清洗；解析按钮 label 内嵌 `<i>` 标签的正则坑详见脚本 docstring
- 2026-09-16 由 chat_digest 的 x 视频下载任务转正，`tools_manifest.json` 注册于 `media_dl` 分类

## 已迁移到 workspace/

以下脚本已迁移到 `workspace/<task_name>/` 任务工作区（脚本+数据+配置统一存放）：

| 任务工作区 | 包含脚本 |
|----------|---------|
| `workspace/accounting/` | bill_converter.py、记账.bat、name_mapping.json、review_config.json |
| `workspace/arknights_gacha/` | arknights_gacha.py、arknights_import.py |
| `workspace/bilibili_gacha/` | bilibili_gacha.py、bilibili_cleanup.py、bilibili_check_lottery.py |
| `workspace/community_review/` | save_community.py、extract_community.py、sync_community.py、update_community.py、json_to_md.py、community_stats.py、community_stats_forShow.py、summarize_posts.py、migrate_timestamps.py、community_utils.py |
| `workspace/endfield_gacha/` | endfield_gacha.py |
| `workspace/niuke_review/` | nowcoder_embedded.py、nowcoder_review.py |
| `workspace/official_gacha/` | official_gacha.py |
| `workspace/wuwa_gacha/` | wuwa_gacha.py |
| `workspace/yihuan_gacha/` | yihuan_gacha.py、yihuan_clean.py、yihuan_review.py |
| `workspace/yihuan_simulator/` | simulator/editor.py、simulator/viewer.py |

## 已清理

- `tools/agent/` — 空目录，已删除（未知创建时间和用途）
- `tools/accounting/` — 已迁移到 `workspace/accounting/`
- `tools/yihuan/` — 已迁移到 `workspace/yihuan_simulator/`
- `tools/gacha/` — 抽卡脚本曾短暂存放于此，已分散迁移到各 `workspace/<game>_gacha/`
