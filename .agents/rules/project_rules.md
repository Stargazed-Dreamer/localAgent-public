# 功能变更检查清单

每次新增功能、修改模块、重构代码后，按此清单逐项检查并更新。

## 新功能需求处理流程（前置流程，避免造轮子）

**接到"做/实现/加/搞个"类新功能需求时**，在动手写代码或克隆仓库前，按以下顺序排查现有实现。**核心原则：先找现成实现，避免造轮子；外部脚本必查安全性；不擅自安装额外库。**

### 1. 检查现有 skill（必做）

调用 `agent_guide(task='用户任务描述')` 获取候选清单，或读 `.agents/skills/_index.md` 查看详细 skill 信息（task_type 完整清单见 `_index.md` 核心 Skill 段 + 组件化模块段，数量随新增/重构变动不在这里写精确数字）。**重点关注**：
- **`dev/` 桶**（工程化开发 skill 集）：`to-spec` / `to-tickets` / `wayfinder` / `implement` / `triage` / `tdd` / `codebase-design` / `domain-modeling` / `prototype` / `grill-me` / `grilling` / `grill-with-docs` / `resolving-merge-conflicts` — 新功能开发几乎所有场景都值得先看 dev/ 桶
- **`daily/` 桶**：`teach`（教学）/ `cangjie_extraction`（蒸馏方法论）
- 元 skill：`skill-creator`（创建新 skill）/ `anti_hallucination`（小修补防幻觉）/ `neat-freak`（文档同步）/ `task_closure`（任务收尾）

若现有 skill 能覆盖需求 → 直接路由到对应 skill，不重复造轮子。

### 2. 检查 MCP 工具优先级（必做）

按 `AGENTS.md` "MCP 工具优先原则"和"工具选择决策树"4 层结构：
1. **直连 MCP 工具**（白名单，见 `server/mcp_whitelist.py`）→ 直接用
2. **`localagent_advanced_tool` 网关**（运行时从 OpenAPI 动态构建，GET 类免审批）
3. **`localagent_template_tool`**（预定义工作流）
4. **`exec_python`**（仅用于真正需要运行代码的场景）

只有当 MCP 工具无法满足需求时，才写自定义脚本，且必须写到 `temp/` 目录。

### 3. 检查外部 awesome-llm-apps 仓库（如涉及通用 AI agent 场景）

仓库地址：https://github.com/Shubhamsaboo/awesome-llm-apps （Apache-2.0，100+ 模板）

**适用场景**：用户要"做个 AI agent"、"加个 RAG 流水线"、"搞个 MCP agent"、"做个投资/金融/研究 agent"等通用 AI 应用需求时。

**查找路径**（按需选 1-N 类）：
- 🌱 Starter AI Agents（单文件 agent，30 秒跑起来）
- 🚀 Advanced AI Agents（生产级 agent，含工具/记忆/多步推理；投资/研究/欺诈调查/会议等）
- 🤝 Multi-agent Teams（多 agent 协作）
- 🗣️ Voice AI Agents（语音 agent）
- ♾️ MCP AI Agents（Model Context Protocol agent）
- 📀 RAG Tutorials（各种 RAG 流水线）
- 🧩 Awesome Agent Skills（19 个即插即用 agent skill 文件）

找到候选模板后，**不要直接 clone**，先记录候选清单（标题 + URL + 一句话描述），然后走第 5 步安全检查。

### 4. 搜索 GitHub 高星项目（如 awesome-llm-apps 无合适模板）

用 WebSearch 搜 `site:github.com <feature> stars` 或 `<feature> best repo`。**优先选 star ≥ 500 的项目**（除非用户明确指定小众需求）。记录候选清单。

### 5. 安全性检查（强制，避免恶意脚本）

对每个候选项目执行：

- [ ] **读 README**：理解项目用途、依赖、运行方式
- [ ] **检查依赖列表**（`requirements.txt` / `pyproject.toml` / `package.json`）：是否引入可疑库（如 `subprocess`、`socket`、`requests` 配合可疑 base64 字符串、`eval`/`exec` 相关库、未知私有库）
- [ ] **扫描入口脚本**（`*.py` / `*.js` / `*.sh`）：grep `subprocess`、`os.system`、`eval(`、`exec(`、`pickle.loads`、`urllib.request.urlopen` + 可疑 URL、`base64.b64decode` 后立即 `exec`、`yaml.load`（非 `safe_load`）等危险模式
- [ ] **检查网络请求**：是否向非项目域名发送数据（可能窃取信息）
- [ ] **检查文件系统操作**：是否读写项目外路径（`..`、`~/.ssh`、`%APPDATA%` 等）
- [ ] **不擅自安装额外库**：用户明确说过"不希望安装额外的库"。若项目必须装新库，必须先报告用户库清单 + 用途 + 大小，获得明确批准后才装
- [ ] **检查 LICENSE**：保留原作者所有权（项目硬约束），Apache-2.0 / MIT / BSD 可用，GPL / AGPL 需用户确认

**安全检查发现可疑代码** → 不集成，向用户报告具体可疑位置 + 推荐替代项目。

### 6. 询问用户是否需要（强制）

把候选清单 + 安全检查结论用 `AskUserQuestion` 提交给用户：

- 列出每个候选的：标题 / GitHub URL / star 数 / 一句话描述 / 安全检查结论（通过/有疑点/不推荐）/ 需要装的库（如有）
- 询问"是否需要集成？选择哪个？还是放弃？"
- 用户明确批准后才进入下一步

### 7. 评估任务规模走 SDD（如新功能中等以上复杂度）

按 `workspace/dev_toolkit/AGENTS.md` "SDD 主流程"：
- **小修小补**（改个 bug、调个参数、加一行）→ 直接 `implement` 或 `anti_hallucination`
- **中等以上**（新功能、跨模块改动、架构调整）→ **主动建议走 SDD**，从 `grill-me` 开始 → `to-spec` → `to-tickets` → `implement`
- 用户明确说"别走流程，直接写" → 放行，但提示"返工风险自负"

### 与现有规则的整合

本节是以下现有规则的**前置扩展**（不替代）：
- `AGENTS.md` "## MCP 工具优先原则" — 工具选择 4 层决策树
- `workspace/dev_toolkit/AGENTS.md` "## SDD 主流程" — 中等以上任务走 SDD
- `.agents/skills/_index.md` "## 使用规则" — 优先匹配 skill，无 skill 时用 MCP 工具，再无则询问用户是否需要创建新 skill
- `.agents/skills/anti_hallucination/SKILL.md` — 小修补防幻觉 6 步流程

本节新增的是：**外部参考源（awesome-llm-apps + GitHub 高星项目）+ 安全检查 + 用户确认** 这三步前置流程。

---

## 文档存档与 HTML 路径（强制）

**项目根目录已多次因 SDD 流程产物和 HTML 展示文件污染而清理，自 2026-07-30 起强制如下规则：**

### SDD 流程产物

任何 SDD 流程产物（grill-me、design-decisions、spec、tickets、checklist、00-overview、NN-*.md、BLOCKED、plan、design 等）必须写入 `temp/sdd/<feature-slug>/`：

- ✅ 路径：`temp/sdd/<feature-slug>/`
- ❌ 禁止裸放项目根目录
- ⚠️ SDD 流程产物默认不写入 `planning_notes/`（走 `temp/sdd/`）；但用户手动存放的规划/路线图文档允许保留在 `planning_notes/` 下，不视为违规
- ❌ 禁止写入 `docs/`、`workspace/`、`tools/`、`server/`、`client/` 等位置
- 详见 `AGENTS.md` "功能规划文档存档路径（强制）" 段落

### HTML 展示文件

任何用于浏览器预览、可视化报告、调试页面等展示用途的 `.html` 文件必须写入 `temp/html/`：

- ✅ 路径：`temp/html/<feature-name>.html` 或 `temp/html/<feature-name>/`（多文件时建子目录）
- ❌ 禁止在项目根目录创建任何 .html 文件
- ❌ 禁止在 `docs/`、`workspace/`、`tools/`、`server/`、`client/`、`planning_notes/` 等位置创建 HTML 展示文件
- 例外：`client/resources/` 下 PySide6 资源文件（如 `style.qss`）、`tools/release/` 下发布模板 HTML（如有）属项目固有资源，不在此规则范围

### 历史归档

- `temp/planning_archive/sdd/` 存放 2026-07-30 之前从 `planning_notes/` 迁移的历史 SDD 文档（只读归档，不再修改）
- `temp/planning_archive/sdd/v6/` 存放历史 v6-*.md 设计文档系列

### 检查项（每次代码/文档变更后必查）

- [ ] 新建的 SDD 文档（spec/tickets/checklist 等）是否在 `temp/sdd/<slug>/` 下？
- [ ] 新建的 HTML 展示文件是否在 `temp/html/` 下？
- [ ] 项目根目录是否新增了 .md / .html 文件？（应为空，若有则违反规则）
- [ ] `planning_notes/` 下是否有 SDD 流程产物误写入？（SDD 走 `temp/sdd/`；用户手动存放的规划/路线图文档不管）
- [ ] **Glob 工具受 gitignore 过滤**：验证 gitignored 目录/文件存在性时（如 `chrome_debug/` `config.toml` `private_vault/` `temp/` `USER_ONLY_*.md` `file_sample.json` `.venv/`），用 PowerShell `Test-Path` 或 `Get-ChildItem -Force`，不要用 `Glob`/`LS`（它们会漏报）

---

## 文档与注释变更（2026-09-16 防过时整改新增，详见 AGENTS.md「文档防过时规范」）

- [ ] 是否新写了易漂移的统计数字（面板/工具/条目/图标/ADR/schema 版本/间隔天数）？→ 改为指向代码常量、运行时清单或定性描述
- [ ] 行为重构（尤其是"改掉旧行为"类）是否 grep 了旧行为关键词并同步全部 docstring / `docs/*.md` / config 注释？
- [ ] 文档中引用的文件路径是否实际存在？移动/删除文件后是否全仓 grep 清理了引用？
- [ ] 写"当前是 X"前是否实测（import 常量 / 数文件 / 跑发现逻辑），而非照抄其他文档？
- [ ] 改 `.agents/rules/` 或 `.trae/rules/` 后是否双向同步并 diff 确认一致？

---

## 代码变更

- [ ] 新增的路由是否在 `server/main.py` 中注册？
- [ ] 新增的Pydantic模型是否定义完整（Request/Response）？
- [ ] 新增的接口是否在 `/health` 中反映状态？
- [ ] 新增的接口是否有 `/status` 查询端点？
- [ ] 错误处理是否完善（不暴露内部细节）？
- [ ] 是否有硬编码的路径/端口/密钥？→ 路径/端口移入 `config.toml`；密钥移入 `data/secret/secrets.toml`（非 LLM 密钥）或 `data/llm/keys.json`（LLM 密钥），通过 `lib/secret` 读取

## Guide 关键词维护（强制，每次新增/修改 skill 必查）

> **背景**：2026-08-08 测试 guide 功能时发现"我今天都干了啥，帮我捋一下时间线"无法路由到 daily_summary（得分仅 6，未达 strong_match 阈值），且 60+ skill 的 keywords 存在大量跨 skill 重复、泛动词、同 skill 内重复，导致路由串台。本规则防止同类问题复发。

**核心原则：keywords 是路由的"指纹"，必须唯一、具体、可区分。向量化是安全网，不是脏关键词的借口。**

### 评分机制速查（写 keyword 前必读）

GUIDE_REGISTRY 的 `match_task_candidates` 五路加权打分：
1. **kw_exact**（+10/词）：keyword 完整出现在用户 query 中 → 最强信号
2. **2-gram 重叠**（+2/重叠 bigram，上限 16）：query 与 entry 的 2-gram 交集
3. **同义词组命中**（+3/组 + 2/组不同词）：语义相关
4. **kw_fuzzy**（+3/词 if ratio≥50%）：keyword 的 2-gram 大部分在 query 中
5. **domain_hints**（+10/domain）：context 含特定域名时加分

**strong_match 判定**：kw_exact 命中 OR kw_fuzzy ratio≥80% OR 向量化（cosine≥0.6 OR score≥阈值 AND cosine≥0.4）。

### 检查项（每次新增/修改 skill 的 keywords 时必查）

- [ ] **跨 skill 重复检查**：新增 keyword 前，用 Grep 搜该词在所有 `GUIDE_REGISTRY` 条目（`server/agent_guide_data.py` + `workspace/*/loop_actions.py`）中是否已存在。若已存在于其他 skill → 必须改为组合词（如"抽卡"→"鸣潮抽卡"）或放弃该 keyword
- [ ] **禁止泛动词**：单独的"开发/翻页/磁盘/记账/抽卡/寻访/日程/存档"等泛动词禁止作为 keyword——它们会让多个 skill 同时 kw_exact 命中导致平局。必须用组合词（"磁盘清理"/"鸣潮抽卡"/"文章存档"）
- [ ] **同 skill 内去重**：同一 skill 的 keywords 列表内不能有重复词，且不能有语义重叠的变体（如"客户端开发"与"开发客户端"同时存在）
- [ ] **短英文 keyword（≤4 字符纯 ASCII）**：IRR/NPV/DCF/TDD/ADR/WIP 等已自动跳过 kw_fuzzy（避免 bigram 误匹配），但仍参与 kw_exact。新增短英文 keyword 时确认它的完整子串不会出现在常见英文 query 中
- [ ] **向量化兜底验证**：对口语化 query（无 keyword 命中），确认向量化能正确路由。测试方法：`uv run python temp/test_match_logic.py`（或直接调 `match_task_candidates(query, top_n=3)`）
- [ ] **盲测集回测（强制）**：修改 keywords 后必须跑 `uv run python tests/guide_eval/run_eval.py`（tests/guide_eval/cases.jsonl 固化 ~135 条真实/口语 query + 期望 top1 标注），top1 命中率 < 85% 即红。pytest 挂 quick 层（tests/guide_eval/test_guide_eval.py）。known_gap=true 的用例是已知路由缺口 backlog，修复串台后把 known_gap 移除并更新 note

### 已知坑点（历史教训）

| 坑点 | 症状 | 修复 |
|------|------|------|
| 4 个抽卡 skill 都有单独"抽卡" | "鸣潮抽卡"让 4 个 skill 同时 kw_exact +10 平局 | 移除单独"抽卡"，保留"鸣潮抽卡"等组合词 |
| "异环"/"鸣潮"/"终末地"单独词 | yihuan_gacha 与 yihuan_simulator 串台 | 移除单独游戏名词，用"异环抽卡"/"异环棋盘"区分 |
| "记账"在 accounting 和 cross_workspace_advisor 重复 | "记账"串到 cross_workspace_advisor | 从 cross_workspace_advisor 移除，保留组合词"生活记账" |
| dev.goal_engineering 有单独"开发" | "开发新功能"命中所有 dev 桶 skill | 移除单独"开发" |
| daily_summary 无口语化变体 | "干了啥/捋/时间线"得分仅 6，未达 strong_match | 加正式变体 + 向量化兜底 |

## 密钥统一管理（强制，2026-08-06 lib/secret 改造起）

**核心原则：禁止将密钥硬编码到代码或写入 config.toml，所有密钥读取必须经过 lib/secret 中转。**

### 密钥分类与存储位置

| 密钥类型 | 存储位置 | 读取方式 |
|----------|----------|----------|
| LLM/VL/AIGC 密钥（api_key/base_url/model） | `data/llm/keys.json` | `lib.secret.get_llm_keys_path()` 获取路径，`server/llm_pool/key_store.py` 统一加载 |
| 非 LLM 密钥（tushare_token/github_token/gh_mirror_pat 等） | `data/secret/secrets.toml` `[tokens]` 段 | `lib.secret.get_secret(key)` 或 `lib.secret.get_secret_or_raise(key)` |

### 禁止行为

- ❌ 将密钥（api_key/token/secret/password）硬编码到 `.py` / `.js` / `.toml` / `.json` / `.md` 文件
- ❌ 将密钥写入 `config.toml`（config.toml 只存非敏感配置，不存任何密钥）
- ❌ 在代码中直接 `open("data/secret/secrets.toml")` 或 `open("data/llm/keys.json")` 读取密钥（必须通过 `lib/secret` 中转）
- ❌ 在日志、错误信息、文档中回显密钥真实值

### 正确做法

- ✅ 新增非 LLM 密钥：写入 `data/secret/secrets.toml` `[tokens]` 段，代码中用 `from lib.secret import get_secret; token = get_secret("key_name")`
- ✅ 新增 LLM 密钥：写入 `data/llm/keys.json`，通过 `server/llm_pool/key_store.py` 加载
- ✅ 密钥文件路径：用 `lib.secret.get_llm_keys_path()` / `lib.secret.get_secrets_toml_path()` 获取，不硬编码路径
- ✅ 密钥不存在时的降级：`get_secret(key, default=None)` 返回 None，`get_secret_or_raise(key)` 抛 `SecretNotFoundError`

### 迁移工具

- `tools/migrate_secrets.py`：将 config.toml 中的旧密钥字段迁移到 secrets.toml（幂等，支持 `--dry-run`）
- 迁移后 config.toml 中的 `tushare_token` / `github_token` 字段应已删除

### 检查项（每次代码变更后必查）

- [ ] 新增的密钥是否存到了 `secrets.toml` 或 `keys.json`（而非 config.toml 或代码中）？
- [ ] 读取密钥的代码是否通过 `lib/secret` 中转（而非直接 open 文件）？
- [ ] 密钥文件路径是否通过 `lib.secret.get_*_path()` 获取（而非硬编码）？
- [ ] 日志/错误信息中是否泄露了密钥真实值？

## ADR 评估（强制，每次架构层变更必查）

本次变更是否触发 ADR 评估？按 `docs/adr/README.md` 三项标准逐项打分：

- [ ] **Hard to reverse**：移除/反转需修改多个消费方、agent 行为契约、外部 API？（改一个 if-block/字段名/回退 list = 不通过）
- [ ] **Surprising without context**：未来读者会问"为什么这里多了一层防御/校验/间接"？（行为与模块既有设计哲学一致 = 不通过）
- [ ] **Real trade-off**：有具体替代方案被拒绝且拒绝理由不显然？（没替代如修复 bug，或替代方案显然不可行 = 不通过）

**3/3 通过 → 写 ADR**（扫 `docs/adr/` 最高编号递增一，遵循 `.agents/skills/dev/domain-modeling/ADR-FORMAT.md` 模板，写完在 `docs/adr/README.md` 索引追加一行）

**2/3 通过 → 边缘案例**，优先考虑撤销修改或调整设计而非"为了写 ADR 而写 ADR"（边缘案例往往意味着设计本身有矛盾）

**<2/3 通过 → 直接做**，CHANGELOG 足矣

**反模式（不写 ADR）**：bug 修复、安全补丁、性能优化、可逆代码调整、字段语义显式化、内存泄漏修复、单点焦点校验、"未来可能 hard to reverse"（当前没人消费某字段，未来"如果"消费了就难移除——等真的发生时再写）

详见 `.agents/skills/dev/domain-modeling/ADR-FORMAT.md`（含反模式示例 + 评分表）

## 待办模块变更

- [ ] 新增的 todos/wip 端点是否在 `server/main.py` 中注册？
- [ ] `_mcp_include` 是否更新（高频查询 todos_due/wip_list/wip_get 直连）？
- [ ] `_CATEGORY_PREFIXES` 是否添加新前缀（todos_, wip_）？
- [ ] `/health` 是否返回 todos 状态？
- [ ] `agent_guide.py` 中相关条目的 first_action / mcp_tools_priority 是否更新？
- [ ] 涉及读详情/CRUD 的 task_type，first_action 是否提示用 `localagent_advanced_tool`（而非 exec_python 发 HTTP）？
- [ ] 启动时迁移逻辑是否幂等（检查 schema_info 标志）？

## 项目结构变更

- [ ] 新增/删除/重命名一级或二级目录？→ 用 `exec_python` 调 `server.project_structure.sync_baseline(add_descriptions={"新路径/": "描述"})` 归位 baseline（`data/project_structure.json`；`update_baseline_descriptions()` 仅补 top_level 描述，二级目录要用它会静默无效）
- [ ] `data/project_structure.json` 是否同步更新？（baseline 是漂移检测的真源，task_closure 自动用 `structure_diff` 检测）
- [ ] `/health` 中 `project_structure.baseline_exists` / `baseline_entries` / `baseline_updated` 是否正确反映状态？
- [ ] 新增模块是否需要在 `agent_guide(include_structure=true)` 响应的 `project_structure` 字段中体现？
- [ ] task_closure 收尾时若 `structure_diff.unknown_paths` 非空，是否已补全 baseline 或报告用户确认？

## 文档更新

- [ ] `.agents/skills/_index.md` - Skill列表、API接口表、项目结构是否更新？
- [ ] `README.md` - 快速开始、Skill使用指南、API列表、项目结构是否更新？
- [ ] **CHANGELOG.md** - 每次功能变更/Bug修复/安全修复后，在 `[Unreleased]` 段对应分类（Added/Changed/Fixed/Security）下添加条目。格式：`- 简述变更（涉及文件路径，为什么改）`。release 时把 `[Unreleased]` 改为 `[版本号] - YYYY-MM-DD` 并在顶部加新的空 `[Unreleased]`，然后跑 `uv run python tools/migrate_changelog.py` 把旧 release 段移到 `docs/changelog-archive.md`（默认保留最近 1 个，`--keep N` 改保留数）。CHANGELOG.md 仅含 `[Unreleased]` + 最近 1 个 release，避免活跃段过长被 agent 误伤。详见 `.agents/wip/changelog_setup.md`
- [ ] 对应Skill的 `.md` 文件 - 工作流程、参数、依赖是否更新？
- [ ] `config.example.toml` - 新增的配置项是否添加模板？
- [ ] GUI 监控面板（`client/panels/monitoring.py`）- 新模块/新状态是否在面板中展示？
- [ ] 记忆 key 是否需要新增或更新？
- [ ] `tools_manifest.json` - 新增工具是否添加到清单？（GUI 自动读取此文件）
- [ ] `client/widgets/tool_runner.py` - 仅在 GUI 展示、分类、队列或启动流程变化时更新；新增工具通常只改 `tools_manifest.json`。（旧 `tools_launcher.py`/`tools_manifest.md`/`launcher.bat` 已删除，由 `client/panels/tools.py` + `client/widgets/tool_runner.py` 替代）
- [ ] `AGENTS.md` - 工具脚本表是否更新？（项目结构已程序化，见上方"项目结构变更"段；AGENTS.md 只保留简要大纲）
- [ ] `server/agent_guide.py` 的 `GUIDE_REGISTRY` - 新增 skill 时是否同步添加条目？（task_type 命名规范：`{scope}.{name}`，内置 scope=recurring/adhoc/system/dev/daily，workspace 扩展可引入新 scope 如 recording）

## 配置变更

- [ ] `config.example.toml` 是否同步更新？
- [ ] `server/config.py` 的 `get_*_config()` 是否支持新配置？
- [ ] 新配置项是否有合理默认值？
- [ ] `data/config_descriptions.json` 是否同步更新？（Settings 面板"说明"列的真源。新增/重命名/删除 config 字段时必须补对应条目，路径用点分隔，动态段名用 `*` 通配。格式 `{"path.to.field": {"desc": "说明", "example": "示例"}}`）

## Git

- [ ] `.gitignore` 是否排除了敏感文件（config.toml、temp/、workspace/ 下的敏感数据子目录）？
- [ ] 变更是否已提交？提交信息是否描述了"为什么"而非"做了什么"？

## 测试

- [ ] 后端能否正常启动？`uv run python -m server.main`
- [ ] `/health` 接口是否返回正确状态？
- [ ] 新增接口是否可通过 curl/requests 调用？
- [ ] 记忆 API 是否可通过 curl/requests 调用？
- [ ] 监控面板是否正常展示新状态？

### 危险操作测试铁律（强制，2026-08-03 教训）

> **触发**：auto_shutdown 重构期间，`test_trigger_minimal_request` 漏传 `dry_run=True`，测试运行时真的调了 `shutdown /s /t 60`，用户紧急 `shutdown /a` 才中止。单个测试能单方面触发不可逆系统操作是严重设计失误。

**核心原则：危险操作（关机/重启/递归删除/强杀进程/写系统目录/绕过 command_guard 的 subprocess 调用等）测试尽可能不实操，必须实操时要有机制级安全网。**

写测试时强制检查：
- [ ] **能 mock 就不实操**：危险 API（`subprocess.Popen` / `os.remove` / `shutil.rmtree` / `taskkill` 等）必须 mock，绝不真调
- [ ] **autouse 安全网 fixture**：测试模块顶部有 autouse fixture 默认 mock 所有危险 API，从机制上兜底（即便单个测试忘传安全参数也不会真触发）
- [ ] **显式传安全参数作为双保险**：即便有 autouse 兜底，测试调用时仍显式传 `dry_run=True` / `safe_mode=True`
- [ ] **参数默认值偏向安全**：API 设计时 `dry_run` / `safe_mode` 默认 `True`，不要让"忘传参数"等于"危险路径"
- [ ] **静态审查**：grep 所有调危险端点的测试代码，逐个确认要么传安全参数要么 mock 了底层 API
- [ ] **PR 自检**：问自己"这个测试最坏情况会做什么？"——如果答案是"真关机/真删文件/真杀进程"，停下来加安全网

详见 `docs/dev-workflow.md` "危险操作测试铁律" 章节（含反面案例 + 正确做法代码示例）。

### 测试修复铁律（强制，2026-08-03 教训）

> **触发**：exec_python 修复 + 测试套件修复期间，多次用"巧妙"方案绕过失败测试而非查根因：
> 1. `test_e2e.py::test_small_image_ocr_then_parse` 调用已删除的 `/vision/parse` 端点（OmniParser 移除时删），测试本应删除/改写，却加 `@pytest.mark.gpu` 跳过——让过时测试"通过"而非诚实删除
> 2. `test_monitoring_task_authorization.py` Qt 模态对话框 crash，用 `try/except SystemError` 掩盖而非 mock UI 交互
> 3. `ruff check --fix --unsafe-fixes` 修了 19 个 lint 错误后没跑测试验证，导致潜在功能回归未发现
> 4. exec_python 代码修复后没重启后端，声称"修好了"但生产环境（0.15.0）根本没生效，temp 还在堆积

**核心原则：测试失败是信号不是障碍。绕过测试 = 自欺欺人，下次同样的问题还会出现。**

**测试修复时的强制检查清单**

- [ ] **禁止用跳过绕过过时测试**：测试因端点删除/ API 变更/功能移除而失败时，**删除或改写测试**，不要加 `@pytest.mark.skip` / `@pytest.mark.gpu` 让它"通过"。跳过 = 丢失覆盖 + 假绿
- [ ] **禁止用 try/except 掩盖 crash**：测试 crash 时先查根因（环境问题？被测代码 bug？测试设计问题？），mock 掉有问题的 UI/IO 交互测业务逻辑，不要 `try/except Exception` 吞掉异常假装通过
- [ ] **禁止放宽断言让测试通过**：断言失败时先查"断言对还是实现对"，不要把 `assert x == 5` 改成 `assert x >= 1` 或删断言。UI 重构后断言确实该改的，必须在注释里说明"为什么改"+ 验证新断言对应真实行为
- [ ] **改测试前先读被测代码**：确认测试期望的行为是否还存在。端点删了？文案变了？返回结构改了？先读源码确认，再决定删/改/保留测试
- [ ] **ruff --unsafe-fixes 必须验证**：`--unsafe-fixes` 可能改变语义（如删"未使用"变量但实际被 eval 引用、合并 import 但循环导入）。**用完必须跑全测试套件**，不能只看 lint 通过就提交
- [ ] **代码修复必须验证生效**：改了后端代码必须重启后端确认修复生效（`/health` 查版本 + 实际行为验证），不能只看代码改了就声称"修好了"
- [ ] **每个测试修复都问"这是修测试还是绕测试"**：如果答案是"让测试不再报错但不验证真实行为"→ 停下来，这是绕过

**反面案例（本次教训）**

```python
# ❌ 错误：/vision/parse 端点已删除，加 @pytest.mark.gpu 跳过让过时测试"通过"
@pytest.mark.gpu
def test_small_image_ocr_then_parse(self, client, small_ui_image_base64):
    resp = client.post("/vision/parse", json={...})  # 端点都删了，跳过有什么意义？
    assert resp.status_code == 200  # 永远不会执行

# ✅ 正确：删除过时测试 + 注释说明为什么删
# 注：原 test_small_image_ocr_then_parse 已删除。
# /vision/parse 端点在 OmniParser 移除时已删除（见 test_vision.py TestVisionParseRemoved）。
# 测试本身过时，正确做法是删除而非跳过。OCR 流程已有 test_ocr.py 覆盖。
```

```python
# ❌ 错误：try/except 掩盖 Qt crash，测试"通过"但线程问题依然存在
def test_monitoring_request_waits_for_server_prompt_timeout(qapp):
    try:
        panel._on_persistent_request_clicked()  # Qt 模态对话框 crash
    except SystemError:
        pass  # 吞掉异常假装通过

# ✅ 正确：mock 掉有问题的 UI 交互，测业务逻辑
def test_monitoring_request_waits_for_server_prompt_timeout(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    panel._on_persistent_request_clicked()  # 不弹真实对话框，测 HTTP 请求/线程/超时逻辑
```

```python
# ❌ 错误：ruff --unsafe-fixes 后只看 lint 通过就提交
ruff check --fix --unsafe-fixes server client tests  # 修了 19 个
# 没跑 pytest，潜在功能回归未发现

# ✅ 正确：--unsafe-fixes 后必须跑全测试套件验证
ruff check --fix --unsafe-fixes server client tests
uv run python -m pytest tests/ -v --tb=short  # 验证没破坏功能
```

**判卷标准**：测试修复若让测试"通过"但不验证真实行为（跳过过时测试 / try-except 吞 crash / 盲目放宽断言），算修复失败，必须重做。

详见 `docs/dev-workflow.md` "测试修复铁律" 章节。
