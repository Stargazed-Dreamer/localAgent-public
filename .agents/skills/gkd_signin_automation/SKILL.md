---
name: gkd-signin-automation
task_type: adhoc.gkd_signin_automation
description: >
  采集、解析、编写、导入和实机回归 GKD Android 自动签到规则。当用户提到 GKD、
  自定义规则、签到自动化、快照、inspect、节点选择器、规则失效、App 更新后重新适配、
  ADB 导出 GKD 快照，或想判断某个手机签到流程能否稳定自动化时使用。
  触发词：GKD签到、GKD规则、自动签到、快照解析、节点匹配、规则失效、实机回归、
  gkd sign-in、GKD snapshot、selector drift。
---

# GKD 签到规则自动化

## 目标

把一次人工签到拆成可验证的状态转换，完成“流程确认 -> 快照采集 -> 节点解析 -> 规则编写 -> 导入 -> 日志回归”。每日签到清单和是否执行最终签到由用户决定。

## 前置条件

- Android 已开启开发者模式与 USB 调试，`adb devices -l` 中设备状态为 `device`。
- 手机已安装并启用 GKD 无障碍服务；默认数据根目录为 `/sdcard/Android/data/li.songe.gkd/files`。
- 屏幕操作前先读 `.agents/skills/computer_use.md`。每次操作必须先截图，操作后再截图或读取结构化状态验证。
- 任务资料写到 `workspace/gkd_signin/`；skill 目录只保存通用脚本和方法。
- 开始具体 App 前读取 `workspace/gkd_signin/user prompt.txt`，它是用户范围、App 现状和自动化边界的需求真源。

## 可行性分级

先分级再采集，避免把不适合 GKD 的流程硬塞进无状态规则：

| 级别 | 条件 | 路径 |
|---|---|---|
| A | 目标有稳定无障碍节点，流程可由页面状态推进 | GKD 规则 |
| B | 节点存在但需等待加载或依赖前一步 | 用稳定“就绪节点”约束；无就绪节点时才测量 `actionDelay` |
| C | 关键图标无节点、位置随机，或需要输入/循环/计数 | ADB + OCR/视觉的有状态 Computer Use 流程 |
| D | 涉及支付、账号切换、高风险提交，或无法可靠验证成功 | 保留人工操作 |

关键动作无节点时，GKD 选择器无法凭像素识别它。坐标只能处理几何固定的界面，不能解决随机尺寸、随机位置的弹窗。

## 工作流

### 1. 固化人工流程

记录每一步的：进入条件、目标动作、成功后页面、可观察的成功信号、失败分支和停止条件。完成标准是用户确认整条路径，且没有把“等待一下”“应该到了”当作状态。

应用已知情况按需读取：

- 森空岛：`references/apps/skland.md`
- QQ 音乐：`references/apps/qq_music.md`
- 夸克：`references/apps/quark.md`
- 元宝：`references/apps/yuanbao.md`

小黑盒和不背单词当前已稳定自动签到，保持现状，不主动改写其规则。

### 2. 采集状态证据

在每个动作的操作前、操作后、失败页和已完成页各截一份 GKD 快照；动态加载页面额外采集 `t0/t+3s/t+6s`。保留完整 JSON 和 PNG，`.min.json` 可能没有节点树，不能单独用于选择器分析。

从手机批量拉取最近快照、日志、订阅和执行计数：

```powershell
python .agents/skills/gkd_signin_automation/scripts/collect.py `
  --output workspace/gkd_signin/incoming_snapshots/<app>/<date> `
  --snapshots 12 --logs 2
```

先用 `--dry-run` 查看传输清单。已有文件默认不覆盖；只有明确要替换同名副本时使用 `--force`。完成标准是每个状态都有快照 ID、App 版本、Activity 和截图对应关系。

### 3. 直接解析节点

用文字、`vid`、完整资源 ID 或 class 搜索快照：

```powershell
python .agents/skills/gkd_signin_automation/scripts/inspect_snapshot.py <snapshot.json-or-zip> --query "立即领"
python .agents/skills/gkd_signin_automation/scripts/inspect_snapshot.py <snapshot.json-or-zip> --query "check" --field vid --exact
```

脚本同时报告原节点、最近可点击祖先和基础选择器候选。选择器取舍必须再读 `references/selector_strategy.md`。完成标准是目标节点在操作前快照中唯一或可被稳定上下文消歧，并且操作后状态可独立验证。

### 4. 编写候选订阅

从最小稳定约束开始：目标 class + `vid`/语义文本 + `clickable=true` + `visibleToUser=true` + 完整 Activity。一个动作只保留一条实际点击规则；完整 `id` 与同一末段 `vid` 是同一资源，不得作为两个并列点击候选。

规则组至少显式考虑：

- `actionMaximum`: 签到动作通常为 `1`。
- `matchTime`: 覆盖真实加载窗口，不用无限等待掩盖错误状态。
- `activityIds`: 优先完整 Activity 名，减少跨页误触。
- `resetMatch`: 明确它控制的是哪类生命周期；`app` 不是自然日去重。
- `actionDelay`: 只有采样证明页面必须等待且找不到“加载完成”节点时使用。

保存为新的版本号，不覆盖已验证旧版证据。完成标准是订阅 JSON 可解析、规则键唯一、没有等价点击规则。

### 5. 静态检查与快照回放

```powershell
python .agents/skills/gkd_signin_automation/scripts/validate_rule.py <subscription.json> `
  --snapshot <before.json> --snapshot <after.json>
```

验证器会检查订阅结构、重复 key、等价 `id`/`vid` 选择器，并在所给快照上回放单节点精确选择器。复杂关系选择器标为 `unchecked_complex`，仍需 Inspector 或实机验证。完成标准是 `valid=true`，预期前态有匹配，非目标页面不匹配或被 Activity 限制。

### 6. 导入或更新 GKD

本地文件优先通过临时 HTTP + ADB reverse 传递：

```powershell
python -m http.server 8767 --bind 127.0.0.1
adb reverse tcp:8767 tcp:8767
```

订阅 URL 使用 `http://127.0.0.1:8767/<file>.json`。首次在 GKD 添加订阅；同一订阅 ID 更新后，在“订阅”列表下拉刷新。卡片底部的时钟图标是触发记录，不是更新按钮。截图确认名称、版本、应用数和规则数后才进入实测。

### 7. 实机回归

1. 记录当前日志行数或时间戳，截图当前手机页。
2. 进入目标 App，依次观察规则动作和页面变化。
3. 从基线之后的日志中核对订阅 ID、版本、组 key、规则 `index`、`ActionResult`。
4. 同一次进入 App 时，每个预期动作只允许一条成功记录；检查是否有重复选择器或重复事件。
5. 用业务状态验证签到成功，例如“已领取”“1/1”或独立签到记录；点击成功不等于业务成功。
6. 保存最终截图、完整当日日志和手机上的实际订阅文件。

已签到状态仍命中时，先比较操作前后节点属性。若属性完全相同、仅 drawable 像素变化，记录为 GKD 可观测性上限；不得声称选择器能实现自然日去重。

### 8. App 更新后的漂移诊断

采集同一状态的新旧两版快照，依次比较 App 版本、Activity、目标 `vid/id/text/desc`、最近可点击祖先和页面成功信号。只修复发生漂移的最小约束，重新跑静态回放和一次实机流程；不要重写整条父子链。

## 硬约束

- 用户决定每日 App 清单与是否执行最终签到；agent 不擅自扩大到其他 App。
- 登录、支付、删除、兑换、发帖和账号切换等动作必须停下确认。
- 规则只对快照中实际存在的属性负责，不根据截图猜节点字段。
- 坐标点击必须有稳定锚点、明确分辨率边界和实机证据；随机弹窗不使用裸坐标规则。
- 每次测试保留旧规则、快照和日志，使用新版本号形成可回退证据链。

## 输入与输出

- 输入：用户确认的流程、GKD 快照 ZIP/JSON、手机当前订阅和 GKD 日志。
- 输出：`workspace/gkd_signin/rules/` 中的订阅 JSON、`reports/` 中的诊断记录、`session/` 中的截图与 UI 结构、`incoming_snapshots/` 中的原始证据副本。

## 配套文件

| 文件 | 用途 |
|---|---|
| `scripts/collect.py` | ADB 拉取快照、日志、订阅和 store 数据并生成哈希 manifest |
| `scripts/inspect_snapshot.py` | 直接解析 GKD JSON/ZIP 节点树与可点击祖先 |
| `scripts/validate_rule.py` | 订阅结构检查、等价规则检查、简单选择器快照回放 |
| `references/selector_strategy.md` | 稳定选择器与状态验证决策 |
| `references/apps/*.md` | 应用特定事实、限制与下一轮采集计划 |
