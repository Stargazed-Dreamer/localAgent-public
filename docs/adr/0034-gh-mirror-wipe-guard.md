# gh_mirror 同步脚本：清库守护（wipe guard）

## Context

2026-09-02 发现 `ILoveBingLu/CipherTalk` 上游被作者 force-push 清库：仓库仍在（2233 star），但 `size=0`，只剩 1 个孤儿 commit + 3 个空壳 tag，历史全部抹除，项目转为会员制收费。

镜像仓 `mirror-CipherTalk` 保存了 1334 个提交、108 个 tag、98 个 release（20.62 GB），成为孤本。排查同步脚本 `workspace/gh_mirror/workflow_template.yml` 后发现**三个保护机制全部失效或不存在**：

1. **`backup/${DATE}` 安全网打错对象**（真实缺陷）
   `git tag "backup/${DATE}" "upstream/${DEFAULT_BR}"` 打的是**上游**的 HEAD。上游一旦被清库，这个"安全网"就指向了那个空 commit —— 等于没有任何保护。

2. **`push --force-with-lease` 无效**（认知偏差）
   该参数比对的是镜像**自己** origin 的 SHA，无人并发时必然通过。它防的是"别人偷偷推了"，不防"我自己拿空历史覆盖自己"。此前误以为它能拦住清库场景。

3. **无锚点 tag，保护全靠运气**
   唯一让历史存活的，是 `v2026.820.0` 恰好覆盖到 main tip 前 1 个提交（1333/1334）。若最后一个 tag 落后 main 几百个提交，这次就真丢了。

另有两项**意外生效**的保护，属无意为之，改动时必须保留：
- `git push --force origin "refs/tags/*:refs/tags/*"` 不带 `--delete`/`--prune`，git push 默认不删远端 ref —— 救下 108 个 tag
- 未使用 `git push --mirror`，避免整仓 ref 覆盖

## Decision

在 sync 脚本中新增**三层防护**，全部作用于 `checkout -B`（重置分支）之前，此时镜像内容尚未受损：

### 防护 1：每分支滚动锚点 `anchor/<branch>`

同步前把镜像自己的 tip 记到 `git tag -f anchor/<branch> <ORIGIN_SHA>`。每轮 force 覆盖，不累积（每分支固定 1 个 tag）。分支名中的 `/` 替换为 `-`，避免 ref 目录/文件冲突。

作用：即使后续判定为"正常同步"并强制重置，也至少有上一周期的 tip 可回溯。

### 防护 2：清库 / 强制回退检测（核心）

```
UP_COUNT = 0                                              → upstream_empty，阻断
MI_COUNT >= 50 且 UP_COUNT < MI_COUNT / 5                 → commit_cliff，阻断
```

阈值通过 env 暴露：`WIPE_GUARD_MIN_COMMITS=50`、`WIPE_GUARD_RATIO=5`。

**分叉（rebase）不阻断，只告警**：上游 rebase / force-push 会让历史互不祖先，但提交数相近时属于正常开发行为。若对分叉也阻断，会导致 37 个仓库永久卡死 —— 这是明确权衡后的取舍。

阻断时：跳过该分支的 reset + push，固化永久快照 `backup/<branch>/<日期>-<sha7>`，记入 `WIPE_BLOCKED`。

### 防护 3：retag 保护

`fetch --tags --prune --force` 之前先给本地 tag 拍快照，fetch 后 diff，对被上游改动或删除的 tag，把**我们的旧值**另存为 `retag-guard/<tag>/<日期>`。

修复的隐患：上游 retag（同名 tag 指向新 commit）会通过 `push --force refs/tags/*` 连带覆盖镜像的同名 tag，旧 commit 若不被任何 ref 引用即被 GC。

### 失败语义

有分支被阻断时，在 `push --force refs/tags/*` **之后** `exit 1`（保证保护性 tag 已落地远端），输出 `::error title=...` 触发 GitHub 通知。

紧急绕过：仓库 Variables 设 `WIPE_GUARD_BYPASS=1`（人工确认上游是 rebase 而非清库后使用）。

## 验证

三个场景全部实测通过（测试仓 `mirror-CipherTalk-test`，上游用真实的已清库 `ILoveBingLu/CipherTalk`）：

| 场景 | 输入 | 结果 |
|---|---|---|
| 清库阻断 | 上游=1 / 镜像=1335 | `commit_cliff` 阻断，main 保持 1335，固化 `backup/main/20260902-1646-24b3e03`，conclusion=failure |
| 正向（自镜像） | 上游=1336 / 镜像=1336 | 不阻断，无 warning，conclusion=success |
| 正向（真实健康仓） | `mirror-Netease_url` 上游=90 / 镜像=92 | 不阻断，conclusion=success，tag 9→10（新增 anchor/main） |

**关键对照**：同一测试仓在旧脚本下 main 被从 1334 重置为 2；新脚本下 main 完整保持 1335。

部署后全量校验 37/37 个 enabled 镜像仓的 `.github/workflows/dmca-backup.yml` 均已含 `WIPE_GUARD_MIN_COMMITS`。

## Consequences

**正面**
- 任何单个上游清库不再能摧毁镜像。最坏情况是同步停止 + 告警，而非内容被覆盖。
- retag 场景下的历史版本不再静默丢失。
- 每次同步自动产生 `anchor/<branch>`，为所有 37 个仓补齐了此前缺失的锚点保护。

**代价 / 风险**
- 上游若发生**超过 80% 的大规模 rebase**（罕见），会误判为清库并阻断。此时 run 失败 + 告警，人工确认后设 `WIPE_GUARD_BYPASS=1` 重跑即可，不会丢数据。
- 渐进式破坏（每次删一点，单轮跌幅 < 80%）仍在防护盲区内。缓解手段是 `anchor/<branch>` 提供的一个周期回滚窗口。
- `anchor/<branch>` 每分支固定 1 个 tag，对多分支仓（如 `cc-switch` 133 分支）会增加等量 tag。可接受，且不随同步轮次增长。

**未采纳的方案**
- 监控侧检测到 `upstream_wiped` 后自动 disable 该仓 workflow（`loop_actions.py`）：能提供更快的止损，但需改后端 Python + 重启服务，且脚本层防护已覆盖该场景。列为后续可选项。
- 对历史分叉也阻断：会导致正常的 rebase 仓库永久卡死，误报代价远高于漏报。

## 相关文件

- `workspace/gh_mirror/workflow_template.yml` —— 同步脚本模板（本次修改）
- `workspace/gh_mirror/gh_mirror_init.py::update_workflow_all_repos` —— 批量部署入口
- `workspace/gh_mirror/loop_actions.py::_disable_mirror_sync_workflows` —— 监控侧联动：wipe 类 critical 自动禁用同步/备份 workflow（第二层防线）
- `workspace/gh_mirror/RUNBOOK_wiped_upstream.md` —— 上游清库处置手册
- `temp/gh_mirror_health.py` —— 全仓体检脚本（只读，输出上游/镜像提交数对比）
