# 工具脚本详细文档
> 本文档是 `AGENTS.md` 中工具脚本的详细展开。工具目录总览见 `tools/README.md`，机器可读清单见 `tools_manifest.json`。

## exec_python 统一 terminal 机制 + 3 分钟唤醒 LLM（v15 起，v16 内联等待增强）

**v15 起 exec_python 改为异步 terminal 机制**（SDD 流程 `temp/sdd/exec-terminal-unify/`）。exec_python 不再同步阻塞，而是写 `temp/exec_<uuid>.py` → 调 `terminal_spawn` 起后台子进程 → 返回 `{terminal_id, status, temp_file, pid}`。所有 exec_* 共用 terminal registry，inspect/kill/send_input/wait 4 个工具与 `exec_terminal_*` 行为一致。

**v16 内联等待**（`temp/sdd/exec-python-inline-wait/`）：exec_python 在 spawn 成功后默认内联等 `inline_wait_secs` 秒（`[server] exec_python_inline_wait_secs`，默认 10s）。子进程在 N 秒内结束 → 响应 `status="done"` + 完整 `stdout/stderr/exit_code/elapsed`（agent 一步搞定）；超时仍 running → 响应 `status="running"` + `terminal_id`（同 v15）。`0=禁用`回退 v15 立即返回行为。

### 5 个工具（全部 MCP 直连）

| 工具 | 参数 | 行为 | SessionRunner 包装 |
|------|------|------|---------------------|
| `exec_python` | `code`, `cwd?`, `environment?`, `python_path?` | 写 temp .py → 调 terminal_spawn → 默认内联等 10s：done 返回完整 stdout/exit_code，running 返回 terminal_id | **是**：status=done 短路直接用 result；status=running 自动 exec_inspect(tid, timeout=180) |
| `exec_inspect` | `tid`, `timeout?` | 不传 timeout：立即返回快照；`timeout>0`：**新输出/子进程结束/N秒到任一触发立即返回**（spec D4） | 否 |
| `exec_kill` | `tid` | 终止子进程（SIGKILL 等价，幂等） | 否 |
| `exec_send_input` | `tid`, `text` | 向 stdin 发送文本+换行 | 否 |
| `wait` | `seconds` | 纯 sleep（通用工具，不查 terminal 状态，spec Anti-Cheat 11） | 否 |

### 用法示例

**短任务（<10s，v16 默认）**：exec_python 内联等待返回 done + 完整结果，一步搞定

```
exec_python(code="print('hello')")
# 响应：{status: "done", stdout: "hello\n", exit_code: 0, elapsed: 0.3, terminal_id: "t_xxx"}
# agent 无需再调 exec_inspect
```

**长任务（>10s）**：exec_python 超时返回 running，SessionRunner 3 分钟后唤醒 LLM 决策

```
exec_python(code="import time; time.sleep(300); print('done')")
# 10s 内联等待超时 → 响应 status="running" + terminal_id
# SessionRunner 自动 exec_inspect(tid, timeout=180)，3 分钟到仍 running → 唤醒 LLM
# LLM 决策：wait(N) / exec_kill / exec_inspect / exec_send_input
```

**裸 agent（MCP 直连）**：

```
# 短任务：一步搞定
result = exec_python(code="print(1+1)")
# result = {status: "done", stdout: "2\n", exit_code: 0, terminal_id: "t_xxx"}

# 长任务：两步流程
result = exec_python(code="import time; time.sleep(60)")
# result = {status: "running", terminal_id: "t_xxx"}
snap = exec_inspect(tid=result.terminal_id, timeout=60)
# snap = {status: "done", stdout_so_far: "...", exit_code: 0, elapsed: 60.2}
```

**交互式 stdin**：

```
exec_python(code="name = input('your name: '); print(f'hello {name}')")
# 返回 terminal_id（input() 阻塞 → 超过 10s 内联等待 → status="running"）

exec_send_input(tid="t_xxx", text="alice")
# 向 stdin 发送 "alice\n"

snap = exec_inspect(tid="t_xxx", timeout=2)
# snap.stdout_so_far 含 "your name: hello alice\n"
```

**纯等待 vs 频繁查看**：

- 想纯等待：`wait(N)` + `exec_inspect(tid, timeout=0)` 两步流程（避免持续输出场景频繁唤醒）
- 想频繁看进展：`exec_inspect(tid, timeout=N)`（新输出即返回，不会等满 N 秒）

### v16 内联等待配置

```toml
[server]
# exec_python 内联等待秒数：N 秒内子进程完成 → status=done + 完整输出；
# 超时 → status=running + terminal_id。0=禁用回退 v15 立即返回。默认 10s。
exec_python_inline_wait_secs = 10
```

### 3 分钟唤醒机制（仅 v6-lite SessionRunner 场景，status=running 路径）

- exec_python 返回 status=running 后，SessionRunner 自动调 `exec_inspect(tid, timeout=180)` 等 3 分钟
- 3 分钟内子进程结束：tool_result 含完整 stdout/stderr/exit_code
- 3 分钟到仍 running：唤醒 LLM，prompt 含当前 stdout/stderr 尾部 + 已耗时 + 4 个干预工具清单 + wait/inspect 区别提示
- 后续不再自动唤醒，LLM 自己决定何时 inspect/wait
- 配置：`[runner] long_tool_first_check_secs = 180`（默认 3 分钟，0=禁用）

### terminal 生命周期三层保障

1. **LLM 主动 exec_kill**：LLM 决定终止时调 `exec_kill(tid)`
2. **SessionRunner session 结束自动 kill**：session finalize/interrupted/failed 时自动 kill 所有残留 terminal
3. **后端 TTL 清理**：finished/killed 状态 30 分钟后移除（running 不移除），关联 .py 临时文件 + stdout/stderr 日志文件一起删

### exec_python 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `code` | str | Python 代码（支持 top-level await，asyncio.run 自动应用） |
| `cwd` | str? | 工作目录（None=项目根；设为 `<dir>` 时自动加入 sys.path/PYTHONPATH） |
| `environment` | str? | Python 环境名（`workspace_venv` 用 `<cwd>/.venv`；其他值原样传入） |
| `python_path` | str? | 显式指定 Python 解释器路径（覆盖 environment 解析） |

返回结构（ExecResponse）：

status=done（v16 内联等待完成，短任务）：
```json
{
  "success": true,
  "terminal_id": "t_xxx",
  "status": "done",
  "temp_file": "temp/exec_abc123def456.py",
  "pid": 12345,
  "python_executable": "F:\\project_temp\\localAgent\\.venv\\Scripts\\python.exe",
  "cwd": "F:\\project_temp\\localAgent",
  "environment": "default",
  "stdout": "hello\n",
  "stderr": "",
  "exit_code": 0,
  "elapsed": 0.32,
  "stdout_truncated": false,
  "stderr_truncated": false,
  "stdout_total_chars": 6,
  "stderr_total_chars": 0
}
```

status=running（内联等待超时或禁用，长任务，同 v15）：
```json
{
  "success": true,
  "terminal_id": "t_xxx",
  "status": "running",
  "temp_file": "temp/exec_abc123def456.py",
  "pid": 12345,
  "python_executable": "F:\\project_temp\\localAgent\\.venv\\Scripts\\python.exe",
  "cwd": "F:\\project_temp\\localAgent",
  "environment": "default"
}
```

**注意**：v15 起 `ExecRequest` 移除了 `timeout` 字段（spec D5）。长任务的超时控制由 SessionRunner 的 `long_tool_first_check_secs` 配置或 LLM 主动 exec_kill 处理。v16 done 路径的输出截断由 `_truncate_output` 处理（MAX_OUTPUT_CHARS=8000，超长自动截断含标记）。

### 参数使用率统计（T04，简化版）

每次 exec_python 调用会记录到 `data/tool_usage_stats.json`（best-effort，fail-open）：
- `call_count`：总调用次数
- `code_length_buckets`：代码长度分桶（<100 / 100-1k / 1k-10k / >10k 字符）
- `environment`：environment 分布
- `has_cwd_true/false`：是否传 cwd
- `total_elapsed_ms` / `elapsed_count`：累计耗时

无面板，无端点，仅落盘审计。用户审计时直接读 `data/tool_usage_stats.json`。

---

## 专项 Skill 的工具脚本（用 `exec_python` 运行）

任务专属脚本已迁移到 `workspace/<task_name>/` 任务工作区（脚本+数据+配置统一存放），通过 `exec_python` 运行：

| Skill | 脚本路径 | 用法 |
|-------|---------|------|
| 异环抽卡 | `workspace/yihuan_gacha/yihuan_gacha.py` | `uv run python workspace/yihuan_gacha/yihuan_gacha.py` |
| 异环清洗 | `workspace/yihuan_gacha/yihuan_clean.py` | `uv run python workspace/yihuan_gacha/yihuan_clean.py` |
| 鸣潮抽卡 | `workspace/wuwa_gacha/wuwa_gacha.py` | `uv run python workspace/wuwa_gacha/wuwa_gacha.py` |
| 明日方舟抽卡 | `workspace/arknights_gacha/arknights_gacha.py` | `uv run python workspace/arknights_gacha/arknights_gacha.py` |
| 明日方舟导入 | `workspace/arknights_gacha/arknights_import.py` | `uv run python workspace/arknights_gacha/arknights_import.py` |
| 终末地抽卡 | `workspace/endfield_gacha/endfield_gacha.py` | `uv run python workspace/endfield_gacha/endfield_gacha.py` |
| <data_drive>:\<bilibili_videos>抽奖 | `workspace/bilibili_gacha/bilibili_gacha.py` | `uv run python workspace/bilibili_gacha/bilibili_gacha.py` |
| <data_drive>:\<bilibili_videos>抽奖清理 | `workspace/bilibili_gacha/bilibili_cleanup.py` | `uv run python workspace/bilibili_gacha/bilibili_cleanup.py` |
| <data_drive>:\<bilibili_videos>抽奖核查 | `workspace/bilibili_gacha/bilibili_check_lottery.py` | `uv run python workspace/bilibili_gacha/bilibili_check_lottery.py` |
| 社群帖子爬取 | `workspace/community_review/save_community.py` | `uv run python workspace/community_review/save_community.py` |
| 社群JSON转MD | `workspace/community_review/json_to_md.py` | `uv run python workspace/community_review/json_to_md.py` |
| 社群增量更新 | `workspace/community_review/update_community.py` | `uv run python workspace/community_review/update_community.py` |
| 社群定期同步 | `workspace/community_review/sync_community.py` | `uv run python workspace/community_review/sync_community.py` |
| 社群帖子提取 | `workspace/community_review/extract_community.py` | `uv run python workspace/community_review/extract_community.py` |
| 社群帖子评分 | `workspace/community_review/summarize_posts.py` | `uv run python workspace/community_review/summarize_posts.py` |
| 社群数据统计 | `workspace/community_review/community_stats.py` | `uv run python workspace/community_review/community_stats.py` |
| 记账转换 | `workspace/accounting/bill_converter.py` | `uv run python workspace/accounting/bill_converter.py` |
| 官方抽奖 | `workspace/official_gacha/official_gacha.py` | `uv run python workspace/official_gacha/official_gacha.py` |
| 牛客面经嵌入版 | `workspace/niuke_review/nowcoder_embedded.py` | `uv run python workspace/niuke_review/nowcoder_embedded.py` |
| 牛客面经评分 | `workspace/niuke_review/nowcoder_review.py` | `uv run python workspace/niuke_review/nowcoder_review.py` |

## 通用工具脚本（保留在 `tools/`，不归属单一任务）

| 类型 | 脚本路径 | 用法 |
|------|---------|------|
| 磁盘扫描 | `workspace/disk_manager/scripts/scan_disk.py` | `uv run python workspace/disk_manager/scripts/scan_disk.py` |
| 磁盘清理 | `workspace/disk_manager/scripts/cleanup.py` | `uv run python workspace/disk_manager/scripts/cleanup.py` |
| 环境备份 | `workspace/disk_manager/scripts/backup_env.py` | `uv run python workspace/disk_manager/scripts/backup_env.py` |
| 调试 Chrome 启动 | `tools/browser/start_debug_chrome.py` | `uv run python tools/browser/start_debug_chrome.py` |
| 批量打开标签页 | `tools/browser/open_tabs.py` | `uv run python tools/browser/open_tabs.py` |
| 文件自动分类 | `tools/file_classifier/classifier_gui.py` | `uv run python tools/file_classifier/classifier_gui.py` |
| 批量代码注释 | `tools/llm/mimo_batch_comment.py` | `uv run python tools/llm/mimo_batch_comment.py --project <目录> --no-check` |
| 用户消息发送 | `tools/user_message_gui.py` | `uv run python tools/user_message_gui.py` |
| 图片自动分类 | `tools/image_organizer/organize.py` | `uv run python tools/image_organizer/organize.py` |
| <data_drive>:\<bilibili_videos>视频分类 | `tools/media_classifier/bilibili_classifier.py` | `uv run python tools/media_classifier/bilibili_classifier.py` |
| 文字篇章整理 | `tools/media_classifier/wenzhang_classifier.py` | `uv run python tools/media_classifier/wenzhang_classifier.py` |
| 歌词分析 | `tools/media_classifier/mimo_lrc_analyze.py` | `uv run python tools/media_classifier/mimo_lrc_analyze.py` |
| 帖子批量评分 | `tools/llm/summarize_posts_direct.py` | `uv run python tools/llm/summarize_posts_direct.py` |
| LLM池测试 | `tools/llm/test_llm.py` | `uv run python tools/llm/test_llm.py` |
| Token统计 | `tools/llm/llm_token_stats.py` | `uv run python tools/llm/llm_token_stats.py` |
| API端点测试 | `tools/llm/test_api_batch.py` | `uv run python tools/llm/test_api_batch.py` |
| 操作录制器 | `workspace/recorder/tools/main.py` | `uv run python -m workspace.recorder.tools.main`（详见下方"操作录制器"小节） |
| 微信收藏整理 | `tools/wechat_export_organize.py` | `uv run python tools/wechat_export_organize.py` |
| OCR 数据调试 | `tools/debug/debug_ocr_data.py` | `uv run python tools/debug/debug_ocr_data.py` |
| OCR bbox 回归 | `tools/debug/verify_bbox_affine.py` | PaddleOCR/PaddleX 更新后必跑；三档分辨率目标 RMS 1-3px、max≤5px |
| OCR bbox 校准 | `tools/debug/sample_bbox_offset.py` | 仅当直接 detector A/B 也有系统偏差时使用，禁止拿来补偿 UVDoc |
| Fake LLM Proxy | `tools/fake_llm_proxy.py` | `uv run python tools/fake_llm_proxy.py` |
| MindForge 守护进程 | `tools/mindforge/converter_daemon.py` | 由 server/mindforge.py 自动启动 |

## 工具清单与 GUI 面板

项目维护统一的工具清单，GUI 面板作为统一入口：

- **工具清单（JSON）**: `tools_manifest.json` - GUI 读取用，包含所有工具的元数据
- **GUI 面板**: `client/panels/tools.py`（ToolsPanel）+ `client/widgets/tool_runner.py`（工具运行组件）- PySide6 面板，自动读取清单展示工具

> 旧 `tools_launcher.py` / `tools_manifest.md` / `launcher.bat` 已删除，由 `client/panels/tools.py` + `client/widgets/tool_runner.py` 替代。

**维护顺序**：新增或调整工具时，通常先更新 `tools_manifest.json`；只有当 GUI 的布局、分类、队列或运行行为需要变化时，才修改 `client/widgets/tool_runner.py`。

GUI 特点：
- 各脚本独立运行（subprocess），GUI 关掉也不影响正在运行的脚本
- 按分类展示工具（社群/抽卡/<data_drive>:\<bilibili_videos>/牛客/磁盘/记账/调试/系统）
- 每个工具显示说明、选项、需求提示（浏览器/管理员权限）
- 实时显示脚本输出

**新增工具时需更新 `tools_manifest.json`**，GUI 会自动读取新清单。`client/widgets/tool_runner.py` 一般不需要跟着改，除非你要改界面或运行逻辑。

每个工具需填写：`name`、`command`、`category`、`description`、`usage`（agent/user/both）、`options`（可选）。新增分类时同步更新 `categories` 数组。

## 文件自动分类工具

`tools/file_classifier/` 提供可视化文件分类工具（PySide6 GUI），支持文件+文件夹 LLM 辅助预测、类交互式文件夹分类协议、state.json 状态持久化。**完整操作指引见 [tools/file_classifier/file_classifier_guide.md](file:///f:/<project_root>/tools/file_classifier/file_classifier_guide.md)**。

### 工作流程

1. **扫描目录**：选择源目录（如下载文件夹），GUI 列出文件+文件夹（文件夹置顶，其余按修改时间倒序）
2. **手动分类样例**：勾选文件后拖拽到右侧分类区，创建分类样板（复选框=真选中，拖拽只携带勾选行）
3. **LLM 预测**：点击"预测全部"，文件走 predict_categories，文件夹走 classify_folder 类交互式协议
4. **审核**：在审核对话框中修改预测结果或跳过文件，冲突可选覆盖/跳过/重命名（文件夹支持合并）
5. **应用移动**：审核通过后按分类配置中的真实目录路径移动文件，自动标记"已处理"写入 state.json

### 交互模型

- **单击** = 翻转复选框（反选语义）；**shift+单击** = 批量取反；**ctrl+单击** = 打开对象（文件夹→进入列表，文件→默认程序打开）；**ctrl+enter** = 详情弹窗；**右键** = 4组10项菜单
- 列宽可拖拽调整并记忆；类型列显示系统图标+友好类型描述

### 分类配置

`tools/file_classifier/categories.json` 定义虚拟分类名到真实目录的映射：
```json
{
  "categories": [
    {"name": "文档", "path": "F:\\<data_drive>:\Documents", "extensions": [".pdf", ".doc", ".docx", ".txt", ".md"]},
    {"name": "图片", "path": "F:\\<data_drive>:\Pictures", "extensions": [".jpg", ".png", ".gif", ".bmp", ".webp"]},
    {"name": "暂存", "path": "", "extensions": []}
  ]
}
```

- `path` 为空 = 暂存分类（不移动但标记已处理，适合长期放置的文件）
- 后端不可用时自动降级为扩展名规则匹配

### 状态持久化

`tools/file_classifier/state.json` 记录：分类即已处理（避免后端重复扫描）、列宽、上次源目录、加载的分类配置。GUI 和后端 <data_drive>:/DownloadscanAction 共享。右键"重置分类"可清除已处理状态。

### 后端监控

后端 <data_drive>:/DownloadscanAction 每 5 分钟扫描下载文件夹（`config.toml` `[loops.download_watcher]`），文件+文件夹分别分类，读取 state.json 跳过已处理条目，按 disposition（move/inspect/unknown）推送 inbox。

### 启动

```bash
uv run python tools/file_classifier/classifier_gui.py
```

## 图片自动分类工具

> **当前项目状态**：旧版 `tools/image_organizer/organize.py` 仅作为历史 Florence-2 参考，当前图片整理开发和抽样验证统一在 `workspace/image_organize_remote_vl_sample/`。该工作区当前默认运行本地索引 MVP，恢复前请先阅读其中的 `RESUME.md`。

`tools/image_organizer/organize.py` 使用视觉模型（Florence-2，从 HF cache 加载）+ LLM 池对大规模图片进行自动分类。

### 当前 Remote-VL 抽样入口

```powershell
.venv\Scripts\python.exe workspace\image_organize_remote_vl_sample\organize_remote_vl.py --report-only
```

默认本地索引（不调用远程 VL、不移动图片）：

```powershell
.venv\Scripts\python.exe workspace\image_organize_remote_vl_sample\organize_remote_vl.py --limit 5
```

只读搜索：

```powershell
.venv\Scripts\python.exe workspace\image_organize_remote_vl_sample\organize_remote_vl.py --search 截图
```

当前 ModelScope provider 会被安全策略拒绝，不能作为正式 Remote-VL 来源。完整隐私和方案记录见 `workspace/image_organize_remote_vl_sample/DESIGN_NOTES.md`。

实时面板：

```powershell
.venv\Scripts\python.exe workspace\image_organize_remote_vl_sample\dashboard.py
```

说明、恢复顺序和安全边界见：
`workspace/image_organize_remote_vl_sample/RESUME.md`。

### 运行环境

主项目 `.venv` 即可运行（Florence-2 通过 `transformers` + `torch` 从 HF cache 加载；OmniParser 移除后不再依赖 `ultralytics`/`torchvision` 等）：

```bash
uv run python tools/image_organizer/organize.py
```

### 历史 Florence-2 工作流程（不可直接作为当前入口）

1. Florence-2 对每张图片生成 caption + OCR 文本
2. 将 caption + OCR 发送给后端 LLM 池进行多维度分类
3. 按分类结果移动文件到目标目录
4. 建立 SQLite 标签库（支持后续查询和统计）

### 辅助工具

- `tools/image_organizer/view_db.py` — 查看分类数据库
- `tools/image_organizer/view_failed.py` — 查看失败文件
- `tools/image_organizer/florence_bench.py` — Florence-2 测速基准
- `tools/image_organizer/test_models.py` — 模型对比测试

## 媒体分类工具集

`tools/media_classifier/` 提供多种媒体内容的 LLM 分类工具：

| 工具 | 用途 | 说明 |
|------|------|------|
| `bilibili_classifier.py` | <data_drive>:\<bilibili_videos>视频分类 | 扫描视频文件+弹幕 → LLM 10类分类 |
| `bilibili_mover.py` | <data_drive>:\<bilibili_videos>视频移动 | 按分类+置信度分层 move |
| `wenzhang_classifier.py` | 文字篇章整理 | txt/docx 解析 → LLM 15类分类 |
| `mimo_lrc_analyze.py` | 歌词分析 | 情感/主题/风格分析，支持 MuseArc 关联 |

所有工具均需要后端运行（LLM 池），通过 `call_via_backend` 代理调用。

## 独立虚拟环境（envs/）

原本地 VL 环境 `envs/vl/` 已于 2026-07-07 删除（迁移到远程 VL，详见 `.agents/wip/local_vl_decommissioned.md`）。当前所有工具均使用主 `.venv` 运行，无需独立环境。`.gitignore` 仍保留 `envs/*` 排除规则，供未来需要时使用。

## 批量代码注释工具

`tools/llm/mimo_batch_comment.py` 使用 LLM 为 Python/C#/TypeScript/JavaScript 源码批量添加中文注释。

### 安全机制（5 层验证 + 并发重试）

1. **`verify_unit_code_preserved`**：验证原始代码行（去除注释后）在 LLM 响应中逐字存在，捕获缩进修改和代码篡改
2. **`fix_response_indentation`**：修正 LLM 返回的错误缩进（跳过注释行检测 def/body 缩进）
3. **逐单元 AST 验证**（Python）：每个单元替换后单独验证 AST 合法性，防止 docstring 缩进错误
4. **文件级完整性验证**：Python 检查顶层定义名集合不变；C#/TS 检查大括号平衡和行数
5. **优雅降级**：文件级验证失败时逐个移除单元直到通过，最大化保留有效结果

### 并发与重试

- 单文件内使用 `MAX_WORKERS` 并发调用后端 LLM 池
- 多文件使用 `FILE_WORKERS` 并发处理，避免一个大文件阻塞整个项目
- 单元校验失败后会额外重试 `RETRY_TIMES=1` 次，重试提示词会明确要求保留 `def`/签名行和多行签名
- 任何重试结果仍必须通过代码行保留校验和文件完整性校验，否则拒绝写入

### 用法

```bash
# 单个项目
uv run python tools/llm/mimo_batch_comment.py --project F:\<external_project_root>\MuseArc --no-check

# 多个项目
uv run python tools/llm/mimo_batch_comment.py --project F:\<external_project_root>\MuseArc --project F:\<external_project_root>\MusePlayer --no-check

# 只扫描不调用 API
uv run python tools/llm/mimo_batch_comment.py --project F:\<external_project_root>\MuseArc --dry-run

# 强制重新处理已处理文件
uv run python tools/llm/mimo_batch_comment.py --project F:\<external_project_root>\MuseArc --force --no-check

# 调试模式（保存被拒绝的单元到文件）
uv run python tools/llm/mimo_batch_comment.py --project F:\<external_project_root>\MuseArc --debug --no-check
```

### 注意事项

- 需要后端运行且 LLM 池已初始化
- 状态文件 `tools/llm/mimo_comment_state.json` 记录已处理文件，支持断点续传
- 被拒绝的单元不会写入文件，原始代码保持不变
- `--debug` 标志会保存被拒绝单元的原始/响应对比到 `.debug_*.txt` 文件
- 注释任务以安全为第一优先级：宁可拒绝部分单元，也不能修改、删除或重排原始代码行

## 操作录制器（L0+L1+L2+L3+L4 全层入口）

`workspace/recorder/tools/` 提供键鼠操作 + 屏幕截图 + 麦克风音频 + 窗口焦点的同步录制，生成可回放的录制包。**独立进程运行（不走后端 API）**，是录制功能分层架构的 L0 采集层；L1 处理层入口 `process_recording_package` 在 `lib/recorder/processor/` 中实现（编排 P1 STT / P4 事件聚合 / P5 关键帧抽取）；L2 时间轴层入口 `build_timeline` 在 `lib/recorder/timeline/` 中实现（合并三 JSON 为统一 timeline.json + 章节切分 + 初始化空 annotations.json）；L3 编辑层入口 `python -m workspace.recorder.tools.editor` 在 `lib/recorder/editor/` 中实现（13 个 action + annotations.json + merge 逻辑）；L4 消费层在 `workspace/recorder/consumer/` 中实现（agent 直接 import 公用库发现/读取/VL 协议辅助/消费日志，**无后端 HTTP 端点**，D055 变更）。

**完整用户文档**：[docs/recorder-guide.md](file:///f:/<project_root>/docs/recorder-guide.md)（含 L4 消费层章节：consumer API + 多轮 VL 协议 + 场景路由）

**端到端测试说明**：[docs/recorder-test-guide.md](file:///f:/<project_root>/docs/recorder-test-guide.md)（覆盖 L0-L4 全部功能和 GUI 外观验收，含 25 个测试文件索引和设计决策覆盖矩阵）

**工具面板入口**：在 client 工具面板的 `🎥 操作录制` 分类下，含录制器（小模式）、录制器（大模式）、录制包编辑器、时间轴构建 4 个工具条目，可通过选项面板配置参数后直接启动。

### 启动

```bash
# 小模式（默认，悬浮条）
uv run python -m workspace.recorder.tools.main

# 大模式（监控面板，双屏场景）
uv run python -m workspace.recorder.tools.main --mode large

# 指定录制范围
uv run python -m workspace.recorder.tools.main --range monitor --monitor-index 1
uv run python -m workspace.recorder.tools.main --range window --window-hwnd 0x001A0B2C

# 无 GUI 冒烟模式（自动启动，阻塞等待自动停止）
uv run python -m workspace.recorder.tools.main --no-gui --autostart
```

### 核心组件

| 文件 | 职责 |
|------|------|
| `main.py` | 启动入口（CLI 参数解析 + GUI 协调 + 无 GUI 冒烟模式） |
| `config_loader.py` | `config.toml` `[recording]` 段加载器（独立于 server/config.py） |
| `config.py` | `RecordingConfig` dataclass + detailed/coarse 工厂函数 |
| `config_dialog.py` | 范围/模式/参数选择对话框 |
| `hotkey_manager.py` | `Ctrl+Alt+R` 全局热键（start/stop 切换） |
| `floating_bar.py` | 小模式悬浮条（单屏场景） |
| `monitor_window.py` | 大模式监控面板（双屏场景，5 个子面板：状态/事件流/最新帧/波形/焦点） |
| `recorder_app.py` | `RecorderApp` 协调类 + 30 分钟自动停止 + 接近上限提前 120s 告警 |

底层传感器在 `lib/recorder/sensors/`（keyboard/mouse/screen/audio/window），与后端共享 `lib/` 公用库。

L1 处理层在 `lib/recorder/processor/`，入口为 `process_recording_package(package_root, options)`，编排 P1 STT / P4 事件聚合 / P5 关键帧抽取，产出 `blocks.json` + `keyframes.json` + `transcript.json` 三个 JSON（共享录制包时间轴）。P2 UIA 结构化器 / P3 VL 标注器仅定义 Protocol 接口签名（L1 不实现），供 L4 agent 消费时按需注入实现。详见 `docs/recorder-guide.md` 第十六章。

L2 时间轴层在 `lib/recorder/timeline/`，入口为 `build_timeline(package_path)`，合并 L1 三 JSON 为统一 `timeline.json`（四类块按时间戳排序 + 章节切分 + 块 ID 前缀 b/f/t/c）+ 初始化空 `annotations.json`。CLI 入口 `python -m workspace.recorder.tools.timeline_cli <package_path>` 打印 6 项预览（录制包概览/章节列表/块类型分布/时间跨度/三时间戳对齐验证/帧压缩比）。详见 `docs/recorder-guide.md` 第十七章。

```bash
# 构建 timeline.json + 初始化 annotations.json + 打印预览
uv run python -m workspace.recorder.tools.timeline_cli workspace/recorder/recordings/rec_20260723_162924

# 只预览不重新构建（要求 timeline.json 已存在）
uv run python -m workspace.recorder.tools.timeline_cli <package_path> --preview-only

# 自定义章节切分空闲阈值（默认 2.0s）
uv run python -m workspace.recorder.tools.timeline_cli <package_path> --idle-threshold 3.0
```

L3 编辑层入口在 `workspace/recorder/tools/editor/`，CLI 启动 `python -m workspace.recorder.tools.editor <package_path>`（`workspace/recorder/tools/editor/editor_app.py` 的 `run_editor`）：检查 `timeline.json`（不存在则调 `build_timeline` 生成）→ 加载 timeline + annotations → 启动 PySide6 GUI（三区域：左章节列表 + 中时间轴 + 下详情预览 + 工具栏 12 项编辑功能）。所有编辑写入 `annotations.json`，`timeline.json` 只读（D044）；录制器 `save()` 后自动拉起编辑器（D032/D052）。L4 agent 调 `merge_timeline_annotations(package_path)` merge timeline + annotations 获取最终视图。详见 `docs/recorder-guide.md` 第十八章。

```bash
# 独立启动 L3 编辑器（位置参数）
uv run python -m workspace.recorder.tools.editor workspace/recorder/recordings/rec_20260723_162924

# 也可用 --package-path 命名参数（工具面板 / 脚本调用）
uv run python -m workspace.recorder.tools.editor --package-path workspace/recorder/recordings/rec_20260723_162924

# 查看用法
uv run python -m workspace.recorder.tools.editor --help
```

L4 消费层在 `workspace/recorder/consumer/`（agent 公用库，**无后端 HTTP 端点**，D055 变更）。agent 直接 import consumer API 发现/读取录制包，调多轮 VL 协议（`select_vl_candidates` + `build_vl_question` + `understand_image` + `log_consumption`），agent 自主决定后续路由（不预设 routes_to，D056 变更）。详见 `docs/recorder-guide.md` 第十九章。

```bash
# agent 公用库（本地直接 import，无需 HTTP 端点）
python -c "from workspace.recorder.consumer import list_recordings; print([s.recording_id for s in list_recordings()])"
```

### 录制范围

- `fullscreen` — 全屏（所有显示器合成画面，默认）
- `monitor` — 指定显示器（配合 `--monitor-index`）
- `window` — 指定窗口（配合 `--window-hwnd`，PrintWindow + PW_RENDERFULLCONTENT 截取被遮挡窗口）

### 录制模式

- `detailed` — 详细采样（默认）：键鼠全事件 + 屏幕每 0.5s + click 后 50/100/150ms 三帧 burst
- `coarse` — 粗略采样：键鼠 click 级 + 屏幕每 2.0s + click 后 100/200/300ms 三帧 burst

### 安全与隐私

- **密码框保护**：检测到密码框自动 mask（`IsPassword → password_masked`，`physical_keys=[]` 不记录按键内容）
- **最小化暂停**：窗口模式下检测到目标窗口最小化自动暂停截图
- **自动停止**：默认 30 分钟自动停止（可通过 `--max-duration` 或 config 覆盖）
- **全局热键**：`Ctrl+Alt+R` 随时切换 start/stop

### 配置

`config.toml` `[recording]` 段（详见 `config.example.toml`）：

```toml
[recording]
default_mode = "small"             # small | large
default_range = "fullscreen"       # fullscreen | monitor | window
default_detail_level = "detailed"  # detailed | coarse
max_duration_seconds = 1800        # 30 分钟自动停止
audio_enabled = true
base_dir = "workspace/recorder/recordings"

[recording.capture]
interval_detailed = 0.5
interval_coarse = 2.0
burst_intervals_detailed = [0.05, 0.10, 0.15]
burst_intervals_coarse = [0.10, 0.20, 0.30]

[recording.audio]
sample_rate = 16000
channels = 1

[recording.stt]                    # L1 处理层 P1 STT 配置（D037/D038）
enabled = false                    # 是否启用 STT（默认关，需 GPU）
model = "large-v3"                 # faster-whisper 模型（可改 large-v3-turbo）
model_dir = "E:/<data_drive>:\<working_root>/- 工作中项目/视频配字幕/stt-main/models"  # 模型权重目录（复用 stt-main）
language = "zh"                    # 转写语言（zh/en/fr/de/ja/ko 等 15 种）
vad_threshold = 0.5                # VAD 阈值（0.0-1.0，传给 faster-whisper vad_parameters）
junk_threshold = 0.3               # 垃圾文本判定阈值（JUNK_PHRASES 占比 > 阈值判垃圾）
target_lufs = -15.0                # LUFS 归一化目标响度（重试策略第 2 步）
lufs_attempt = 4                   # LUFS 归一化重试次数（每次 +lufs_step）
lufs_step = 3.0                    # LUFS 归一化步长（-15 → -12 → -9 → -6）
max_chars_per_line = 40            # transcript.json 每行最大字符数
min_duration_per_line = 1.5        # transcript.json 每行最小时长（秒）
```

CLI 参数优先级高于 config.toml，详见 `docs/recorder-guide.md`。
