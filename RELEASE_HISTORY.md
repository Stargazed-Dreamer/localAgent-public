# 发布历史

本文件由发布流程自动生成,记录每个公开版本的演进脉络:
版本间包含的私有仓库 commit 数量、开发时间跨度与主要更改摘要
(摘自 CHANGELOG,仅取条目标题)。

<!-- 由 tools/release/engine/history.py 维护,请勿手工编辑 -->

## 0.49.0
- 自上次发布以来:私有仓库 **47 个 commit**(起点 2026-09-17)
- 主要更改:
  - HCI VM：收下真机投递包（144/144 逐字节复验）+ 定位并修复「无 GPU 机器 OCR 准入被永久卡死」（`config.toml` 两键 + `jsd_research/VM_BOOTSTRAP.md` 新增 §1.1/§1.2 + `docs/environment-constraints.md` 新增专节）
  - homework 引入两层 profile：新增本机覆盖层 `profile.local.toml` + 统一解析器 `tools/hw_profile.py`
  - SpaceSniffer 复刻：`.sns` 快照的 squarified treemap 浏览器（`tools/spacesniffer/`，PySide6 GUI）
  - 新增 `.agents/skills/browser_lessons/sites/wjx-cn.md`（问卷星站点经验档）
  - 新增 `workspace/ai_club_recruit/tools/export_png.py`：海报 HTML → 两档交付 PNG，交付图明确不入库
  - 新增 `workspace/ai_club_recruit/` 工作区：2026 秋季招新的架构口径（真源）+ 对外物料立册
  - `workspace/homework/tools/video_mcp/`：homework 自有 MCP 视频帧服务独立入库 + MCP 注册说明 + jsd 工具登记
  - 新增 `tools/network/wifi_adapter_repair.bat`（无线网卡掉线一键修复，桌面副本 `WiFi网卡修复.bat`）+ `bat_writing` skill 补 4 条实测坑
  - x_video_dl 下载账本去重（`tools/x_video_dl.py` + 输出目录下 `download_manifest.json`）
  - 新增 `doc.weflow.top`（《微信相关技术研究笔记》）全站归档能力：WARC + 离线镜像 + 单文件 + Markdown 四形态留档（一次性脚本 `extract_weflow_doc.py` + `dev/verify_weflow_doc_browser.py` 已在同一轮按用户决策回收，见 Removed；归档产物保留在 `workspace/web_archive/weflow/`，方法学沉淀在 `SKILL.md` 专节）
  - gh_mirror 初始化支持 clone 加速前缀 `GH_MIRROR_CLONE_PROXY`（`workspace/gh_mirror/gh_mirror_init.py` 新增 `resolve_clone_url()`）
  - 重型依赖 extras 化（D2 路径 B）：20 个直接依赖移入 `[project.optional-dependencies]` 5 个能力组，主依赖降至 28 项
  - public 仓部署说明改造：四阶段渐进式部署 + 三份文档口径统一 + 5 个发布 gate 缺陷
  - `chat_digest` 把"选档位"前置为开工第一问，并把三份提示词抄录入库（`workspace/chat_digest/SKILL.md` + `prompts/`）
  - `extract_feishu.py` 补 `quote_container`（引用容器）渲染分支 + 飞书游客态知识库接口定性（`workspace/web_archive/extract_feishu.py` + `SKILL.md` + `browser_lessons/sites/feishu.cn.md`）
  - 远程 VL 默认模型换成 `Qwen/Qwen3.5-35B-A3B`，并把"魔搭会下架模型 ID"固化进文档（`AGENTS.md` + `docs/environment-constraints.md` + `docs/llm-pool.md` + `docs/mcp-reference.md` + `docs/computer-use-reference.md` + `docs/deployment.md` + `.agents/skills/ocr.md` + `browser_lessons/sites/modelscope-cn.md`）
  - `web_archive` 工作区目录整改 + 立规矩（`workspace/web_archive/`，14 个文件位移 + `.gitignore`/`manifest.toml`/`SKILL.md` 同步）
  - weflow 归档迁入仓库内
  - `manifest.toml` 修两条真实漂移
  - 消除机器绝对路径
  - `SKILL.md` 新增「目录布局与维护规矩」章节
  - 验证：显式 `uv run pyright workspace/web_archive/*.py dev/*.py` 仅 3 条既有 `wechat_decod
  - `AGENTS.md`「磁盘清理规则」补回收站失败定位方法论
  - 依赖映射表/审计报告停止进发布包，生成落点改 `temp/release_deps/`（`tools/release/generate_dependency_map.py` + `tools/release/audit_dependencies.py` + `release/audience/friend.toml`/`public.toml`）
  - `friend-full` 发布线（打包 ZIP 分发给朋友）整体移除
  - 两份依赖生成物退出版本跟踪（`release/dependency_map.toml` 9,878 B + `release/dependency_audit.json` 72,088 B，`git rm --cached` + 工作树副本送回收站）
  - 回收 `web_archive` weflow 一次性归档脚本（`workspace/web_archive/extract_weflow_doc.py` 39,731 B + `workspace/web_archive/dev/verify_weflow_doc_browser.py` 11,225 B 送回收站）
  - `web_archive` 两份个人 URL 清单退出版本跟踪（`git rm --cached` + `.gitignore` 新增 `inputs/urls_wechat_source.json`）
  - 公开部署说明的轻量安装清单有误：把 `pyside6` 当可选项排除（`docs/deployment.md` + `README_public.md` + `tools/release/templates/DEPLOYMENT.md.j2`）
  - `browser_write_lesson` 同一站点被写成两份档案（`server/browser/site_lessons.py`）+ 清理既有重复 `sites/scnu-edu-cn.md`
  - browser_lessons 站点索引补同步 + 站点索引用简写路径易被误读（`.agents/skills/browser_lessons/references/site_index.md` + `AGENTS.md` + `docs/operations-manual.md`）
  - `chat_digest/digest_room.py`：`--room` 传完整 `@chatroom` ID 会"无匹配"，修掉群名互为子串时的选群歧义（`resolve_room()`）
  - `fix_bat_encoding.py` 的 `--enc` 值被当成输出路径，`--inplace --enc utf8` 会在项目根写出名为 `utf8` 的文件而源文件不动（`.agents/skills/bat_writing/scripts/fix_bat_encoding.py`）
  - gh_mirror 首次初始化必定失败：`work_root` 从未创建导致磁盘检查抛 WinError 3（`workspace/gh_mirror/gh_mirror_init.py`）
  - public-full path_mapping 补 clash_verge 安装路径映射（`release/profiles/public-full.toml`）
  - release 编排增量发布锚定死锁修复（`tools/release/cli.py` + `tools/release/engine/triage.py`）
