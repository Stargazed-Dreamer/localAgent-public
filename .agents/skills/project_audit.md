---
name: project_audit
description: >
  项目全面复审：让用户从 12 大类（架构/测试/文档/API/配置/可用性/安全/数据/性能/Git/环境/UX）
  中选择审查方向，按检查清单扫描问题并输出结构化报告。当用户说"项目复审"、"全面复审"、
  "项目体检"、"项目巡检"、"project audit"时触发。区别于 code_review（单文件）和 neat-freak（文档同步）。
task_type: system.project_audit
---

# 项目全面复审 (project_audit)

> 对 LocalAgent 项目进行多维度健康审计。**核心特征：先让用户选择审查方向，再按检查清单扫描**。
> 触发：用户说"项目复审"/"全面复审"/"项目体检"/"project audit" + 可选方向（默认全量）。

## 适用范围

- **全量复审**：12 大类全扫（适合周期性项目体检，例如每月/每季度一次）
- **定向复审**：用户选 1-N 个大类（适合针对性排查，例如"只看安全和性能"）
- **不适用**：
  - 单文件/单 PR 代码审查 → 用 `code_review` skill
  - 文档同步与洁癖审查 → 用 `neat-freak` skill
  - 临时 bug 排查 → 直接调试，不走本 skill

## 工作流（5 步）

### Step 1: 展示方向清单 + 让用户选择

**MUST**：第一步必须向用户展示 12 大类清单，让用户选择审查方向（multiSelect）。
**禁止**：未征询用户就自行全扫——12 大类全扫耗时长，用户可能只关心某几类。

用 `AskUserQuestion` 工具（`multiSelect=true`）展示方向，每个选项的 description 列出子项数量。
默认推荐：A（架构）+ B（测试）+ C（文档）+ F（可用性）+ J（Git）作为最小集。

**12 大类清单**（详见下方"审查方向详细清单"章节）：

| 编号 | 大类 | 子项数 | 简述 |
|------|------|--------|------|
| A | 架构与代码质量 | 7 | 架构拆分、循环依赖、命名、错误处理、类型注解、并发、资源泄漏 |
| B | 测试覆盖 | 5 | 单元/E2E/质量/覆盖率/conftest |
| C | 文档体系 | 5 | AGENTS.md 拆分、_index.md、docs/、Skill 完整性、文档代码一致性 |
| D | API 与工具膨胀 | 4 | 端点冗余、MCP 工具膨胀、零调用端点、命名一致性 |
| E | 配置与依赖 | 4 | config 遗弃项、pyproject、版本冲突、CVE |
| F | 可用性与监控 | 5 | loop 任务、apikey/model、功能模块、监控面板、日志可观测性 |
| G | 安全与权限 | 6 | 命令注入、密钥泄露、dcg、审批日志、CORS、SSRF |
| H | 数据与存储 | 4 | SQLite 索引、迁移幂等、备份、灾恢 |
| I | 性能与执行 | 4 | 阻塞热点、执行时长、内存、base64 污染 |
| J | Git 与变更管理 | 4 | changelog、commit 规范、历史大文件、分支 |
| K | 环境与兼容性 | 4 | PaddlePaddle/Florence2、CUDA、Playwright、Chrome 调试 |
| L | 客户端与 UX | 4 | GUI 卡顿、停止/跳过、错误提示、PySide6 性能规范 |

### Step 2: 持久化准备（长任务必做）

如果用户选了 ≥3 个大类，先创建持久化文件，避免上下文中断丢失进度：

- `temp/project_audit_plan.md` — 计划 + 进度（✅/⏳/⏸ 状态标记 + 每类预计产出）
- `temp/project_audit_findings.md` — 已发现问题（按大类分节，表格形式）
- 每完成一个大类，更新 plan 文件的状态和 findings 文件的对应章节

### Step 3: 上下文收集（按所选大类并行扫描）

- 并行 Read 关键文件（不超过 5 个并行）：
  - A/B/D：`server/main.py`、`server/core/`、`tests/conftest.py`、`server/mcp_whitelist.py`
  - C：`AGENTS.md`、`.agents/skills/_index.md`、`docs/` 全目录
  - E：`config.example.toml`、`pyproject.toml`、`server/config.py`
  - F：`data/loops/tasks.json`、`config.toml [loops.*]`、`client/panels/monitoring.py`、`mcp_stats`
  - G：`server/command_guard*.py`、`server/http_guard.py`、`server/approval_*.py`、`data/approvals.jsonl`
  - H：`server/todos/migration.py`、`server/memory_v2/`、`data/memory.db`（用 sqlite3 PRAGMA）
  - I：`/health` 响应、`/mcp/stats` 调用统计、`data/activity/hourly/` 异常时段
  - J：`git log --since="3 months ago"`、`git log --stat | head`、`CHANGELOG.md`
  - K：`docs/environment-constraints.md`、`pyproject.toml` 依赖版本
  - L：`client/panels/*.py` 的 `on_show()`、`AGENTS.md` PySide6 性能踩坑章节
- 必要时用 Grep/SearchCodebase 查找跨文件调用关系
- 必要时调 MCP 工具：`mcp_stats`/`loop_list_tasks`/`llm_pool_status`/`memory_status`/`/health`

### Step 4: 按所选大类的检查清单扫描

每个大类有详细的子项清单（见下方"审查方向详细清单"）。
对每个子项：
1. **判断**：是否符合预期？是否有问题？
2. **证据**：用 Read/Grep 找到具体代码位置或数据
3. **建议**：给出可执行的修复方向（不要空泛"建议优化"）

### Step 5: 输出报告 + 收尾

每个大类输出包含：

1. **健康度评分**：🟢 良好 / 🟡 需关注 / 🔴 紧急（1-2 句话理由）
2. **问题表格**：
   ```
   | No. | 严重度 | Issue Title | 证据 | 建议 | Code Link |
   |-----|--------|-------------|------|------|-----------|
   | 1   | 🔴     | 简述问题     | 引用 | 改进 | [file:line](file:///path#L123-L145) |
   ```
3. 同时把问题表格追加到 `temp/project_audit_findings.md`

**全部完成后**：
- 输出汇总报告（共性问题 / Top 3 紧急 / 改进路线图）
- 调 `agent_guide(task_type='system.task_closure')` 收尾
- 用户确认后写入结构化记忆（consumption_contexts=`["system.project_audit"]`）

## 审查方向详细清单

### A. 架构与代码质量（8 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| A1 架构拆分合理性 | Read `server/main.py` 路由注册 + `server/` 顶层文件数 | 顶层 30+ 平铺模块是否需要分包？main.py 是否过长？ |
| A2 循环依赖 | Grep `from server\.` 查跨模块 import | 不应有函数内 import 绕循环依赖 |
| A3 命名一致性 | Grep 路由路径风格 + Python 命名 | API 路径 kebab/snake 统一；skill 文件名统一（`html-dev-debug` vs `wuwa_gacha`） |
| A4 错误处理一致性 | Grep `except Exception` + HTTPException | 不吞异常；错误消息不暴露内部细节；状态码规范 |
| A5 类型注解完整性 | Grep `def \w+\(.*\) ->` | 函数签名有返回类型；Pydantic 模型字段完整；无 `str = None` |
| A6 并发与线程安全 | Grep `QThread`/`check_same_thread`/`async def` | asyncio 中无阻塞调用；SQLite 跨线程用 check_same_thread=False + 写锁 |
| A7 资源泄漏 | Grep `open(`/`connect_over_cdp`/`subprocess` | 文件用 with；浏览器实例复用 CDP；子进程有回收 |
| A8 深化机会（借鉴 mattpocock improve-codebase-architecture） | 见下方 A8 详细说明 | 识别 shallow modules（interface ≈ implementation 复杂度），寻找 deepening opportunities；与 `dev/codebase-design` 和 `dev/domain-modeling` 联动 |

#### A8 详细说明：深化机会扫描

借鉴 mattpocock/skills 的 improve-codebase-architecture，扫描"shallow modules"（interface 几乎和 implementation 一样复杂）并提出"deepening opportunities"——把 shallow modules 变成 deep modules 的 refactors。目标是 testability 和 AI-navigability。

**探索清单**（用 `subagent_type=search` 遍历 codebase，记录 friction 处）：

- 理解一个概念是否需要在许多小 modules 之间来回跳？
- 哪些 modules 是 shallow 的（interface 几乎和 implementation 一样复杂）？
- 是否存在为了 testability 抽出的 pure functions，但真正 bugs 藏在它们如何被调用之处（没有 locality）？
- 哪些 tightly-coupled modules 泄漏到了 seams 之外？
- Codebase 的哪些部分未测试，或很难通过当前 interface 测试？

**Deletion test**：对任何怀疑 shallow 的东西，问"删除它会让复杂度集中，还是只把复杂度移动到别处？"——"yes, concentrates" 才是 deepening opportunity 的 signal。

**Architecture vocabulary**：使用 `dev/codebase-design` 的术语（module、interface、depth、seam、adapter、leverage、locality），不要漂移到 "component"、"service"、"API" 或 "boundary"。`CONTEXT.md` 中的 domain language 会为好的 seams 命名；`docs/adr/` 中的 ADRs 记录不应重新争论的 decisions。

**输出**：列出 deepening opportunities 候选清单，每个候选包含 Files / Problem / Solution / Benefits / Recommendation strength（Strong / Worth exploring / Speculative）。若用户选中某个候选，可路由到 `dev/grill-me` skill 走 grilling loop，与用户走完 design tree；side effects 随 decisions 成形而内联发生（更新 `CONTEXT.md` 或提议写 ADR）。

### B. 测试覆盖（5 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| B1 测试覆盖率 | `uv run pytest --cov=server tests/` | 关键模块（command_guard/exec/screen）覆盖率 ≥ 70% |
| B2 测试缺失 | 对比 `server/*.py` 与 `tests/test_*.py` | 每个 server 模块有对应测试（30+ 模块 vs 39 测试文件） |
| B3 E2E 测试 | Read `tests/test_e2e.py` | 关键流程（启动→OCR→exec→关闭）有 E2E |
| B4 测试质量 | Read `tests/conftest.py` + 部分测试 | 无大量 skip；fixture 复用；不依赖外部服务（除非显式 mock） |
| B5 测试可运行 | `uv run python -m pytest tests/ -v --tb=short` | 全部通过（或失败有明确原因） |

### C. 文档体系（5 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| C1 AGENTS.md 拆分 | `wc -l AGENTS.md` + 内容分节 | > 500 行考虑拆分到 docs/ 子文档；AGENTS.md 只保留必读 |
| C2 _index.md 时效 | Read `.agents/skills/_index.md` + 对比 skills/ 目录 | 30 个 skill 都在索引中；触发词/依赖/产出字段完整 |
| C3 docs/ 时效 | Read `docs/*.md` 文件 mtime + 内容 | 6 个文档反映当前代码状态；无过时 API 描述 |
| C4 Skill 完整性 | 逐个 Read `.agents/skills/*.md` | 每个 skill 有触发词/工作流/依赖/输入输出/关键规则 |
| C5 文档代码一致性 | Grep 文档中提到的 API 路径 → 实际是否存在 | 文档中的端点真实存在；已删除的端点文档同步删除 |

### D. API 与工具膨胀（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| D1 REST 端点冗余 | Grep `@app\.\(get\|post\|put\|delete\)` + 调 `/openapi.json` | 无零调用端点；功能重叠的端点合并 |
| D2 MCP 工具膨胀 | Read `server/mcp_whitelist.py` + `localagent_list_tools()` | DIRECT_TOOLS ≤ 40；GATEWAY_EXCLUDE 维护；新增端点有归属决策 |
| D3 零调用端点 | `mcp_stats` 调用统计 + 90 天 0 调用端点 | 零调用端点考虑移除或归入 GATEWAY_EXCLUDE |
| D4 命名一致性 | Grep API 路径风格 | kebab-case 统一（`/screen/capture` vs `/screen/wait-for`）；无混用 |

### E. 配置与依赖（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| E1 config 遗弃项 | Read `config.example.toml` + Grep 配置 key 在代码中引用 | 无 config 项在代码中 0 引用；新增配置在 example 有模板 |
| E2 pyproject 依赖 | Read `pyproject.toml` + `uv pip list` | 无未使用依赖；版本无冲突；Python 版本声明一致 |
| E3 版本冲突 | `uv pip check` | 无依赖冲突；无缺少依赖 |
| E4 CVE 漏洞 | `uv pip audit`（或 `pip-audit`） | 无已知高危 CVE；中危 CVE 有评估记录 |

### F. 可用性与监控（5 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| F1 loop 任务可用性 | `loop_list_tasks` + `loop_status` | 任务不长期 paused；最近执行成功率 ≥ 80% |
| F2 apikey/model 可用性 | `llm_pool_status(summary=true)` + `apikey_history` | 无长期失效 key；model 配置的 provider 真实可用 |
| F3 功能模块可用性 | `/health` 各模块状态 | 全部 status=ok；长期 disabled 的模块有说明 |
| F4 监控面板完整性 | Read `client/panels/monitoring.py` + 对比 server 模块 | 新增模块在面板有展示；无僵尸展示项 |
| F5 日志可观测性 | Grep `logger\.\(info\|warning\|error\)` | 关键操作有日志；无敏感信息泄露到日志；日志有轮转 |

### G. 安全与权限（6 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| G1 命令注入 | Grep `subprocess`/`os.system`/`shell=True` | 用户输入不直接进 shell；用 shlex.quote 或参数列表 |
| G2 密钥泄露 | Grep `api_key`/`token`/`password` 在日志/响应中 | 不打印到日志；错误响应不返回 stack trace；config.toml 在 gitignore |
| G3 dcg 配置完整性 | Read `.dcg.toml` + `.dcg/packs/` | 危险命令模式覆盖完整（rm -rf/format/del/sudo）；有审批日志 |
| G4 审批日志审计 | Read `data/approvals.jsonl`（最近 100 条） | LLM 审批有审计；DENY 后强制人审生效；冷却期正常 |
| G5 CORS | Grep `CORSMiddleware` | 不允许 `*`；白名单显式列出允许的源 |
| G6 SSRF | Grep `requests\.get`/`httpx\.get` 用户可控 URL | 远程 VL/MindForge URL 不接受用户输入；有 URL 白名单 |

### H. 数据与存储（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| H1 SQLite 索引 | `sqlite3 data/memory.db ".indexes"` + EXPLAIN QUERY PLAN | 高频查询字段有索引（todos.next_due_at/wip.status/memory.updated_at） |
| H2 迁移幂等性 | Read `server/todos/migration.py` + `server/memory_v2/` | schema_info 标志位检查；重跑迁移不重复执行 |
| H3 备份策略 | Grep `backup`/`dump` 在 server/ + tools/ | DB 有定期备份；config.toml 有备份；workspace/ 关键数据有备份 |
| H4 灾难恢复 | 模拟 DB 损坏/配置丢失场景 | 有恢复流程文档；密钥可轮换；git 仓库可重克隆 |

### I. 性能与执行（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| I1 阻塞热点 | Grep `time\.sleep`/`urllib`/`requests\.` in `async def` | async 函数中无同步阻塞；用 asyncio.to_thread 或 httpx |
| I2 执行时长 | Read `data/activity/hourly/` + `/mcp/stats` 耗时 | 无单次调用 > 30s 的端点（除 OCR/VL 等已知慢操作） |
| I3 内存占用 | `/health` 内存字段 + 长运行后端内存 | 模型常驻内存合理；无内存泄漏（base64 大对象） |
| I4 base64 污染 | Grep `format="base64"` + AGENTS.md 约束 | 截图/文件不返回 base64 到 LLM；用 `format=inline` 或 `format=path` |

### J. Git 与变更管理（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| J1 changelog 及时性 | `git log --since="3 months ago" --oneline` vs `CHANGELOG.md` | 重要变更在 [Unreleased] 段；无遗漏的重大变更 |
| J2 commit 规范 | `git log --format="%s" -50` | 描述"为什么"而非"做了什么"；无 `update`/`fix` 等无意义信息 |
| J3 历史大文件 | `git rev-list --objects --all \| git cat-file --batch-check` | 无 > 10MB 的历史文件（模型/数据/视频误提交） |
| J4 分支管理 | `git branch -a` + `git log main..dev` | 无长期未合并分支；main 可发布 |

### K. 环境与兼容性（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| K1 PaddlePaddle/Florence2 | Read `docs/environment-constraints.md` + `pyproject.toml` | 版本约束文档化；PIR 懒加载禁用；截图 OCR 关闭 UVDoc；bbox 三档回归 1-3px |
| K2 CUDA | `nvidia-smi` + `pyproject.toml` paddlepaddle-gpu 版本 | CUDA 12.6 与 paddle-gpu 3.2.2 匹配 |
| K3 Playwright | `playwright install --dry-run` + `tools/browser/` | 已装好；不需要 `playwright install` |
| K4 Chrome 调试 | `browser_status` + `chrome_debug/` 目录 | CDP 9222 可用；用户数据隔离；不影响工作浏览器 |

### L. 客户端与 UX（4 子项）

| 子项 | 检查方法 | 期望状态 |
|------|----------|----------|
| L1 GUI 卡顿点 | Grep `on_show` in `client/panels/*.py` | on_show 不做重活；用 mtime 检测/懒加载/异步 |
| L2 停止/跳过机制 | Grep `cancel`/`stop`/`abort` in client/ + server | 长任务有停止机制；用户可跳过；有进度反馈 |
| L3 错误提示 | Grep `QMessageBox`/`error` in client/ | 错误消息对用户友好；不暴露 stack trace |
| L4 PySide6 性能规范 | 对照 AGENTS.md "PySide6 GUI 性能踩坑"章节 | QCalendarWidget 懒加载；N+1 查询预取；QThread+Signal 异步 |

## 严重度评级标准

| 级别 | 定义 | 示例 |
|------|------|------|
| 🔴 紧急 | 安全漏洞/数据丢失风险/核心功能不可用 | 命令注入、DB 无备份、loop 任务全失效 |
| 🟡 需关注 | 性能问题/可维护性差/文档过时 | async 阻塞调用、AGENTS.md 过长、零调用端点 |
| 🟢 良好 | 符合预期，无问题 | 通过所有检查项 |
| ⚪ 信息 | 可改进但非问题 | 命名风格建议、注释完善 |

## 评论准则

### 必须报告
- 🔴 紧急：安全漏洞、数据丢失、核心功能失效、资源泄漏
- 🟡 需关注：性能阻塞、循环依赖、文档过时、配置遗弃、零调用端点
- ⚪ 信息：命名一致性、类型注解、注释完善（汇总报告里提）

### 禁止报告
- 纯描述性评论（"这里用了 FastAPI"、"架构清晰"）
- 赞美性评论（"很好"、"不错"）
- 不基于证据的猜测（"可能"、"也许"、"建议检查一下"）
- UI 样式数值（字体大小、间距、颜色）— 默认用户已确认
- 代码风格（缩进、引号风格）— 除非项目有明确规范

### 谨慎报告
- 注释清晰度（除非有重大误导）
- 大功能删除（默认用户是有意的）
- 依赖版本升级（除非有 CVE 或破坏性变更）

## 与其他 skill 的关系

| skill | 重叠点 | 区别 |
|-------|--------|------|
| `code_review` | A（架构与代码质量） | code_review 是单文件/单模块代码审查；project_audit 是项目级多维度审计 |
| `neat-freak` | C（文档体系） | neat-freak 是会话后文档同步；project_audit 是周期性文档健康审计 |
| `task_closure` | 收尾步骤 | project_audit 完成后调 task_closure 收尾 |

## 项目特定关注点（LocalAgent）

### 高频踩坑（来自 AGENTS.md 和 project_rules.md）
- **相对路径**：`Path("data/xxx")` 在 cwd 错位时写错位置，应基于 `__file__`
- **硬编码**：路径/端口/密钥应移入 `config.toml`
- **base64 撑爆上下文**：截图/文件不得返回 base64 到 LLM
- **PowerShell curl 陷阱**：`curl -s` 在 PowerShell 中会卡住
- **`&&` 不支持**：Windows PowerShell 5.1 不支持 `&&`
- **SQLite 跨线程**：需 `check_same_thread=False` + 写锁
- **on_show() 性能**：客户端面板切换不能做重活
- **disk_cleanup 强制审核**：删除任何文件前必须先列清单给用户审核

### 历史包袱预判（来自 todos/ 和 .agents/wip/）
- main.py 曾拆分过（`todos/参考源码进行项目级翻新/`）
- screen 模块曾重构（`todos/建立hook、loop机制监控/`）
- module_refactor.md 有整体改造方案
- 部分模块可能仍有遗留的旧字段/兼容代码
- `todos/` 目录与运行时 `todos` 表命名冲突（易混淆）

### 双轨系统注意
- `.agents/skills/` 项目 skill ↔ `.trae/skills/` Trae IDE skill（两套系统并存）
- `.agents/rules/project_rules.md` ↔ `.trae/rules/project_rules.md` 镜像（修改需同步）
- `.agents/<data_drive>:\Documents/` ↔ `.trae/<data_drive>:\Documents/` 镜像

## 恢复指引

如果复审跨会话中断，新会话恢复步骤：

1. Read `temp/project_audit_plan.md` 了解进度（哪些大类已完成）
2. Read `temp/project_audit_findings.md` 了解已发现问题
3. Read 本文件（`.agents/skills/project_audit.md`）了解方法论
4. 从下一个未完成大类继续（用 TodoWrite 重建 todo list）
5. 每完成一个大类，更新 plan 和 findings 文件
6. 全部完成后输出汇总报告（共性问题 / Top 3 紧急 / 改进路线图）

## 输出语言

- 所有输出使用中文（用户偏好）
- 代码链接用英文路径 + markdown 链接格式 `file:///` + `#L123-L145`（行号范围 ≤ 100 行）
- 代码块用 markdown 标准格式（带语言标签）
- 表格用 markdown 标准格式
- 严重度用 emoji 标记（🔴🟡🟢⚪）

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 项目指引 | `AGENTS.md` | 项目结构和规范（终端文档） |
| Skill 目录 | `.agents/skills/` | Skill 定义文件 |
| Skill 索引 | `.agents/skills/_index.md` | 所有 Skill 的索引 |
| 变更清单 | `.agents/rules/project_rules.md` | 功能变更检查清单 |
| 测试目录 | `tests/` | 测试文件 |
| 配置模板 | `config.example.toml` | 配置项模板 |
| GUI 监控面板 | `client/panels/monitoring.py` | 状态展示面板 |
| MCP 工具 | `mcp_stats`/`loop_list_tasks`/`llm_pool_status`/`memory_status`/`/health` | 运行时状态查询 |
| code_review skill | `.agents/skills/code_review.md` | 单文件代码审查（互补） |
| neat-freak skill | `.agents/skills/neat-freak.md` | 文档同步（互补） |
| task_closure skill | `.agents/skills/task_closure.md` | 复审完成后收尾 |

## 输入/输出

- **输入**：用户选择的审查方向（1-12 大类）+ 项目当前状态
- **输出**：
  - `temp/project_audit_plan.md`（计划+进度）
  - `temp/project_audit_findings.md`（问题清单）
  - 汇总报告（共性问题 / Top 3 紧急 / 改进路线图）
  - 可选：结构化记忆写入（consumption_contexts=`["system.project_audit"]`）
