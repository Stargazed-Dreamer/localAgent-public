# 源码分发策略

本文档是创建隐私安全的 LocalAgent 源码分发的维护计划。私有仓库仍是规范开发树。发布产物从显式 profile 生成，绝不通过删除或清理规范树就地产生。

## 当前决策

- 受众：可信赖的朋友。
- 当前目标 profile：`friend-full`（目前为 draft 状态，不可构建）。
- 许可证：Apache License 2.0。
- 允许商用、修改和再分发。
- 分发格式：不含 `.git` 历史的源码 ZIP。
- API key、浏览器配置、运行时数据与已安装依赖绝不打包。
- `pyproject.toml` 和 `uv.lock` 中的全部依赖声明予以保留。
- 对于 friend profile，活动追踪与每日总结行为保持不变。

机器可读的真源是 `release/policy.toml` 和 `release/profiles/<profile>.toml`。本文档解释其意图但不覆盖它们。

## Profile 生命周期

Profile 使用三种状态：

- `draft`：分类与校验未完成；禁止构建。
- `ready`：所有必需检查通过且用户批准构建。
- `reserved`：为未来工作预留；禁止构建。

`build_enabled` 在发布审计完成前必须保持 `false`。将其改为 `true` 需用户在审阅确切文件清单与发现后做出明确决策。

## 分类规则

使用白名单。每个被追踪的路径必须归入以下之一：

- 在选定 profile 中包含（include）；
- 在包含前审阅（review）；
- 从选定 profile 中排除（exclude）；
- 对所有 profile 强制排除（hard exclude）。

强制排除始终优先于 profile include。绝不包含密钥、运行时状态、本地浏览器配置、个人活动数据、私有仓库列表、个人 WIP 文档或 Git 历史。`.gitignore` 不能作为某文件安全的证据，因为已被追踪的文件仍留在 Git 中。

## Friend-Full 边界

首次发布保留现有的 Windows/GPU 依赖声明和现有的活动追踪行为。不分发任何 provider key。在 profile 变为 ready 之前，需校验空 key 仓库仍允许后端启动，且依赖 key 的 Loop 任务能安全停止而不反复失控失败。

GitHub 仓库备份仅限私有。决策 A 于 2026-07-17 确认：friend 产物不得包含该功能的任何实现、配置、Skill、文档或可发现引用。仅排除其 workspace 是不够的。需将运行时与 Agent Guide 注册隔离在可选的私有组件之后，生成不含私有章节的对外配置与文档，并执行最终的禁用标记扫描。

发布策略文件、发布审计工具和 `public-release` Skill 是内部构建管控件。它们描述了私有排除项，因此不在 `friend-full` 中分发。

`server/`、`client/`、`workspace/`、`.agents/`、文档、测试和工具目录可能在同一棵树中混有私有功能引用、可复用源码、个人数据或本地路径。它们保持在 `review_before_include` 中，直到被追踪文件清单以更细粒度分类。特别是，仅排除私有组件的 workspace 目录是不够的，因为 Agent Guide、Loop 实现、配置与文档也引用了该功能。

## 默认 Workspace 模块决策

以下 workspace 子项目决策最初于 2026-07-18 确认，2026-07-21 更新（web_archive + niuke_review 升级为部分保留；5 个抽卡模块整体关闭），编码在 `release/profiles/friend-full.toml` 中。它们是今后每次 `friend-full` 发布的默认基线。**发布工作流仍需在每轮把它们呈现给用户确认**，因为 workspace 内容会漂移，正常情况下被排除的子项目可能意外需要发布（或反之）。

### 默认排除子项目（仅个人使用，绝不发布）

- `workspace/yihuan_simulator/`
- `workspace/yihuan_gacha/`
- `workspace/community_review/`
- `workspace/image_organize_remote_vl_sample/`
- `workspace/api_tests/`
- `workspace/official_gacha/`（2026-07-21 升级为整体排除）
- `workspace/wuwa_gacha/`（2026-07-21 关闭）
- `workspace/arknights_gacha/`（2026-07-21 关闭）
- `workspace/endfield_gacha/`（2026-07-21 关闭）
- `workspace/bilibili_gacha/`（2026-07-21 关闭）
- `workspace/disk_manager/system_rebuild/`（77 条个人系统重建路径，无法逐行脱敏）

### 默认包含子项目（通用，无个人数据）

- `workspace/__init__.py`
- `workspace/_shared/`（共享卡池注册表与游戏配置）
- `workspace/disk_manager/`（系统重建指南；`system_rebuild/` 子目录单独排除）
- `workspace/dev_toolkit/`（开发者工具包：19 个 skill 蓝图 + 6 个主题文档）

### 部分保留子项目（保留脚本/文档，丢弃个人记录）

对以下每个子项目，profile 保留 `.py` 脚本，但排除个人数据文件（`.json` / `.md` 报告 / `.pdf` / `.obsidian/`）。排除使用 glob 规则，即使个人数据文件名包含时间戳，也能在多次发布间保持稳定。

- `workspace/accounting/` — 保留 `*.py`、`*.html`、`*.bat`；丢弃 `name_mapping.json`、`review_config.json`（`bill_review.txt` / `bill_review_data.json` 已挪到 `private_vault/accounting/`，整个 `private_vault/` 被 .gitignore 排除，release 时天然不进包）
- `workspace/web_archive/` — 保留 `extract_web.py`、`extract_feishu.py`、`analyze_comments.py`、`analyze_sample.py`（4 个通用网页提取脚本）；丢弃 `.obsidian/`、`*.json`、`report.md`（含已抓取的小黑盒/微信/飞书帖子标题、URL、时间戳与个人书签 URL）。另有两个回归 harness `feishu_harvest_baseline.py` / `feishu_render_offline.py` 登记在组件 `manifest.toml` 的 `[exports.source]`，但**不进 include_paths**：它们靠与本目录已存档 MD 逐字节比成立，而那些 MD 属个人数据已被排除，朋友拿到也无对象可比
- `workspace/niuke_review/` — 保留 `nowcoder_review.py`、`nowcoder_embedded.py`（2 个牛客网面经抓取/审核脚本）；丢弃 `*.json`（4 个时间戳 + merged/summarized/progress）、`saved_pdfs/`（5 个含公司名的个人求职 PDF）

### Dotfile 决策

- `.dcg/` 和 `.dcg.toml` 默认保留（命令守卫是对朋友有用的安全层），但打包前需脱敏：剥离本地绝对路径和项目私有规则引用。

## 按需 Workspace 模块开关

某些 workspace 子项目在多次发布间需要按需开启或关闭。为避免每次重新设计脱敏规则，本节记录每个可开关模块的脱敏决策表。**切换开关 = 在 `friend-full.toml` 的 `include_paths` 中注释/取消注释对应 `.py` 行 + 在 `exclude_paths` 中调整整体前缀**。无需新增 `content_replacements` 规则——所有模块的个人路径已被现有 8 变体替换规则覆盖。

### 决策表

| 模块 | 启用时 include | 启用时 exclude（个人数据） | 需要 content_replacements | 当前状态 |
|------|---------------|---------------------------|--------------------------|---------|
| `web_archive` | `extract_web.py` / `extract_feishu.py` / `analyze_comments.py` / `analyze_sample.py` | `.obsidian/`、`*.json`、`report.md` | 无（脚本中 `<project_root>\chrome_debug` 由现有 8 变体规则覆盖；替换后含 `<placeholder>` 被 skip_patterns 跳过。`extract_feishu.py` 已核对：无硬编码绝对路径、无个人 doc token，`/space/api/box/stream/download` 为通用 API 路径） | **开启** (2026-07-21) |
| `niuke_review` | `nowcoder_review.py` / `nowcoder_embedded.py` | `*.json`（4 个时间戳 + merged/summarized/progress）、`saved_pdfs/`（5 个含公司名 PDF） | 无（脚本完全使用 `Path(__file__).parent.parent.parent / "workspace" / "niuke_review"` 相对路径） | **开启** (2026-07-21) |
| `dev_toolkit` | 整个目录 | `dev_toolkit_v2.zip`（历史打包产物） | 无（`.md` 中 `<project_root>\` 引用由现有规则覆盖；`skill_creator.md` 中 "Alice" 示例有专门规则） | **开启** (2026-07-21) |
| `wuwa_gacha` | `*.py`（脚本） | `raw/`、`*.json`（个人抽卡记录） | 无 | **关闭** (2026-07-21 朋友说不要) |
| `arknights_gacha` | `*.py`、`bwiki_pools*.json`、`prts_limited_pools.json`、`pool_registry.json`、`小黑盒界面*.md/jpg` | `raw/`、`gacha_records.json`、`raw_full.json`、`summary.json` | 无 | **关闭** (2026-07-21 朋友说不要) |
| `endfield_gacha` | `endfield_gacha.py` | `char_records.json`、`raw/`、`summary.json`、`weapon_records.json` | 无 | **关闭** (2026-07-21 朋友说不要) |
| `bilibili_gacha` | `*.py`、`checklist.md` | `[0-9]*.json`（UID 命名）、`api_status_check_*.json`、`lottery_check_*.json`、`scan_*.json`、`up.json`、`unfollow_suggestions.md` | 无 | **关闭** (2026-07-21 朋友说不要) |
| `official_gacha` | — | 整个目录 | — | **关闭**（一直关闭） |

### 切换操作步骤

#### 关闭一个开启的模块

1. 在 `friend-full.toml` 的 `include_paths` 中注释掉对应模块的 `.py` 行。
2. 在 `exclude_paths` 中加入 `"workspace/<module>/"` 整体前缀。
3. （可选）把原细分 exclude 规则改为注释，以便未来重新开启时直接复用。
4. 重置 `status = "draft"`、`build_enabled = false`、`review_scan_cleared_at = ""`。
5. 重新运行 Phase 1 + Phase 2 验证。

#### 开启一个关闭的模块

1. 在 `friend-full.toml` 的 `include_paths` 中取消注释对应模块的 `.py` 行（或新增精确 include 规则）。
2. 在 `exclude_paths` 中移除 `"workspace/<module>/"` 整体前缀。
3. 取消注释对应的细分 exclude 规则（保留 `.py` / 排除个人数据）。
4. 重置 `status = "draft"`、`build_enabled = false`、`review_scan_cleared_at = ""`。
5. 重新运行 Phase 1 + Phase 2 验证。
6. 在本节决策表的"当前状态"列更新日期和说明。

### 验证标准

切换开关后必须重跑 v2 compiler CLI（spec-v2-compiler.md）：

```bash
# prepare 阶段含 manifest-audit / tracked-file-inventory / sensitive-content-scan / policy-schema 四个静态 gate
uv run python -m tools.release.cli prepare --profile friend-full --audience friend

# 独立运行 sensitive-content-scan 打印详细命中（不回显匹配值）
uv run python -m tools.release.cli scan --profile friend-full
```

`prepare` 通过（exit 0）后才能进入用户审批和构建。

## 发布工作流（v2 compiler，2026-07-31 起）

新引擎将 release 流程拆为 prepare + build 两阶段（spec-v2-compiler.md），所有入口经 `tools/release/cli.py`：

1. 读取 `release/policy.toml`、`release/profiles/<profile>.toml`、`release/audience/<audience>.toml`。
2. 检查 `git status`，确认源码状态。
3. `python -m tools.release.cli prepare --profile <id> --audience <name>`
   - 固定 source_commit（HEAD → git rev-parse HEAD）
   - 按 audience.export_set 选定组件的 exports.source.files / exports.runtime.files
   - 与 `git ls-files` 取交集（P0-1 tracked-only 保证）
   - 跑 4 个静态 gate：policy-schema / tracked-file-inventory / sensitive-content-scan / manifest-audit
   - 计算 plan_digest（绑定 profile/components/scan/exemptions/source_commit/file_entries）
   - 写 `release/plans/<plan_digest>.json`
4. `python -m tools.release.cli scan --profile <id>` 单独查看敏感扫描结果（可选，不回显匹配值）。
5. 用户审阅 plan 文件 + 扫描结果；明确批准后才进入 build。
6. `python -m tools.release.cli compute-digest --profile <id>` 复制 plan_digest 到 `[approval]` 段并填 approved_at / approved_by。
7. `python -m tools.release.cli build --plan <plan_digest>`
   - 验证 approval.plan_digest == plan.plan_digest（P0-3 digest-bound）
   - 验证 profile_digest / source_commit 未漂移
   - 复制文件到 staging，应用 path_mapping（脱敏本地路径）
   - 跑 build-time gate：no-key-startup / activity-task-safe-stop / focused-tests / archive-verification
   - 生成 ZIP + MANIFEST.json + DEPLOYMENT.md + RELEASE_NOTES.md（Jinja2 模板，无 zip_size 字段）
   - archive-verification 重开 ZIP 比对 sha256（P0-6）
   - 失败产物改名 `.failed.zip` 留在 staging，dist/ 绝不收（P0-7）
   - 成功原子移动到 `release/dist/`，附 `.zip.sha256` sidecar

> 注：v2 compiler engine 目前未自动裁剪 pyproject.toml 冗余依赖（旧 `export_release.py --trim-pyproject` 已随 T18 删除）。若需裁剪，按"依赖映射表与裁剪"章节手动应用 `generate_dependency_map.py` 的报告后重新计算 sha256 入 MANIFEST.json。

## 一键编排与持久副本发布（2026-09-12 起）

> 设计：`temp/sdd/release-orchestrator/design.md`。目标是把每轮发布的人工动作收敛为"跑一条命令、看一份增量报告、确认一次"，并让 public 仓库保留版本演进历史。

### release 子命令（两阶段编排）

```bash
# 第一阶段：prepare + 增量分诊报告（不写快照、不填 approval）
#   NEW 命中非空 → exit 2，人工 Read 核对后更新 profile 再重跑；
#   干净 → exit 0 并打印第二阶段命令
uv run python -m tools.release.cli release --profile public-full

# 第二阶段：快照 scan_digest 锚定校验 → 自动填 [approval] → build →（可选）publish → 写快照
uv run python -m tools.release.cli release --plan <plan_digest> --approve [--publish] [--tag v0.46.0] [--dry-run]
```

增量分诊机制：命中按 `(path, rule) → count` 与上一轮**批准快照**（`release/triage/<profile_id>.json`，gitignore）diff——count 未增自动放行（carried），新 key 或 count 增列为 NEW 必须人工看，消失的标 resolved。快照只存 path/rule/count + scan_digest，**绝不存行内容与行号**。第二阶段用 `snapshot.scan_digest == plan.scan_digest` 锚定"批准的确实是这个 plan 的源码"，不匹配即拒绝。digest-bound 审批语义不变（`_verify_profile_digest` 已废弃，自动填 approval 不触发 profile 漂移）。

### publish 持久副本（append-only 历史）

`publish` 不再"临时目录 git init + force push"，改为本地持久 clone（`release/public_repo/`，gitignore）：

1. 首次 `git clone` 远端——**远端已有 commit 即历史基线**（当前 public 仓库的唯一初始 commit）。
2. 每次发布：清空工作树（保留 .git，校验 origin 指向 `public_repo_url` 防误删）→ 同步 staging（全量，含删除）→ 自动生成/更新 `RELEASE_HISTORY.md` → `git add -A`（git 自动算出真实版本间 diff）→ 一个 commit（`Release <版本> (plan <digest12>)`）→ 可选 tag → push **不带 --force**。
3. public 历史 = 脱敏快照序列 + tag + 真实 diff，append-only；远端领先时 push 被拒（有意性质，历史不可强推重写）。

### RELEASE_HISTORY.md（发布历史叙事）

每次 publish 自动更新，倒序章节：版本号、相对上次发布的私有仓库 commit 数与日期区间（从上一 commit 的 `release-plan.json.source_commit` 统计）、该版本 CHANGELOG 条目标题。**取材纪律**：CHANGELOG.md 本身在发布物 core_files 中、已过 scan，取材零增量风险；绝不逐条复制 private commit message（未经 scan）。commit 数 + 日期是纯元数据，无泄露风险。

### 教训：core_files_exclude 路径漂移（2026-09-12 修复）

8462d31「测试文件大整理」把 `tests/test_release_{v4,policy}.py`、`tests/test_gh_mirror_release.py` 移入 `tests/release_ci/`，但两个 audience 的 `core_files_exclude` 仍写旧路径——glob 失配导致 `test_release_v4.py`（故意构造敏感样本的文件）进入 file_entries，sensitive-content-scan 以 10 条 HIGH 正确拦截。两个 toml 的三条路径已改为 `tests/release_ci/` 前缀。**移动或重命名被 exclude 的文件时必须同步 audience toml**；`release` 编排第一阶段会在发布前把这类漂移拦下来。

## 依赖映射表与裁剪

### 问题背景

`pyproject.toml` 的 `[project].dependencies` 聚合了所有 workspace 模块的依赖，导致：
- 某些依赖只被 workspace 模块用（如 `akshare`/`pandas` 只被 `workspace/stock_advisor/` 用），但朋友不需要该模块时也得装
- 某些依赖是框架间接依赖（如 `python-multipart` 被 FastAPI 自动加载，代码里不显式 import），脚本误判为未使用

### 解决方案：机械映射表 + 行级裁剪

**核心原则**：脚本机械维护依赖映射表，agent 不手动编辑。agent 只起触发检查/输入参数的作用。

#### 三步工作流

1. **生成映射表**：`uv run python tools/release/generate_dependency_map.py`
   - 扫描 `pyproject.toml` 的 37 个依赖
   - 用 `git ls-files` 拿 git-tracked .py 文件清单（与 release 真源一致）
   - 用 Python re 模块（`re.MULTILINE` flag）搜 import 语句
   - 按使用位置分类：`core` / `workspace_only` / `tools_only` / `tests_only` / `unused`
   - 输出三个文件：
     - `release/dependency_map.toml` — 机器可读映射表（profile 裁剪依据）
     - `release/dependency_audit.json` — 详细使用位置报告
     - stdout 摘要
   - 人工补充的 notes 通过修改脚本的 `KNOWN_NOTES` 常量（不是手动编辑 dependency_map.toml）

2. **检查 profile 裁剪建议**：`uv run python tools/release/generate_dependency_map.py --check-profile friend-full`
   - 列出冗余依赖（`workspace_only` 但 required 模块都未 include 的）
   - 列出 unused 类中"真的可移除"（notes 标注可移除）和"建议保留"（notes 说明间接依赖/辅助包）的
   - 不修改任何文件，只读检查

3. **手动应用裁剪**（v2 compiler engine 暂未自动集成）：
   - 按 `--check-profile` 报告手动编辑导出后的 `pyproject.toml`，移除：
     - unused 类 + notes 含"可移除"字样的包
     - workspace_only 类 + 所有 required workspace 模块都未 include 的包
   - 行级文本过滤，保留 `[tool.uv.sources]`、`[tool.ruff]` 等其他配置和注释
   - 修复末尾多余逗号，确保 TOML 仍可被 `tomllib` 解析
   - 重新计算 pyproject.toml 的 size 和 sha256，更新 MANIFEST.json

#### 人工 notes（KNOWN_NOTES 常量）

脚本只看 git-tracked .py 文件的显式 import 语句，无法识别：
1. 未 git-tracked 的 workspace 模块（如 `stock_advisor`，开发中未提交）
2. 框架间接依赖（如 `python-multipart` 被 FastAPI 用于 form 解析）
3. 通过 importlib / 字符串引用的包

这些情况通过修改 `generate_dependency_map.py` 的 `KNOWN_NOTES` 字典补充说明，运行时合并到 dependency_map.toml 的 `notes` 字段。**只有在 notes 中含"可移除"字样的 unused 包才会被裁剪。**

#### 当前状态（2026-07-21）

37 个依赖的分类：
- **core (28 个)**：必装，含 server/client 显式 import 的包 + workspace 模块也用但 core 同时用的包
- **workspace_only (0 个)**：当前无纯 workspace 专属依赖（akshare/pandas 因 stock_advisor 未 git-tracked 被归 unused）
- **unused (6 个)**：
  - `pyperclip` → 真的可移除（项目源码 0 使用）
  - `python-multipart`/`orjson` → 保留（FastAPI 间接依赖）
  - `setuptools` → 保留（Python 打包工具，可考虑移到 dev 依赖）
  - `akshare`/`pandas` → 保留（stock_advisor 依赖，stock_advisor 未 git-tracked）

裁剪效果：当前 friend-full profile 导出时移除 1 个包（pyperclip）。

#### 维护时机

- **新增 workspace 模块**：先跑 `generate_dependency_map.py` 看分类，决定是否在 KNOWN_NOTES 补 notes
- **修改 pyproject.toml 依赖**：跑 `generate_dependency_map.py` 重新生成映射表
- **修改 friend-full.toml 的 include/exclude**：跑 `--check-profile friend-full` 看裁剪建议
- **每次 release**：导出脚本自动应用裁剪，无需手动操作

## 维护关卡

每个新功能必须声明它是公开核心、公开可选、私有还是排除项。同时记录它是否产生个人数据、需要账户或远程服务、改变默认隐私行为、新增依赖或需要示例数据。在同一变更中更新相关 profile、README/config 模板、测试和 CHANGELOG。

## Public Audience（2026-07-31 落地）

`public` audience 已从 `reserved` 升级为完整实现，支持将 release 产物公开发布到 GitHub 仓库，供任何人 clone、审计、重建。机器可读真源是 `release/audience/public.toml` 和 `release/profiles/public-full.toml`。本节描述其设计约束与工作流，**不修改上方 friend audience 章节**——两者并列，互不影响。

### Public Core 边界

Public release 只包含通用工具组件，排除所有个人业务组件。当前 4 个候选组件（声明在 `release/audience/public.toml` 的 `[audience].components`）：

- `workspace/disk_manager/` — 磁盘扫描与管理工具
- `workspace/recorder/` — 桌面操作录制器
- `workspace/modelscope_model_update/` — ModelScope 模型更新检查

排除的个人业务组件：`yihuan_simulator` / `yihuan_gacha` / `community_review` / `web_archive` / `niuke_review` / `accounting` / 各类 `*_gacha` 模块等。

`export_set = "source"`：交付源码（含 tests/docs），不交付预编译产物。所有 file_entries 仍需通过 `git ls-files` 校验（tracked-only 约束）。

### License 清关流程

Public audience 要求所有 file_entries 对应组件的 `license_class ∈ {mit, apache-2, apache-2.0}`（SPDX 标识符兼容）。4 个候选组件的 `workspace/*/manifest.toml` 中 `release_facts.license_class` 已从 `"internal-review"` 升级为 `"apache-2.0"`，每个组件经过审查：

- **原创性确认**：组件代码为项目原创，无第三方代码拷贝
- **无第三方代码引用**：grep 确认无 `Copyright (c)` 外部声明、无 GPL/AGPL/MIT 等 license 头
- **依赖兼容 Apache-2.0**：组件依赖均与 Apache-2.0 兼容（不引入 GPL/AGPL 依赖）

`disk_manager/system_rebuild/` 子目录中的个人系统重建路径已于 2026-07-31 清理（移除第三方破解相关内容），`restore_env.ps1` 和 `recovery_guide.md` 重写为通用恢复指南。

`recorder/stt_runner.py` 原硬编码的个人模型路径已改为从 `config.toml [models].stt_model_dir` 读取，避免暴露开发者环境。

### 3 个 Public 专属 Gate

`release/audience/public.toml` 在 friend audience 11 个 gate（static 4 + build_time 4）基础上新增 3 个 public 专属 gate：

| Gate | 类型 | 检查内容 |
|------|------|---------|
| `license-clearance` | 静态 | 按 file_entries 的 rel_path 反查组件 license_class，全部 ∈ {mit, apache-2, apache-2.0} |
| `no-agpl-import` | 静态 | `pyproject.toml` 无 `ultralytics` 依赖 + file_entries 内容无 `import ultralytics` 语句（双保险） |
| `sbom-generated` | 构建期 | staging 目录含 `sbom.spdx.json`，SPDX 2.3 schema 校验通过，packages 非空 |

完整 gates 清单：static = [policy-schema, tracked-file-inventory, sensitive-content-scan, manifest-audit, license-clearance, no-agpl-import]（6 项），build_time = [no-key-startup, activity-task-safe-stop, focused-tests, archive-verification, sbom-generated]（5 项）。

### SBOM（软件物料清单）

Public release 产物必须包含 SPDX 2.3 JSON 格式的 SBOM，便于接收方审计依赖和许可证。

- **生成器**：`tools/release/engine/sbom.py` 的 `generate_spdx_sbom(plan, manifest_files)` 函数
- **依赖库**：`spdx-tools>=0.8.0`（用户 2026-07-31 批准新增，~2-3 MB，Apache-2.0）
- **输出路径**：ZIP 内 `sbom.spdx.json`（生成后纳入 MANIFEST.json 作为 generated file）
- **SPDX 字段**：
  - `spdxVersion: "SPDX-2.3"`
  - `documentNamespace: https://localagent.local/spdx/localagent-{profile_id}-{plan_digest[:12]}`
  - `packages[0].licenseConcluded: "Apache-2.0"` / `licenseDeclared: "Apache-2.0"`
  - `packages[0].checksums: SHA256(plan.plan_digest)`
  - `files[]`：每个 manifest_file 一个 SPDX File，含 SHA256 checksum
  - `relationships[]`：DOCUMENT DESCRIBES Package + Package CONTAINS File（每个 file 一条）
- `dataLicense: "CC0-1.0"`（SPDX 规范要求）

### CI（Clean-Room 可重现构建）

GitHub Actions workflow 定义在 `.github/workflows/release-public.yml`，**手动 push 触发**（不设自动 schedule，用户主观决定发布时机）。

CI 流程（Ubuntu runner）：

1. **Checkout** public 仓库（fetch-depth=1）
2. **Setup Python 3.12** + **Install uv**
3. **Install dependencies**：`uv sync --no-dev --extra cpu` + `uv pip install pytest httpx`（不装 GPU 依赖，Ubuntu 上 PaddlePaddle 不可用）
4. **Verify no AGPL dependencies**：`! grep -i "ultralytics" pyproject.toml` + `! grep -rn "import ultralytics\|from ultralytics" server/ client/ lib/ tools/ workspace/`
5. **Prepare release plan**：`uv run python -m tools.release.cli prepare --profile public-full --source HEAD --audience public`
6. **Auto-approve plan**：CI 环境自动写入 `[approval]` 段（approved_by="github-actions"）
7. **Build release**：`uv run python -m tools.release.cli build --plan <digest> --skip-heavy-gates`（跳过 no-key-startup / activity-task-safe-stop，Ubuntu 无 key 文件、无活动任务）
8. **Verify SBOM in artifact**：解压 ZIP 验证 `sbom.spdx.json` 存在 + SPDX 2.3 + license=Apache-2.0
9. **Create GitHub Release**：用 `softprops/action-gh-release@v2`，tag = `public-{github.sha}`，标记为 `prerelease`

CI 只跑 release 相关测试（`test_release_public.py` + `test_release_policy.py`），不跑全量测试（PaddleOCR 等重依赖在 Ubuntu 上不可用）。

### Publish Subcommand

`python -m tools.release.cli publish --plan <digest> [--dry-run]` 推送 sanitized source 到 public 仓库触发 CI（**public audience 专属**，friend audience 不可用）。

工作流：

1. 读 plan 文件，校验 `plan.audience == "public"`（friend plan 拒绝）
2. 检查 staging 目录存在（build_release 已完成）
3. 读 `release/profiles/public-full.toml` 的 `[publication]` 段：`public_repo_url` / `public_repo_branch`
4. 创建临时目录，复制 staging 内容（排除 `.zip` / `.zip.sha256`）+ `release-plan.json`
5. `git init` + `git add .` + `git commit -m "Release public-full plan {plan_digest[:12]}"`
6. `--dry-run`：只复制 + commit，不 push；非 dry-run：`git remote add origin <url>` + `git push -u origin <branch> --force`

`--force` 是因为 public 仓库每次 release 覆盖同一分支（prerelease 流程，正式 release 由用户手动转正）。

### 转 Public 时机

用户主观判断，**不设量化阈值**。spec Decisions 决策 #15 明确"先跑通，慢慢打磨"：

- CI 产出 `prerelease` 标记的 GitHub Release
- 用户审阅后手动将 prerelease 转为正式 release
- 不设"测试覆盖率 ≥ N%"、"SBOM 字段完整度 ≥ M"等量化阈值
- 用户认为时机成熟就转，不成熟就继续打磨

### 验证命令

```bash
# 1. public.toml 加载
uv run python -m pytest tests/release_ci/test_release_public.py::test_public_audience_loads -v

# 2. 4 个候选组件 license_class
uv run python -c "
import toml
for name in ['disk_manager', 'auto_shutdown', 'recorder', 'modelscope_model_update']:
    m = toml.load(f'workspace/{name}/manifest.toml')
    assert m['release_facts']['license_class'] == 'apache-2.0', f'{name}: {m[\"release_facts\"][\"license_class\"]}'
    assert m['release_facts']['contains_personal_data'] == False, f'{name} has personal data'
print('4 components license_class=apache-2.0')
"

# 3. 3 个 public 专属 gate
uv run python -m pytest tests/release_ci/test_release_public.py -k "license_clearance or no_agpl_import or sbom_generated" -v

# 4. SBOM 生成
uv run python -m pytest tests/release_ci/test_release_public.py -k "spdx_sbom" -v

# 5. publish subcommand
uv run python -m tools.release.cli publish --help

# 6. 端到端 prepare
uv run python -m tools.release.cli prepare --profile public-full --source HEAD --audience public

# 7. 全套 public 测试 + v2 回归
uv run python -m pytest tests/release_ci/test_release_public.py tests/release_ci/test_release_compiler.py tests/release_ci/test_release_engine.py tests/release_ci/test_release_policy.py -v --tb=short
```

### 工作流（端到端）

```bash
# 1. prepare（生成 plan，跑静态 gate 含 license-clearance + no-agpl-import）
uv run python -m tools.release.cli prepare --profile public-full --source HEAD --audience public

# 2. 用户审阅 plan + 扫描结果
# （plan 文件在 release/plans/<plan_digest>.json）

# 3. compute-digest + 手动填 [approval] 段（approved_at / approved_by）
uv run python -m tools.release.cli compute-digest --profile public-full

# 4. build（生成 SBOM + 跑构建期 gate 含 sbom-generated）
uv run python -m tools.release.cli build --plan <plan_digest>

# 5. publish（推送 sanitized source 到 public 仓库触发 CI）
uv run python -m tools.release.cli publish --plan <plan_digest>
# 或先 dry-run：
uv run python -m tools.release.cli publish --plan <plan_digest> --dry-run

# 6. CI 通过后，在 GitHub 手动将 prerelease 转为正式 release
```
