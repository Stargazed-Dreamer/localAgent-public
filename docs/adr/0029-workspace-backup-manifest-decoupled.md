# workspace 数据备份方案：[backup] 段独立于 [watch] 段

## Context

2026-08-18，用户反馈 workspace 还有很多关键个人数据（股票 watchlist / 账单 / db）未被跟踪备份，希望"manifest.toml 里加一条说明本 work 下哪些是需要备份的文件，然后 loop 任务一起处理好"。

项目已有 `[watch]` 段（`lib/component_manifest.py` 的 `WatchEntry`），扫描结果显示现有 `[watch]` 语义混乱：
- `accounting`: `["账单/", "data/accounting.db", "name_mapping.json", "review_config.json"]` —— 全是数据
- `disk_manager`: `["scripts/scan_disk.py", "scripts/cleanup.py", "scripts/backup_env.py", "scan/", "system_rebuild/manifest.json"]` —— 含源码脚本
- `gh_mirror`: `["repos.json", "repos.example.json"]` —— 含模板（example 不该备份）
- `stock_advisor`: `["watchlist.json", "data/stock_advisor.db", "data/stock_history.duckdb"]` —— 全是数据
- `recorder`: `["recordings/"]` —— 大量录屏

且 `WatchEntry` 当前**没有任何消费方**（grep 主代码库 `.watch` 仅在 manifest 解析层和测试中出现），是纯声明。

## Decision

新增独立 `[backup]` 段（`BackupEntry(paths: list[str])` dataclass），与 `[watch]` 段语义解耦：
- `[watch]` 保留原语义"变更监听"（未来若实现 watchdog/file_arrived 等消费方时使用）
- `[backup]` 专门声明"需要纳入定时备份的个人数据文件/目录"
- 两段独立，paths 不共享，组件作者按需声明各自语义

配套：
- `tools/backup_workspace.py` 实现 `run_backup(location, dry_run) -> dict`，与 `tools/backup_secrets.py` 对称接口设计
- `WorkspaceBackupAction`（action_type=`workspace_backup`）+ `TASK_BUILDERS["workspace_backup"]` 用 `IntervalTrigger` 默认 5 天
- `config.toml` 新增 `[workspace_backup]` 段（路径与保留策略，与 `[secret_backup]` 对称但独立）
- 备份位置：项目内 `backups/workspace/<component>/` + 项目外目录，按组件分子目录隔离
- 保留策略：默认 N=7/M=14（比密钥更激进，因数据更新快）
- 文件 → 原子拷贝；目录（以 "/" 结尾或 is_dir）→ 递归打 zip 后原子写

## Considered Options

被拒绝的替代方案：

1. **复用 `[watch]` 段**（最省事，paths 已声明）
   - 拒绝理由：`[watch]` paths 含源码脚本（disk_manager 的 scripts/）/模板文件（repos.example.json），与备份语义冲突；如果强行筛除非数据文件会把 [watch] 语义搅成"原始 paths + 备份时 filter"双重职责
   - 一旦后续真正实现 watch 的消费方（如文件变更监听），两者语义会撞车

2. **扩展 `[watch]` 段加 `backup = true/false` 标志**
   - 拒绝理由：语法噪声（每个 paths 都要标 backup），且语义仍耦合在 watch 下；读者看到 `[watch].backup` 会困惑"那 watch 是不是也要 backup"

3. **共享 `[secret_backup]` 配置（同一目录 + 同一保留策略）**
   - 拒绝理由：workspace 数据多是大文件（duckdb 几十 MB），共享保留窗口会挤占密钥备份的版本数；workspace 数据更新频次与密钥不同，需要独立配置；密钥泄露比个人数据泄露更严重，应该有更紧的保留策略保护

## Consequences

- **未来读者**：看到 `[backup]` 和 `[watch]` 两段时查本 ADR 即可理解为何不合并
- **扩展性**：新组件按需在 `manifest.toml` 加 `[backup] paths = [...]` 即被 `WorkspaceBackupAction` 自动消费，无需改 backup_workspace.py
- **风险登记**：`SECURITY-RISKS.md` 第 16 项（与第 15 项密钥明文备份同类风险，中危，设计选择）
- **加密层未来扩展**：`BackupEntry` 后续可加 `sensitive: bool = False` 字段，仅对敏感组件加密（与第 15/16 项共享同一加密层）

## References

- 实现：[lib/component_manifest.py](file:///<project_root>/lib/component_manifest.py) `BackupEntry` + `Manifest.backup`
- 工具：[tools/backup_workspace.py](file:///<project_root>/tools/backup_workspace.py)
- loop 集成：[server/activity_tracker/loop_actions.py](file:///<project_root>/server/activity_tracker/loop_actions.py) `WorkspaceBackupAction`
- 风险登记：`SECURITY-RISKS.md` 第 16 项
- 对称设计：ADR-0017 lib/secret 统一密钥中转层（同样是 lib 层 + 配置中转 + 防路径硬编码思路）
