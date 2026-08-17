---
name: resolving-merge-conflicts
description: >
  解决 git merge/rebase 冲突。当用户遇到 merge 冲突、rebase 冲突、git 合并问题、
  分支合并冲突时使用。逐 hunk 解决，保留双方意图，禁用 --abort。触发词：
  resolving-merge-conflicts、merge 冲突、rebase 冲突、合并冲突、解决冲突、git 冲突。
task_type: dev.resolving_merge_conflicts
---

# 解决 Merge 冲突 (resolving-merge-conflicts)

## 工作流

1. **查看当前 merge/rebase 状态**。检查 git history 和冲突文件。

2. **为每个冲突找到 primary sources**。深入理解每个变更为什么产生，以及原始意图是什么。阅读 commit messages，检查 PRs，检查原始 issues/tickets。

3. **解决每个 hunk。** 尽可能保留双方意图。若二者不兼容，选择符合本次 merge 目标的一方，并记录 trade-off。不要发明新行为。始终解决冲突；不要 `--abort`。

4. 发现项目的 **automated checks** 并运行它们，通常是 typecheck、tests、format。修复 merge 引入的问题。
   - 本项目可用：`uv run pytest`（如有测试）、`uv run python -m server.main` 验证启动

5. **完成 merge/rebase。** Stage 所有内容并 commit。若正在 rebase，继续 rebase 流程直到所有 commits 都完成。

## 关键规则

- **绝不 `--abort`**：冲突必须解决，不能放弃。`--abort` 会丢失工作。
- **不发明新行为**：解决冲突时只合并双方意图，不趁机加新功能或重构。
- **记录 trade-off**：当双方不兼容必须取舍时，在 commit message 或 PR 描述里记录为什么这么选。
- **运行检查**：解决后必须运行项目的自动化检查（测试、类型检查、格式化），修复 merge 引入的问题。
