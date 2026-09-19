# Skill 索引

本文件是 Skill 详细参考索引。任务路由走 `agent_guide` 端点，本文件仅在需要查看 skill 完整信息时阅读。

> **agent 优先调 `agent_guide` 端点路由**：`agent_guide(task='用户任务描述')` 一次调用即可获得任务指导 + 候选清单，无需扫描本文件全量索引。本文件为详细参考，仅在需要查看 skill 完整信息时阅读。
## 组件化模块（自动生成，勿手工编辑）

### accounting (accounting) [recurring]

- **描述**：记账模块 - 账单识别+GUI审核+导出Obsidian，本地SQLite不走后端HTTP
- **task_type**：`recurring.accounting`
- **Skill 文件**：`workspace/accounting/SKILL.md`

### apikey_test (apikey_test) [adhoc]

- **描述**：API Key 测试组件 - 验证 LLM API Key 有效性+余额查询（server/apikey.py 路由保留原位）
- **task_type**：`adhoc.apikey_test`
- **Skill 文件**：`workspace/apikey_test/SKILL.md`

### arknights_gacha (arknights_gacha) [recurring]

- **描述**：明日方舟寻访官网/小黑盒数据采集
- **task_type**：`recurring.arknights_gacha`
- **Skill 文件**：`workspace/arknights_gacha/SKILL.md`

### bilibili_gacha (bilibili_gacha) [recurring]

- **描述**：<data_drive>:\<bilibili_videos>互动抽奖动态扫描+开奖检查+取关
- **task_type**：`recurring.bilibili_gacha`
- **Skill 文件**：`workspace/bilibili_gacha/SKILL.md`

### community_review (community_review) [recurring]

- **描述**：小鹅通社群帖子提取+总结
- **task_type**：`recurring.community_review`
- **Skill 文件**：`workspace/community_review/SKILL.md`

### disk_manager (disk_manager) [adhoc]

- **描述**：磁盘空间分析与清理建议、系统环境全面备份（浏览器/游戏存档/桌面/系统设置）
- **task_type**：`adhoc.disk_cleanup`
- **Skill 文件**：`workspace/disk_manager/SKILL.md`

### email_source (email_source) [adhoc]

- **描述**：邮件信源 - IMAP 轮询新邮件推 inbox（触发器）+ smtplib 发信工具，imbox 读取
- **task_type**：`adhoc.email_source`
- **Skill 文件**：`workspace/email_source/SKILL.md`

### endfield_gacha (endfield_gacha) [recurring]

- **描述**：终末地寻访游戏日志/API采集
- **task_type**：`recurring.endfield_gacha`
- **Skill 文件**：`workspace/endfield_gacha/SKILL.md`

### exam_prep (exam_prep) [adhoc]

- **描述**：期末复习资料精确汇总与整理工作流 - 文件梳理+LLM提纲+试卷分析
- **task_type**：`adhoc.exam_prep`
- **Skill 文件**：`workspace/exam_prep/SKILL.md`

<!-- private component removed from public package -->

### mindforge (mindforge) [adhoc]

- **描述**：MindForge 集成组件 - 文档处理+知识库搜索（server/mindforge.py 路由+tools/mindforge/daemon 保留原位）
- **task_type**：`adhoc.mindforge`
- **Skill 文件**：`workspace/mindforge/SKILL.md`

### modelscope_model_update (modelscope_model_update) [adhoc]

- **描述**：ModelScope 模型库更新 - 扫描前沿模型+榜单评估+写入 keys.json（覆盖 LLM/VL/aigc_image/aigc_video+TTS/ASR 仅记录）
- **task_type**：`adhoc.modelscope_model_update`
- **Skill 文件**：`workspace/modelscope_model_update/SKILL.md`

### niuke_review (niuke_review) [recurring]

- **描述**：牛客面经搜索+6维度评分
- **task_type**：`recurring.niuke_review`
- **Skill 文件**：`workspace/niuke_review/SKILL.md`

### official_gacha (official_gacha) [recurring]

- **描述**：扫描官方动态+参与抽奖
- **task_type**：`recurring.official_gacha`
- **Skill 文件**：`workspace/official_gacha/SKILL.md`

### recorder (recorder) [recording]

- **描述**：操作录制与 agent 辅助消费系统 - L0-L4 五层架构，L4 消费层 + L3 编辑器 GUI 在本组件，L0-L3 核心库在 lib/recorder/
- **task_type**：`recording.consume`
- **Skill 文件**：`workspace/recorder/SKILL.md`

### stock_advisor (stock_advisor) [recurring]

- **描述**：炒股指导模块 - 三时段自动分析 + 盘中午询 + wisdom 引用，轻量监控+建议不做交易
- **task_type**：`recurring.stock_advisor`
- **Skill 文件**：`workspace/stock_advisor/SKILL.md`

### web_archive (web_archive) [adhoc]

- **描述**：小黑盒/微信/飞书云文档/腾讯文档批量存档为MD（含评论+图片）
- **task_type**：`adhoc.web_archive`
- **Skill 文件**：`workspace/web_archive/SKILL.md`

### wuwa_gacha (wuwa_gacha) [recurring]

- **描述**：鸣潮抽卡记录日志解密/内存扫描+API采集
- **task_type**：`recurring.wuwa_gacha`
- **Skill 文件**：`workspace/wuwa_gacha/SKILL.md`

### yihuan_gacha (yihuan_gacha) [recurring]

- **描述**：异环抽卡记录截图OCR采集+清洗归档
- **task_type**：`recurring.yihuan_gacha`
- **Skill 文件**：`workspace/yihuan_gacha/SKILL.md`

### yihuan_simulator (yihuan_simulator) [adhoc]

- **描述**：异环卡池棋盘可视化查看器/编辑器
- **task_type**：`adhoc.yihuan_simulator`
- **Skill 文件**：`workspace/yihuan_simulator/SKILL.md`

<!-- END component section -->


## 阅读指南（新 agent 必读）

- **本目录是项目唯一的 Skill 定义目录**，历史 Skill 使用扁平 `.md`，新 Skill 使用 `<name>/SKILL.md`
- **每个 Skill 定义顶部有 YAML frontmatter**（`---` 包裹），`name` 是标识符，`description` 包含触发词和功能描述——agent 靠 description 匹配用户意图
- **`_index.md`（本文件）是索引入口**，列出所有 Skill 的触发词、文件路径、依赖、输出
- **`skill-creator.md`** 包含完整的 Skill 编写规范、模板和反模式——创建或修改 Skill 时必读
- **`neat-freak.md`** 包含文档审查与同步规范——会话结束或用户说"整理一下"时触发
- 新 Skill 目录命名：英文小写+连字符，如 `internal-workflow/SKILL.md`；历史扁平文件按原名维护
- **提交前机械防线**：pre-commit hook 会跑 `tools/check_hard_rules.py`（BOM/硬编码密钥/文档路径越界/import 边界），违反即拦截 commit；GUI 面板结构参考 `data/feature_map.json`，guide 路由改动需回测 `tests/guide_eval/`（`run_eval.py`，基线 88.9%）

---

## 核心 Skill

### 1. OCR 识别 (ocr)

| 属性 | 值 |
|------|------|
| **触发词** | 识别图片、OCR、读取图片 |
| **Skill 文件** | `.agents/skills/ocr.md` |
| **输入** | `temp/img_test/` 或任意图片路径 |
| **输出** | 通过 API 返回 |
| **依赖** | FastAPI 后端 `/ocr/*` 接口 |

**模型**：经典 PaddleOCR（本地文字识别 + 1-3px bbox，Computer Use 定位主路径）+ 远程 VL（文档解析/图像描述）

**接口选择**：
- Qwen VL：`/ocr/vl/file`、`/ocr/vl/base64`、`/ocr/vl/path`
- 经典模型：`/ocr/file`、`/ocr/base64`、`/ocr/path`
- 屏幕文字定位：`/screen/ocr`（窗口模式 bbox 加窗口 left/top 后得到屏幕坐标）

**关键基线**：截图 OCR 必须关闭 `use_doc_unwarping`，`[ocr]` 默认恒等；升级 PaddleOCR/PaddleX 后先运行 `tools/debug/verify_bbox_affine.py`，禁止直接恢复历史仿射参数。

**模型管理**：`/ocr/models/keep`（常驻内存）、`/ocr/models/unload`（卸载）、`/ocr/models/preload`（预加载）

---

### 2. Guarded Computer Use / 受控电脑操作 (computer_use)

| 属性 | 值 |
|------|------|
| **触发词** | 操作电脑、屏幕操作、点击、截图、帮我操作、翻页、自动操作 |
| **Skill 文件** | `.agents/skills/computer_use/SKILL.md` |
| **工具脚本** | `server/screen/`、`server/vl/vision.py`、`server/gui_process.py` |
| **依赖** | `/screen/*`、`/vision/*` |
| **MCP** | `http://127.0.0.1:8766/mcp` |

**安全铁律**：
1. 任何步骤都必须截图思考一遍再点击，不能用脚本直接点
2. 如果完全不能执行就放弃，不要硬做/瞎点；用户 AFK 时尤其必须 fail-closed，不得绕过安全策略
3. 涉及删除/支付/关机等危险操作必须用户确认
4. 优先使用指定窗口模式，避免误操作其他程序
5. 每次操作后必须截图验证结果

**紧急停止**：`Ctrl + `` 反引号键，触发后10秒内禁止Agent操作

**工作流**：
1. 第一次副作用操作前调 `screen_request_control`，由用户选择本次接管或当前任务授权
2. 获取窗口列表 → 截取目标窗口 → 原生控件优先 UIA，浏览器优先 DOM，文字 fallback 用 OCR bbox
3. 纯图标/无文字元素用 `understand_image` 描述，仍无法定位时才调用 `vision_locate`
4. 思考决策 → 执行普通操作（任务授权内免重复确认；危险操作仍确认）→ OCR/DOM 验证
5. 任务结束调 `screen_release_control`；遗漏时空闲 300 秒自动撤销

---

### 3. Skill 创建器 (skill-creator)

| 属性 | 值 |
|------|------|
| **触发词** | 创建skill、新建skill、写个skill、优化skill、skill-creator、创建技能 |
| **Skill 文件** | `.agents/skills/skill-creator.md` |
| **用途** | 创建新 Skill、修改和优化已有 Skill |

**功能**：引导用户从意图捕获到 Skill 文件编写、测试验证、迭代优化的完整流程。包含标准模板和4种常见Skill类型模板。

---

### 4. 文档洁癖 (neat-freak)

| 属性 | 值 |
|------|------|
| **触发词** | 同步一下、整理文档、整理一下、更新记忆、梳理一下、收尾、/sync、/neat |
| **Skill 文件** | `.agents/skills/neat-freak.md` |
| **用途** | 会话结束后对项目文档进行洁癖级审查与同步 |

**功能**：审查 AGENTS.md、Skill 文件、_index.md、测试、配置、监控面板的一致性，确保文档跟得上代码变化。

### 5. 未完成任务追踪 (wip_tracker)

| 属性 | 值 |
|------|------|
| **触发词** | 留档、存档、标记未完成、WIP、断头工作、继续之前的工作、有哪些未完成、工作进度 |
| **Skill 文件** | `.agents/skills/wip_tracker.md` |
| **数据存储** | `wip_tasks` 表（SQLite，共享 memory.db） |
| **MCP 工具** | `wip_list` / `wip_create` / `wip_get` / `wip_update` / `wip_delete` |
| **用途** | 中断任务留档、查询未完成任务、恢复断头工作、去重识别 |

**核心功能**：
- **留档**：任务中断时 `wip_create` 创建一条记录（含 title/goal/progress/next_steps/current_state/related_files/extra_data）
- **查询**：`wip_list` 汇总所有未完成 WIP（可按 status 过滤）
- **恢复**：`wip_get` 读详情，从 next_steps 继续执行
- **更新**：`wip_update` 推进进度/改状态（active/blocked/paused/completed）
- **去重**：用户重复提需求时，先 `wip_list` 搜索识别已有探索
- **富字段**：`extra_data` 字段存储非标准字段（JSON 对象），保留迁移前的完整上下文

> 旧机制（`.agents/wip/*.json` + `wip_index` 记忆 key）已迁移到 `wip_tasks` 表（启动时幂等迁移，见 `server/todos/migration.py`），迁移后 .json 文件移至 `temp/wip_migrated_backup/`。DB 是程序唯一真源，所有读写通过 MCP 工具操作。
>
> `.agents/wip/*.md` 是迁移前与 .json 双写的人类可读副本，仅作阅读参考，不再作为数据源使用。新增留档一律用 `wip_create`。

---

### 6. HTML 工具开发与调试 (html-dev-debug)

| 属性 | 值 |
|------|------|
| **触发词** | 写HTML页面、创建工具页面、HTML调试、可视化页面、Dashboard、前端页面 |
| **Skill 文件** | `.agents/skills/html-dev-debug.md` |
| **输出位置** | `workspace/_shared/` 或组件目录（`server/static/` 已移除） |
| **依赖** | 浏览器控制（CDP 9222）、截图（capture_screen）、OCR（ocr_file）、Playwright |

**核心方法**：浏览器控制 + 截图 + OCR 闭环验证

**工作流**：
1. 编写/修改 HTML 代码（Write/Edit 工具）
2. 浏览器打开页面（browser_open 或 Playwright）
3. 等待渲染完成（networkidle + sleep）
4. 截图保存（Playwright screenshot，full_page=True）
5. OCR 识别截图内容（ocr_file）
6. 分析问题（JS 错误捕获 + DOM 检查 + OCR 结果比对）
7. 修改代码 → 重新验证，每轮只修 1-2 个问题

**常见陷阱**：模板字面量引号冲突、嵌套反引号、静态文件路径 404、Canvas 参数为负、浏览器缓存

---

### 7. 任务提醒 (task_reminder)

| 属性 | 值 |
|------|------|
| **触发词** | 有什么任务、该做什么、提醒我、任务清单、待办清单、今天做啥、这周做啥、有什么没做、任务提醒；空闲额度类：额度很多、额度有余、额度多余、额外额度、空闲额度、长期自动化、顺便跑、能跑什么 |
| **Skill 文件** | `.agents/skills/task_reminder.md` |
| **数据存储** | `todos` 表（SQLite，共享 memory.db） |
| **MCP 工具** | `todos_due` / `todos_list` / `todos_create` / `todos_update` / `todos_mark_done` / `todos_delete` / `todos_check_trigger` |
| **联动** | `wip_tracker`（一次性待办）、各周期性 skill |
| **用途** | 检查周期任务是否到期 + 汇总 WIP 待办，给出"当前该做的事"综合报告 |

**核心功能**：
- **查询到期**：`todos_due` 返回所有到期周期任务（含从未完成和已过 next_due_at 的）
- **查询 WIP**：`wip_list` 列出未完成的一次性待办
- **标记完成**：周期任务完成后 `todos_mark_done` 更新 `last_done_at` + `next_due_at`，避免重复提醒
- **增减任务**：`todos_create` / `todos_delete` 随时增删周期任务
- **条件任务**：带 `condition` 的任务可通过 `todos_update` 设置 `condition_status=inactive` 跳过到期检查

**todo 类型**：
- `recurring`：周期任务（daily/weekly/monthly），按 frequency 到期
- `phased_recurring`：阶段性周期任务，在 `[start_date, end_date]` 区间内按 frequency 递推；超出 `end_date` 自动置 `archived`（不再到期）
- `triggered`：触发式任务，不按周期到期；由 loop 轮询 `trigger_condition`（首版支持 `file_arrived` 事件）自动触发，触发后推送 `/user/message` 提醒；不进 `todos_due` 列表，处理后 `todos_mark_done` 重置

**当前周期任务**：**不在本文件维护快照**（此处曾驻留一份手工抄录的表，已出现"条目早不存在/新条目没同步"的漂移）。真源两处：
- 运行时清单：`todos_list` / `GET /todos`（含频率、`last_done_at`、`condition`）
- 语义与台账：`docs/periodic-task-inventory.md`（三类触发机制、已注册条目语义、机会型跑批清单、有意不周期化清单）

**到期判断**：`server/todos/store.py` 的 `get_due_todos()`——`last_done_at` 为 null 视为到期；否则按 `next_due_at <= today`（`next_due_at` 仅由 `todos_mark_done` 写入）。条件任务（如"找工作期间"）不确定时询问用户。

> 旧机制（`task_reminders` 记忆 key）已迁移到 `todos` 表（启动时幂等迁移，见 `server/todos/migration.py`）。

---

### 8. 记忆生成 (memory_generation)

| 属性 | 值 |
|------|------|
| **触发词** | 生成记忆、记忆生成、记忆检查、提取记忆、更新记忆、记忆维护、保存经验、记忆总结 |
| **Skill 文件** | `.agents/skills/memory_generation.md` |
| **触发方式** | 用户说"生成记忆" → agent 调 `agent_guide(task_type='recurring.memory_generation')` → 返回 5 步工作流 → 执行 |
| **输出** | 结构化记忆写入 facts 表（含 type/name/description/content/why/how_to_apply） |
| **依赖** | `memory_list` / `memory_get` / `memory_set` / `localagent_advanced_tool(memory_maintain)` |

**记忆类型**：preference（用户偏好）/ project（项目状态）/ reference（参考资料）

**不存储**：代码结构（可 grep）、git 历史、已修复 bug、临时调试信息、AGENTS.md/_index.md 已记录内容、config.toml 已配置值

**与维护器的关系**：memory_generation 是用户触发、agent 执行（提取+写入新记忆）；maintainer 是后端自动（每6h 老化+验证已有记忆）。

---

### 9. 今日工作总结 (daily_summary)

| 属性 | 值 |
|------|------|
| **触发词** | 今日总结、工作总结、今天干了什么、给我今日工作总结、日报、今日工作总结 |
| **Skill 文件** | `.agents/skills/daily_summary.md` |
| **依赖** | Loop 活动追踪系统（`[loops.activity_tracker]` 必须启用） |
| **输入** | `data/activity/hourly/YYYYMMDD_HH.md`（每小时总结，Loop 自动生成） |
| **输出** | `data/activity/daily/YYYYMMDD.md`（日总结，用户可编辑） |

**工作流**：
1. **确定"今天"日期**：当前时间 ≥ 05:00 用当天日期，< 05:00 用前一天日期（按 05:00 分界规则，详见 `.agents/skills/daily_summary.md`）。立即生成，不等"今天结束"——用户要日报时不会期待更多记录
2. 读取 `data/activity/hourly/` 下该日期所有 `.md` 文件（数据源优先级：hourly md 本源 → git log 补缺失 → memory 仅参考）
3. 若无数据，提示用户后端 Loop 未运行（检查 `[loops.activity_tracker]` 配置）
4. 汇总为日总结（LLM powerful 层 GLM-5.2）
5. 写入 `data/activity/daily/YYYYMMDD.md`（已存在则追加 `_v2`）
6. 提示用户审阅（不要在日报中加入 agent 的 task_closure 工作报告）

**数据来源**：Loop activity_tracker 每分钟采集窗口列表、每5分钟截图+VL描述、每小时整点 LLM 总结。若后端未全程运行，hourly 文件可能不全，需在日总结中标注缺失时段。

---

### 10. 任务收尾 (task_closure)

| 属性 | 值 |
|------|------|
| **触发词** | 收尾、任务结束、做完了、总结一下、这个任务完成了、收尾guide、task closure、结束了 |
| **Skill 文件** | `.agents/skills/task_closure.md` |
| **触发方式** | agent 自主触发（任务完成/中断/会话结束前），或用户说"收尾" |
| **输出** | WIP 更新 + 结构化记忆（含 consumption_contexts）+ 文档自查报告 + 收尾报告 |
| **依赖** | `wip_list`/`wip_update`/`wip_create` / `memory_list`/`memory_get`/`memory_set` / `.agents/rules/project_rules.md` |

**6 步工作流**：1.评估任务状态 → 2.WIP处理 → 3.经验提炼+可消费性自检 → 4.查重写入 → 5.文档自查 → 6.收尾报告

**与现有 skill 关系**：并存+编排。task_closure 是入口，内部复用 memory_generation（step_3-4）+ wip_tracker（step_2）+ neat-freak（step_5 只做快速自查，完整审查由用户触发）。

**可消费性自检**（核心约束）：写入记忆前必须填 `consumption_contexts`（哪些 task_type 应读取）+ `trigger_keywords`（任务描述出现什么词时读取），否则跳过——避免"存了没人用"。

---

### 11. temp 文件夹清理 (temp_cleanup)

| 属性 | 值 |
|------|------|
| **触发词** | 整理temp、清理temp、temp清理、临时文件整理、temp太乱、清理临时文件、temp cleanup |
| **Skill 文件** | `.agents/skills/temp_cleanup.md` |
| **触发方式** | 用户主动触发（"整理 temp"），或 temp 目录过大时建议触发 |
| **输出** | 清理后的 `temp/` + 可选 wip 待办 + 清理报告 |
| **依赖** | `tools/disk/recycle.py`（回收站唯一入口，纯 ctypes；⚠️ PowerShell `Add-Type` 本机被安全策略拦截）/ `wip_list`（open 任务检查，从 DB 查询）+ `.agents/wip/*.md`（人类可读留档）/ `git`（只读校验 worktree 注册状态） |

**7 步工作流**：1.扫描 + 三问判定 → 2.生成**静态路径快照**清单询问用户 → 3.执行前二次 diff → 4.回收站删除 → 5.执行移动 → 6.wip 待办处理 → 7.收尾报告

**核心规则（硬约束）**：
- **删除必走回收站且只用 `tools/disk/recycle.py`**：`uv run python tools/disk/recycle.py --list <快照文件> --on-error continue`，不得用 `Remove-Item`/`rm`/`shutil.rmtree`
- **清单是静态路径快照**：禁止"根目录所有 `*.py`"这类执行时求值的活规则（实测清理进行中 temp 被并发会话写入，根目录 `.py` 94 → 95）
- **清单先行且不覆盖保护类**：用户说"全部删"也不能删保护清单里的东西，冲突必须明确指出
- **业务数据不删**：个人创作/笔记/财务等唯一副本只能移动到 workspace
- **备份分级**：回收站本身即备份，已确认入站时不再叠一层；仅逼近容量/非 NTFS/可能覆盖同名的场景强制先备份

**三问决策树**（按性质，不按名字）：Q1 可重建 → 回收站 / Q2 唯一副本 → ★保护 / Q3 有归属 → 移回归属地，无归属标"待定"交用户

**四条交叉校验**（判定后再过一遍）：活跃性（近 2 小时 mtime / wip 关联 / 运行中终端日志）· 引用性（被 AGENTS.md·docs/·tests/·client/·server/ grep 命中）· 仓库注册对象（git worktree → 回收站删目录后 `git worktree prune`）· 跨项目性

**绝对保护**：`temp/sdd/<slug>/`（上下文压缩后的恢复锚点）+ `temp/planning_archive/`（只读历史归档），归档由用户决策

**temp 无固定结构，不要强加**——清理目标是减少体积和风险，不是整形

---

### 12. PySide6 客户端开发 (client_dev)

| 属性 | 值 |
|------|------|
| **触发词** | 开发客户端、GUI开发、新增面板、面板卡顿、PySide6、客户端性能、面板开发、widget优化 |
| **Skill 文件** | `.agents/skills/client_dev.md` |
| **task_type** | `dev.client_dev` |
| **依赖** | 无后端依赖（纯 GUI 开发） |

**面板架构**：`PanelBase` 抽象基类 + `PanelRegistry` 自动发现 + `QStackedWidget` 切换。`__init__` 启动时一次性实例化所有面板，`on_show()` 每次切换触发。

**性能规范（强制）**：
- `on_show()` 不能做重活：同步 DB 查询 / 大量 widget 重建会导致面板切换卡顿
- 异步加载模式：DB 查询放 `QThread`，通过 `Signal` 回主线程填充 widget
- 避免 N+1 查询：循环填充表格时一次性预取数据
- 重控件懒加载：`QCalendarWidget` 延迟到 tab 首次切换时创建

**参考实现**：`accounting.py` 的 `_ReviewDataLoader`（异步加载+N+1修复）、`tools.py` 的 `_read_manifest_mtime`（mtime检测）

---

### 13. WIP 归档 (wip_archive)

| 属性 | 值 |
|------|------|
| **触发词** | 归档 WIP、清理已完成 WIP、把完成的 WIP 转记忆、WIP 太多、WIP 膨胀 |
| **Skill 文件** | `.agents/skills/wip_archive.md` |
| **task_type** | `system.wip_archive` |
| **依赖** | `wip_list`（直连）/ `localagent_advanced_tool(wip_get/wip_delete)` / `memory_set` |

**用途**：批量筛选 `completed` WIP → 提炼经验 → 写入结构化记忆 → 物理删除 WIP 记录。解决 WIP 表膨胀阻碍网页审查面板渲染和 `wip_list` 查询可读性。

**与 wip_tracker 的关系**：
- `wip_tracker` 负责日常 WIP CRUD（留档/查询/恢复/更新）
- `wip_archive` 负责周期性批量归档（completed WIP → 记忆 → 删除）
- task_closure 的 step_2 是单条关闭（`wip_update(status=completed)`），wip_archive 是批量归档（`wip_delete`）

---

### 14. 代码审查 (code_review)

| 属性 | 值 |
|------|------|
| **触发词** | code review、代码审查、review 一下、审查代码、全量 review |
| **Skill 文件** | `.agents/skills/code_review.md` |
| **用途** | 对本项目进行全量或定向代码审查，按 7 类检查清单（正确性/安全性/可维护性/性能/类型契约/项目约定/依赖架构）扫描问题 |

**适用范围**：
- **全量 review**：按"分层 + 风险优先"分 8 批审查（安全层 → 入口生命周期 → 能力端点 → 执行端点 → 数据状态 → Agent 编排 → 辅助模块 → 客户端）
- **定向 review**：单文件/单模块/单 PR

**工作流**（6 步）：1.确定范围与批次 → 2.持久化准备（长任务写 plan/findings 到 temp/）→ 3.上下文收集 → 4.推断作者意图 → 5.按 7 类清单扫描 → 6.输出问题表格 + mermaid 架构图

**禁止报告**：纯描述性评论、赞美性评论、无证据猜测、UI 样式数值、代码风格

**长任务恢复**：跨会话中断时读 `temp/code_review_plan.md` + `temp/code_review_findings.md` 恢复进度

**核心约束**：
- 用户审核是硬约束：生成清单后必须等待用户明确批准，不擅自写入或删除
- 记忆消费闭环：写入记忆必须填 `consumption_contexts` + `trigger_keywords`
- 物理删除不可恢复：归档前确认记忆已捕获关键信息

---

### 15. 项目全面复审 (project_audit)

| 属性 | 值 |
|------|------|
| **触发词** | 项目复审、全面复审、项目审查、项目体检、project audit、全面审查一下、项目健康度、复审一下项目、项目巡检 |
| **Skill 文件** | `.agents/skills/project_audit.md` |
| **task_type** | `system.project_audit` |
| **用途** | 对项目进行多维度健康审计，让用户从 12 大类（架构/测试/文档/API/配置/可用性/安全/数据/性能/Git/环境/UX）中选择审查方向，按检查清单扫描问题并输出结构化报告 |

**适用范围**：
- **全量复审**：12 大类全扫（适合周期性项目体检，每月/每季度一次）
- **定向复审**：用户选 1-N 个大类（适合针对性排查）
- **不适用**：单文件代码审查（用 `code_review`）、文档同步（用 `neat-freak`）、临时 bug 排查

**12 大类清单**（每类有 4-7 个子项，详见 skill 文件）：

| 编号 | 大类 | 编号 | 大类 |
|------|------|------|------|
| A | 架构与代码质量（7 子项） | G | 安全与权限（6 子项） |
| B | 测试覆盖（5 子项） | H | 数据与存储（4 子项） |
| C | 文档体系（5 子项） | I | 性能与执行（4 子项） |
| D | API 与工具膨胀（4 子项） | J | Git 与变更管理（4 子项） |
| E | 配置与依赖（4 子项） | K | 环境与兼容性（4 子项） |
| F | 可用性与监控（5 子项） | L | 客户端与 UX（4 子项） |

**工作流**（5 步）：1.展示方向清单+让用户选择（multiSelect） → 2.持久化准备（≥3 类时写 temp/plan+findings） → 3.上下文收集（并行 Read+MCP） → 4.按检查清单扫描 → 5.输出报告+收尾（含健康度评分 🔴🟡🟢⚪）

**与 code_review/neat-freak 的区别**：
- `code_review`：单文件/单模块代码审查（A 类的子集，深入代码细节）
- `neat-freak`：会话后文档同步（C 类的子集，聚焦文档同步）
- `project_audit`：项目级多维度健康审计（12 大类全维度，周期性体检）

**核心特征**：先让用户选择审查方向（`AskUserQuestion` multiSelect），再按检查清单扫描。禁止未征询用户就自行全扫。

**恢复指引**：跨会话中断时读 `temp/project_audit_plan.md` + `temp/project_audit_findings.md` 恢复进度。

---

### 16. 反幻觉工作流 (anti_hallucination)

| 属性 | 值 |
|------|------|
| **触发词** | 改代码、修 bug、报错、不显示、不对、排查、定位、加功能、重构、重命名、这段代码、怎么工作、帮我看看 |
| **Skill 文件** | `.agents/skills/anti_hallucination/SKILL.md`（+ `references/diagnosing-bugs.md`：棘手 bug 6 阶段诊断循环） |
| **task_type** | `adhoc.anti_hallucination` |
| **依赖** | Read/Grep/MCP 工具（建立上下文用） |
| **用途** | 日常小修改的防幻觉安全网：6 步流程确保先读懂再动手 |

**核心原则**：索引先于动手，读到再用，禁止编造，方案先确认。

**4 种模式**：Bug / 需求 / 重构 / 理解（Step 4 按模式切换对齐模板）

**6 步流程**：定位起点 → 建立上下文 → 压缩 → 对齐确认（等用户确认方向）→ 可选 log 调试 → 提方案（等确认后执行）

**与现有 Skill 的关系**：
- 大任务走 task_closure SDD 链，小任务走本 skill
- Step 5 轻量 log 调试；复杂 bug（并发/非确定性/性能回退/3 轮未定位）转 `references/diagnosing-bugs.md` 6 阶段
- 本 skill 是 AGENTS.md "Think Before Coding" 和 "MCP 工具优先原则" 的具体执行流程

---

### 17. (内部维护入口，未包含在本包中)

本节描述的源码分发工作流属于项目原作者的内部维护流程；相关 skill、策略文件与发布引擎均不在本包内。

---

### 18. 分网站浏览器操作经验 (browser_lessons)

| 属性 | 值 |
|------|------|
| **触发词** | 浏览器踩坑、网站操作经验、分网站记录、browser lessons、网站经验、浏览器经验、操作经验、网站坑、DOM 坑 |
| **Skill 文件** | `.agents/skills/browser_lessons/SKILL.md` |
| **task_type** | `adhoc.browser_automation`（亦作元 skill 与所有浏览器相关 task_type 联动） |
| **用途** | 分网站维度记录浏览器操作经验。任务开始前按主域查 `sites/<domain>.md` 避免重复踩坑；任务收尾时强制把非显然行为写入对应网站文件 |

**适用范围**：任何调用 `browser_*` MCP 工具、Playwright、`connect_over_cdp`、`page.goto` 的任务；含 `web_archive`/`community_review`/`bilibili_gacha`/`arknights_gacha`/`endfield_gacha`/`official_gacha`/`wuwa_gacha`/`html-dev-debug` 等浏览器相关 skill。

**与 key_pitfalls 的分工**：网站通用踩坑（DOM 结构、反爬、URL 规则、登录态、防盗链）→ `sites/<domain>.md`；skill 特定踩坑（换网站就不适用）→ `agent_guide.py` 的 `key_pitfalls`；跨网站通用浏览器踩坑 → AGENTS.md "API 常见陷阱" + SKILL.md 末尾。

**目录结构**：
```
.agents/skills/browser_lessons/
├── SKILL.md                       # 元 skill：工作流 + 强制规则 + 跨网站通用踩坑
├── sites/                         # 分网站经验文件（按主域动态增长，不在此逐文件列举）
│   └── _template.md               # 新网站模板
└── references/
    └── site_index.md              # 已记录网站索引（主域 → 文件映射）
```

**已记录网站**：以 `sites/` 目录为准（`browser_match_site(domain=...)` 按域名/别名实时查询，本文件不维护静态清单）。

**强制规则**（已并入 AGENTS.md "浏览器操作经验记录" 章节和 task_closure step 5）：浏览器操作任务收尾时必须检查本次是否遇到非显然行为，若是则写入 `sites/<domain>.md`，无对应文件时按 `_template.md` 创建并同步 `site_index.md`。

**站点经验查询工具**：`browser_match_site(domain=...)` MCP 工具（或 `tools/browser/match_site.py`）一键按域名查站点经验，支持 frontmatter `aliases` 别名匹配，替代手动读 site_index + sites 两步。

---

### 19. 联网工具选择决策表 (web_access)

| 属性 | 值 |
|------|------|
| **触发词** | 联网、上网查、搜一下、读网页、读链接、抓网页、web访问、联网工具、web_access、搜索、search、fetch、爬取、抓取页面 |
| **Skill 文件** | `.agents/skills/web_access/SKILL.md` |
| **task_type** | （元 skill，不对应单独 task_type；任何联网任务的入口决策层） |
| **用途** | 联网任务前先按决策表选工具（WebSearch/WebFetch/CDP/curl/Jina/本地书签检索），避免选错工具浪费时间或拿到空内容 |

**6 条决策路径**：
1. 只需摘要/概念 → WebSearch
2. URL 明确 + 公开静态页 → WebFetch
3. URL 明确 + 需登录态/JS渲染/反爬 → CDP（`browser_open` + Playwright）
4. 找用户访问过但没收藏的页面 → 本地书签/历史检索（`browser_find_url`）
5. 需要搜索结果页原始 HTML → curl 抓 SERP
6. 被反爬/需 token/要 Markdown → Jina Reader

**与现有 skill 的关系**：本 skill 是联网任务的**入口决策层**，不替代具体 skill 的工作流。`niuke_review` 用 WebSearch+WebFetch、`web_archive`/`community_review` 用 CDP——本 skill 解释为什么这么选。选了路径 3（CDP）后必须先查 `browser_lessons` 站点经验。

---

### 20. 深度研究 (deep_research)

| 属性 | 值 |
|------|------|
| **触发词** | 深度研究、研究一下、帮我分析、做个研究、调研一下、竞品分析、行业研究、从零研究、横纵分析、商业模式拆解 |
| **Skill 文件** | `.agents/skills/deep_research/SKILL.md` |
| **task_type** | `adhoc.deep_research` |
| **用途** | 产品/公司/行业/概念/技术/人物深度研究报告（Markdown+PDF），横纵分析法+行业研究实战范式 |

对任意研究对象进行系统性深度研究，产出结构化研究报告。融合横纵分析法（时间深度+同期广度+交汇洞察）与行业研究实战范式（商业模式拆解+单位毛利归因）。必须联网搜索，商业对象必做商业模式拆解。

---

### 21. Word 文档生成 (office_docx)

| 属性 | 值 |
|------|------|
| **触发词** | 生成 Word、生成 docx、md 转 docx、生成复习提纲、生成报告、学术论文、word form、可填表单、SDT |
| **Skill 文件** | `.agents/skills/office_docs/SKILL_docx.md` |
| **task_type** | `adhoc.office_docx` |
| **用途** | Markdown/JSON/文本→docx（纯Python，python-docx+oxml；含学术论文/Word 表单/Report 三场景） |

---

### 22. Excel 表格生成 (office_xlsx)

| 属性 | 值 |
|------|------|
| **触发词** | 生成 Excel、生成 xlsx、md 转 xlsx、JSON 转 xlsx、数据报表、财务模型、DCF、KPI dashboard |
| **Skill 文件** | `.agents/skills/office_docs/SKILL_xlsx.md` |
| **task_type** | `adhoc.office_xlsx` |
| **用途** | JSON/CSV/Markdown→xlsx（纯Python，openpyxl；含财务模型/数据仪表盘两场景） |

---

### 23. PowerPoint 演示文稿生成 (office_pptx)

| 属性 | 值 |
|------|------|
| **触发词** | 生成 PPT、生成 pptx、md 转 pptx、汇报演示、融资路演、pitch deck |
| **Skill 文件** | `.agents/skills/office_docs/SKILL_pptx.md` |
| **task_type** | `adhoc.office_pptx` |
| **用途** | Markdown/JSON→pptx（纯Python，python-pptx；含融资路演/通用配方两场景+6 anti-AI-slop 设计原则） |

---

### 24. PDF 文档生成 (office_pdf)

| 属性 | 值 |
|------|------|
| **触发词** | 生成 PDF、md 转 pdf、归档材料、合并 PDF、拆分 PDF、PDF 加密、PDF 水印 |
| **Skill 文件** | `.agents/skills/office_docs/SKILL_pdf.md` |
| **task_type** | `adhoc.office_pdf` |
| **用途** | Markdown/文本→pdf + PDF 操作（合并/拆分/加密/水印/提取）（纯Python，reportlab+pypdf+pdfplumber） |

---

### 62. 自动关机 (auto_shutdown) [adhoc]

| 属性 | 值 |
|------|------|
| **触发词** | 自动关机、定时关机、睡前关机、挂机关机、条件关机、后台关机 |
| **Skill 文件** | `.agents/skills/auto_shutdown.md` |
| **task_type** | `adhoc.auto_shutdown` |
| **用途** | agent 自写检测 loop + 调 POST /auto_shutdown/trigger（120s 倒计时，可 /cancel 中止）；关机不走 computer use |

---

## dev/ 桶（工程化开发）

### 25. 目标工程编排 (goal_engineering) [dev] [entry]

| 属性 | 值 |
|------|------|
| **触发词** | 做个功能、重构、开发、帮我定目标、定义目标、让 agent 自己跑、goal engineering、目标工程、开发新功能 |
| **Skill 文件** | `.agents/skills/dev/goal_engineering/SKILL.md` |
| **task_type** | `dev.goal_engineering` |
| **role** | `entry`（开发任务统一入口） |
| **用途** | 三阶段闸门流程：询问（调研+追问≤5问+拍板）→写方案（spec融合Harness，审核闸门）→执行（拆tickets+implement，不再询问） |

开发任务统一入口，引用 `dev.leader` 的七问+Harness 心法。阶段1路由 `dev.grilling` 追问，阶段2路由 `dev.to_spec` 写融合 Harness 的 spec，阶段3路由 `dev.to_tickets`+`dev.implement` 执行到底。执行阶段不再询问用户，遇问题按 spec 的 Anti-Cheat 节自行决策。上下文压缩后必须先读 `temp/sdd/<slug>/spec.md`+`tickets.md` 续跑。

---

### 26. 目标工程方法论 (leader) [dev] [method]

| 属性 | 值 |
|------|------|
| **触发词** | 帮我定目标、写个目标、目标任务书、帮我拆目标、让 agent 自己跑、leader、目标工程、goal engineering、定义目标、Commander's Intent、Harness |
| **Skill 文件** | `.agents/skills/dev/leader/SKILL.md`（+ references/anatomy.md） |
| **task_type** | `dev.leader` |
| **role** | `method`（方法论参考，被 entry 引用） |
| **用途** | Commander's Intent + 目标七问（Why/Done/Proof/Anti/Bounds/Trade/Unknown）+ Harness 五种死法（作弊达标/幻觉命令/失忆/一条道走到黑/静默事故） |

来源 [KKKKhazix/khazix-skills/leader](https://github.com/KKKKhazix/khazix-skills/tree/main/leader)（Apache-2.0），本地化接入。核心信念：Harness（什么不能做）比 Goal（做什么）更重要。本项目无 /goal，产物写入 `temp/sdd/<slug>/spec.md` 融合六节。被 `dev.goal_engineering` 编排引用，也可独立触发做目标定义辅导。

---

### 27. 测试驱动开发 (tdd) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | TDD、测试驱动、红绿循环、red-green-refactor、先写测试、测试先行 |
| **Skill 文件** | `.agents/skills/dev/tdd/SKILL.md`（+ tests.md / refactoring.md / mocking.md） |
| **task_type** | `dev.tdd` |
| **用途** | TDD 开发流程：RED 写失败测试 → GREEN 写最小实现 → REFACTOR 重构 |

借鉴 mattpocock/skills engineering/tdd 改造，加 `task_type: dev.tdd`。测试命令：`uv run pytest`。

---

### 28. 代码库设计 (codebase-design) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 代码库设计、架构设计、deep module、shallow module、seam、design it twice、深化机会 |
| **Skill 文件** | `.agents/skills/dev/codebase-design/SKILL.md`（+ DEEPENING.md / DESIGN-IT-TWICE.md） |
| **task_type** | `dev.codebase_design` |
| **用途** | 架构设计原则：deep modules、seams、deletion test、design-it-twice |

借鉴 mattpocock/skills engineering/codebase-design 改造。规范术语：module/interface/depth/seam/adapter/leverage/locality。

---

### 29. 领域建模 (domain-modeling) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 领域模型、通用语言、ubiquitous language、术语表、CONTEXT.md、ADR、架构决策记录 |
| **Skill 文件** | `.agents/skills/dev/domain-modeling/SKILL.md`（+ ADR-FORMAT.md / CONTEXT-FORMAT.md） |
| **task_type** | `dev.domain_modeling` |
| **用途** | 构建领域模型、维护 CONTEXT.md 词汇表、记录 ADR |

借鉴 mattpocock/skills engineering/domain-modeling 改造。ADR 存 `docs/adr/`，词汇表更新 `CONTEXT.md`。

---

### 30. 解决合并冲突 (resolving-merge-conflicts) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 合并冲突、merge conflict、resolve conflict、解决冲突、rebase 冲突、git 冲突 |
| **Skill 文件** | `.agents/skills/dev/resolving-merge-conflicts/SKILL.md` |
| **task_type** | `dev.resolving_merge_conflicts` |
| **用途** | 解决 git 合并冲突的规范流程 |

借鉴 mattpocock/skills engineering/resolving-merge-conflicts 改造，加项目特定测试命令 `uv run python -m server.main`。绝不 `git merge --abort`。

---

### 31. 写 Spec (to-spec) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 写 spec、整理 spec、出 PRD、把讨论记下来、to-spec、规格说明 |
| **Skill 文件** | `.agents/skills/dev/to-spec/SKILL.md` |
| **task_type** | `dev.to_spec` |
| **用途** | 把当前对话综合成 spec 并存档（不做访谈） |

借鉴 mattpocock/skills engineering/to-spec 全套改造。issue tracker 替换为 `wip_tasks` 表 + `workspace/specs/<slug>.md`，斜杠命令替换为 agent_guide 路由。

---

### 32. 拆 Tickets (to-tickets) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 拆 ticket、拆任务、to-tickets、spec 转 ticket、任务拆解、tracer bullet |
| **Skill 文件** | `.agents/skills/dev/to-tickets/SKILL.md` |
| **task_type** | `dev.to_tickets` |
| **用途** | 把 spec 拆成可执行的 tickets（tracer-bullet 垂直切片） |

借鉴 mattpocock/skills engineering/to-tickets 全套改造。ticket 存 `workspace/tickets/<feature-slug>/<NN>-<slug>.md`，用 `extra_data.blocked_by` 表达阻塞关系。

---

### 33. Wayfinder 寻路 (wayfinder) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | wayfinder、寻路、fog of war、战争迷雾、探索地图、chart the map、work through the map |
| **Skill 文件** | `.agents/skills/dev/wayfinder/SKILL.md` |
| **task_type** | `dev.wayfinder` |
| **用途** | 处理 fog of war（未知领域）：chart the map 建图 / work through the map 执行 |

借鉴 mattpocock/skills engineering/wayfinder 全套改造。双模式：建图存 `workspace/wayfinder/<map-slug>.md`，ticket 类型 research/prototype/grilling/task（HITL vs AFK），claim 机制用 `wip_update(extra_data={claimed_by})`。

---

### 34. 原型探索 (prototype) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 原型、prototype、试一下、看看效果、设计变体、UI 选项、prototype this、let me play with it |
| **Skill 文件** | `.agents/skills/dev/prototype/SKILL.md`（+ `references/UI.md`：UI 变体分支 / `references/LOGIC.md`：状态机终端 app 分支） |
| **task_type** | `dev.prototype` |
| **用途** | 在承诺方案前构建一次性原型细化设计：UI 多变体切换 / 状态机终端驱动 |

借鉴 mattpocock/skills engineering/prototype。双分支：UI 问题走 `?variant=` URL 切换的浮动底栏；Logic 问题走终端 TUI 按键驱动。完成后 winner 折进真实 code，原 prototype 移到 `temp/prototype_<name>/`。与 `html-dev-debug` 配合做 UI 验证，与 `dev/tdd` 互补（prototype 不写测试，折进真实 code 时再用 tdd）。

---

### 35. 实现 Ticket (implement) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | implement、实现 ticket、领取 ticket、执行 ticket、开发 ticket |
| **Skill 文件** | `.agents/skills/dev/implement/SKILL.md` |
| **task_type** | `dev.implement` |
| **用途** | 领取并实现 ticket 的 6 步流程 |

借鉴 mattpocock/skills engineering/implement 全套改造。6 步：领取 → TDD 实现（可选 dev.tdd）→ 定期检查 → 代码审查 → 提交 → 更新状态。斜杠命令替换为 agent_guide 路由到 dev/tdd 和 code_review skill。

---

### 36. 分诊 Issues (triage) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | triage、分诊、issue 分诊、bug 分类、feature request 分类、审查 issue |
| **Skill 文件** | `.agents/skills/dev/triage/SKILL.md`（+ AGENT-BRIEF.md / OUT-OF-SCOPE.md） |
| **task_type** | `dev.triage` |
| **用途** | 通过角色驱动的状态机分诊 issues（category + state） |

借鉴 mattpocock/skills engineering/triage 全套改造。category: bug/enhancement；state: needs-triage/needs-info/ready-for-agent/ready-for-human/wontfix。AI disclaimer 必填。

---

### 37. 拷问我的计划 (grill-me) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | grill me、grill-me、拷问我、压力测试计划、找漏洞、追问计划、打磨计划 |
| **Skill 文件** | `.agents/skills/dev/grill-me/SKILL.md` |
| **task_type** | `dev.grill_me` |
| **用途** | 对计划或设计进行持续追问式访谈，压力测试其稳健性 |

借鉴 mattpocock/skills productivity/grill-me 改造（按用途归入 dev 桶：开发决策压力测试）。本 skill 是 `dev.grilling` 的入口，路由到 grilling 核心访谈逻辑。

---

### 38. 追问访谈核心 (grilling) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | grilling、追问、压力测试、拷问、design tree、打磨计划、找漏洞、grill |
| **Skill 文件** | `.agents/skills/dev/grilling/SKILL.md` |
| **task_type** | `dev.grilling` |
| **用途** | 围绕计划或设计持续追问用户的核心访谈逻辑 |

借鉴 mattpocock/skills productivity/grilling 改造（按用途归入 dev 桶：开发决策压力测试）。一次只问一个问题，沿 design tree 逐个解决决策依赖，每个问题附推荐答案，达成共同理解前不执行。fact 能查到的不要问用户，decision 才问。

---

### 39. 边拷问边沉淀文档 (grill-with-docs) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | grill-with-docs、grill with docs、边追问边记录、边拷问边出 ADR、边打磨边沉淀文档 |
| **Skill 文件** | `.agents/skills/dev/grill-with-docs/SKILL.md` |
| **task_type** | `dev.grill_with_docs` |
| **用途** | 对计划或设计进行持续追问式访谈，并在过程中沉淀 ADR 和项目词汇表 |

借鉴 mattpocock/skills engineering/grill-with-docs 改造（原版在 engineering bucket，本项目同样归 dev 桶）。比 grill-me 更重，适合需要长期决策记录的场景。路由到 `dev.grilling` + `dev.domain_modeling`。

---

### 40. 前端设计工艺 (impeccable) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 设计前端、UI 设计、polish、craft、shape、audit UI、critique 设计、design、redesign、落地页、dashboard 设计、组件设计、UI 太丑、AI 味、impeccable |
| **Skill 文件** | `.agents/skills/dev/impeccable/SKILL.md` |
| **本地 upstream 档案** | `.agents/skills/dev/impeccable/upstream/`（完整原版 SKILL.md + 8 根文档 + docs/ 5 开发文档；2026-07-21 融合自 pbakaus/impeccable v3.9.1 Apache-2.0） |
| **reference/** | `.agents/skills/dev/impeccable/reference/`（30 个命令规范：craft/audit/polish/critique/distill/shape/harden/optimize/adapt/animate/colorize/extract/bolder/quieter/delight/onboard/layout/typeset/live/product/brand/init/codex/clarify/hooks/document/overdrive/ios/android/interaction-design + adapt.native/audit.native） |
| **scripts/** | `.agents/skills/dev/impeccable/scripts/`（11 类 mjs 脚本，仅方法论参考，本地不执行） |
| **task_type** | `dev.impeccable` |
| **上游仓库** | https://github.com/pbakaus/impeccable（Apache-2.0，v3.9.1，2026-07-21 融合到本地） |
| **用途** | 工程化前端设计工艺：23 个命令覆盖从 craft 新 UI 到 polish 现有 UI 到 audit AI 痕迹到 distill 设计 DNA 的完整设计工艺流程 |

**23 命令清单**：init / craft / shape / audit / polish / critique / clarify / distill / harden / optimize / adapt / animate / colorize / extract / bolder / quieter / delight / onboard / layout / typeset / live / product / brand

**Register 选择**：marketing/landing/campaign/portfolio → `reference/brand.md`；app/admin/dashboard/tool → `reference/product.md`（不可跳过）。

**Absolute Bans（匹配即重写）**：side-stripe borders（`border-left/right > 1px` 彩色 accent）/ gradient text（`background-clip: text` + gradient）/ glassmorphism as default / hero-metric template / identical card grids / tiny uppercase tracked eyebrow above every section / numbered section markers as default scaffolding。

**设计规范速查**：
- Color：OKLCH（非 hex/rgb）；对比度 body ≥4.5:1, large ≥3:1；4 档策略 Restrained/Committed/Full palette/Drenched
- Typography：行长 65-75ch；字体配对走对比轴；hero clamp() max ≤6rem；letter-spacing ≥-0.04em
- Layout：Flexbox 1D, Grid 2D；`repeat(auto-fit, minmax(280px, 1fr))`；语义化 z-index scale
- Motion：指数曲线 ease-out（quart/quint/expo），无 bounce/elastic；必含 `@media (prefers-reduced-motion: reduce)` fallback
- Interaction：dropdown 用 `<dialog>`/popover/fixed/portal 逃 stacking context；组件覆盖 8 状态（default/hover/focus-visible/active/disabled/loading/error/success）

**路径适配**：原版 `node .pi/skills/impeccable/scripts/*` 本项目不执行（无 node 环境），scripts/ 仅作为方法论参考；`context.mjs` 改为 agent 直接读项目根目录的 PRODUCT.md/DESIGN.md。

**与 hallmark 边界**：impeccable 偏向"如何做好一个 UI"（23 命令完整设计工艺），hallmark 偏向"如何避免 AI 味"（4 动词 + 58 slop-test）。两者可叠加：impeccable craft 后用 hallmark audit 检查。

**调用惯例**：不主动执行 scripts/*（本地无 node）/ Setup 跳过 context.mjs 直接读 PRODUCT.md/DESIGN.md / 命令选择按用户动词映射（模糊动词默认 polish）/ register 选择必读 / absolute bans 触发即重写 / mobile 必检 320/375/414/768 px 4 档。

**与项目其他 skill 的联动**：`hallmark` 互补（craft 后 audit AI slop）；`prototype` 双分支快速验证（hallmark 走 production-grade 完整设计）；`web_archive` 抓取参考站点后用 `distill` 提取 DNA；`cangjie_extraction` 蒸馏设计方法论到 `.agents/skills/dev/`；`neat-freak` 同步新增设计规范。

---

### 41. 反 AI-slop 设计 (hallmark) [dev]

| 属性 | 值 |
|------|------|
| **触发词** | 反 AI 味、AI slop、audit 设计、redesign、study 设计、extract 设计 DNA、build 落地页、设计主题、hallmark、避免 AI 味、看起来手工做的 |
| **Skill 文件** | `.agents/skills/dev/hallmark/SKILL.md` |
| **本地 upstream 档案** | `.agents/skills/dev/hallmark/upstream/`（完整原版 SKILL.md + README/LICENSE/ROADMAP + docs/ 3 文档；2026-07-21 融合自 nutlope/hallmark v1.1.0 MIT） |
| **references/** | `.agents/skills/dev/hallmark/references/`（25+ 主题文件 + 5 子目录：components/genres/macrostructures/themes/verbs，含 anti-patterns/slop-test/themes/carnival/cobalt/hum/lumen 等） |
| **task_type** | `dev.hallmark` |
| **上游仓库** | https://github.com/nutlope/hallmark（MIT，v1.1.0，2026-07-21 融合到本地） |
| **用途** | 反 AI-slop 专项设计 skill：4 动词（build/audit/redesign/study）+ 20 内置主题 + 58 slop-test 检查门 + 结构多样性规则，让 UI"看起来是手工做的，不是 AI 生成的" |

**4 动词清单**：
- *(default)* → Design flow 走完整设计流程（用户要 build/design/make 新页面）
- `hallmark audit <target>` → 读目标对照 anti-pattern 列表打分返回 ranked punch list，**不编辑**
- `hallmark redesign <target> [--mood <name>]` → 在现有实现边界内重新设计视觉结构（保留路由/组件归属/文案意图/brand/IA）
- `hallmark study <screenshot | URL>` → 提取设计 DNA（macrostructure/archetypes/type-pairing/color anchor），可选重建或 emit 便携 `design.md`；URL 自动走 URL mode，其他走 image mode

**6 大跨动词 disciplines**（所有动词通用）：
1. Pre-emit self-critique（6 维 1-5 分自评，<3 触发 revision）
2. Honest copy — no fabricated content（不发明 metrics/testimonials/logos/case counts）
3. Locked tokens — no mid-render improvisation（color/font-family 必须引用命名 token）
4. Re-drawn chrome forbidden（不手画假 browser bar/手机框/code-block window/IDE chrome）
5. Mobile responsiveness at 320/375/414/768 px（4 档必检，非协商项）
6. Typography purity — no italic headers（标题永远 roman，italic 仅作 body copy 段内强调）

**Component-scope flow**（组件级，非页面级）：brief 命名单 UI 元素 + 短 ≤30 词 + 目标文件是单组件 + 用户说"just the X"——任 2 个触发，所有交互组件必须覆盖 8 状态。

**20 个内置主题**：carnival / cobalt / hum / lumen / ...（完整列表见 `references/themes/`），按 diversification rule 同一会话内不同 brief 不重复使用。

**路径适配**：原版 `references/<topic>.md` 改为 `.agents/skills/dev/hallmark/references/<topic>.md`；`.hallmark/log.json` 改为 `workspace/hallmark/log.json`；`design.md` emit 到 `workspace/hallmark/<project>/design.md`。

**与 impeccable 边界**：hallmark 偏向"如何避免 AI 味"（4 动词 + 58 slop-test），impeccable 偏向"如何做好一个 UI"（23 命令完整设计工艺）。两者可叠加：hallmark build 后用 impeccable polish 打磨细节。

**调用惯例**：URL vs screenshot 自动分流 / audit 永不编辑只返回 punch list / redesign 默认 in-place 不重建 route tree（多组件删除需确认）/ study URL mode 严格（emit design.md 需 attest 来源）/ diversification rule 记到 `.hallmark/log.json` / mobile 4 档必检。

**与项目其他 skill 的联动**：`impeccable` 互补（build 后 polish）；`prototype` 双分支快速验证；`web_archive` 抓取参考站点后用 `study` 提取 DNA；`deep_research` 配合 study 深度调研；`cangjie_extraction` 蒸馏设计方法论；`neat-freak` 同步新增设计规范。

---

### 42. 工作区决策树（跨工作区/生活/临时任务） (cross_workspace_advisor) [dev] [entry]

| 属性 | 值 |
|------|------|
| **触发词** | 开发网站、开发游戏、写后端、做 App、跨工作区开发、无关开发、新项目开发、开发一个、做个独立项目、生活任务、临时任务、工作区决策、其他工作、记账、日程、临时脚本、生活记账、排个班、健身记录 |
| **Skill 文件** | `.agents/skills/dev/cross_workspace_advisor/SKILL.md` |
| **task_type** | `dev.cross_workspace_advisor` |
| **role** | `entry`（工作区决策入口） |
| **用途** | 非本项目维护的请求按决策树选工作区位置：分支A独立项目级开发→新项目文件夹+dev_toolkit；分支B生活/其他工作→判断临时性→temp/或workspace/。建立后询问是否协助打开，完成时明确工作区位置 |

决策树两层判断：第一层判本项目维护（是→不走本skill）；第二层分支A（独立项目级开发，4项全满足）→新项目文件夹+复制dev_toolkit+重开对话；分支B（生活/其他工作）→自主判断临时性（一次就结束/一天内失效=临时→temp/；反复使用/项目插件=持久→workspace/），判断不了用AskUserQuestion询问（必须给判断标准）。分支B通用收尾：建文件夹后询问是否协助打开，完成时汇总明确工作区绝对路径，严防用户找不到。

---

## daily/ 桶（日常事务）

### 43. 教学 (teach) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | teach、教我、我想学、学一下、给我上课、讲解概念、teach me、learn |
| **Skill 文件** | `.agents/skills/daily/teach/SKILL.md`（+ MISSION-FORMAT.md / LEARNING-RECORD-FORMAT.md / RESOURCES-FORMAT.md / GLOSSARY-FORMAT.md） |
| **task_type** | `daily.teach` |
| **用途** | 在持久化教学工作区中教用户新技能或概念（多 session 状态化） |

借鉴 mattpocock/skills productivity/teach 改造。教学工作区从"当前目录"改为 `workspace/teaching/<topic-slug>/`，避免污染项目根目录。lesson 是自包含 HTML，存 `lessons/0001-<slug>.html`；MISSION.md 是教学 grounding，必须先填。

---

### 44. 蒸馏长内容为方法论 skill (cangjie_extraction) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 拆书、蒸馏、提取方法论、把书做成 skill、cangjie、RIA-TV++、把视频做成 skill、蒸馏一本书、抽象出理论 |
| **Skill 文件** | `.agents/skills/daily/cangjie_extraction/SKILL.md` |
| **本地 upstream 档案** | `.agents/skills/daily/cangjie_extraction/upstream/`（完整 RIA-TV++ 方法论、extractors、templates；2026-07-21 融合自 cangjie-skill） |
| **task_type** | `daily.cangjie_extraction` |
| **上游仓库** | https://github.com/kangarooking/cangjie-skill（MIT，2026-07-21 融合到本地；_vendor 副本已删除） |
| **用途** | 把书 / 长视频转写 / 播客文字稿 / 课程 / 访谈蒸馏成一组原子化、可被 agent 调用的方法论 skill；也用于 agent 主动识别任务中的可复用方法论并抽象 |

**6 阶段流水线**（RIA-TV++）：0.Adler 整书理解 → 1.5 个 sub-agent 并行提取 → 1.5.三重验证筛选 → 2.RIA++ 构造 skill → 3.Zettelkasten 链接 → 4.压力测试（darwin 兼容）→ 5.交付+产物路由

**输入要求**：必须从用户处确认（1）源文本路径（PDF/EPUB/TXT/字幕/转写稿，禁止凭记忆蒸馏）；（2）元信息（书名+作者+年份 / 视频标题+UP主+发布时间）；（3）是否首次试点（首次建议先蒸馏 1 份验证流程）。

**输出路径适配**（与原版 `books/<slug>/` 不同）：
- 蒸馏工作目录：`workspace/cangjie/<slug>/`（含 PIPELINE_STATE.md / BOOK_OVERVIEW.md / candidates/ / rejected/ / verified.md / INDEX.md / GLOSSARY.md / DIGEST.md / <skill-slug>/SKILL.md）

**产物路由决策**（阶段 5 必执行，按产物通用性四向路由）：

| 产物特征 | 路由目的地 |
|---------|-----------|
| 通用方法论（跨领域复用） | `.agents/skills/<scope>/<skill-slug>/SKILL.md` + 更新 `_index.md` + `agent_guide.py` + `tools/check_skills` |
| 领域特定方法论（仅本项目/当前任务有用） | 保留在 `workspace/cangjie/<slug>/`，不进项目 skills |
| 符合 memory_generation 标准的洞察事实 | 写入 memory（key 前缀 `cangjie_extraction.<slug>`，必填 `consumption_contexts` 和 `trigger_keywords`） |
| 未完成的蒸馏 / 待测试的 skill / 待用户决策的安装 | wip（`wip_create`，`extra_data` 存工作目录路径） |

**与 nuwa-skill / darwin-skill 的边界**：cangjie 蒸馏书（方法论/框架/原则），nuwa 蒸馏人（思维方式/表达 DNA），darwin 进化任意 skill。本 skill 不做：书摘、读后感、作者人设角色扮演。

**质量红线**：每 skill 必须通过三重验证 + 完整 R/I/A1/A2/E/B 六段 + 原文引用 ≤150 字/段 + `test-prompts.json` 含诱饵测试（至少 1 条同书兄弟 skill 场景）+ `description` 字段明确 trigger 条件。

**调用惯例**：先试点 1 本 / 阶段间主动汇报 / 不凭记忆拆书 / 保留 candidates+rejected 审计轨迹 / 随时可续跑（PIPELINE_STATE.md）/ 阶段 5 不能只装到 skills 目录，必须按路由表分别路由。

**与项目其他 skill 的联动**：阶段 5 路由到 memory 时复用 `memory_generation`；路由到 wip 时用 `wip_tracker.wip_create`；装到 `.agents/skills/` 的 skill 应符合 `skill-creator` 规范；新增项目级 skill 后触发 `neat-freak` 同步；源是网页时先用 `web_archive` 抓取；任务结束时走 `task_closure`。

---

### 45-55. 思维工具集（11 个 daily skill）[daily]

来源：文章[《都 Agent 时代了，我还是想分享给你这 12 个我最常用的 Prompt》](https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ)（作者：数字生命卡兹克，2026-08-21 抓取整合）。12 个 Prompt 中 2 个已整合到现有 skill（横纵分析法→`adhoc.deep_research`、人生设计术→`recurring.life_design`），其余 10 个+用户自定义组合版共 11 个独立 skill。

**组合哲学**：这些 skill 是积木不是流水线，发散优先、举例非穷尽、agent 自行判断组合方式、拿不准列给用户选。详见 [daily/README.md](daily/README.md) 组合哲学段。

每个 skill 目录：`SKILL.md`（路由元数据+工作流+兄弟工具）+ `prompt.md`（文章原文 Prompt 逐字保存，不改写）。

**与 dev.grill_me 的关系**：grill-me 是决策前拷问计划（dev/），steel_man_decision 是决策中二选一（daily/），decision_protocol 是重大决策走完整协议（daily/）。三者可串联：grill-me 澄清→steel-man 决策，或直接走 decision_protocol。

---

### 45. 苏格拉底式提问 (socratic_questioning) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 苏格拉底提问、苏格拉底式问诊、澄清困惑、找到真正的问题、问清问题、我到底想问什么、socratic questioning |
| **Skill 文件** | `.agents/skills/daily/socratic_questioning/SKILL.md`（+ prompt.md 原文 Prompt） |
| **task_type** | `daily.socratic_questioning` |
| **场景** | 问清问题 |
| **用途** | 用户困惑模糊时，通过最多 6 个逐个追问找到真正值得回答的问题。每次只问一个，信息足够时立即停止 |

---

### 46. 双层解释法 (dual_layer_explanation) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 双层解释、双层解释法、小白专家两版解释、学一个概念、听不懂的概念、用两层解释帮我学、dual layer explanation |
| **Skill 文件** | `.agents/skills/daily/dual_layer_explanation/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.dual_layer_explanation` |
| **场景** | 学习 |
| **用途** | 学陌生概念时分别从小白和专家两个角度解释一遍，避免"好像懂了"的错觉 |

---

### 47. 反向拆解 (reverse_decomposition) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 反向拆解、拆解优秀作品、拆解范例、学习它好在哪、拆解这个产品、反向工程一个作品、reverse decomposition |
| **Skill 文件** | `.agents/skills/daily/reverse_decomposition/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.reverse_decomposition` |
| **场景** | 学习 |
| **用途** | 看到优秀作品想学习它好在哪时，先说它解决了什么问题，再反向拆解为什么有效，最后给可复用规律+操作清单+小练习 |

---

### 48. 事实核查 (fact_checking) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 事实核查、核查说法、验证观点、检查推理链、这个说法对吗、可信度评估、fact checking、笛卡尔怀疑 |
| **Skill 文件** | `.agents/skills/daily/fact_checking/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.fact_checking` |
| **场景** | 学习 |
| **用途** | 对任何说法做笛卡尔式怀疑：拆三层+联网核查 5 档可信度+推理链 5 项漏洞+补强版本 |

---

### 49. 专家会诊 (expert_consultation) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 专家会诊、多专家视角、三视角分析、互补专家团、专家互相质疑、expert consultation、多视角会诊 |
| **Skill 文件** | `.agents/skills/daily/expert_consultation/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.expert_consultation` |
| **场景** | 解决问题 |
| **用途** | 为问题选择 3 种真正互补的专业视角，各自重新定义问题+推荐路径，然后互相质疑找出真正分歧，最后综合输出推荐方案 |

---

### 50. 第一性原理 (first_principles) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 第一性原理、拆到本质、回到本质、first principles、重新推导路径、打补丁不如重推、路径依赖拆解 |
| **Skill 文件** | `.agents/skills/daily/first_principles/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.first_principles` |
| **场景** | 解决问题 |
| **用途** | 把问题拆回最底层（基本事实/习惯性假设/真正目标/现实约束），暂时放下现成方案，只从基本事实重新推导可行路径 |

---

### 51. 跨领域借解 (cross_domain_borrowing) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 跨领域借解、跨领域类比、其他行业怎么解决、跨领域借鉴、cross domain、跨界借解、底层结构相似 |
| **Skill 文件** | `.agents/skills/daily/cross_domain_borrowing/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.cross_domain_borrowing` |
| **场景** | 解决问题 |
| **用途** | 把问题剥掉行业术语抽象成底层结构，从历史案例和至少 3 个距离较远的领域寻找相似解法，翻译成适合当前处境的方案 |

---

### 52. 双向钢人论证 (steel_man_decision) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 钢人论证、双向钢人、犹豫不决、两个选项选哪个、难以决定选哪个、steel man、决策二选一、纠结选哪个 |
| **Skill 文件** | `.agents/skills/daily/steel_man_decision/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.steel_man_decision` |
| **场景** | 决策 |
| **用途** | 两个选项间犹豫不决时，分别构造双方最强论证（不是稻草人），找出真正分歧，只问一个最关键的问题，再给判断。与 grill-me 区别：grill-me 决策前拷问计划，钢人决策中二选一 |

---

### 53. 用最小实验替代空想 (minimal_experiment) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 最小实验、最小可行实验、用实验替代空想、试一下再说、低成本验证、minimal experiment、验证假设 |
| **Skill 文件** | `.agents/skills/daily/minimal_experiment/SKILL.md`（+ prompt.md） |
| **task_type** | `daily.minimal_experiment` |
| **场景** | 决策 |
| **用途** | 当纸上谈兵无法更清晰时，找出最需要验证的 3 个假设，设计一个低成本、可逆、7 天内能完成的最小实验 |

---

### 54. 挖掘隐藏天赋 (talent_mining) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 挖掘天赋、隐藏天赋、天赋挖掘、我的天赋是什么、个人天赋说明书、talent mining、找天赋、人生天赋 |
| **Skill 文件** | `.agents/skills/daily/talent_mining/SKILL.md`（+ prompt.md，含完整 Role/对话规则/主线/输出结构） |
| **task_type** | `daily.talent_mining` |
| **场景** | 认识自己 |
| **用途** | agent 扮演资深生涯咨询师，通过多轮深度对话（最多 10 个主问题），在怪癖/缺点/嫉妒/无意识胜任区/能量模式里找到被压抑的天赋，最终产出万字《个人天赋使用说明书》 |

---

### 55. 重大决策协议 (decision_protocol) [daily]

| 属性 | 值 |
|------|------|
| **触发词** | 重大决策、人生抉择、重要选择、重大决定、难以决定人生方向、decision protocol、决策协议、重大人生决策 |
| **Skill 文件** | `.agents/skills/daily/decision_protocol/SKILL.md`（+ prompt.md，用户自定义组合版） |
| **task_type** | `daily.decision_protocol` |
| **场景** | 决策 |
| **用途** | 预烘焙组合套餐：启动前对齐（目标/成功标准/资源/限制/协作对象）+最强论证（双向钢人）+执行纪律。适用于重大人生抉择。简单二选一不走本 skill 走 steel_man_decision |

---

### 56. GKD 签到规则自动化 (gkd-signin-automation) [adhoc]

| 属性 | 值 |
|------|------|
| **触发词** | GKD签到、GKD规则、自动签到、快照解析、节点匹配、规则失效、实机回归、GKD snapshot |
| **Skill 文件** | `.agents/skills/gkd_signin_automation/SKILL.md` |
| **task_type** | `adhoc.gkd_signin_automation` |
| **工具脚本** | `scripts/collect.py`、`inspect_snapshot.py`、`validate_rule.py` |
| **用途** | Android 签到流程可行性分级、ADB快照采集、直接节点解析、规则静态回放、订阅导入与实机日志回归 |

---

### 57. 人生设计 (life_design) [recurring]

| 属性 | 值 |
|------|------|
| **触发词** | 人生设计、斯坦福人生设计课、人生蓝图、奥德赛计划、重启人生、人生规划、life design、设计我的人生、人生设计师、重力问题 |
| **Skill 文件** | `.agents/skills/daily/life_design/SKILL.md` |
| **Prompt 真源** | `workspace/life_design/prompt.txt`（完整角色设定+对话规则+提问流程+输出规范，agent 运行时必读） |
| **task_type** | `recurring.life_design` |
| **频率** | 每季度（quarterly）到期一次 |
| **产出位置** | `private_vault/life_design/`（dialogues/ 对话存档 + blueprints/ 蓝图产出，obsidian vault，不进 release） |
| **用途** | agent 读 prompt 后按斯坦福人生设计师角色与用户多轮深度对话（6-9 主问题，每轮一个），区分重力问题与可设计的真问题，最终产出《个人人生设计蓝图》（8000-12000 字） |

**对话型 recurring 任务**：到期后 agent 主动开启，但执行主体是用户——agent 是"陪练"。对话存档到 `private_vault/life_design/dialogues/YYYYMMDD_dialogue.md`，蓝图产出到 `private_vault/life_design/blueprints/YYYYMMDD_blueprint.md`。`private_vault/` 是项目私有文档仓库（obsidian vault），整个目录被 .gitignore 排除，朋友包零影响。

**关键约束**：每轮只聚焦一个主问题（禁止一次性抛出所有问题）；苏格拉底式追问但有度；温暖而犀利（共情+点出逻辑漏洞/重力问题死磕）；不评判不替用户决定；反向推演可选且需征求同意。

---

### 58. 后端自主 agent 会话 (adhoc.headless_session) [adhoc]

| 属性 | 值 |
|------|------|
| **触发词** | headless session、后端 agent、后台 agent、定时 agent 任务、无头会话、headless_agent、judge agent、判定 agent、后端自主会话、定时触发 agent、周期 agent 任务 |
| **Skill 文件** | （无独立 skill 文件，工作流定义见 `server/agent_guide_data.py` `adhoc.headless_session` 条目） |
| **task_type** | `adhoc.headless_session` |
| **配置位置** | `config.toml` `[loops.headless_session]` 段（参考 `config.example.toml`） |
| **用途** | 后端独立管理完整 agent 交互任务（定时触发 → 静默执行 → 客户端 chat 面板查看历史/实时运行），不依赖 IDE 在线。主 agent 跑完后由 judge agent 判定 success/failed/uncertain，uncertain 一律算 failed |

**资源限制**：`max_iterations=100` / `wall_clock_budget_secs=1800s`（默认值，可通过 `[loops.headless_session]` 覆盖）。B2 后只记账 token 不控制 cost 预算（见 [ADR 0016](../../docs/adr/0016-chat-engine-bookkeeping-only.md)）。

**关键约束**：judge verdict=uncertain 一律算 failed（不要为了让任务通过把 uncertain 当 success）；主 agent 和 judge agent 用独立的 RunnerConfig；中断主会话 → judge 不启动 → 整体 failed；headless session 写同一个 agent.db（WAL 模式支持并发写）。

**详细架构**：见 `server/agent_guide_data.py` `adhoc.headless_session` 条目的 first_action / workflow_summary / key_pitfalls 字段。

---

### 59. 版本发布 (system.release) [system]

| 属性 | 值 |
|------|------|
| **触发词** | 发版、发个版本、出版本号、改版本号、归档版本、release |
| **Skill 文件** | （无独立 skill 文件，工作流定义见 `server/agent_guide_data.py` `system.release` 条目） |
| **task_type** | `system.release` |
| **配套脚本** | `tools/bump_version.py`（版本号同步）+ `tools/migrate_changelog.py`（CHANGELOG 归档） |
| **用途** | CHANGELOG `[Unreleased]` → 版本号 release + 归档旧版本 + git commit（项目纯本地，不发 git tag / GitHub release / push remote） |

**6 步流程**：①读 CHANGELOG.md 确认 [Unreleased] 段有内容 → ②确定新版本号（patch/minor 递增）→ ③`uv run python tools/bump_version.py <版本号>` 同步 VERSION 常量 → ④Edit CHANGELOG.md 改段名 + 加空 [Unreleased] → ⑤`uv run python tools/migrate_changelog.py` 归档旧 release → ⑥git add 按目录 + git commit。

**与 `system.internal_workflow` 的区别**：`system.release` 只做 CHANGELOG 版本归档和私有仓库 commit；`system.internal_workflow` 才负责源码分发审计与打包。

**关键约束**：项目纯本地（不发 git tag / GitHub release / push remote）；PowerShell 不支持 bash HEREDOC（commit 信息用单个 -m 传单行标题）；git add 不要用 -A（按目录 add 避免误纳入敏感文件）；[Unreleased] 段空时不要 release。

**详细架构**：见 `server/agent_guide_data.py` `system.release` 条目的 first_action / workflow_summary / key_pitfalls 字段。

---

### 60. README 编写 (readme_author) [adhoc]

| 属性 | 值 |
|------|------|
| **触发词** | 写 README、改 README、README 公开版、整理 README、README 太弱、README 怎么写、awesome-readme、公开项目 readme |
| **Skill 文件** | `.agents/skills/readme_author/SKILL.md` |
| **task_type** | `adhoc.readme_author` |
| **用途** | 编写或修订面向公开分享的项目 README，特别是"主要自己用、顺便公开"的个人项目；含 7 段结构、tone 校准、竞品对比规则、增量修订流程、反模式清单 |

**核心哲学**：README 是作者和读者的一次诚实对话。30 秒内必须回答"这是什么 / 谁会用 / 凭什么用你的"。反模式：把 README 当 feature list。

**7 段结构**：一句话定位 → 是什么/不是什么 → 为什么写这个/定位 → 能力一览 → 快速开始 → 架构/核心概念 → 局限性+License+致谢。

**tone 谱系**（推荐"诚实自用型"）：官方产品型 / 工程师作品集型 / 诚实自用型 / 自嘲型 / 过度防御型。

**6 步流程**：0.收集 5 项上下文（定位/态度/读者/部署/差异化）→ 1.tone 校准 → 2.竞品对比规则 → 3.快速开始诚实原则 → 4.增量修订 vs 从零起草 → 5.反模式清单 → 6.交付前 checklist。

**与 `internal-workflow` 的关系**：`internal-workflow` 编排源码分发审计与打包；本 skill 是公开前 README 修订的执行层，常被 `internal-workflow` 编排调用。

**与 `neat-freak` 的边界**：`neat-freak` 做会话后文档一致性同步；本 skill 起草/修订 README 门面文件。

---

### 61. 批处理编写与修复 (bat_writing) [adhoc]

| 属性 | 值 |
|------|------|
| **触发词** | 写bat、bat脚本、批处理、.bat、.cmd、启动脚本、bat乱码、bat跑不了、批处理报错、不是内部或外部命令、找不到批处理标签、batch script、cmd脚本 |
| **Skill 文件** | `.agents/skills/bat_writing/SKILL.md`（+ `references/guide.md` 实测细节 / `scripts/fix_bat_encoding.py` 自检+修复） |
| **task_type** | `adhoc.bat_writing` |
| **用途** | 编写和修复能在中文 Windows 上跑起来的 .bat/.cmd；核心铁律 GBK(CP936)+CRLF+无BOM，写完必须 `--check` 自检全 OK 才算完成 |

**何时必须遵循**：新建任何 bat/cmd 脚本、修改现有 bat、排查"bat 跑不了/乱码/'XX' 不是内部或外部命令/找不到批处理标签"——先跑自检排除编码/行尾问题再查逻辑（九成"乱码报错"是 LF-only 行尾问题）。

**工作流**：读 SKILL.md → 确认目标机代码页（本机 ACP=OEMCP=936 → GBK）→ 按模板编写（标签用英文/`%~dp0` 拼路径/中途不 chcp）→ `uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件> --check` 自检 → 不过则 `--inplace` 修复后复检 → cmd 实跑验证。

**关键坑**：LF-only 是最常见死法（cmd 按字符数定位行偏移，GBK 双字节逐行错位，报错全是半截汉字；纯 ASCII 的 LF-only 往往能跑，所以坑只在中文脚本上爆）；不信任任何写入工具的行尾/编码；含中文禁止 UTF-8+chcp 方案。

---

## 后端 API 接口

> 完整 API 速查已移至 [docs/api-reference.md](../../docs/api-reference.md)（按功能分桶、精简描述、浏览器 API 分 Session/核心/辅助三层）。
> agent 优先调 `agent_guide(task='...')` 获取工具优先级，或访问 `http://127.0.0.1:8766/docs` 查看 OpenAPI 文档。

### 快速参考

| 模块 | MCP 工具示例 | 详细文档 |
|------|-------------|----------|
| 屏幕 | `screen_ocr` `execute_action` `capture_screen` `screen_snapshot` `screen_request_control` `screen_release_control` `screen_match_app` `screen_write_lesson` | `computer_use/SKILL.md` |
| 浏览器 | `browser_session_create` `browser_snapshot` `browser_action` `browser_wait_and_action` `browser_match_site` `browser_write_lesson` | `browser_lessons/SKILL.md` |
| 代码执行 | `exec_python`（v16 默认内联等 10s：done 返回完整 stdout/exit_code，running 返回 terminal_id） `exec_cmd` `exec_apply_patch` `exec_terminal_spawn` `exec_terminal_detail` `exec_terminal_input` `exec_terminal_kill` | `docs/tools-guide.md` |
| 代码执行（新） | `exec_inspect` `exec_kill` `exec_send_input` `wait` | `docs/mcp-reference.md` exec_python 统一 terminal 机制段 |
| 记忆 | `memory_get` `memory_set` `memory_search` | `docs/memory-system.md` |
| LLM | `agent_chat` `agent_score` | `docs/llm-pool.md` |
| OCR | `ocr_file` `ocr_path` | `.agents/skills/ocr.md` |
| 视觉 | `vision_locate` `understand_image` | `computer_use/SKILL.md` |
| 待办 | `todos_due` `wip_list` `wip_get` | `task_reminder.md` |
| 高级网关 | `localagent_advanced_tool` (116 工具) | `docs/mcp-reference.md` |
| MCP | `/mcp`（44 个直连工具，含任务授权 request/release；其余走网关） | `docs/mcp-reference.md` |

## 配置

**`config.toml`**（gitignore，实际配置）| **`config.example.toml`**（模板）

- `[llm.providers.xxx]`：定义提供商的 `base_url` 和 `api_key`
- `[llm.models.xxx]`：通过 `provider="xxx"` 引用提供商，也可直接覆盖 `base_url`/`api_key`
- 三层模型：`default` / `cheap` / `powerful`

---

## 调试浏览器隔离方案

- 调试浏览器使用独立 `user-data-dir`（默认 `chrome_debug/`，可在 `config.toml [browser] user_data_dir` 配置），CDP 端口 9222
- 支持任何 Chromium 内核浏览器（Chrome/Edge/Brave/Vivaldi），脚本会自动探测；可用 `--browser-path` 显式指定
- 详见 `docs/environment-constraints.md`

---

## 使用规则

1. **每次新会话开始**，先执行 AGENTS.md 中的"会话启动检查清单"（确认 MCP → 检查到期任务 → 读取相关记忆）
2. 需要查看 skill 完整信息（keywords/workflow/pitfalls/API 接口表）时，读本文件；任务路由走 `agent_guide`
3. 如果匹配到 skill，按 skill 定义的工作流程执行
4. 如果没有匹配的 skill，但任务可以用 MCP 工具完成，直接用 MCP 工具（详见 AGENTS.md "MCP 工具优先原则"）
5. 如果没有匹配的 skill，且需要多次执行的工作流，询问用户是否需要创建新 skill
6. 每次创建新 skill 后，更新本索引文件
7. **工具脚本**（非 Skill）的完整清单见 `tools/README.md` 和 `docs/tools-guide.md`
8. **项目结构**详见 `AGENTS.md` 中的"项目结构"章节
9. **二级文档**（操作手册/工程流程/MCP 架构/工具指南/记忆系统/LLM 池/环境约束/录制器/源码分发等 15 个文档）详见 `AGENTS.md` 文档导航表和 `docs/` 目录
