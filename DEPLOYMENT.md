# LocalAgent 部署指南

> Profile: `public-full` | Audience: `public` | 构建时间: 2026-09-19T18:29:58.409116+00:00

LocalAgent 是一个个人 AI Agent 项目，集合了电脑操控、Agent 工具和日常可自动化工作流。
本源码分发版基于 git commit `3cd02a10556d79ef591ee001e1f8cf55ec72fa90` 构建。

**最低部署目标是"先让后端跑起来，再按需增强"**——下面的阶段 0～2 不会下载任何本地推理模型。
完整细节（配置文件逐段说明、可选增强的安装命令、全部已知卡点）见 `docs/deployment.md`。

---

## 系统要求

| 项 | 要求 | 说明 |
|----|------|------|
| 操作系统 | Windows 10/11 | 键鼠操控走 Win32 API + UIA，浏览器调试实例路径假设 Windows 文件系统 |
| Python | ≥ 3.12 | 用于跑自检脚本；项目依赖由 uv 管理 |
| uv | 最新版 | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Git | 任意版本 | 用于 clone |
| 管理员权限 | 启动后端时需要 | `start.bat` 会自动 UAC 提权 |
| LLM API Key | 至少 1 个（tier 3） | 后端**无 key 也能启动**，但 Agent 干活需要 |
| NVIDIA GPU + CUDA 12.6 | 可选 | 只影响 OCR / 本地嵌入的推理速度，**不影响核心启动** |

---

## 阶段 0 — clone 与前置自检（不装任何依赖）

```bat
REM 1. clone（任意目录；路径别带中文和空格）
git clone https://github.com/Stargazed-Dreamer/localagent-public.git localAgent
cd localAgent

REM 2. 把分发包里的路径占位符换成你本机的真实路径
REM    （--data-drive 传盘符字母，不要带冒号；先加 --dry-run 可只看清单不写入）
python tools\deploy\apply_placeholders.py . --project-root D:\code\localAgent --username your_name --data-drive D

REM 3. 前置自检：7 项检查，输出 ✅/⚠️/❌ 与修复命令
REM    用系统 python 直跑，不要写 uv run（uv run 会先触发依赖下载）
python tools\deploy\preflight_check.py
```

**通过判据**：没有 ❌。全过或仅 ⚠️ 时退出码为 0；有 ❌ 为 1。
此时 `config.toml` / `data\llm\keys.json` 缺失属正常（阶段 1 创建）。

---

## 阶段 1 — 轻量安装 + 启动后端

```bat
REM 4. 安装依赖：主依赖只含启动必需的轻量包；本地推理 / 行情 / 文档等重型栈
REM    放在 extras（ocr/inference/stt/quant/office），可选增强阶段再装（约 6.06 GB）
uv sync

REM 5. 创建配置
copy config.example.toml config.toml
REM 再按 docs/deployment.md 创建 data\llm\keys.json（至少 1 个 tier 3 的 LLM key）

REM 6. 启动后端（会请求管理员权限）
start.bat
```

**通过判据**：

```bat
curl.exe -s http://127.0.0.1:8766/health    REM → "status": "ok"
python tools\deploy\diagnose.py             REM → 逐项解读，无 ❌
```

> PowerShell 里 `curl` 是 `Invoke-WebRequest` 的别名，会卡住等待输入 —— 用 `curl.exe` 或 `Invoke-RestMethod`。

> **不要修改 `start*.bat`**：内部用 `%~dp0` + 相对 `.venv` 路径，clone 到任何目录都开箱可用。
> bat 对编码（GBK + CRLF + 无 BOM）与行尾极敏感，顺手改很容易把脚本改坏。

---

## 阶段 2 — REST 验证 + 接入 IDE

**先不急着配 IDE，用 REST 端点确认后端与任务路由是活的**：

```bat
curl.exe -s http://127.0.0.1:8766/health
curl.exe -s "http://127.0.0.1:8766/guide?task=hello"
```

`/guide` 返回 JSON（含 `task_categories` / `mcp_tool_categories` / `environment_notes` 等）即表示路由层正常。

**接入 IDE**：后端通过 MCP（Streamable HTTP）暴露能力，端点 `http://127.0.0.1:8766/mcp`。

- **直连 Streamable HTTP**（Trae / CatPaw / CodeBuddy 等）：`{"mcpServers":{"localagent":{"type":"streamableHttp","url":"http://127.0.0.1:8766/mcp"}}}`
- **STDIO 桥接**（Claude Code / Codex 等）：`node tools/mcp_bridge.js`（前置 `npm install -g mcp-remote`）
- **自带 GUI**（PySide6）：`start_client.bat`，走 HTTP + SSE，无需 MCP 配置

> ⚠️ **MCP 不是热重载的**：新加完 MCP server 配置后，需要在 IDE 里**重启或新开一个会话**才会加载到这份工具列表。
> 当轮对话里工具调不到，不代表后端有问题——用上面的 REST 端点就能把两者区分开。

**通过判据**：IDE 新开会话后 `agent_guide(task='hello')` 有返回。

---

## 配置说明

`config.example.toml` 每个字段都有内嵌注释。主要段：

| 段 | 作用 |
|----|------|
| `[server]` | 监听地址与端口（默认 `127.0.0.1:8766`） |
| `[browser]` | 调试浏览器 CDP 端口与独立用户数据目录 |
| `[models]` | 模型存储根目录、PaddleOCR 推理设备 |
| `[llm]` / `[llm.pool]` | LLM 池：重试次数、限流冷却、路由与熔断 |
| `[ocr]` | PaddleOCR 预热开关等 |
| `[memory]` | 三层记忆系统 |
| `[loops]` | 后台轮询任务（活动追踪 / 待办触发 / 记忆维护等） |

密钥**不放在 `config.toml`**：LLM / VL 密钥放 `data/llm/keys.json`，其他 token 放 `data/secret/secrets.toml`。
两者都已被 `.gitignore` 排除，所有密钥读取统一经 `lib/secret` 中转。

---

## 可选增强（不装不阻塞）

核心后端 + MCP + 记忆（BM25）+ 屏幕 + 浏览器控制在上面的阶段 0～2 后即可用。以下均为独立可选项：

| 增强项 | 装了得到什么 | 不装会怎样 | 代价 | extras |
|--------|--------------|------------|------|--------|
| PaddleOCR + CUDA 12.6 | 截图文字定位（Computer Use 主路径） | 截图类文字定位不可用；远程 VL 仍可描述布局 | `paddlepaddle-gpu` 0.98 GB + `paddleocr`/`paddlex` | `ocr` |
| 本地嵌入模型（bge-small-zh-v1.5） | 三层记忆的语义检索 | **静默**退化为 BM25-only（不报错，召回变弱） | `torch` 4.17 GB + `onnx`/`onnxruntime-gpu`/`transformers`/`tokenizers` | `inference` |
| 远程 VL key（ModelScope） | 文档解析、图像描述 | 相关工具不可用 | 一个 API key | — |
| STT（faster-whisper） | 录制器语音转写 | 录制器音频层不可用 | `faster-whisper` + `ctranslate2`，约 65 MB | `stt` |
| 行情 / 数据模块（`workspace/stock_advisor` 等） | 股票数据源、回测、本地行情库 | 该模块**导入即失败**（顶层 import pandas / matplotlib） | 约 390 MB | `quant` |
| 文档解析（docviewer） | 预览 PDF / Word / PPT | 文档预览不可用（Excel 开箱可用，openpyxl 在主依赖） | 约 40 MB | `office` |
| PySide6 GUI | 图形面板 + 独立审批面板 | 纯 MCP + HTTP 也够用 | 679 MB（主依赖已含） | — |
| 调试浏览器（CDP 9222） | 浏览器自动化 | 浏览器类任务不可用 | 0（复用已有 Chrome / Edge） | — |

补装方式（extras 组名见上表最后一列）：

```bat
uv sync --all-extras --inexact                       REM 装齐全部
uv sync --inexact --extras ocr --extras inference    REM 或只补某几组
```

补完重启后端并用 `diagnose.py` 确认。
⚠️ 缺包**不会报错**：嵌入链路是静默降级、OCR 是首次调用才懒加载，别指望启动日志报缺失模块名。
无 GPU 用户请把 `[models] paddle_device` 设为 `cpu`，并把 `pyproject.toml` 的 `[tool.uv.sources]` 中
`paddlepaddle-gpu` 换成 CPU 版 `paddlepaddle`。详见 `docs/deployment.md` 阶段 3。

---

## 常见卡点（速查）

| 症状 | 先查这里 |
|------|----------|
| LLM 调用全失败，`llm_pool.total_keys = 0` | `data/llm/keys.json` 是否存在且 JSON 合法、是否含 tier 3 key |
| 键鼠操作静默失败但 API 返回 200 | 是否以管理员权限启动（`/health` 看 `screen.admin`） |
| OCR 调用报 CUDA 错误 | 是否装了 PaddlePaddle-GPU 且 CUDA 为 12.6 |
| `connect_over_cdp failed` | 调试浏览器未启动：`uv run python tools\browser\start_debug_browser.py` |
| 配置里仍有 `<placeholder>` | 重跑 `apply_placeholders.py`（注意 `--data-drive` 不带冒号） |
| `memory.embedding_ready = false` 且无报错 | 嵌入链路未装（可选增强项），记忆退化为 BM25-only |
| 终端卡住不响应 | PowerShell 用了 `curl` 别名 / `git log` 进分页 → 用 `curl.exe`、`git --no-pager` |

完整清单与根因分析见 `docs/deployment.md` 的「已知卡点」与「其他机器部署常见问题」。

---

## 网络优化（首次下载模型慢）

模型下载走 HuggingFace / ModelScope，网络受限时先设镜像与缓存目录：

```bat
set HF_ENDPOINT=https://hf-mirror.com
set MODELSCOPE_CACHE=D:\ai_models\modelscope
```

---

## 密钥期望

本包**不提供**任何 API key 或运行时凭据。后端在无 key 配置时应能启动，
依赖 key 的功能（LLM 对话、OCR 等）会安全停止并报告缺失 key，不崩溃。

期望行为：`backend-starts-and-dependent-tasks-stop-safely`

## 包含组件

- **disk_manager** v0.0.0 — 
- **recorder** v0.0.0 — 
- **modelscope_model_update** v0.0.0 — 
- **accounting** v0.0.0 — 
- **dev_toolkit** v0.0.0 — 
- **life_design** v0.0.0 — 
- **arknights_gacha** v0.0.0 — 
- **wuwa_gacha** v0.0.0 — 
- **endfield_gacha** v0.0.0 — 
- **yihuan_gacha** v0.0.0 — 

> 本包为开源版（除个人数据外全发）：通用工具与游戏组件在包内，绑定个人生活场景的模块不在。
> 详见 `docs/deployment.md` 的「public 版能力边界」。

## 文件统计

- 文件总数：1589
- 总大小：24067237 字节（约 23.0 MB）
- 源 git commit：`3cd02a10556d79ef591ee001e1f8cf55ec72fa90`

## 审计追溯

- **plan_digest**: `888793f6d1886f7ed08d7f5558bcb7f42c2b7447f48e6dc4c6f55b42952a15bd`
- **profile_id**: `public-full`
- **audience**: `public`
- **built_at**: 2026-09-19T18:29:58.409116+00:00

plan_digest 是本构建计划的 SHA-256 摘要，绑定所有输入（profile / components / scan / exemptions / source_commit / file_entries）。任何输入变化都会让 plan_digest 变化，使旧审批失效。

## 豁免声明

本包含以下敏感内容豁免（已审计，认为可安全分发）：

- `.agents/skills/bat_writing/SKILL.md` — sensitive_line_skips
- `.agents/skills/bat_writing/references/guide.md` — sensitive_line_skips
- `.agents/skills/deep_research/references/pdf_report_style.md` — sensitive_line_skips
- `.agents/skills/office_docs/references/pdf_basic.md` — sensitive_line_skips
- `CHANGELOG.md` — sensitive_line_skips
- `README_public.md` — sensitive_line_skips
- `client/core/agent/builtin_tools/base.py` — sensitive_line_skips
- `docs/adr/0007-omniparser-removal.md` — sensitive_line_skips
- `docs/changelog-archive.md` — post_process_remove_lines
- `docs/changelog-archive.md` — sensitive_line_skips
- `docs/deployment.md` — sensitive_line_skips
- `docs/dev-workflow.md` — sensitive_line_skips
- `docs/environment-constraints.md` — sensitive_line_skips
- `docs/tools-guide.md` — sensitive_line_skips
- `lib/recorder/processor/stt_engine.py` — sensitive_line_skips
- `server/agent_guide_data.py` — sensitive_line_skips
- `server/approval_review.py` — sensitive_line_skips
- `server/browser/wait_endpoints.py` — sensitive_line_skips
- `tests/approval_screen/test_approval_low_risk.py` — sensitive_line_skips
- `tests/files_tools/test_spacesniffer.py` — sensitive_line_skips
- `tests/guide_loops/test_inbox.py` — sensitive_line_skips
- `tests/llm_vision/test_model_manager_mindforge.py` — sensitive_line_skips
- `tests/release_ci/test_release_orchestrator.py` — sensitive_line_skips
- `tests/server_endpoints/test_todos.py` — sensitive_line_skips
- `tools/browser/start_debug_browser.py` — sensitive_line_skips
- `tools/deploy/apply_placeholders.py` — sensitive_line_skips
- `tools/file_classifier/file_classifier_guide.md` — sensitive_line_skips
- `tools/image_organizer/prompt_old.txt` — sensitive_line_skips
- `tools/llm/mimo_batch_comment.py` — sensitive_line_skips
- `tools/media_classifier/mimo_lrc_analyze.py` — sensitive_line_skips
- `tools/release/engine/prepare.py` — sensitive_line_skips
- `tools/spacesniffer/README.md` — sensitive_line_skips
- `workspace/disk_manager/SKILL.md` — sensitive_line_skips
- `workspace/disk_manager/references/sns_format.md` — sensitive_line_skips
- `workspace/disk_manager/scripts/backup_env.py` — sensitive_line_skips
- `workspace/disk_manager/scripts/cleanup.py` — sensitive_line_skips
- `workspace/disk_manager/tests/test_parse_sns.py` — sensitive_line_skips
- `workspace/recorder/tools/editor/stt_runner.py` — sensitive_line_skips

## 部署遇到问题时

先跑一次脱敏诊断，把输出贴给维护者即可定位大部分问题：

```bat
python tools\deploy\diagnose.py --anonymize
```

## 许可证

Apache License 2.0

- 允许商用、修改和再分发
- 版权所有 (c) 2026 <copyright_holder>

详见 `LICENSE` 文件。
