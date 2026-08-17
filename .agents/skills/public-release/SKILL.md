---
name: public-release
description: >
  规划、审计并构建隐私安全的 LocalAgent 源码分发包（基于机器可读的 release profile）。
  当用户请求为朋友打包源码、公开发布、匿名化发布、发布 profile，
  或检查哪些内容可以进入归档时使用此 skill。触发词包括打包给朋友、源码分发、公开发布、
  脱敏发布、发布包、friend-full、public release、public distribution。
---

# 源码公开发布（public-release）

> 本 skill 已迁移到 v2 release compiler（spec-v2-compiler.md）。所有工作流经 `tools/release/cli.py` 单入口 CLI。旧的 `audit_profile.py` 和 `export_release.py` 已于 2026-07-31 删除（T18）。

本 skill 从私有规范仓库产出隐私安全的源码归档。每次发布都重复完整工作流，因为 workspace 内容、密钥和私有引用会在多次发布间漂移。不存在"已经做过"的事——即便存在默认基线，每一步都必须重新执行并呈现给用户。

## v2 compiler 工作流概览

v2 release compiler 把发布流程拆为 prepare + build 两阶段，对外只暴露 `prepare_release()` 和 `build_release()` 两个核心接口，所有 CLI 操作都路由到这两个接口。审计和构建消费同一份不可变 `PreparedRelease` 计划。

5 个 CLI subcommand（详见 `python -m tools.release.cli --help`）：

| subcommand | 用途 | 内部接口 |
|------------|------|---------|
| `prepare` | 编译输入为 PreparedRelease，写 `release/plans/<plan_digest>.json` | `prepare_release()` |
| `compute-digest` | 计算 plan_digest 并打印各 digest 供操作员填入 `[approval]` 段 | `prepare_release()` |
| `scan` | 独立运行 sensitive-content-scan gate，打印扫描结果 | `prepare_release()`（取 scan gate） |
| `build` | 读 plan 文件 + profile `[approval]` → 构建产物到 `release/dist/` | `build_release()` |
| `list-components` | 列出 audience policy 允许的组件清单 | `load_audience_policy()` |

完整设计与约束见 `temp/sdd/release-engine/spec-v2-compiler.md`。

## PreparedRelease 不可变计划

`PreparedRelease` 是 frozen dataclass（13 字段精简集），由 `prepare_release()` 产出，由 `build_release()` 消费。审计和构建消费同一份计划——任何分叉 = 失败。

**plan_digest 绑定所有输入**：
- `plan_digest = SHA-256(canonical_json(plan_fields))`
- `plan_fields` 含 profile_digest / components_digest / scan_digest / exemptions_digest / source_commit / file_entries（按 rel_path 排序）
- 任何输入变化让 plan_digest 变化，使旧 approval 失效

**approval 必须匹配 plan_digest**：
- `Approval.plan_digest` 必须等于 `PreparedRelease.plan_digest`，否则 `build_release` 拒绝
- `build_release` 还独立验证当前 git commit == `plan.source_commit`（防源码漂移）和当前 profile digest == `plan.profile_digest`（防 profile 漂移）
- 这三道验证是 fail-closed 的硬约束，不可绕过

## 工作流（高层概览）

1. 读取 `release/policy.toml`、`docs/release-policy.md`、`release/audience/friend.toml`、`release/profiles/friend-full.toml`。
2. 检查 `git status` 并用 `git ls-files` 列出被追踪文件清单（v2 engine 内部自动跑 `git ls-files` 固定源码快照）。把未提交的用户改动视为用户所有，绝不 reset。
3. **Phase 1（prepare）**：运行 `python -m tools.release.cli prepare --profile friend-full --source HEAD --audience friend`，engine 内部自动完成 manifest 加载、组件选择、tracked-file-inventory gate、sensitive-content-scan gate、manifest-audit gate、policy-schema gate，并产出 `PreparedRelease` 计划文件 `release/plans/<plan_digest>.json`。
4. **Phase 2（scan，可独立重跑）**：运行 `python -m tools.release.cli scan --profile friend-full` 单独打印 sensitive-content-scan gate 的扫描结果。Phase 1 已自动跑过 scan，但 scan 命令便于反复验证替换规则。
5. 产出确切的候选清单、待审清单和发现报告。报告内不得打印密钥值或个人数据。
6. **Phase 3（用户审批 + build）**：在创建导出树、归档、tag、远端或 PR 之前，停下来等待用户明确批准。用户批准后用 `compute-digest` 输出 digest，填入 profile `[approval]` 段，再运行 `build` 构建产物。
7. v2 engine 绝不就地脱敏规范源码树——`build_release` 复制源文件到 `release/staging/<plan_digest>/` 后才应用 path_mapping，源码树保持原样。绝不包含 `.git` 历史。
8. v2 engine 内部跑构建期 gates（no-key-startup / activity-task-safe-stop / focused-tests / archive-verification）。任一 gate 失败 → raise + 失败 ZIP 改名 `.failed.zip` 留 staging。
9. 成功产物原子移动到 `release/dist/`，外部附 `.zip.sha256` 校验和文件。

## Phase 1：prepare 阶段

**目标**：通过 `prepare_release()` 把所有发布输入编译为不可变 `PreparedRelease` 计划。无需手动分类——engine 内部自动完成 manifest 加载、组件选择、4 个静态 gate。

### 如何运行

```bash
# prepare 会自动跑 4 个静态 gate（policy-schema / tracked-file-inventory /
# sensitive-content-scan / manifest-audit），任一失败 → 退出码 2（fail closed）
uv run python -m tools.release.cli prepare --profile friend-full --source HEAD --audience friend
```

输出（stdout）含 `plan_digest`、`plan file` 路径、`components`、`file_entries` 数量、`static gates` 数量。plan 文件写入 `release/plans/<plan_digest>.json`。

### engine 内部自动完成的步骤

1. 加载 `release/audience/<audience>.toml`（audience policy，含 components + export_set + gates 清单）
2. 解析 `source` 参数为 git commit sha（`git rev-parse`）
3. 加载 `release/profiles/<profile>.toml`
4. 按 audience.export_set 从 19 个组件 manifest 中加载对应 file_entries（`exports.source.files` 或 `exports.runtime.files`）
5. 跑 4 个静态 gate：
   - **policy-schema**：profile 含 schema_version / profile_id / audience / [approval] / [behavior]，且 schema_version=1
   - **tracked-file-inventory**：file_entries 全部 source=tracked（拒绝非追踪文件）
   - **sensitive-content-scan**：扫源文件敏感内容，HIGH 命中 > 0 → fail（除非 profile `[sensitive_line_skips]` 豁免）
   - **manifest-audit**：所有 audience components 的 manifest 满足新 shape（exports + release_facts）
6. 计算 profile_digest / components_digest / scan_digest / exemptions_digest / plan_digest
7. 写 `release/plans/<plan_digest>.json`
8. 返回 PreparedRelease

**关键约束**：静态 gate 在 prepare 跑过一次后，build 不重跑（信任 `plan.gates_results`）。这避免重复工作，且静态 gates 在 prepare 时已绑定到 plan_digest。

### 用户改动 workspace 时

workspace 内容会在多次发布间漂移。每轮之前：
1. 重新运行 `prepare` 获取当前 file_entries。
2. 与上一轮的 `release/plans/<old_plan_digest>.json`（若已保存）对比 file_entries。
3. 把新增的 HIGH 命中呈现给用户——它们可能表示一个新子项目或新引入的密钥引用。
4. 把现有 `[sensitive_line_skips]` 之外的新增 MEDIUM 命中呈现给用户——可能需要新增 path_mapping 规则或 line_skips。

### 决策日志

每个 workspace 子项目决策必须记录在两处：
- `release/audience/friend.toml`（components 数组）+ `release/profiles/friend-full.toml`（path_mapping / line_skips）—— 机器可读的规则。
- `docs/release-policy.md` 的"默认 Workspace 模块决策"章节 —— 人类可读的原因。

默认基线（2026-07-18）覆盖 16 个 workspace 子项目。新子项目需在两处添加新决策条目，外加一行 CHANGELOG。

### 按需 Workspace 模块开关（2026-07-21 引入）

某些 workspace 子项目在多次发布间需要按需开启或关闭。为避免每次重新设计脱敏规则，所有可开关模块的脱敏决策表记录在 `docs/release-policy.md` 的"按需 Workspace 模块开关"章节。

**切换机制（audience policy + TOML 注释开关）**：

1. 在 `release/audience/friend.toml` 的 `[audience].components` 数组中添加/移除/注释对应模块名。
2. 对应的细分 path_mapping 规则以注释形式保留在 `release/profiles/friend-full.toml` 中，未来重新开启时取消注释即可复用。
3. 切换后必须重置 `[approval].plan_digest = ""`（让 build fail-closed）、`review_scan_cleared_at = ""`，并重跑 Phase 1 + Phase 2 验证。

**关键决策（2026-07-21）**：

| 模块 | 当前状态 | 验证结论 |
|------|---------|---------|
| `web_archive` | 开启 | 3 个 `.py` 脚本中 `f:\<project_root>\chrome_debug` 由现有 path_mapping 规则覆盖；Phase 2 扫描 0 命中 |
| `niuke_review` | 开启 | 2 个 `.py` 脚本完全使用 `Path(__file__)` 相对路径；Phase 2 扫描 0 命中 |
| `dev_toolkit` | 开启 | `.md` 中路径引用由现有规则覆盖；`skill_creator.md` 的 "Alice" 示例有专门规则 |
| `wuwa_gacha` / `arknights_gacha` / `endfield_gacha` / `bilibili_gacha` / `official_gacha` | 全部关闭 | 用户决策 2026-07-21：朋友不要抽卡模块 |

**重要**：所有可开关模块都不需要新增 path_mapping 规则——它们的个人路径已被现有 path_mapping 覆盖。如果未来加入新模块，必须先用 `exec_python` 在源码中 `grep` 所有路径变体验证是否需要新增规则。

## Phase 2：敏感内容扫描

**目标**：扫描每个 file_entry 的源文件内容，查找密钥、账号标识、私有仓库引用和本地绝对路径。只输出 `(rule, path, line_number)` 三元组——绝不回显匹配到的值。

### 如何运行

```bash
# 独立运行 sensitive-content-scan gate（Phase 1 已自动跑过，scan 命令便于反复验证）
uv run python -m tools.release.cli scan --profile friend-full
```

scan 命令内部调 `prepare_release()` 并打印 `sensitive-content-scan` gate 的 details + `scan_digest` + 扫描的文件数。

### 规则类别（来自 `tools/release/engine/prepare.py` 的 `_SENSITIVE_PATTERNS`）

每个类别有一个严重度和一条或多条正则模式。**HIGH 命中 > 0 → prepare fail closed**；MEDIUM 命中可由 path_mapping 在 build 阶段清掉。

| 类别 | 严重度 | 捕获内容 |
|----------|----------|-----------------|
| `api-key-or-token` | HIGH | `sk-...`（OpenAI）、`AKIA...`（AWS）、`eyJ...` JWT token 等 |
| `private-repository-reference` | HIGH | 私有仓库引用相关字串等 |
| `local-absolute-path` | MEDIUM | `[A-Z]:\\...`（Windows）、`/home/<user>/...`（Linux）、`/Users/<user>/...`（macOS）等 |

完整模式定义见 `tools/release/engine/prepare.py` 的 `_SENSITIVE_PATTERNS` 字典。

### 输出格式

scan 命令输出含 `gate.passed`、`gate.details`（HIGH 命中数、MEDIUM 命中数、扫描文件数）、`scan_digest`。

**操作纪律**：绝不包含匹配到的文本。agent 仅在调查特定命中时才通过 `Read` 工具读取该行，且不得把匹配值粘贴回对话——应描述为"第 N 行的值是真实 API key"或"第 N 行的值是示例字符串，误报"。

### 文件类型过滤

engine 内部跳过：
- 按扩展名的二进制文件：`.png`、`.jpg`、`.jpeg`、`.gif`、`.bmp`、`.ico`、`.pdf`、`.zip`、
  `.gz`、`.tar`、`.7z`、`.wav`、`.mp3`、`.mp4`、`.avi`、`.mov`、
  `.onnx`、`.pt`、`.pth`、`.bin`、`.pkl`、`.npy`、`.npz`

### 误报分诊

扫描完成后：
1. 按 `path` 分组命中。0 命中的文件是干净候选。
2. 每条命中分类为：
   - **TRUE_POSITIVE**：真实密钥/PII —— 必须排除该文件或重写该行。
   - **FALSE_POSITIVE_EXAMPLE**：示例/占位字符串 —— 保持原样，在报告中标注。
   - **FALSE_POSITIVE_DOC**：文档引用（如"在此设置你的 API key"）—— 保持原样。
   - **AMBIGUOUS**：无上下文无法判断 —— 呈现给用户。
3. 仅含 FALSE_POSITIVE_* 命中的文件可通过 `[sensitive_line_skips]` 豁免（需用户批准后）。
4. 含 TRUE_POSITIVE 命中的文件必须从 audience components 移除，或通过 `[post_process_remove_lines]` 物理删除命中行。

### 内容替换规则（local-absolute-path 脱敏专用）

源码中含真实本地路径（`C:\<user_home>\`、`F:\<project_root>`、`I:\<data_drive>:\<source_images_root>` 等）。这些路径在本地运行时必需，**不能直接改源码**。脱敏通过两层机制实现：

#### 1. 在 profile 定义 `[content_replacements.path_mapping]`

v2 把原 74 条 `[[content_replacements.literal]]` 路径规则重组为 ~26 条结构化 path_mapping 规则。每条规则是 `(find, replace)` 对，**大小写不敏感字面匹配**（`re.sub` + `re.IGNORECASE`），find 用 TOML basic string（反斜杠 `\\` 转义），replace 用 TOML literal string（反斜杠原样）。

```toml
[content_replacements.path_mapping]
# 大小写不敏感，re.sub + re.IGNORECASE
"project_temp\\localagent" = '<project_root>'
"users\\admin" = '<user_home>'
"<data_drive>:\<working_root>" = '<data_drive>:\<working_root>'
```

**重要：源码中的路径有 4 种字面形式**，path_mapping 用大小写不敏感字面匹配覆盖：

| 形式 | 示例 | 出现场景 |
|------|------|----------|
| 单反斜杠 + 大写盘符 | `F:\<project_root>` | raw string `r"..."`、文档 |
| 单反斜杠 + 小写盘符 | `f:\<project_root>` | 部分 Python 代码 |
| 双反斜杠（转义） | `F:\\project_temp\\localAgent` | Python basic string `"..."`、TOML basic string `"..."` |
| 正斜杠 | `F:/<project_root>` | 跨平台代码、URL 风格 |

由于 path_mapping 是大小写不敏感字面子串匹配（不解析为路径），单条 `find = "project_temp\\localagent"` 会同时命中 `<project_root>`、`project_temp\\localagent`、`Project_Temp\\LocalAgent` 等多种字面形式。但 4 种字面形式（单反斜杠 / 双反斜杠 / 正斜杠 / 转义）仍需在 path_mapping 表中分别覆盖——`re.sub` 不解析路径，只做字面子串匹配。

多行内容删除仍使用 `[[content_replacements.literal]]`（详见 `friend-full.toml` 中对 `_index.md` / `AGENTS.md` / `agent_guide.py` 的脱敏条目）。

#### 2. 在 profile 定义 `[deployment_mapping.required_mappings]`

每个占位符对应一条部署说明，`build_release` 渲染 `DEPLOYMENT.md` 时读取此表（Jinja2 模板）：

```toml
[[deployment_mapping.required_mappings]]
placeholder = "<username>"
meaning = "本地 Windows 用户名"
example = "C:\\Users\\<your_name>\\"

[[deployment_mapping.required_mappings]]
placeholder = "<project_root>"
meaning = "LocalAgent 项目根目录的绝对路径"
example = "D:\\code\\localAgent"
```

部署者拿到导出包后，按 `DEPLOYMENT.md` 反向映射到自己机器的路径。

#### 3. 用 `scan` 命令验证 path_mapping 有效性

scan 命令打印当前扫描结果。**注意**：scan 跑在源码原状上，不应用 path_mapping（path_mapping 在 build 阶段应用）。所以 scan 命中数包含所有真实路径引用，build 阶段应用 path_mapping 后这些路径会被替换为占位符。

**验证标准**：scan 命中中 `local-absolute-path` 的 HIGH 命中必须为 0；MEDIUM 命中必须由 path_mapping 覆盖（人工核对每条 MEDIUM 命中是否在 path_mapping 表中有对应规则）。

### 占位符跳过模式（避免替换后假阳性）

应用 path_mapping 后，行内容变成 `C:\Users\<username>\AppData\...`。这仍可能被 `local-absolute-path` 的 `\b[A-Z]:\\[^\s"'<>|*?]+` 匹配（因为 `<` 是 stop char，匹配到 `C:\Users\` 这一小段）。

v2 engine 在 build 阶段应用 path_mapping 后不再重跑 scan（信任 prepare 的 scan_digest），所以这个问题在 prepare 阶段表现为：含占位符的行被计入 MEDIUM 命中。**应对方式**：在 profile `[sensitive_line_skips]` 中为含占位符的行添加跳过规则。

```toml
[sensitive_line_skips]
# 跳过含 <placeholder> 的行（如路径替换后的 C:\Users\<username>\... ）
"some/file.py" = ['<([a-zA-Z_][a-zA-Z0-9_]*)>']
```

注意：这会跳过任何含 HTML/XML 标签的行。在 LocalAgent 代码库中这种交叉污染可接受（markdown 中的 `<tag>` 通常不与 `C:\` 路径共现），但**每次新增 path_mapping 规则时要重跑 scan 验证**。

### 测试夹具路径跳过

测试文件中的 `C:\nonexistent_file_12345.png`、`E:/other/data.csv`、`Z:/nonexistent/file.pdf` 是明显假路径，应通过 `[sensitive_line_skips]` 跳过：

```toml
[sensitive_line_skips]
"tests/test_xxx.py" = [
  'C:[\\/]+nonexistent',
  'E:[\\/]+other',
  'Z:[\\/]+nonexistent',
]
```

**踩坑**：源码中字符串字面值的 `\\` 是两个字符（反斜杠 + 反斜杠），不是转义后的一个反斜杠。`[\\/]` 只匹配一个反斜杠，必须用 `[\\/]+` 或 `[\\\\/]` 才能匹配双反斜杠。

## Phase 3：用户审批 + build 阶段

### 批准前报告

在请用户批准构建之前，产出包含以下内容的报告：

1. 最终 `file_entries` 数量和文件清单（或按目录汇总）。
2. 含 TRUE_POSITIVE 或 AMBIGUOUS 命中的文件——含行号的完整清单。
3. 仅含 FALSE_POSITIVE 命中的文件——每文件一行摘要。
4. 按规则分组的命中数量。
5. 待验证项：无 key 启动测试、冒烟测试（`build_release` 会自动跑）。
6. `plan_digest`（用户审批的凭据）。

### 批准关卡

用户必须明确批准：
- 最终 `file_entries` 清单。
- 每条 TRUE_POSITIVE/AMBIGUOUS 命中的解决方案（移除组件 vs. 添加 line_skips vs. 接受原样）。
- 把 `[approval].plan_digest` 等字段填入 profile（通过 `compute-digest` 命令获取）。
- 把 `status` 翻转为 `ready`、`build_enabled` 翻转为 `true`（语义信号，沿用 P0 习惯；实际 fail-closed 由 `[approval].plan_digest` 非空保证）。

仅在批准后才继续 build。

### compute-digest：获取审批凭据

```bash
# 计算 plan_digest 等各 digest，输出到 stdout 供操作员复制
uv run python -m tools.release.cli compute-digest --profile friend-full
```

输出（stdout）含可直接复制到 `[approval]` 段的 TOML 片段：

```toml
[approval]
plan_digest = "<64-char SHA-256>"
profile_digest = "<64-char SHA-256>"
components_digest = "<64-char SHA-256>"
scan_digest = "<64-char SHA-256>"
exemptions_digest = "<64-char SHA-256>"
approved_at = "<ISO 8601 时间，如 2026-07-31T12:00:00+08:00>"
approved_by = "<审批人姓名>"
```

操作员复制这段 TOML 到 `release/profiles/friend-full.toml`，填好 `approved_at` 和 `approved_by`，然后跑 `build`。

### build：构建产物

```bash
# 从 release/plans/<plan_digest>.json 读 plan + 从 profile [approval] 读 approval → 构建产物
uv run python -m tools.release.cli build --plan <plan_digest>
```

`build_release` 内部完成 13 步（详见 `tools/release/engine/build.py`）：

1. 验证 `approval.plan_digest == plan.plan_digest`（digest-bound）
2. 验证当前 git commit == `plan.source_commit`（防源码漂移）
3. 验证当前 profile digest == `plan.profile_digest`（防 profile 漂移）
4. 复制源文件到 `release/staging/<plan_digest>/`（按 `plan.file_entries`，仅 tracked 文件）
5. 应用 path_mapping（大小写不敏感字面替换）
6. 生成 `DEPLOYMENT.md` / `RELEASE_NOTES.md`（Jinja2 模板，**无 zip_size 字段**——zip_size 仅在外部 `.zip.sha256` 中体现）
7. 生成 `MANIFEST.json` + sidecar（生成文件 `DEPLOYMENT.md` / `RELEASE_NOTES.md` 也纳入清单，sha256 在 path_mapping 后重算）
8. 跑构建期 gates（no-key-startup / activity-task-safe-stop / focused-tests / archive-verification）
9. 生成 ZIP（**只打包一次**，无预览 ZIP）
10. archive-verification gate（ZIP 重新打开比对每个文件的 sha256）
11. 任一 gate 失败 → raise + 失败 ZIP 改名 `.failed.zip` 留 staging
12. 成功 → 原子移动到 `release/dist/localagent-<profile_id>.zip`
13. 写外部 `.zip.sha256` sidecar（含 zip 的 sha256）

**关键约束**：
- build 信任 prepare 的静态 gates 结果（`plan.gates_results`），**不重跑静态 gates**
- ZIP 只打包一次（spec Anti-Cheat 硬约束），无预览 ZIP
- 失败产物绝不进入 `dist/`（P0-7 行为保证）

### 依赖映射表与裁剪（2026-07-21 引入）

**问题**：`pyproject.toml` 聚合所有 workspace 模块依赖，导致 release 包导出时把不需要的依赖也带上了（如朋友不要 stock_advisor 但 pyproject.toml 仍含 akshare/pandas）。

**解决原则**：脚本机械维护依赖映射表，agent 不手动编辑。agent 只起触发检查/输入参数的作用。

#### 三步工作流

1. **生成映射表**（每次 pyproject.toml 或 workspace 模块变化后跑）：
   ```bash
   uv run python tools/release/generate_dependency_map.py
   ```
   - 扫描 `pyproject.toml` 的 37 个依赖在 git-tracked .py 文件中的 import 使用
   - 用 Python re 模块 + `re.MULTILINE` flag 搜（POSIX ERE 不支持 `\b` 和 `(?:...)`）
   - 分类为 `core` / `workspace_only` / `tools_only` / `tests_only` / `unused`
   - 输出 `release/dependency_map.toml`（机器可读）+ `release/dependency_audit.json`（详细报告）+ stdout 摘要

2. **检查 profile 裁剪建议**（修改 friend-full.toml 后跑）：
   ```bash
   uv run python tools/release/generate_dependency_map.py --check-profile friend-full
   ```
   - 列出冗余依赖（`workspace_only` 但 required 模块未 include）
   - 区分 unused 类中"真的可移除"（notes 标注可移除）vs "建议保留"（notes 说明间接依赖/辅助包）
   - 只读检查，不修改任何文件

3. **手动应用裁剪**（release 时）：
   - **v2 engine 暂未自动集成 pyproject.toml 裁剪**——`build_release` 不调用 `trim_pyproject_dependencies()`
   - 操作员需根据 `--check-profile` 输出，手动编辑 `pyproject.toml` 移除冗余依赖（unused + notes 含"可移除"字样 + workspace_only 且 required 模块未 include）
   - 行级文本过滤，保留 `[tool.uv.sources]`、`[tool.ruff]` 等其他配置
   - 修复末尾多余逗号，确保 TOML 仍可被 `tomllib` 解析
   - 修改 `pyproject.toml` 后必须重跑 `prepare`（pyproject.toml 是 tracked 文件，digest 变化会让 plan_digest 变化，使旧 approval 失效）

#### 人工 notes（KNOWN_NOTES 常量）

脚本无法识别的场景（需在 `generate_dependency_map.py` 的 `KNOWN_NOTES` 字典补充）：
1. 未 git-tracked 的 workspace 模块（如 `stock_advisor` 开发中未提交，akshare/pandas 是其依赖）
2. 框架间接依赖（如 `python-multipart` 被 FastAPI 用于 form 解析）
3. 通过 importlib / 字符串引用的包
4. 辅助生态包（transformers 自动加载 `accelerate`）

**重要**：只有 notes 中含"可移除"字样的 unused 包才会被裁剪建议。当前可移除：`pyperclip`。

详见 `docs/release-policy.md` 的"依赖映射表与裁剪"章节。

**导出后人工验证**（engine 自动跑的 gates 之外，agent 或用户可手动追加）：
- 跑 `uv run python -m pytest tests/ -v --tb=short` 全量测试（engine 的 focused-tests gate 只跑 `tests/test_release_policy.py` 子集）
- 扫描敏感字符串残留：用 `exec_python` 写扫描脚本检查 staging 目录中是否还有个人路径、私有组件引用等（参考本轮扫描：311 文件 0 命中为通过标准）

**剥离 Git 历史**：`build_release` 是扁平文件拷贝到 staging，不是 `git clone`，天然不含 `.git` 历史。

### 发布说明内容

`DEPLOYMENT.md` / `RELEASE_NOTES.md` 由 Jinja2 模板渲染（`tools/release/templates/*.j2`），含：

- 许可证（Apache License 2.0）和必要声明。
- 环境要求（Windows、CUDA 12.6、Python 3.11+ 等）。
- key 配置说明（`keys.json` 放在哪、支持哪些 provider）。
- 已知限制（无私有仓库备份功能、不打包 weights 等）。
- 文件清单参考。
- 占位符映射表（来自 `[deployment_mapping.required_mappings]`）。

## 踩坑（来自真实发布周期）

### fnmatch 的 `*` 跨 `/`（仅历史参考，v2 不再用 fnmatch）

Python 的 `fnmatch.fnmatchcase('a/b/c', 'a/*.py')` 返回 True，因为 `*` 匹配任意字符
包括 `/`。这会破坏 `workspace/wuwa_gacha/*.json` 这类单层 glob 规则。

v2 engine 用 `audience.export_set` + manifest 的 `exports.source.files` 列表选择文件，不再用 fnmatch 做路径分类。但写 path_mapping 规则时若用 glob 库，仍需注意此问题。

### UID 命名文件需用字符类 glob

`workspace/bilibili_gacha/227446513.json` 以 Bilibili UID 命名。要只匹配数字文件名而不匹配
`bilibili_gacha.py`，用 `workspace/bilibili_gacha/[0-9]*.json`。manifest 的 `exports.source.files` 列表中可用此模式。

### 带时间戳的个人数据文件名每轮都变

`workspace/wuwa_gacha/wuwa_gacha_raw_20260614_145515.json` 每次发布时间戳都不同。
字面 `exclude_paths` 条目每轮都得改写。用 `workspace/wuwa_gacha/*.json`（glob）一次性
匹配该目录下所有 JSON 文件，或直接不把 `wuwa_gacha` 加入 audience components。

### `.gitignore` 不是被追踪文件安全的证据

`data/` 在 `.gitignore` 中，但若 `data/` 下的文件曾被 `git add -f`，它仍是追踪状态。
判断真实追踪集始终用 `git ls-files`，不要用 `ls` 或 `git status`。v2 engine 内部用 `git ls-files` 固定源码快照。

### `docs/release-policy.md` 和 `release/` 自身被排除

发布基础设施（policy、profiles、audience、release 工具、本 Skill）描述了私有排除项，因此不在 friend
产物中发布。它们在 `friend-full.toml` 的 path_mapping / literal 规则中被脱敏（把 `system.public_distribution` / `public-release` 等内部标识符替换为通用文本）。不要"修复"成包含它们。

### 审计期间用户改动

若用户在审计中途提交或非追踪文件变化，清单会过期。最终批准前重新运行 `prepare`，
打包前再运行一次。把审计当作快照，不是缓存。

注：`build_release` 内部独立验证 `git HEAD == plan.source_commit`，若用户在 prepare 后又 commit，build 会 fail closed 并提示 "source commit drifted"。

### 敏感内容扫描绝不回显值

即便在调查特定命中时，也不要把匹配值粘贴到对话或 WIP 的 `next_steps` 中。描述为
"第 N 行含真实 Bearer token"或"第 N 行是占位符"。用户可自行 `Read` 文件查看。

### 替换规则需覆盖 4 种字面形式 + 大小写变体

源码中 `F:\<project_root>` 路径会以 4 种形式出现（单反斜杠大写盘符、单反斜杠小写盘符、双反斜杠转义、正斜杠），加上 `localAgent` vs `localagent` 大小写共 8 个变体。漏写任一变体都会残留命中。

v2 path_mapping 用 `re.sub` + `re.IGNORECASE` 大小写不敏感字面匹配，单条规则 `find = "project_temp\\localagent"` 能覆盖 `localAgent` / `localagent` 大小写变体。但 4 种字面形式（单反斜杠 / 双反斜杠 / 正斜杠 / 转义）仍需在 path_mapping 表中分别覆盖——`re.sub` 不解析路径，只做字面子串匹配。

**第一轮**只写了 4 个变体（大写 `localAgent` + 4 形式），扫描后剩 33 个命中——其中 5 个是小写 `localagent`、20+ 个是转义反斜杠形式。**第二轮**补齐 path_mapping 规则覆盖全部字面形式后，命中清零。

**教训**：写 path_mapping 规则前，先用 `exec_python` 在源码中 `grep` 所有路径变体，列出全部字面形式。不要凭印象写。

### PowerShell 引号转义破坏 Python `-c` 一行命令

`.venv\Scripts\python.exe -c "import json; d=json.load(open('temp/scan.json',encoding='utf-8')); ..."` 在 PowerShell 中触发 `SyntaxError: unterminated string literal`。PowerShell 对双引号包裹的字符串中的 `\"`、`\\` 解释不一致。

**规避**：写临时脚本到 `temp/` 后用 `RunCommand` 执行，不要用 `-c` 一行命令。

### PowerShell 重定向默认写 UTF-16 BOM

`.venv\Scripts\python.exe ... > temp\scan.json` 写出的文件是 UTF-16 LE BOM 编码，Python `open(..., encoding='utf-8')` 读取报 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0`。

**规避**：在 Python 脚本内 `open('temp/scan.json', 'w', encoding='utf-8').write(...)` 主动写 UTF-8，不要用 PowerShell `>` 重定向。v2 CLI 的 `prepare` / `scan` / `build` 命令直接打印 stdout，不需要重定向——直接读 stdout 即可。

## 长期要求

以下要求适用于每个发布周期，无例外：

1. **每轮重新运行 prepare + scan。** workspace 漂移、新增密钥、新增私有引用——这些都不缓存。
2. **每轮把默认 workspace 决策呈现给用户。** 尽管 8 个子项目默认排除，仍要呈现清单并询问"仍排除？"——正常情况下被排除的子项目可能意外需要发布（如某工具变成通用工具）。
3. **绝不读取 `USER_ONLY_PROJECT_OPERATIONS_AGENT_DO_NOT_READ_OR_EDIT.md`。** 它是 hard-exclude。文件名即指令。
4. **`status` 为 `draft` 或 `reserved` 时绝不构建。** 仅 `ready` 可构建。
5. **`build_enabled` 为 `false` 时绝不构建。** 用户最终批准后才翻转。
6. **绝就地脱敏规范树。** `build_release` 复制源文件到 staging 后才应用 path_mapping；私有仓库保持原样。
7. **绝不包含 `.git` 历史。** `build_release` 是扁平文件拷贝到 staging。
8. **绝不包含 `data/`、`memory/`、`weights/`、`chrome_debug/`、`.venv/`、`temp/`、
   `planning_notes/`、`references/`、`.agents/wip/`、`.trae/`。**
   这些目录不被 audience components 的 `exports.source.files` 列出，自然不会进入 file_entries。
9. **保持 `system.release` 和 `system.public_distribution` 分离。** 前者给 CHANGELOG 版本号并提交私有仓库；后者产出隐私安全的产物。不要混淆。
10. **每个新子项目决策记录到三处**：`release/audience/friend.toml`（components）+ `release/profiles/friend-full.toml`（path_mapping / line_skips）、`docs/release-policy.md`（原因）、`CHANGELOG.md`（审计轨迹）。
11. **新增规则类别或扫描模式时更新本 Skill。** 未来的自己不会记得 regex 约定；在 Phase 2 文档化它们。
12. **每轮 release 前必须用 `scan` 重跑 Phase 2 验证替换规则仍清零。** 源码会持续新增路径引用（如新工具、新 workspace 子项目、新个人数据盘符），path_mapping 规则表会逐步失效。验证标准：scan 命中中 `local-absolute-path` 的 HIGH 命中为 0，MEDIUM 命中均由 path_mapping 表中规则覆盖（人工核对）。命中残留时必须先补全 `[content_replacements.path_mapping]` 再进入 Phase 3。
13. **新增路径相关源码后必须重新 grep 路径变体并补全替换规则。** 写 path_mapping 规则前先用 `exec_python` 在源码中 `grep` 所有路径变体（4 种字面形式 × 大小写），列出全部字面形式再写规则——不要凭印象写。每条概念需覆盖 4 形式（单反斜杠 / 双反斜杠 / 正斜杠 / 转义），遗漏任一形式都会残留命中。
14. **`[content_replacements.path_mapping]` / `[[content_replacements.literal]]` 和 `[deployment_mapping.required_mappings]` 必须同步。** 每个新增占位符（如 `<new_data_root>`）必须同时出现在两处：path_mapping 或 literal 替换规则表（定义 find→replace 字面规则）+ 部署映射表（定义占位符的 meaning 和 example）。`build_release` 渲染 `DEPLOYMENT.md` 时读取部署映射表，部署者按此反向映射到自己机器。占位符单边存在会导致脱敏产物无法部署。
15. **scan MEDIUM 命中必须由 path_mapping 覆盖，build 阶段不再重跑 scan。** prepare 阶段的 scan 在源码原状上跑，看到所有真实路径引用（MEDIUM 命中）。这些命中必须由 `[content_replacements.path_mapping]` 中的规则覆盖——否则 build 阶段应用 path_mapping 后，未覆盖的路径会以原值进入 ZIP，泄漏个人数据。`build_release` 信任 prepare 的 scan_digest，不重跑 scan，所以 scan 阶段的命中清零（或全部由 path_mapping 覆盖）是 release 安全的硬前提。
16. **私有组件必须通过动态加载机制隔离。** 主代码库（`server/`）不得硬编码引用任何 workspace 私有组件的类名、函数名、配置段名或 task_type。组件通过 `workspace/<component>/loop_actions.py` 导出三个约定接口（`register(manager)` / `LOOP_TASK_DEFS` / `GUIDE_REGISTRY_ENTRIES`），主代码库 3 个文件（`loop_actions.py` / `loop_manager.py` / `agent_guide.py`）通过 `_load_optional_*` 函数用 `pkgutil.iter_modules` 动态发现并加载。新增私有组件时：在 `workspace/<component>/` 实现功能 + 导出接口，主代码库自动发现，无需修改。移除组件时：删除 `workspace/<component>/` 目录，主代码库自动跳过。此模式使主代码库保持"零私有引用"，Phase 2 扫描自然清零。
17. **导出文档永远使用中文。** `DEPLOYMENT.md` / `RELEASE_NOTES.md` 等所有面向接收者的导出文档模板**永远使用中文**——包括标题、章节、说明、FAQ、步骤、表格表头等所有自然语言文本。这是项目的长期约定，因为发布受众为中文用户。v2 engine 用 Jinja2 模板（`tools/release/templates/*.j2`），模板已固化为中文；新增模板或修改现有模板时也必须使用中文。Profile 的 `description` 字段也使用中文。代码块中的命令、路径、变量名等保持原样（不翻译），但代码块前后的说明文字仍用中文。

## 当前 Profile

- `friend-full`：保留当前依赖、活动追踪和每日总结行为。不提供 key。发布前验证缺 key 启动和 Loop 任务安全停止。
- `public`：仅预留。在所有被追踪路径和第三方许可证审阅完毕前不构建（`release/audience/public.toml` 仅 `status = "reserved"`，phase 3 实现）。

## 硬性规则

- 应用 Apache License 2.0 并保留每行 `Required Notice:`。
- 允许商用、修改和再分发。
- 排除私有仓库备份代码/配置、密钥、浏览器配置、运行时数据、个人 workspace、WIP 文档、本地绝对路径和真实截图/活动日志。
- 使用白名单（audience components 的 `exports.source.files`）。`.gitignore` 不能让已追踪文件变安全。
- 保持 `system.release` 分离：它给 CHANGELOG 版本号并提交私有仓库；
  `system.public_distribution` 管控隐私安全的源码产物。
- `draft` 或 `reserved` profile 不可构建。
- `plan_digest` 不匹配的 approval 不可构建（digest-bound，P0-3 行为保证）。

## 失败处理

- prepare 静态 gate 失败：报告 gate 名称 + details，profile 保持原状（仍写 plan 文件，但 plan_digest 不变；旧 approval 自然失效）。
- 敏感内容 HIGH 命中：只报告规则、路径和行号；绝不回显匹配值。
- 缺 API key：`build_release` 的 no-key-startup gate 验证优雅启动和任务暂停/停止；不添加或分发 key。
- build 构建期 gate 失败：失败 ZIP 改名 `.failed.zip` 留 staging（`release/staging/<plan_digest>/`），不进入 `dist/`。
- source commit 漂移：`build_release` raise "source commit drifted"，提示重新跑 prepare。
- profile 漂移：`build_release` raise "profile drifted"，提示重新跑 prepare + compute-digest + 重填 [approval]。
- 审计期间用户改动：批准前和打包前刷新清单（重跑 prepare）。

## 输出

批准前只输出审计报告（plan_digest + file_entries + scan 命中清单）。批准且 build 成功后输出：

- 不含 Git 历史的源码 ZIP（位于 `release/dist/localagent-<profile_id>.zip`）；
- SHA-256 校验和（外部 sidecar `release/dist/localagent-<profile_id>.zip.sha256`）；
- 确切的文件清单（ZIP 内 `MANIFEST.json` + `MANIFEST.json.sha256`）；
- 含许可证、环境、key 配置和已知限制的发布说明（ZIP 内 `DEPLOYMENT.md` / `RELEASE_NOTES.md`，中文）；
- plan 文件（`release/plans/<plan_digest>.json`，审计追溯凭据）。
