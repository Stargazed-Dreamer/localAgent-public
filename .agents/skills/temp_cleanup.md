---
name: temp_cleanup
description: >
  temp 文件夹定期清理工作流。当用户提到整理 temp、清理 temp、清理临时文件、temp 太乱、
  临时文件整理时触发。触发词：整理temp、清理temp、temp清理、临时文件整理、temp太乱、
  清理临时文件、temp cleanup。
---

# temp 文件夹清理 (temp_cleanup)

## 触发词
整理temp、清理temp、temp清理、临时文件整理、temp太乱、清理临时文件、temp cleanup、temp整理

## 概述

`temp/` 是 LocalAgent 的临时目录，用于存放：
- `exec_python` 接口的临时脚本
- 调试日志（后端启动日志、终端会话日志）
- 一次性任务的中间数据（git diff、扫描结果、测试图等）
- 临时解压/提取内容

**问题**：temp 容易堆积大量历史文件（日志、中间产物、旧测试数据），需要定期清理。

**本 skill 的工作流**：扫描分类 → 生成清单 → 用户确认 → 删除/移动（走回收站） → 收尾报告。

## 前置条件
- LocalAgent 后端已启动（可选，用于调用 MCP 工具）
- PowerShell 可用（删除走回收站需要 `Microsoft.VisualBasic.FileIO`）

## 分类决策表

扫描 `temp/` 后，把每个文件/目录归到以下类别：

| 类别 | 特征 | 默认处理 | 注意事项 |
|---|---|---|---|
| **A. 调试日志** | `*.log`、`backend_stderr*.log`、`backend_stdout*.log`、`fake_llm.log` | 删（回收站） | 一般可重新生成 |
| **B. 终端日志** | `terminals/` 下的 `term_*.stdout.log` / `term_*.stderr.log` | 删（回收站） | 大部分为 0 字节；运行中终端的日志不要删 |
| **C. 任务中间产物** | changelog 研究的 `git_*.txt`、`git_research/`、`changelog_research/`、扫描结果等 | 看任务是否完成：完成→删；未完成→保留或归档 | 检查 `.agents/wip/` 是否有对应 open 任务 |
| **D. 业务数据误放** | 用户业务数据（如 `exam_prep/`、`微信收藏导出/`） | 移到 `workspace/{任务名}/` | temp 只放临时文件，业务数据归 workspace |
| **E. 备份/参考** | 外部 skill 参考、zip 包、git 克隆 | `temp/refs/` 或 `references/` | references/ 放外部克隆项目；temp/refs/ 放本项目相关备份 |
| **F. 散落脚本** | 一次性 `.py` 脚本（非 exec_python 临时执行） | `temp/scripts/` | 与 `tools/` 重复的删（先 MD5 对比） |
| **G. OCR/视觉调试数据** | `bbox_*.json`、`grid_*.png`、采样结果 | WIP 已完成；保留基线参考数据，临时重复图按清单审核 | 升级 PaddleOCR/PaddleX 时需要三档坐标回归 |
| **H. 旧测试图** | 早期 OCR/UI 测试截图（如 `img_test/`） | 删（回收站） | 超过 3 个月且无 wip 关联 |
| **I. 临时解压/提取** | `old_kit/`、`pdf_extract/` 等 | 保留（正常临时使用） | 这是 temp 的正当用途，不要清理 |
| **J. 其他项目文件** | 不属于 LocalAgent 的文件（如 `mimo_*.json` 属于 MuseArc） | 移回原项目目录 | 询问用户原项目位置 |
| **K. SDD 流程产物** ★保护 | `temp/sdd/<slug>/` 下的 spec.md / tickets.md / checklist.md / design-decisions.md / grill-me.md / 00-overview.md / NN-*.md / BLOCKED.md / plan.md / design.md / README.md | **绝不删除**（项目真源，工作记忆） | SDD 文档是上下文压缩后的恢复锚点；如需归档由用户决策迁入 `workspace/sdd_archive/`，agent 不得擅自清理 |
| **L. HTML 展示文件** | `temp/html/<feature>.html` 或 `temp/html/<feature>/` | 看是否还在用：开发中→保留；已完成且无引用→删（回收站） | 删前先 grep 项目源码是否还有指向该 HTML 的引用 |
| **M. 历史归档** ★保护 | `temp/planning_archive/sdd/` 下从 `planning_notes/` 迁移的历史 SDD 文档 | **绝不删除**（只读归档） | 2026-07-30 从 `planning_notes/` 迁移而来，保留作为历史参考 |

## 工作流

### Step 1: 扫描与分类

1. 列出 `temp/` 下所有文件和目录（含子目录内容概览）
2. 按分类决策表归类每一项
3. 对不确定的项目：
   - 检查 `.agents/wip/` 是否有相关 open 任务
   - 检查 `tools/` 是否有重复文件（MD5 对比）
   - 检查文件是否属于其他项目（看内容路径引用）
4. 统计每类的大小和数量

### Step 2: 生成清单并询问用户

**强制规则：删除/移动操作前必须给用户完整清单并获得明确同意。**

清单格式：

```
## 🗑️ 删除清单（送回收站，可恢复）
| 类别 | 项 | 大小 | 备注 |
|---|---|---|---|
| A. 日志 | backend_stderr*.log | 84 KB | |
...

## 📦 移动清单
| 类别 | 源 → 目标 | 大小 |
|---|---|---|
| D. 业务数据 | temp/exam_prep/ → workspace/exam_prep/ | 4.3 MB |
...

## ❓ 不确定项
（列出需要用户决定的项目）
```

用 `AskUserQuestion` 询问：
1. 整体清单是否同意
2. 不确定项的处理方式
3. 是否需要新增 wip 待办（如发现遗漏的任务）

### Step 3: 执行删除（走回收站）

**强制规则：删除必须走 Windows 回收站，不得用 `Remove-Item` 直接删除。**

PowerShell 实现：

```powershell
Add-Type -AssemblyName Microsoft.VisualBasic
$recycle = [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
$opts = [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException

# 删除文件
[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($path, $recycle, $opts)
# 删除目录
[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($path, $recycle, $opts)
```

**不要用**：
- `Remove-Item -Force`（直接删除，不可恢复）
- `del`、`rm`（同上）
- `rd /s /q`（同上）

### Step 4: 执行移动

用 `Move-Item -Force`：

```powershell
# 移动文件
Move-Item -Path $src -Destination $dst -Force
# 移动目录（目标目录已存在且为空时，先删空目录再移动）
if (Test-Path $dst) { Remove-Item $dst -Force -Recurse }
Move-Item -Path $src -Destination $dst -Force
```

**移动前创建必要目录**：
```powershell
$dirs = @('temp\refs', 'temp\scripts', 'workspace\{任务名}')
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { New-Item -Path $d -ItemType Directory -Force | Out-Null }
}
```

### Step 5: wip 待办处理

清理过程中如果发现：

1. **遗漏的 wip 任务**（如 `wip_migrated_backup/` 中有未迁移的 JSON）：
   - 转为 `.md` 格式恢复到 `.agents/wip/`
   - 在 skill 文件中记录恢复动作

2. **业务数据搬迁需要后续处理**（如微信收藏搬到 workspace 后还需进一步整理）：
   - 在 `.agents/wip/` 创建新待办，说明背景、next steps、关联

3. **skill 需要更新**（如 exam_prep 关键文档未同步到 skill）：
   - 创建 wip 待办记录需要同步的内容

### Step 6: 收尾报告

报告内容：
1. 删除项数和释放空间（如"85 项删除，释放 60 MB"）
2. 移动项数和目标位置
3. 新增的 wip 待办列表
4. temp 目录最终状态（大小、结构）
5. 发现的异常（如操作期间产生的新目录）

## 关键规则

1. **删除必走回收站**：用 `Microsoft.VisualBasic.FileIO.FileSystem.DeleteFile/DeleteDirectory` + `RecycleOption.SendToRecycleBin`，不得用 `Remove-Item`
2. **清单先行**：所有删除/移动操作前必须给用户完整清单，获得明确同意后才执行
3. **业务数据不删**：业务数据（微信收藏、考试资料等）只能移动到 workspace，不能删除
4. **wip 关联检查**：对每个文件，检查 `.agents/wip/` 是否有相关 open 任务；有则保留或归档
5. **重复检查**：脚本类文件先与 `tools/` 对比 MD5，重复才删
6. **跨项目检查**：不属于 LocalAgent 的文件（如 MuseArc 状态文件）移回原项目
7. **OCR 校准数据谨慎**：WIP 已完成，但基线 JSON 和三档网格仍用于升级回归；只清理明确重复的临时副本，且遵循清单审核
8. **运行中终端不删**：`terminals/` 下属于运行中终端的日志不要删（先查 `exec_terminals_list`）
9. **临时解压保留**：`old_kit/`、`pdf_extract/` 等正常临时使用不要清理，这是 temp 的正当用途
10. **清理后归档**：清理完成后，把分类决策和发现记录到记忆或 wip，方便下次清理
11. **SDD 文档保护（强制）**：`temp/sdd/<slug>/` 下的所有文件（spec/tickets/checklist/design-decisions/grill-me/00-overview/NN-*.md/BLOCKED.md 等）是项目真源和工作记忆，**绝不删除**。它们是上下文压缩后的恢复锚点（见 AGENTS.md「上下文压缩防护」）。如需归档，由用户决策迁入 `workspace/sdd_archive/`，agent 不得擅自清理或移动
12. **历史归档保护（强制）**：`temp/planning_archive/sdd/` 下的文档是 2026-07-30 从 `planning_notes/` 迁移的历史 SDD 归档，**绝不删除**（只读归档）
13. **HTML 展示文件谨慎清理**：`temp/html/` 下的 .html 文件删前必须 grep 项目源码确认无引用；开发中的 HTML 保留，已交付且无引用的可删（走回收站）

## 默认目录结构（清理后）

```
temp/
├── sdd/               # ★ SDD 流程产物（保护，不清理）
│   └── <feature-slug>/  # spec.md / tickets.md / checklist.md / design-decisions.md 等
├── html/              # HTML 展示文件（谨慎清理，删前 grep 引用）
│   └── <feature-name>.html 或 <feature-name>/
├── planning_archive/  # ★ 历史归档（保护，不清理）
│   └── sdd/           # 2026-07-30 从 planning_notes/ 迁移的历史 SDD 文档
├── refs/              # 备份与参考（本项目相关）
│   ├── ocr_calibration/  # OCR bbox 校准数据
│   ├── external_skills/  # 外部 skill 参考
│   └── ...
├── scripts/           # 散落的一次性脚本
│   └── ...
├── {临时目录}/        # exec_python 临时执行、解压查看等
└── {临时目录}/
```

## 与其他 skill 的关系

| skill | 关系 |
|---|---|
| `disk_manager` | 磁盘清理 skill（系统级），temp_cleanup 专注于 temp 目录 |
| `neat-freak` | 文档审查 skill，temp_cleanup 专注于文件清理 |
| `wip_tracker` | 清理中发现遗漏 wip 时用 wip_tracker 恢复 |
| `task_closure` | 清理任务本身的收尾走 task_closure |

## 依赖

| 依赖 | 用途 |
|---|---|
| PowerShell | 删除走回收站、移动文件 |
| `Microsoft.VisualBasic.FileIO` | 回收站 API |
| `.agents/wip/` | 检查 open 任务 |
| `tools/` | 重复文件对比 |

## 输入/输出

- **输入**：`temp/` 目录
- **输出**：清理后的 `temp/` + 可选的 wip 待办 + 清理报告
- **可恢复**：所有删除项进 Windows 回收站，可恢复

## 触发频率建议

- 用户主动触发（"整理 temp"）
- temp 目录超过 100 MB 时建议触发
- 大型任务完成后（如 changelog 整理、skill 创建）建议检查 temp

## 记忆 key

- `temp_cleanup`：上次清理时间、清理了什么、发现的问题
