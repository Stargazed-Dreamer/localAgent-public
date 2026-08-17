---
name: implement
description: >
  基于 spec 或 ticket 实现一段工作。当用户说"实现这个 ticket"、"按 spec 做"、
  "implement"、"领取 ticket 开干"、"执行 ticket"时使用。从 frontier 领取一个 ticket，
  用 TDD 实现，完成后用 code-review 审查，提交到当前 branch。触发词：implement、
  实现 ticket、领取 ticket、按 spec 实现、执行 ticket、做这个任务。
task_type: dev.implement
---

# Implement (implement)

基于 spec 或 ticket 集合实现一段工作。

## 改造说明（原版 → 本版）

原版（mattpocock/skills）用 `/tdd` 和 `/code-review` 斜杠命令，依赖 Claude Code/Codex 的斜杠命令机制。本版改造为：

- **领取 ticket**：`wip_list(type=ticket, status=active)` → 过滤 frontier（blockers 都 completed）→ `wip_get` 读详情
- **TDD 实现**：引用 `dev/tdd` skill 的工作流（agent_guide 路由到 `dev.tdd`）
- **代码审查**：引用 `code_review` skill（融合升级后含两轴审查）
- **状态更新**：完成后 `wip_update(status=completed)`，释放下游 ticket 的 blocker

## Process

### 1. 领取 ticket

1. `wip_list(type=ticket, status=active)` 获取所有未完成 ticket
2. 按 `extra_data.blocked_by` 过滤出 frontier：blockers 都 completed 的 ticket
3. 如果有多个 frontier ticket，询问用户选哪个；用户点名就领那个，否则按编号顺序拿第一个
4. `wip_get(wip_id=<ticket_id>)` 读完整详情（goal、acceptance_criteria、ticket_path）
5. 读 `workspace/tickets/<feature-slug>/<NN>-<slug>.md` 获取完整 ticket body

### 2. 用 TDD 实现

按 `dev/tdd` skill 的工作流实现：

- 探索 codebase，理解当前状态
- 与用户确认测试 seams（优先用现有 seams）
- Red before green：先写 failing test，再写最小实现
- One slice at a time：每个 cycle 只处理一个 seam、一个 test、一个 minimal implementation
- Refactoring 不属于本步骤（留给 code-review 阶段）

### 3. 定期检查

- 定期运行 typecheck（`uv run pyright` 或 `mypy`，如有配置）
- 定期运行单个测试文件（`uv run pytest <file> -v`）
- 最后运行完整测试套件（`uv run pytest`）

### 4. 代码审查

完成后，用 `code_review` skill 审查这次工作：

- Standards 轴：代码是否符合项目规范（见 `.trae/rules/project_rules.md`）
- Spec 轴：实现是否满足 ticket 的 acceptance_criteria
- 修复审查发现的问题

### 5. 提交

1. 把工作提交到当前 branch（git add + commit）
2. commit message 描述"为什么"而非"做了什么"，引用 ticket wip_id

### 6. 更新 ticket 状态

```
wip_update(
  wip_id=<ticket_id>,
  status="completed",
  progress=100,
  extra_data={
    "completed_at": "<ISO 时间>",
    "commit_sha": "<git commit sha>",
    "review_status": "passed"
  }
)
```

这会释放下游 ticket 的 blocker（下游 ticket 的 `extra_data.blocked_by` 包含本 ticket 的 wip_id）。

## 关键规则

- **一次只领一个 ticket**：每个 session 只处理一个 frontier ticket，完成后清空 context 再领下一个
- **TDD 优先**：尽可能在预先认可的 seams 上用 TDD（red-green 循环）
- **定期检查**：不要等全部写完才跑测试，定期 typecheck + 单文件测试
- **必须 code-review**：实现完成后必须用 code_review skill 审查，不能跳过
- **wip_update 必做**：完成后必须更新 ticket 状态，否则下游 ticket 无法解锁

## 与其他 skill 的关系

- **上游**：`to-tickets` 产出 tickets → `implement` 领取执行
- **内部调用**：`dev/tdd`（实现）+ `code_review`（审查）
- **下游**：`triage` 监控 ticket 状态；`wip_archive` 归档 completed ticket
