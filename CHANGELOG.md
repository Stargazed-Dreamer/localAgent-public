# Changelog

所有重要变更均记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### Added

- (暂无)

### Fixed

- **release 编排增量发布锚定死锁修复（`tools/release/cli.py` + `tools/release/engine/triage.py`）**：第二阶段校验 `snapshot.scan_digest == plan.scan_digest`，但快照只在批准生效点整体重写、第一阶段从不刷新 digest——任何增量发布（第二轮起）必然失配被拒，且错误提示的"重跑第一阶段"无法解开死锁（v0.46.0 走 `--first-triage` 首轮路径，该缺陷未暴露；v0.48.0 发布实测踩中）。修复：第一阶段出干净报告时把快照 scan_digest 对齐到本轮扫描（新增 `refresh_snapshot_digest()`，已批准命中集与审批元数据不动；存在 NEW 命中时仍拒绝且不刷新，"必须处理到干净"的纪律不变）。顺带修 `--first-triage` 路径下 `snapshot` 可能为 None 的 pyright 报错；新增 3 个回归测试（干净报告刷新锚点 / NEW 不刷新 / helper 单测）。（2026-09-16）

## [0.48.0] - 2026-09-16

### Added

- **新增 docs/project-intro.md 深度项目导览（第一批：第 0-5 章 + 附录 D 25 问，方案与进度见 temp/sdd/project-intro/plan.md）**：面向新维护者/想了解项目的人的导览层文档——比喻为主干（一人公司/临时顾问/经验飞轮）、三条阅读路径各带验收标准、全文执行"文档防过时规范"（零易漂移数字，清单一律指向 `_index.md` 自动段/代码常量/运行时清单）。本批内容：三种读法、人话版定位、全景地图（四角色 + 目录地图 + **data/ 运行时产物地图**（谁写的/能否删）+ 数据流图）、跟一条记账任务走完全程、**宏观工作流**（经验飞轮/inbox 与 user_message 两条消息通路/GUI 面板接线/LLM 池供血）、**真实案例与可扩展性**（记账人机协作闭环/股市 loop 时间线/抽卡多游戏共享框架/群聊总结组件孵化/网页存档站点经验库/磁盘管家审批安全，深描 6 案 + 全景表指针化）。验收机制落地：零上下文 agent 只读文档作答自测题，首批 9 题试读 8 题高置信通过，7 处写作缺陷（授权不持久化缺理由/自造词/路径矛盾等）已按试读反馈修复；试读沉淀的未答疑问清单回流 plan.md 供后续批次与 FAQ。（2026-09-16）

- **project-intro 第二、三批完成（第 6-15 章九个新章，文档现 1312 行）**（`docs/project-intro.md` + `temp/sdd/project-intro/brief-batch2.md`/`brief-batch3.md` 简报）：第 6-9 章四个核心机关（agent_guide 打分路由与记忆消费 / 三层记忆与证据链 / MCP 三层网关审批链与对外边界 / LLM 池档位与三层熔断），第 10-15 章（手和眼睛含反检测哲学 / 对话引擎与 GUI 设计系统含 ADR-0006 裁剪故事 / Skill-workspace-Todo-SDD-ADR 沉淀制度 / 挂机与自动化生态含条件关机与合规框 / 工具生态地图 / 源码分发系统“打包当编译”）。全部按三级流水线（主会话简报 → subagent 细读代码深写、逐声明带 文件:行 证据 → 主会话抽验 → 零上下文新读者只读文档作答附录 D 对应自测题）：两批试读 13/13 题高置信通过；按试读反馈修复 12 处表述缺陷（前向引用、术语撞车、行话无解释、指代突兀等）；试读沉淀 13 条未答疑问回流 plan.md FAQ 候选。**顺带修复文档失真：`tools/recorder/` 已不存在但 AGENTS.md“录制器三处归属”与 tools/README.md 整节仍描述它 → 改为两处归属（lib 共享核心 + workspace/recorder 入口 `python -m workspace.recorder.tools.main`）**。（2026-09-16）

- **project-intro 第四批完成，全文交付（第 16-20 章 + 附录 A/B，定稿 1776 行）**：第 16 章配置与密钥纪律（三份文件分工/lib/secret 单一读取门/三种改配置方式/备份与真实误删找回）、第 17 章安全与隐私模型（威胁边界"家门内安保"/SECURITY-RISKS 四层地图读法/两条人审线（超时≠拒绝、授权归零是特性）/提交即执法/AI 预审对抗面诚实段/坏记忆修正入口）、第 18 章开发工作流与测试（两道机械防线含 workspace/tools 盲区、三种测试跑法与铁律、审计工具链）、第 19 章跑起来+上手路线、第 20 章常见误解 12 条 + 真实读者 FAQ 15 问（可持续追加）、附录 A 术语表 41 条、附录 B 关键文件速查两表。**最终零上下文验收：全新会话只读全文作答附录 D 全部 25 题，25/25 高置信、零"没讲清楚"、零自相矛盾**；README.md 开发指南段与 AGENTS.md 文档导航表挂载 project-intro 行。顺带修复：AGENTS.md 引用已删除的 `tools/migrate_secrets.py` → 据实改为指向 `tools/restore_secrets.py`；README.md 开发指南段去掉易漂移的"15 个文档"计数。（2026-09-16）

- **新增 x.com(Twitter) 视频下载工具 `tools/x_video_dl.py`（savetwitter.net 免梯子链路，`tools_manifest.json` 新增 `media_dl` 媒体下载分类）**：走 savetwitter.net 解析 API（`POST /api/ajaxSearch`）拿到 dl.snapcdn.app 签名直链（JWT 1h 有效）后流式下载，免梯子直连实测可用；顺序前台执行、逐条输出进度、条间 1.5s 限速（用户明确的执行风格偏好：不暴力批跑），自动选最高分辨率（`--max-res` 可设上限）并校验 MP4 文件头（ftyp），单条失败重试一次且不拖垮整批。CLI：`uv run python tools/x_video_dl.py <URL> [URL ...] [--out DIR] [--seq-start N] [--prefix TAG]`，输出默认 `~/<data_drive>:/Downloads/x_videos_<日期>/`。沉淀两个实测坑进脚本 docstring：① savetwitter 下载按钮 label 内嵌 `<i>` 图标标签，直链正则必须 `(.*?)</a>` 去标签（`[^<]*` 会全量匹配失败、症状为 status=ok 但"无直链"）；② 输出文件名含 `:` 会被 Windows 静默写进 NTFS ADS（主文件 0 字节、播放器打不开、数据在 `dir /r` 才可见的流里），脚本对文件名做白名单清洗，本次任务中 11 条"下载成功"视频即靠 ADS 读回修复、未重新下载。来源：2026-09-16 珥_eclipse 聊天 x 视频批量下载（14 条全成功）后从 temp/ 临时脚本转正。验证：显式 pyright 0 错误、小文件端到端冒烟通过、drift_detector 零新增发现（存量 HIGH 为 email_source/recorder 已知问题）。（2026-09-16）

- **AGENTS.md 新增「文档防过时规范」+ project_rules.md 新增「文档与注释变更」检查清单（双镜像同步）**（`AGENTS.md` 关键约束段新增一节；`.agents/rules/project_rules.md` + `.trae/rules/project_rules.md` 新增五条自查项）：把 2026-09-16 文档一致性整改的教训固化为流程规则——① 禁写易漂移统计数字（面板/工具/schema 版本/间隔天数等），改为指向代码常量、运行时清单或定性描述，事实性配置值实测后可写；② 行为论断锚定代码路径，引用文件前验证存在，移动/删除文件时全仓 grep 清理引用；③ 行为重构同一变更内 grep 旧行为关键词同步全部 docstring/docs/config 注释；④ 写"当前是 X"前先实测，两文档冲突以代码为准并双向修正；⑤ rules 双镜像改后 diff 确认一致。（2026-09-16）

### Fixed

- **屏幕授权 watchdog 升级请求不再被已有授权短路（`server/overlay_client.py`）**：原逻辑"已存在任务授权 → 任何 `request_control` 直接 skipped 放行"，导致 normal 模式下请求升级到 watchdog 时弹窗被短路，用户勾选确认的入口永远不出现。现改为仅模式一致或平级/降级请求短路放行，升级请求（normal→watchdog）照常弹窗让用户勾选确认。（2026-09-16）

- **文档与代码一致性集中整改（对照外部实测文档 project-intro.md 的"文档与代码不一致清单"逐条在当前仓库核实后修正）**（`AGENTS.md` / `README.md` / `README_public.md` / `docs/architecture.md` / `docs/deployment.md` / `docs/memory-system.md` / `docs/chat-engine.md` / `docs/mcp-reference.md` / `docs/api-reference.md` / `docs/llm-pool.md` / `docs/computer-use-reference.md` / `docs/ui/README.md` / `docs/ui/icons.md` / `.agents/rules/project_rules.md`（与 `.trae` 镜像双向同步）/ `server/route_tags.py` 仅 docstring）：① GUI 面板多处仍写"8 个面板"→ 改为 PanelRegistry 自动发现 + main/monitor/advanced 分组的定性描述（数量以代码为准）；② `architecture.md` 把 `x-agent-callable` 误写为"默认 fail-closed"→ 改为 fail-open + safety_map/approval_level 二道防线（对齐 ADR-0018 与 route_tags 实现）；③ `chat-engine.md` + `mcp-reference.md` 的 DoomLoop 命中后行为从旧版"retry 3 次"改为 B1 重构后的不重试（partial 落库 + system_warning + interrupted，对齐 runner.py 实际代码）；④ `memory-system.md` 删"当前已采用 enable_semantic=false"（实际 config 为 true）、"非定时器"（`[loops.memory]` 定时循环真实存在，惰性触发降级为兜底）；⑤ `llm-pool.md` 健康检查间隔改指 `HEALTH_CHECK_INTERVAL_DAYS`、vl_* default_tier (5,5)→(2,5)（实测 `USE_CASE_REGISTRY`）、keys.json schema 版本与迁移链改为指向 `SCHEMA_VERSION` 与 migrate 系列函数；⑥ `AGENTS.md`：triggered todo"推送"改为"拉取式注入"、`lib/uia.py` 定位纠正（UIA 语义层在 `server/screen/uia.py`）、EventStore schema 版本号改为指向 `_SCHEMA_VERSION`、`/output/{exec_id}` 标注仅覆盖 exec_cmd（exec_python 完整输出走 `/terminal/{tid}/output`）；⑦ 删除指向不存在文件的引用：`docs/walkthroughs.md` / `docs/code-knowledge-graph.md`（AGENTS.md 文档导航表 + architecture.md 进一步阅读）、README 项目结构里已不存在的 `static/`、`server/mcp_stats.py`（已归并 core/middleware.py）、`gui_process.py`、`loop_manager.py`/`loop_actions.py`（实际在 `server/activity_tracker/`），README server 结构块按包现状重写；⑧ `temp/sdd/chat-panel-v2/`、`temp/sdd/v6-lite-streaming-gui/` 等已清理的 SDD 目录引用标注"已随 temp 清理"并指向 `docs/chat-engine.md`；⑨ `.agents/rules/project_rules.md` 落后于 `.trae` 镜像（镜像侧已修 task_type 计数并多出"Guide 关键词维护"节）→ 用镜像覆盖回灌，另补 scope 枚举缺失的 daily；⑩ `route_tags.py` docstring 删除过时的"146 个 decorator"计数；⑪ computer_use skill 旧扁平路径引用（`.agents/skills/computer_use.md`）统一改为目录路径 `computer_use/SKILL.md`（AGENTS.md 会话检查清单 / computer-use-reference.md / api-reference.md）。整改原则：易漂移的统计性数字一律去掉、改为指向代码常量或运行时清单；事实性配置值（如 tier 区间）保留并按当前代码校准。验证：烟测 `test_smoke_endpoints.py` 全绿 + `uv run pyright` 0 错误（route_tags docstring 改动）。（2026-09-16）

### Removed

- **清理 zcode_plugins/watchdog ZCode 插件的全部文档引用（用户确认插件已废弃移除、完全不可用）**（`AGENTS.md` 删"ZCode 插件"段；`data/project_structure.json` 清 subdirs 中残留的空键 `zcode_plugins/`——`sync_baseline()` 只清 top_level 与子目录，清不了这种空父键）：注意区分三套同名概念，屏幕授权的 WATCHDOG 受控托管模式（`server/screen/session/`）与后端挂机条件关机（`server/auto_shutdown.py`，operations-manual"挂机监控+条件关机"章）均为**在用的独立机制，保留不动**；仅移除 ZCode CLI 插件本体引用。`memory_get("reference_zcode_plugin_dev")` 记忆已不存在，无需清理。（2026-09-16）

- **删除 4 个零引用 REST 端点（端点冗余清理 wip_1f161bdc 收尾决策）**（`server/agent_guide.py` 删 `GET /guide/usage`（`agent_guide_usage`，undertriggering 统计读取出口）；`server/core/llm_endpoints.py` 删 `GET /llm/pool/health-status`（`llm_pool_health_status`，健康状态只读——执行检查的 `/llm/pool/health-check` 保留）、`GET /llm/pool/session` + `DELETE /llm/pool/session/{session_id}`（v12 LKGP 粘性查看/清除端点，粘性机制本身不变）；`server/mcp_whitelist.py` GATEWAY_EXCLUDE 同步移除 `agent_guide_usage`/`llm_pool_health_status`；文档同步 api-reference/agent-guide/llm-pool/mcp-reference 四处）。背景：2026-07-12 端点冗余分析（162 端点五层分类）后用户决策"等 mcp_stats 数据稳定再删"；2026-09-15 重扫（`temp/endpoint_analysis2.py` 重写版，基线 115 个零调用工具已被 v0.46/v0.47 整改清完）确认这 4 个端点前端/MCP/脚本四维零引用，用户决策删除。`_record_usage` 统计机制保留不删（`data/agent_guide/usage.json` 全量聚合是 guide keywords 分析数据源，与 50 条滚动的 history.json 互补）。（2026-09-15）

