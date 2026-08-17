# auto_shutdown — 自动关机（挂机监控）

> task_type: `adhoc.auto_shutdown`
>
> 关联端点：`POST /auto_shutdown/trigger` + `POST /auto_shutdown/cancel`（[server/auto_shutdown.py](file:///f:/<project_root>/server/auto_shutdown.py)）
>
> 历史背景：原 `workspace/auto_shutdown/monitor.py` 独立 CLI 子进程已于 2026-08-03 删除，关机流程收进后端端点。agent 现在在 Trae 会话里自己跑检测 loop，不依赖独立子进程。

## 何时使用

用户说以下任一时：
- "挂机到 XX 完成自动关机" / "游戏挂机关机" / "等下载完关机"
- "23:30 自动关机" / "定时关机" / "X 点关机"
- "等 Trae 里另一个 agent 完成后关机" / "条件关机"
- "睡前关机" / "后台关机"

## 工作流（agent 自己跑 loop + 调端点）

```
1. 确认触发条件 + task_id
   - task_id 用于截屏目录隔离（temp/auto_shutdown/<task_id>/final_screenshot.png）
   - reason 描述触发原因（agent 描述检测到什么）

2. agent 自己写检测 loop
   - 用 time.sleep 或 asyncio.sleep 控制检测间隔（60s 常用）
   - 检测函数 agent 自行编写（识图 / 查日志 / 查 API / 查文件 / 查时间）
   - 不依赖任何内置条件检测（旧 6 种条件已删除）

3. 条件满足 → 调 POST /auto_shutdown/trigger
   - 端点内部自动：截屏 + 推 inbox + 调 shutdown /s /t 120
   - 120s 倒计时（不是 60s，多 60s 宽限用户刹停）
   - 返回 {success, task_id, trigger_count, shutdown_scheduled, dry_run, screenshot_path, cancel_command}

4. 用户反悔 → 调 POST /auto_shutdown/cancel
   - 端点调 shutdown /a 中止倒计时
   - 幂等：重复调不报错

5. 关机完成 / 用户中止
```

## 关键约束（强制）

### 1. 调 trigger 前先关闭有窗口的程序

**模拟器、全屏应用、有未保存数据的程序在系统直接关机时会阻碍关机流程**（弹窗阻止 / 资源占用 / 文件锁）。agent 调 trigger 前应：

- 用 `list_windows` 查当前所有窗口
- 识别需要关闭的程序（模拟器如 MuMu/雷电/Nox、游戏客户端、IDE、有未保存文档的程序）
- 用 `screen_window_close` 或 `execute_action`（Alt+F4）逐个关闭
- **不要用 taskkill 强杀**（可能丢失用户数据），优先 graceful close
- 模拟器类程序关闭可能需要确认弹窗，agent 要处理

端点不强制此约束（端点只负责关机本身，关窗口是 agent 的 loop 责任）。

### 2. 120s 倒计时

`shutdown /s /t 120` 触发后 Windows 弹窗 120s 倒计时。用户可在 120s 内：
- 喊"取消关机" → agent 调 `/auto_shutdown/cancel`
- 或自行执行 `shutdown /a`

### 3. 关机命令绕过 command_guard

用户睡前无法审批 command_guard 弹窗，端点内部直接调 `subprocess.Popen(["shutdown", "/s", "/t", "120"])`，绕过 command_guard。这是设计取舍，已明确同意关机。

### 4. 端点幂等

- 重复调 trigger：递增 trigger_count，不报错
- 重复调 cancel：返回 success=True，不报错

### 5. 副作用容错

- 截屏失败（mss 异常）→ screenshot_path=None，仍触发关机
- inbox 推送失败（后端不可用）→ 记日志，仍触发关机
- 用户睡前 inbox 不重要，关机本身重要

## 端点参数

### POST /auto_shutdown/trigger

```json
{
  "task_id": "game_afk_20260803",  // 必填，截屏目录隔离用
  "reason": "游戏完成",             // 可选，默认 ""
  "screenshot": true,               // 可选，默认 true（关机瞬间截屏存档）
  "dry_run": false                  // 可选，默认 false（true 跳过 shutdown 调用，测试用）
}
```

返回：
```json
{
  "success": true,
  "task_id": "game_afk_20260803",
  "trigger_count": 1,
  "shutdown_scheduled": true,
  "dry_run": false,
  "screenshot_path": "temp/auto_shutdown/game_afk_20260803/final_screenshot.png",
  "cancel_command": "shutdown /a"
}
```

### POST /auto_shutdown/cancel

```json
{"dry_run": false}  // 可选，默认 false
```

返回：
```json
{
  "success": true,
  "dry_run": false,
  "cancel_command": "shutdown /a"
}
```

## MCP 工具调用

```
# 触发关机
localagent_advanced_tool(
    tool="auto_shutdown_trigger",
    params={"task_id": "game_afk", "reason": "游戏完成", "screenshot": true, "dry_run": false}
)

# 取消关机
localagent_advanced_tool(
    tool="auto_shutdown_cancel",
    params={"dry_run": false}
)
```

## 示例检测脚本（agent 自行编写，仅供参考）

### 示例 1：游戏完成检测（识图）

```python
import time
import urllib.request
import json

def check_game_done():
    """截图 + OCR 判断游戏是否完成"""
    # 用 MCP 工具：capture_screen + screen_ocr
    # 或用 localagent_advanced_tool(tool='screen_ocr', params={...})
    # 返回 True/False
    pass

task_id = "game_afk_20260803"
while not check_game_done():
    time.sleep(60)  # 60s 检测间隔

# 条件满足，先关窗口程序（模拟器等），再触发关机
# ... list_windows + screen_window_close ...

# 触发关机
data = json.dumps({"task_id": task_id, "reason": "游戏完成", "screenshot": True}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8766/auto_shutdown/trigger",
    data=data, headers={"Content-Type": "application/json"}, method="POST"
)
urllib.request.urlopen(req, timeout=10)
```

### 示例 2：定时关机（处理跨日边界）

```python
import time
from datetime import datetime, timedelta

def get_next_target(today_now: datetime, target_hhmm: str) -> datetime:
    """处理跨日边界：23:35 启动 target="23:30" → 明天 23:30"""
    h, m = map(int, target_hhmm.split(":"))
    target_today = today_now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target_today <= today_now:
        # 目标时间已过，定为明天
        return target_today + timedelta(days=1)
    return target_today

target = get_next_target(datetime.now(), "23:30")
while datetime.now() < target:
    time.sleep(30)

# 触发关机
# ... 调 /auto_shutdown/trigger ...
```

### 示例 3：文件出现检测

```python
import time
from pathlib import Path

flag_file = Path("temp/agent_done.flag")
while not flag_file.exists():
    time.sleep(30)

# 触发关机
# ... 调 /auto_shutdown/trigger ...
```

## 常见陷阱

1. **不要找 monitor.py**：旧独立子进程已删除，agent 自己跑 loop
2. **不要用 subprocess 直接调 shutdown**：必须走端点（端点内部统一处理截屏 + inbox + 命令）
3. **dry_run 测试**：测试时显式传 `dry_run=true`，生产传 `dry_run=false`
4. **关机前关窗口**：模拟器/全屏应用会阻碍关机，agent 调 trigger 前先关
5. **120s 不是 60s**：倒计时 120s，多 60s 宽限用户刹停
6. **inbox 失败正常**：用户睡前 inbox 不重要，关机本身重要
7. **trigger_count 不持久化**：后端重启归零，inbox 有历史记录可查

## 看门狗模式联动（自然行为，不单独 spec）

- agent loop 跑 screen 操作前，应确认看门狗授权 active（调 `/screen/grant/status`）
- agent 每次 screen 操作成功会自动续期看门狗授权
- 用户撤销看门狗授权 → agent 下次 screen 操作被拒 → agent 应识别此信号并停止 / 询问用户
- agent 触发关机时，看门狗授权自然失效（系统关机），无需显式撤销

## 相关文件

- [server/auto_shutdown.py](file:///f:/<project_root>/server/auto_shutdown.py) — 端点实现
- [tests/test_auto_shutdown.py](file:///f:/<project_root>/tests/test_auto_shutdown.py) — 测试（含 autouse 安全网 fixture 防真关机）
- `temp/sdd/auto-shutdown-refactor/spec.md` — SDD 规约（含危险操作测试铁律）
- `docs/dev-workflow.md` "危险操作测试铁律" 章节 — 测试安全规范
