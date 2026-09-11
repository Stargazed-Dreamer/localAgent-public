# Changelog

所有重要变更均记录于此。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### Fixed

- **release 引擎 P0 隐私缺口：policy.toml 的 hard_exclude_paths 从未被引擎消费**（`tools/release/engine/manifest.py` `compute_file_entries` 新增 `hard_exclude` 参数 + `prepare.py` fail-closed 加载 policy + 泄漏防御复查）：T12 声称的 "never in ANY release artifact" 保证从未实现——spec-v4 引入 core_files 并集后绕过了组件白名单，而 hard_exclude 只被数据契约测试断言配置内容，导致 `tools/release/**`（发布引擎自身+脱敏规则）与 `.agents/skills/public-release/**`（私有排除清单）进入发布 file_entries。现 hard_exclude 在最终并集上强制过滤（目录前缀归一化 glob），prepare 期对解析结果做二次防御复查（泄漏即 raise）。连带处置：file_classifier 个人数据文件（backup/sessions/个人笔记，396+ 行本机路径）入 hard_exclude；`workspace/disk_manager/system_rebuild/**` 两个 audience 同步排除；补齐 path_mapping 组合盘符规则 30+ 条（`<external_project_root>`/`F:i_models`/`<media_root>` 等个人路径 + `project_temp` 双反斜杠形式），修复 6 个 profile 触发的契约测试回归；新增 triage 核对脚本（temp/，mapping 模拟+残留形状分类）。发布阻塞与残留清零验证：NEW 命中 871→187 处，mapping 后残留 165→60 行且全部为系统路径/测试假数据/文档示例基线。

### Fixed

### Added

- (暂无)

## [0.46.0] - 2026-09-12

### Added

- **release 一键编排 + publish 持久副本发布（append-only 公开历史）**（`tools/release/cli.py` 新 `release` 子命令 + `cmd_publish` 重写 + 新增 `tools/release/engine/{triage,history}.py` + `collect_sensitive_hits()` 从 gate 提取共用；设计 `temp/sdd/release-orchestrator/design.md`）：每次公开发布的人工动作从 7 步（prepare→scan→全量人工核对→compute-digest→手填 approval→build→publish）收敛为「跑一条命令、看一份增量报告、确认一次」。① **增量分诊**——命中按 `(path, rule) → count` 与上轮批准快照（`release/triage/<profile>.json`，只存 path/rule/count+scan_digest，绝不存行内容/行号）diff：count 未增自动放行、新增/增量列 NEW 必须人工、消失标 resolved；第二阶段用 `snapshot.scan_digest == plan.scan_digest` 锚定防源码漂移，通过后文本级写入 `[approval]`（build 的 profile_digest 验证早已废弃，不触发漂移）→ build → publish → 才写快照（批准生效点）。② **publish 持久副本**——废弃「临时目录 git init + force push 覆盖」（旧模式每次抹掉 public 历史），改为本地持久 clone（`release/public_repo/`）：首次 clone 远端（已有 commit 即基线）、每次发布同步 staging 全量 → `git add -A` 由 git 算出真实版本间 diff → 一个 commit + 可选 tag → push 绝不带 `--force`（远端领先即拒绝，历史 append-only 不可重写）。③ **RELEASE_HISTORY.md 自动生成**——每版本倒序章节：相对上次发布的私有仓库 commit 数与日期区间（由上一 commit 的 release-plan.json.source_commit 统计，纯元数据无泄露）+ 该版本 CHANGELOG 条目标题（CHANGELOG 已在发布物内过 scan，取材零增量风险；绝不逐条复制 private commit message）。测试 16 用例（triage 三态/快照 roundtrip/CHANGELOG 解析/持久副本双发布真实 diff/远端分叉拒推/approve 全链与 dry-run 无副作用），release_ci 78 全绿（连带救活 3 个因 gate 误拦而 skip 的存量测试）。**顺带修复发布阻断**：8462d31 测试大整理把 3 个敏感样本测试文件移入 `tests/release_ci/`，两个 audience toml 的 `core_files_exclude` 路径未跟（glob 失配 → `test_release_v4.py` 的 10 条构造 HIGH 进入扫描 → gate 正确拦截一切发布），路径已修正。

### Fixed

- **pre-commit 钩子会重写 `uv.lock` 的 registry 源，静默撤销「lock 用官方源」的决策**（`scripts/hooks/pre-commit`）：钩子 `exec uv run python tools/check_hard_rules.py --staged` 每次提交都触发 `uv run`，而本机全局 uv 配置的 `index-url` 是阿里云镜像、仓库 `uv.lock` 记的是官方 pypi 源，uv 据此判定「lock 过期」并整文件重写（实测 3109 行 registry 从 `pypi.org` 翻成 `mirrors.aliyun.com`），静默撤销 `f7ab2a2`（uv.lock 镜像源从阿里云切回官方）的意图；且 `docs/release-policy.md` L13 明确 `uv.lock` 会保留进分发包，等于替接收方决定镜像源。修复：钩子改为 `exec env UV_FROZEN=1 uv run ...`（`UV_FROZEN` 只冻结 lock、不重解析依赖），实测钩子输出与退出码不变（staged 检查通过）、`uv.lock` 保持干净。同轮加固：新增 `.gitattributes`（`scripts/hooks/* text eol=lf` + `*.sh text eol=lf`）——本机 `core.autocrlf=true`（来自系统级 gitconfig）且仓库无行尾规则，Windows 上重新检出会把这两个 shell 脚本写成 CRLF（`git add` 时即报「LF will be replaced by CRLF」），而钩子必须以 LF 存在才能执行（CRLF 下末行 `--staged` 会带上 `\r` 变成非法参数）。已知边界：只治钩子这一处，手动 `uv run`/`uv sync` 仍会重写（提交前 `git status` 瞄一眼，漂了即 `git restore uv.lock`）；`UV_FROZEN` 不校验 lock 与 `pyproject.toml` 的一致性（本钩子只做静态检查，影响可忽略），更严格的 `--locked` 在本机直接失败（lock 源与全局索引不一致，uv 判其需更新）。
- **gh_mirror 清库守护第二层（自动禁用镜像 workflow）上线以来从未生效**（`workspace/gh_mirror/loop_actions.py` + 新增 `workspace/gh_mirror/tests/test_gh_mirror_wipe_autodisable.py`）：404 `upstream_deleted`（原 L233-241）与 451 `dmca_takedown`（原 L222-232）两个分支都漏设 `wipe_detected = True`，只有 `size==0` 分支设了 → 唯一的触发点 `if wipe_detected and auto_disable` 实际只对 size=0 走得通，`_WIPE_ALERT_KINDS` 名义覆盖三种清库类型。铁证：`data/gh_mirror/sync_status.jsonl` 全 1727 条快照里 `sync_workflows` 字段 **0 次出现**；功能由 `e578de0`（2026-09-03）引入，恰晚于 CipherTalk 最后一次 size=0（09-02），上线即失效。后果：中招镜像仓的 `dmca-backup.yml` / `release-backup.yml` 长期 active（对 404 上游白跑 Actions；release 路径还会把清库后不受信的上游二进制拉进镜像，正是该防线要防的供应链风险）。修复：删掉手写布尔标志，改为由 alerts 反推 `wipe_detected = any(a["level"]=="critical" and a["kind"] in _WIPE_ALERT_KINDS for a in alerts)`，让 `_WIPE_ALERT_KINDS` 成为唯一真源（将来新增清库类型只改常量表）。测试 10 用例：覆盖 404 / 451 / size=0 三条触发路径、四类不误触发（正常仓 / 500 限流 / 开关关闭 / 常量表守卫）、幂等与禁用失败告警。**存量补偿**（修复只管将来，已停用监控的仓不进检查循环）：`mirror-CipherTalk` 与 `mirror-ManifestHub2` 各两个 workflow 已人工禁用（现为 `disabled_manually`）。⚠️ 需重启后端生效（该模块由 `importlib.import_module` 在启动时加载一次；`repos.json` 名单则每次运行重读）。

