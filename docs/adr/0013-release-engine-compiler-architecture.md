# release engine 编译器式架构（PreparedRelease + prepare/build 双接口，审计 = 构建）

release 设计为编译器式深模块，对外只有两个核心操作 `prepare_release()` → `build_release()`。`prepare_release()` 产出不可变的 `PreparedRelease`（含全部 digest 绑定），审计和构建都必须消费同一个 `PreparedRelease`——审计一套状态、实际打包另一套状态在结构上不可能。三层责任分离：组件 manifest 声明中立事实 / audience policy 决定交付内容 / release engine 可靠构建并证明产物正确。

## Context

重构前 release 流程由 `tools/export_release.py`（78KB / ~2000 行，10 个函数）和 `audit_profile.py`（32KB / ~800 行，17 个函数）两个巨型脚本承担。用户报告指出核心问题：

1. **审计与构建消费不同状态**：`audit_profile.py` 跑一次扫描产出报告，`export_release.py` 跑另一次扫描产出产物，两次扫描间源码 / profile / 组件选择可能变化，导致"审计看到的安全状态 ≠ 实际打包的安全状态"。
2. **组件 manifest 承担策略判断**：`ReleaseEntry.distribution = "include" | "exclude" | "partial"` 让组件自己知道"给朋友还是给公众"，违反关注点分离——组件不应感知 audience。
3. **审批无 digest 绑定**：`friend-full.toml` 的 `review_scan_cleared_at = "2026-07-30T21:30:00+08:00"` 是可手动修改的 ISO 时间戳，未绑定任何源码 / profile / 扫描摘要（详见 ADR-0014）。
4. **全仓库 literal replacements**：`friend-full.toml` 826 行中约 500 行是 `[[content_replacements.literal]]`，多盘符多形式变体盲目替换。

`export_release.py` 单文件 2000 行，P0 止血在其上就地扩展会让"巨大脚本 + 新安全机制"耦合，后续返工成本指数上升。

## Decision

把 release 重构为编译器式深模块，对外只暴露两个核心操作：

```python
prepared: PreparedRelease = prepare_release(profile, audience, source_commit)
artifact: Artifact = build_release(prepared, approval)
```

**深模块原则**（Ousterhout）：接口窄、实现深。`prepare_release()` 内部完成组件选择、文件清单生成、扫描、digest 计算、静态 gates 检查；`build_release()` 内部完成动态 gates、archive 构建、archive verification、产物摘要。调用方不需要也不应该知道这些步骤。

**审计 = 构建**：`PreparedRelease` 是不可变 `@dataclass(frozen=True)`，`prepare_release()` 返回后所有字段（含 `plan_digest` / `profile_digest` / `components_digest` / `scan_digest`）固定。审计工具消费 `PreparedRelease` 看到的状态 = `build_release()` 实际打包的状态，结构上消除"审计 ≠ 构建"的可能。任何输入变化让 `plan_digest` 变化，使旧 approval 自动失效（详见 ADR-0014）。

**三层责任分离**：

| 层 | 职责 | 不做什么 |
|----|------|----------|
| 组件 manifest（`manifest.py`） | 声明中立事实：组件拥有什么文件、`release_facts` | 不判断"给谁发布" |
| audience policy（`audience.py`） | 决定本次允许交付什么：friend / public 各自的 components + export_set + gates | 不持有任何构建逻辑 |
| release engine（`prepare.py` + `build.py`） | 可靠构建并证明产物正确：digest-bound、fail-closed、archive verification | 不持有 audience 判断 |

**PreparedRelease 数据载体**：Python `@dataclass(frozen=True)` + JSON 序列化（`to_dict` / `from_dict`），不可变 + 可哈希算 digest。计划文件存储在 `release/plans/<plan-id>.json`（gitignore，与 profiles/ 同级）。

**旧 CLI 兼容**：`audit_profile.py` / `export_release.py` 保留为适配器，内部委托新引擎，标记 deprecated 但不删除（用户报告明确要求）。

## Considered Options

1. **在现有 export_release.py 上就地扩展**——被拒绝：78KB / 2000 行单脚本 + 新安全机制（digest / fail-closed / archive verification）耦合，P0 止血与架构重构交织，返工成本高；用户报告"建议的迁移顺序"第一阶段未提新引擎，但 P0 完成后第二阶段必须重写。
2. **纯脚本式 + 人工审批（不改架构）**——被拒绝：保留"审计跑一次 + 构建跑一次"的双扫描结构，结构上无法保证"审计 = 构建"，digest 绑定也无法落地。
3. **TOML schema 而非 dataclass**——被拒绝：TOML 适合配置文件不适合不可变计划，无法表达 frozen 语义，digest 计算需先转 JSON 再哈希反而绕路。
4. **编译器式深模块 + PreparedRelease dataclass（本决策）**——采用：接口窄（两个函数）+ 实现深（digest / gates / verification 全在内部），结构上保证"审计 = 构建"，旧 CLI 通过适配器保留向后兼容。

## Consequences

**正面**：
- "审计 ≠ 构建"在结构上不可能，digest 绑定 + fail-closed 自然落地（ADR-0014）。
- 三层责任分离让组件 manifest 不再感知 audience，friend / public 共享打包引擎不共享发布规则。
- PreparedRelease 不可变 + JSON 序列化，可独立审计、可重放构建、可缓存。
- 旧 CLI 适配器让现有调用方零改动平滑迁移。

**负面**：
- 引入新模块层（`tools/release/engine/` 下 8 个文件：models / prepare / build / digest / manifest / audience / sbom / templates），文件数增加。
- 旧 CLI 适配器是"暂时性技术债"，未来某次大清理时移除会再产生一次迁移成本。
- PreparedRelease 13 字段精简集是设计取舍——某些场景可能需要更多字段（如签名），目前 `Approval.signature` 字段保留但 phase 2 不实现验证。

**回退路径**：旧 CLI 适配器（`audit_profile.py` / `export_release.py`）保留为 fallback，若新引擎出现严重 bug 可临时切回；适配器内部已委托新引擎，回退 = 绕过新引擎直接走旧路径（仅在紧急情况）。

## References

- SDD 来源：`temp/sdd/release-engine/design-decisions.md` 一.1（目标架构）+ 一.2（三层责任分离）+ 四.4（审计 = 构建）+ 三.3（dataclass 载体）+ 三.4（旧 CLI 适配器）
- 关键代码：
  - [tools/release/engine/models.py](file:///f:/<project_root>/tools/release/engine/models.py)（`PreparedRelease` frozen dataclass L61-L117，13 字段含 4 digest）
  - [tools/release/engine/prepare.py](file:///f:/<project_root>/tools/release/engine/prepare.py)（`prepare_release()`）
  - [tools/release/engine/build.py](file:///f:/<project_root>/tools/release/engine/build.py)（`build_release()`）
  - [tools/release/engine/audience.py](file:///f:/<project_root>/tools/release/engine/audience.py)（friend / public policy seam）
  - [tools/release/engine/manifest.py](file:///f:/<project_root>/tools/release/engine/manifest.py)（组件中立事实）
- 相关 ADR：ADR-0014（digest 绑定 > 人工审批，依赖本决策的 PreparedRelease 不可变性）
