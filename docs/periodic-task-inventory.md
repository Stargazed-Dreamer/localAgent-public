# 周期任务与空闲额度跑批台账（workspace 触发真源）

> **用途**：一次性回答三个问题——① 哪些 workspace 任务本质上需要周期性跑；② 现在分别由谁提醒；③ 用户说"我额度很多 / 想跑长期自动化"时，agent 该核对哪些、能提议什么。
> **下次别再重新探**：本表就是探完的结果。新增/停用 workspace 时同步改本表（见「维护」段）。
> 判据一律指向代码或运行时端点，不写会漂移的计数。

## 三种触发机制（先分清，再谈"谁该提醒我"）

| 机制 | 谁动 | 消费入口 | 特征 |
|------|------|----------|------|
| ① 自动 loop | loop 引擎自己 | `GET /loop/tasks`（间隔来自 `config.toml` 的 `[loops.*]`） | 完全不用人管；`workspace/*/manifest.toml` 的 `[loop_tasks]` 声明候选动作 |
| ② 到期看板 | **用户开口** | `GET /todos` → `GET /todos/due`，判据 `server/todos/store.py` 的 `get_due_todos()`；展示在 client DueTodos 面板 + Dashboard | 到点了只会"挂在看板上"，**没有推送**，agent 不会因此自主跑 |
| ③ 机会型 | 用户主动问 | 本文件「空闲额度自查流程」 | 不注册任何定时器；只在用户表示额度有余时由 agent 核对后提议 |

`frequency` 只支持 `daily / weekly / monthly / quarterly`（`server/todos/models.py` 的 `TodoFrequency`）。`condition` 是自由文本，其自动化判定依赖 `todos_trigger_check` action——该 action 已注册但**没有实例化任务**，所以"只有条件成立才提醒"这件事系统做不到，只能靠 `condition_status` 人工置位（`niuke_review` 就是这么用的）。这正是第 ③ 类存在的理由。

### 第 ③ 类怎么被路由到（2026-09-20 落地，别再重新探）

`agent_guide` 决定返不返完整 TaskGuide 的门是 **`strong_match`**，不是分数：`is_weak = score < WEAK_MATCH_THRESHOLD or not strong_match`（`server/agent_guide.py` 的 `get_agent_guide`），而 `strong_match` 只认 `kw_exact`、`kw_fuzzy ratio≥80%`、或语义补强（`cosine≥0.6`，或 `score≥阈值 且 cosine≥0.4`）。**分数再高但 `strong_match=False` 照样走 `low_confidence`**（实测：一次弱匹配 score=33 仍 `low_confidence`）。`low_confidence` 分支刻意不注入任何记忆与项目结构（省 token，且此时 task_type 未定）。

要让"额度很多"这类口语进得来，做的是**把词注册进 `system.task_reminder.keywords`**（`server/agent_guide_data.py`），并把台账指针直接写进该条目的 `first_action`——这样 TaskGuide 一返回就带着指针，不依赖记忆注入的排序。已注册词：额度很多/额度有余/额度多余/额外额度/空闲额度/长期自动化/顺便跑/能跑什么。**实测**四句原话全部靠 `kw_exact` 强匹配（score 19-42，与 top-2 分差 ≥17），且把语义补强整体关掉后分数一字不变——即命中不依赖 embedding 可用；"起个后端自主会话"仍归 `adhoc.headless_session`（`tests/guide_eval/cases.jsonl` 已加防回归用例）。

同批修掉的两个消费侧缺陷（`server/agent_guide.py` 的 `_build_task_guide`）：

- **`trigger_keywords` 此前是死字段**：调用 `find_consumable_memories` 时传的是候选 skill 的**显示名**（如"任务提醒"），不是用户原话，所以按名字做子串匹配的关键词永远命中不上——而 AGENTS.md 与 `展示文档/architecture_analysis/04_skill_routing.md` 都写作"任务描述出现这些词时应读取"。现改为透传 `task_query=task`，无文本时（`task_type=` 精确调用）退回旧的显示名行为。
- **【必读记忆】此前恒取最旧 5 条**：`find_consumable_memories` 没有 ORDER BY（返回≈写入序），`first_action` 却直接 `[:5]`，导致必读块长期被几十天前的通配记忆占据、新写的记忆永远排不进。现按 `_MUST_READ_RANK`（静态 `memory_key` > `context+keyword` > `keyword` > 仅 `context`）+ 越新越前排序。回归测试在 `tests/memory/test_find_consumable_memories.py`。

> **生效前提**：`GUIDE_REGISTRY` 与 `_build_task_guide` 都在后端进程里，改完要**重启后端**；registry 变更后 embedding 缓存（`data/agent_guide/embeddings_cache.json`）按 hash 自动重建，首次匹配会慢一次。

## 空闲额度自查流程（agent 必读）

**触发语**："我现在额度很多"、"想跑一下长期自动化"、"有什么可以顺便跑的"、"空着的额度能干啥"。

**动作**：逐项取下面台账的"上次活动"信号 → 与阈值比较 → 一次性报告**超期项**，每项附一句"跑它要什么、大概多久、有什么风险" → 等用户点单。**禁止**不核对就断言"都新鲜"，也**禁止**用户没点就开跑。

| 任务 | 阈值 | 上次活动核对信号 | 怎么跑 | 注意 |
|------|------|------------------|--------|------|
| homework 作业巡检 | 额度有余且 >1 天没动；无条件 >1 周没动 | `workspace/homework/records/journal.md`、`records/todos.md` 的 mtime | 按 `workspace/homework/AGENTS.md` 的五段流水线（获取/检查/完成/汇总/人工提交） | 砺儒云统一身份认证需人工登录，agent 不代填密码；提交环节恒为人工 |
| web_archive 超链接群导出+跑 | >1 周没动 | `workspace/web_archive/reports/`、`state/` 内最新文件 mtime；weflow 增量另看 `weflow/doc.weflow.top/` | 见 `workspace/web_archive/SKILL.md` 每日流程 | 全站慢爬每天限额 20 篇、每篇间隔 15~30s，**本来就是手动触发** |
| gh_mirror 告警处理 | 额度有余且 >1 周没人工处理 | `GET /loop/tasks` 里 `gh_mirror.monitor` 的 `last_run_at` + `GET /inbox?status=new` 是否有未处理镜像告警 | `workspace/gh_mirror/health_check.py`；上游被删走 `RUNBOOK_wiped_upstream.md` | 监控本身每 24h 自动（`config.toml` `[loops.gh_mirror] monitor_interval`），要人做的是"处理告警" |
| official_gacha 官方抽奖扫描 | 无自动；有空可跑 | `workspace/official_gacha/latest_scan.json` 的 mtime | `workspace/official_gacha/SKILL.md`（Playwright 连 CDP 9222 扫官方账号近 7 天动态） | **故意不注册提醒**（风控顾虑）。参与抽奖是键鼠+OCR 实操，弹窗需用户确认 |
| modelscope 模型库更新 | 每月；额度有余可提前 | 最新报告文件名日期：`workspace/modelscope_model_update/report_*.md`、`推荐报告_*.md` | `workspace/modelscope_model_update/SKILL.md` | 结果会**覆盖写 `data/llm/keys.json`**，必须用户确认后再写；魔搭会不定期下架模型 ID，拖太久等于模型池悄悄失效 |
| API Key 全量健康检查 | 随时可加跑 | `GET /apikey/status` | `POST /apikey/keys/health-check-all` | 已每 12h 自动（`[loops.key_health]`），此条只是额度多时的补跑 |
| 记忆压缩/维护 | 长会话后、或批量写记忆后 | `GET /memory/stats` | `POST /memory/compress`、`POST /memory/maintain` | `memory.maintain` 已每 6h 自动 |

## 已注册周期任务台账

以 `GET /todos` 为准，下列是当前注册项与其真实语义：

| todo id | 频率 | workspace | 语义 |
|---------|------|-----------|------|
| `accounting` | 每周 | `accounting` | 导出账单 + GUI 审核 + 导出 Obsidian |
| `bilibili_gacha` | 每周 | `bilibili_gacha` | 查开奖、清理未中奖动态与关注 |
| `niuke_review` | 每周 | `niuke_review` | 带 `condition="找工作期间"`；`condition_status=inactive` 时才不挂看板 |
| `wuwa_gacha` | 每月 | `arknights_gacha` + `yihuan_gacha` | **伞形条目**：一条 todo 覆盖两个仍在玩的游戏增量采集（方舟官网只留 90 天，必须按月采） |
| `todo_251d1433` | 每季度 | `life_design` | 被动展示型：到期只挂看板，用户主动说"做人生设计"agent 才读 `workspace/life_design/prompt.txt` 开对话 |
| `todo_20c74a86` | 每月 | `modelscope_model_update` | 2026-09-20 新增，替代"全靠用户记得开口" |
| `todo_df6b969a` | 每周 | `homework` | 2026-09-20 新增；"额度有余且一天没动"那档走上面机会型台账 |
| `todo_955d1de3` | 每周 | `web_archive` | 2026-09-20 新增；对应"超链接群导出+跑一周没动" |

> **已知行为，不要再当 bug 报**：`get_due_todos()` 只在 `next_due_at` 有值且 `<= today` 时判到期，而 `next_due_at` 仅由 `mark_done()` 写入。所以 `last_done_at` 有值、`next_due_at` 为 null 的条目**永远不会再进到期名单**。当前 `community_review` / `arknights_gacha` / `yihuan_gacha` 属此类，均为有意保留（后两条被上面的伞形条目覆盖）；要恢复提醒就正常跑一次并 `todos_mark_done`，`next_due_at` 会被重算。

## 自动 loop 台账（不用你管的那些）

以 `GET /loop/tasks` 为准。当前实例与间隔（间隔值取自 `config.toml` 对应 `[loops.*]` 段）：

| task_id | 间隔 | 作用 |
|---------|------|------|
| `activity_tracker.collect_windows` | 60s | 活动追踪窗口采样 |
| `activity_tracker.screen_vl` | 300s | 屏幕 VL 摘要 |
| `activity_tracker.hourly_summarize` | 每小时 | 小时级活动总结 |
| `activity_tracker.daily_summarize` | 每天 | 日总结（写 `private_vault/activity/daily/`） |
| `activity_tracker.cleanup` | 按配置 | 活动数据清理 |
| `download_watcher.scan` | 300s | 下载目录新文件监视 |
| `gh_mirror.monitor` | 86400s | 镜像 4 类状态检查，告警推 inbox |
| `memory.maintain` | 21600s | 记忆老化+验证维护 |
| `key_health.check` | 43200s | LLM 密钥可用性与余额 |
| `secret_backup.run` | 432000s | 密钥文件备份 |
| `workspace_backup.run` | 432000s | 各 workspace 声明的 `[backup]` 数据文件备份 |
| `stock_advisor.opening` / `.closing` / `.evening` | cron `35 9 * * 1-5` / `5 15 * * 1-5` / `0 21 * * 1-5` | 股市三时段分析（evening 带 catchup） |

`[loop_tasks]` 声明了候选动作但**未注册实例**的：`email_source`（`[loops.email_source] enabled = false`，故意关着）、以及多数 workspace 的 `loop_actions.py`——那里只放 `GUIDE_REGISTRY_ENTRIES`（agent_guide 路由条目），不是 loop 任务。

## 有意不做周期化（下次别顺手补提醒）

| workspace | 为什么不补 |
|-----------|------------|
| `mindforge` | SKILL.md 明写"低频模块，agent 不主动推荐" |
| `endfield_gacha`、`wuwa`（单游戏采集） | 已退坑，不采集（见记忆 `gacha_games_status`） |
| `official_gacha` | 怕风控，故意不注册提醒；有空跑走上面机会型台账 |
| `task_room`、`chat_digest`、`yefeng_group4`、`ai_club_recruit` | 用户裁决"我问再说"；后两者本身有明确时间窗 |
| `exam_prep` | 期末季节性，非全年周期 |
| `disk_manager` | 事件驱动（空间不足才跑）；全面备份功能标注未测试 |
| `stock_advisor` 月度简评 | 用户裁决不提醒；`valid_until` 月底自动失效已有机制 |
| `gh_mirror`、`stock_advisor` | 有 `recurring.*` skill 条目但由 loop 全自动驱动，**不需要**再建 todo |
| `recorder`、`windows_forensics`、`yihuan_simulator`、`dev_toolkit`、`apikey_test`、`image_organize_remote_vl_sample`、`web_archive/dev` | 工具型/取证型，被调用才动 |
| `homework` 的 `SKILL.md`/guide 注册 | 仍是待办：manifest 明写"agent_guide / skill 注册暂缓，待首个科目工作流跑通"。本轮只补了 todo 入口，没动注册状态 |

## 维护

- 新增 workspace：判断它属于"自动 loop / 到期看板 / 机会型 / 有意不周期"四类中的哪一类，写进对应表；机会型必须给**可核对的上次活动信号**（文件路径或端点），否则不进台账。
- 改周期判据逻辑时同步本文件：到期判据在 `server/todos/store.py`，频率枚举在 `server/todos/models.py`，loop 实例在 `config.toml` `[loops.*]`。
- 相关系统文档：`docs/todos-wip.md`（todo/WIP 系统本身）、`docs/operations-manual.md`（loop 与终端 API）、`AGENTS.md`（`agent_guide` 路由与收尾闭环）。
