---
name: daily_summary
description: >
  今日工作总结：读取后端 Loop 系统采集的每小时活动汇总，生成当日工作总结日志。
  当用户说"今日总结"、"工作总结"、"今天干了什么"、"日报"、"今日工作总结"时使用。
  日期划分按 05:00（当前<05:00 用前一天日期）。触发词：今日总结、工作总结、今天干了什么、
  日报、今日工作总结、daily summary。
task_type: recurring.daily_summary
---

# daily_summary - 今日工作总结

## 概述

读取后端 Loop 系统采集的每小时活动汇总（`data/activity/hourly/*.md`），生成当日工作总结日志，存到 `private_vault/activity/daily/`（obsidian vault，不进 release）供用户审阅删改。

**依赖**：后端 Loop 系统的 `activity_tracker` 任务需运行（每分钟采集窗口 + 每5分钟VL描述 + 每小时LLM总结）。若 Loop 未运行，hourly 目录无数据。

## 日期划分规则（重要）

**每天按 05:00 划分，而非 00:00。** 即"当天" = 当日 05:00 ~ 次日 05:00。

- 凌晨 00:00-05:00 的活动**归属前一天**（例如 07-11 02:50 的活动算作 07-10 当天）
- 文件名 `YYYYMMDD` 中的日期代表**当天的开始日期**（即 05:00 那个时刻的日期）
- 示例：`20260710.md` 包含 2026-07-10 05:00 ~ 2026-07-11 05:00 的数据

**划分原因**：用户的"一天"以凌晨 05:00 为界（睡眠周期结束），凌晨时段的工作应归属于前一天的活动。

### "今天"在日报场景中的定义（关键，agent 易误解）

用户说"总结今日" / "今天的日报" / "今日工作" 时，**"今天" = 从上一个 05:00 到当前时刻**，不是"等到下一个 05:00 才算今天结束"。

- 用户要日报时通常是要睡觉或结束工作，**不会期待更多记录**——立即基于已有 hourly 文件汇总生成，不要因"今天还没到 05:00 结束"而推迟或询问"要不要等"
- 日报覆盖的时段是 **上一个 05:00 → 当前时刻**（不是上一个 05:00 → 下一个 05:00）
- 缺失的当前小时（如 04:00-05:00 时段 hourly 尚未触发）在日报中标注"进行中/尚未生成"即可

**文件名由当前时间决定**：
- 当前时间 ≥ 05:00 → 日报文件名用**当天日期**（如 07-14 10:00 请求 → `20260714.md`，覆盖 07-14 05:00 → 当前）
- 当前时间 < 05:00 → 日报文件名用**前一天日期**（如 07-14 04:00 请求 → `20260713.md`，覆盖 07-13 05:00 → 当前）

## 触发方式

**两种触发方式**：

### 1. Loop 自动触发（默认，推荐）

后端 Loop 系统的 `DailySummarizeAction` cron `"30 5 * * *"` 每天 05:30 自动生成前一天的日报，`catchup=True` 后端未启动时启动后自动补跑错过的触发。无需 agent 介入。

- 触发时刻：05:30（给 05:00 的 hourly_summarize 留 30 分钟完成时间）
- 覆盖时段：前一天 05:00 ~ 当天 05:00（与日期划分规则一致）
- 输出文件：`private_vault/activity/daily/YYYYMMDD.md`（YYYYMMDD = 前一天日期，obsidian vault 不进 release）
- 已存在不覆盖（避免覆盖用户已编辑的日报）

配置项（`config.toml [loops.activity_tracker]`）：
- `daily_summarize_cron`：cron 表达式，默认 `"30 5 * * *"`
- `daily_summarize_max_retries`：quota/rate_limit 类失败退避重试次数，默认 3
- `daily_summarize_retry_backoff`：退避基数（秒），默认 60（60→120→240 指数退避）

### 2. Agent 手动触发（补充）

用户说以下任一关键词时，agent 调 `agent_guide(task='今日工作总结')` 命中：
- "给我今日工作总结"
- "今日总结" / "工作总结" / "今天干了什么" / "日报"

适用场景：
- 用户在白天（非 05:30）想立即生成当前已有时段的日报
- 自动生成的日报失败（占位文件），用户要求重新生成
- 用户想基于已有 hourly 数据让 agent 用 IDE 模型重新汇总

**手动触发时**：agent 应先检查 `private_vault/activity/daily/{date}.md` 是否已存在；若已存在且非占位文件，提示用户"今日日报已自动生成"并询问是否覆盖。

## 工作流

### 1. 读取今日 hourly 汇总

```python
# 读取 data/activity/hourly/ 下今日所有 .md 文件
# 注意：hourly 文件按自然日命名（YYYYMMDD_HH.md），但日报按 05:00 分界，
# 凌晨时段（00:00-05:00）的 hourly 文件属于"今天"但文件名是当前自然日
from datetime import datetime, timedelta
from pathlib import Path

now = datetime.now()
hourly_dir = Path("data/activity/hourly")

def _hour_of(name: str) -> int:
    """从 YYYYMMDD_HH.md 或 YYYYMMDD_HH_vN.md 提取小时"""
    return int(name.split("_")[1].split("_v")[0])

if now.hour < 5:
    # 凌晨时段：今天 = 前一天，需跨两个自然日的 hourly 文件
    yesterday = now - timedelta(days=1)
    prev_str = yesterday.strftime("%Y%m%d")  # 前一天 05:00-23:00
    curr_str = now.strftime("%Y%m%d")         # 今天 00:00-当前
    today_files = sorted(
        list(hourly_dir.glob(f"{prev_str}_*.md")) +
        list(hourly_dir.glob(f"{curr_str}_*.md"))
    )
    # 过滤：保留前一天 HH>=5 的 + 今天 HH<5 的
    today_files = [
        f for f in today_files
        if (f.name.startswith(prev_str) and _hour_of(f.name) >= 5)
        or (f.name.startswith(curr_str) and _hour_of(f.name) < 5)
    ]
else:
    # 白天时段：今天 = 当天，只需当天 hourly 文件
    date_str = now.strftime("%Y%m%d")
    today_files = sorted(hourly_dir.glob(f"{date_str}_*.md"))
    # 过滤掉 HH<5 的（属于前一天的日报）
    today_files = [f for f in today_files if _hour_of(f.name) >= 5]

# today_files 现在包含上一个 05:00 到当前时刻的所有 hourly 文件
```

若 `today_files` 为空：
- 提示用户"后端 Loop activity_tracker 未运行或今日无采集数据"
- 检查 `/health` 的 `loops` 字段确认 Loop 是否启用
- 不要继续后续步骤

### 2. 汇总为日总结

将所有 hourly md 内容拼接，调 LLM 生成日总结：
- 输入：各小时的 hourly 总结（按时间顺序）
- 输出：一份结构化日总结（Markdown）
- LLM：用当前 IDE 的模型（GLM-5.2），走 IDE 自身 token

日总结结构建议：
```markdown
# YYYY-MM-DD 工作日志

## 概述
<一句话概括今天的主要工作>

## 时间线
- 09:00-10:00 <活动>
- 10:00-11:00 <活动>
...

## 主要成果
- <成果1>
- <成果2>

## 备注
- <缺失时段说明，如"13:00-14:00 无采集数据">
```

### 3. 写入 daily 目录

```python
daily_dir = Path("private_vault/activity/daily")  # obsidian vault，不进 release
daily_dir.mkdir(parents=True, exist_ok=True)
out_path = daily_dir / f"{date_str}.md"
# 已存在不覆盖，追加 _v2
if out_path.exists():
    out_path = daily_dir / f"{date_str}_v2.md"
out_path.write_text(summary, encoding="utf-8")
```

### 4. 提示用户审阅并修改

告知用户日总结已生成，展示内容摘要。用户会给出修改意见（如"加一段XX"、"去掉YY"、"重点突出ZZ"）。

根据用户意见修改日总结，通过 API 写回：
```python
# 用 exec_python 调用后端 API 更新内容
import requests
API = 'http://127.0.0.1:8766'
requests.put(f'{API}/activity/daily/{filename}', json={'content': revised_summary})
```

### 5. 审核标记

用户确认无需修改后，调用审核 API 将文件移到 `reviewed/` 子目录（永久保留，不被自动清理）：
```python
import requests
API = 'http://127.0.0.1:8766'
r = requests.post(f'{API}/activity/daily/{filename}/review')
# 返回 {"name": "...", "reviewed": true, "moved_to": ".../reviewed/..."}
```

**审核机制说明**：
- 未审核的 daily 文件（在 `daily/` 根目录）：保留 30 天后自动删除
- 已审核的 daily 文件（移到 `daily/reviewed/`）：永久保留
- 用户说"好了"、"可以了"、"没问题"等确认词后即可执行审核标记

## 数据格式

### hourly md 文件（Loop 生成）
- 路径：`data/activity/hourly/YYYYMMDD_HH.md`
- 内容：LLM 生成的小时活动总结（Markdown）
- 文件名示例：`20260710_13.md`（13:00-14:00 的总结）

### daily md 文件（agent 生成）
- 路径：`private_vault/activity/daily/YYYYMMDD.md`（obsidian vault，不进 release）
- 内容：agent 汇总当日所有 hourly 生成的日总结
- **日期含义**：`YYYYMMDD` 代表当天的开始日期，文件覆盖范围为该日 05:00 ~ 次日 05:00（详见"日期划分规则"）
- **审核后路径**：`private_vault/activity/daily/reviewed/YYYYMMDD.md`（永久保留）

## 数据保留策略

| 数据类型 | 保留天数 | 清理方式 |
|---------|---------|---------|
| raw（windows/screen jsonl） | 3 天 | CleanupActivityAction 每天 00:30 自动删除 |
| hourly（每小时总结 md） | 7 天 | CleanupActivityAction 每天 00:30 自动删除 |
| daily（每日总结 md） | **永久** | 不清理（已挪到 private_vault/，用户要求生成了就保留） |
| daily/reviewed/（审核过的） | **永久** | 不清理 |

配置项在 `config.toml [loops.activity_tracker]`：`raw_retention_days`、`hourly_retention_days`、`cleanup_cron`。

## Daily API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/activity/daily` | 列出所有 daily 文件（含 reviewed/） |
| GET | `/activity/daily/{name}` | 读取文件内容 |
| PUT | `/activity/daily/{name}` | 编辑文件内容（body: `{"content": "..."}`） |
| POST | `/activity/daily/{name}/review` | 审核标记（移到 reviewed/） |

## 关键注意事项

1. **hourly 文件可能不全**：后端未全程运行时，部分小时无总结。需在日总结中标注缺失时段。
2. **不覆盖已有日总结**：若 `YYYYMMDD.md` 已存在，写 `YYYYMMDD_v2.md`，避免覆盖用户已编辑的内容。
3. **数据隐私**：hourly 文件含用户活动轨迹（窗口标题、屏幕描述），日总结同样敏感，仅存本地。
4. **raw 数据不进日总结**：`data/activity/raw/*.jsonl` 是原始采集数据，日总结只读 hourly md，不直接处理 raw。
5. **时区**：文件名用本地日期（Asia/Shanghai），`YYYYMMDD` 按本地时间。
6. **日期划分按 05:00**：凌晨 00:00-05:00 的活动归属前一天。文件名由当前时间决定（当前 < 05:00 用前一天日期，详见"日期划分规则"）。**hourly 文件按自然日命名**，凌晨时段需跨两个自然日的文件（详见工作流第 1 步代码）。
7. **不要加入 agent 的 task_closure 工作报告**：日报是用户的活动记录，不是 agent 的收尾报告，两者是不同的东西。

## 验证

```bash
# 检查今日 hourly 文件
dir <project_root>\data\activity\hourly\YYYYMMDD_*.md

# 生成日总结后检查
dir <project_root>\private_vault\activity\daily\YYYYMMDD.md
```
