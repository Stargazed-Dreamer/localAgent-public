# 部署指南

本文档是 LocalAgent 的详细部署指南。普通用户照着做应该能跑起来；遇到卡点看文末"已知卡点"段。

## 环境要求

### 必需

| 项 | 要求 | 说明 |
|----|------|------|
| **操作系统** | Windows 10/11 | 键鼠操控走 Win32 API + UIA，OCR 走 PaddlePaddle-GPU CUDA 12.6，浏览器调试实例路径假设 Windows 文件系统。换平台需要重写一整层 |
| **Python** | 3.12+ | 用 uv 管理，不需要手动装 |
| **uv** | 最新版 | 包管理器。安装：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **Chromium 内核浏览器** | Chrome / Edge / Brave / Vivaldi 任一 | 浏览器自动化用。脚本会自动探测，也可显式指定路径 |
| **管理员权限** | 启动后端时需要 | 键鼠操控走 Win32 `SetCursorPos` / `SetForegroundWindow`，无权限会被 Windows UIPI 静默阻止。`start.bat` 会自动 UAC 提权 |

### 可选但推荐

| 项 | 用途 | 缺失影响 |
|----|------|----------|
| **NVIDIA GPU + CUDA 12.6** | PaddleOCR-GPU 推理 / 嵌入模型（bge-small-zh-v1.5） | 退化为 CPU 推理，OCR 慢 5-10 倍；嵌入模型仍可跑（用 onnxruntime CPU） |
| **LLM API Key** | Agent 实际干活需要 | 后端能启动但所有 LLM 调用失败。至少需要 1 个 tier 3（默认）key |
| **远程 VL API Key** | ModelScope Qwen3-VL 布局/状态描述 | 远程 VL 退化为不可用；OCR bbox 仍能承担文字定位 |
| **第二显示器** | 双屏配置 | 单屏也能跑，但活动追踪的截屏 VL 会拼接所有显示器（`mss.monitors[0]`） |

### 平台兼容性约束

PaddlePaddle / Playwright / 调试浏览器实例的兼容性约束详见 [docs/environment-constraints.md](environment-constraints.md)。

## 安装依赖

### 1. 克隆仓库

```bash
git clone <repo-url> localAgent
cd localAgent
```

### 2. 安装 Python 依赖

**默认（GPU 模式，推荐有 NVIDIA GPU + CUDA 12.6 的用户）**：

```bash
uv sync
```

`uv sync` 会按 `pyproject.toml` 和 `uv.lock` 装齐所有依赖，包括 PaddlePaddle-GPU 3.2.2（CUDA 12.6）和 PyTorch（CUDA 12.6）。

**CPU-only 模式（无 GPU 用户）**：

需要手动改 `pyproject.toml` 的 `[tool.uv.sources]` 段，把 `paddlepaddle-gpu` 改为 `paddlepaddle`（CPU 版），把 `torch` 的 cu126 索引删除。具体见 [docs/environment-constraints.md](environment-constraints.md)。

> 注意：CPU 模式下 PaddleOCR 仍可运行，但推理慢 5-10 倍。嵌入模型（bge-small-zh-v1.5）走 onnxruntime CPU，性能可接受。

### 3. 安装 Playwright 浏览器内核

```bash
uv run playwright install chromium
```

> 注意：项目用的是已运行的 Chromium 调试浏览器实例（CDP 9222），不是 Playwright 自带的浏览器。但 Playwright 的 driver 仍需安装。

## 配置文件详解

LocalAgent 用三个配置文件分工管理配置和密钥：

| 文件 | 用途 | 是否进 git | 模板 |
|------|------|-----------|------|
| `config.toml` | 非敏感配置（端口 / 路由策略 / Loop 调度 / 浏览器 / 屏幕安全） | ❌（gitignore） | `config.example.toml` |
| `data/llm/keys.json` | LLM / VL / AIGC 密钥 + tier 系统 + provider 配置 | ❌（gitignore） | 见 `config.example.toml` 注释 |
| `data/secret/secrets.toml` | 非 LLM 密钥（tushare_token / github_token 等） | ❌（gitignore） | 见 `config.example.toml` 注释 |

### 1. 创建 config.toml

```bash
cp config.example.toml config.toml
```

编辑 `config.toml`，关键配置项：

```toml
[server]
host = "127.0.0.1"        # 监听地址（仅本机访问）
port = 8766               # 监听端口

[browser]
debug_port = 9222         # 调试浏览器 CDP 端口
user_data_dir = "F:\\path\\to\\localAgent\\chrome_debug"  # 独立用户数据目录（绝对路径，注意双反斜杠）

[models]
external_dir = ""        # 模型存储根目录。空字符串 = 用项目内 weights/ 目录；个人部署设为绝对路径（如 "D:\\ai_models"）

[llm.pool]
retry_count = 3                          # 失败重试次数
rate_limit_cooldown_seconds = 60.0       # 429 后冷却时间
```

完整配置项说明见 `config.example.toml` 内嵌注释（每个字段都有说明）。

### 2. 创建 data/llm/keys.json

这是 LLM 密钥的统一存储，schema v9。每个 key 含 models 列表，每个 model 含 `name` / `scope` / `tier` / `enabled` / `display_name`。

最小化示例（1 个 LLM provider，1 个 VL provider）：

```json
{
  "keys": [
    {
      "id": "your-llm-provider",
      "label": "Your LLM Provider",
      "key": "sk-your-key-here",
      "base_url": "https://api.your-provider.com/v1",
      "models": [
        {"name": "your-cheap-model", "scope": ["llm"], "tier": 2},
        {"name": "your-default-model", "scope": ["llm"], "tier": 3},
        {"name": "your-powerful-model", "scope": ["llm"], "tier": 5}
      ]
    },
    {
      "id": "modelscope-vl",
      "label": "ModelScope VL",
      "key": "ms-your-vl-key",
      "base_url": "https://api-inference.modelscope.cn/v1",
      "models": [
        {"name": "Qwen/Qwen3-VL-235B-A22B-Instruct", "scope": ["vl"], "tier": 5}
      ],
      "vision": {
        "max_concurrency": 1,
        "timeout": 60,
        "rate_limit_cooldown": 60
      }
    }
  ]
}
```

**tier 系统说明**：

- tier 1: 最轻量（免费/简单任务）
- tier 2: 轻量
- tier 3: 默认
- tier 4: 较强
- tier 5: 最强大（复杂推理 / VL / AIGC）

`use_case` 的 `default_tier` 决定调用时选用哪个 tier 的 key/model。tier 是硬匹配，model 是软偏好（同 tier 内可 fallback）。

> VL provider 配置（base_url / api_key / model / max_concurrency / timeout / rate_limit_cooldown）从 v9 起统一在 keys.json 的 `key.vision` 段。

### 3. 创建 data/secret/secrets.toml

非 LLM 密钥统一存储：

```toml
[tokens]
# tushare_token = "your-tushare-token"        # 股票数据用，不需要可省
# github_token = "your-github-pat"             # GitHub 私有镜像监控用，不需要可省
# example_token = "your-example-pat"     # 示例 token，不需要可省
```

只填你需要的 token，其他可省略。读取方式（代码层）：

```python
from lib.secret import get_secret, get_secret_or_raise
token = get_secret("tushare_token")               # 不存在返回 None
token = get_secret_or_raise("tushare_token")      # 不存在抛 SecretNotFoundError
```

**安全约束**（见 [.agents/rules/project_rules.md](../.agents/rules/project_rules.md) "密钥统一管理"段）：

- 禁止将密钥硬编码到代码或写入 config.toml
- 所有密钥读取必须经过 `lib/secret` 中转
- 新增密钥必须存到 `secrets.toml [tokens]` 段或 `data/llm/keys.json`（LLM 密钥）
- `data/secret/` 和 `data/llm/` 已被 `.gitignore` 排除，不会进仓库

### 4. 模型权重路径（可选）

`[models].external_dir` 控制模型存储根目录。所有 AI 模型集中存储在此目录下按子目录分类：

```
external_dir = "D:\\ai_models"
├── paddleocr/      - PaddleOCR 模型 (server/ocr.py)
├── embeddings/     - 嵌入模型 bge-small-zh-v1.5 (server/memory/)
├── faster_whisper/ - Whisper STT 模型 (lib/recorder/processor/)
├── huggingface/    - HF 缓存 (由环境变量 HF_HOME 指向)
└── modelscope/     - ModelScope 缓存 (由环境变量 MODELSCOPE_CACHE 指向)
```

留空 `""` 时各模块回退到项目内 `weights/` 目录（发布模式默认）。

## 启动后端

### 方式 A：start.bat（推荐）

```bash
start.bat
```

`start.bat` 会：

1. 检测管理员权限，无权限则自动 UAC 提权
2. 杀掉占用 8766 端口的旧后端进程
3. 设置 UTF-8 stdio（`PYTHONIOENCODING=utf-8`）
4. 启动 `.venv\Scripts\python.exe -m server.main`

启动成功后控制台会输出：

```
[INFO] Starting LocalAgent...
[INFO] API: http://127.0.0.1:8766
[INFO] Dashboard: http://127.0.0.1:8766/static/index.html
[INFO] MCP: http://127.0.0.1:8766/mcp
```

### 方式 B：手动启动

```bash
# 管理员权限的 PowerShell
.venv\Scripts\python.exe -m server.main
# 或
uv run python -m server.main
```

### 健康检查

```bash
curl.exe -s http://127.0.0.1:8766/health
# 或 PowerShell
Invoke-RestMethod -Uri http://127.0.0.1:8766/health
```

`/health` 返回后端各模块状态：

```json
{
  "status": "ok",
  "version": "0.37.0",
  "screen": {"admin": true, ...},
  "ocr": {"ready": true, ...},
  "browser": {"cdp_port": 9222, ...},
  "llm_pool": {"total_keys": 2, ...},
  "memory": {"db_path": "...", ...},
  "project_structure": {"baseline_exists": true, ...}
}
```

> **PowerShell 陷阱**：在 PowerShell 终端用 `curl -s http://...` 会触发 `Invoke-WebRequest`（`curl` 是它的别名），把 `-s` 解析为 `-Session`，然后等待用户输入 `Uri:` 参数，导致终端卡住。**用 `curl.exe` 强制调用真正的 curl，或用 `Invoke-RestMethod`**。

## 启动调试浏览器

涉及浏览器操作时（DOM 操作 / 截图 / 表单填写），需要先启动调试浏览器实例：

```bash
uv run python tools/browser/start_debug_browser.py
```

脚本会：

- 自动探测 Chrome / Edge / Brave / Vivaldi（按此顺序）
- 用独立的 `chrome_debug/` 用户数据目录（与工作浏览器隔离）
- 启动 CDP 调试端口 9222
- 非交互模式（不会问用户问题）

**选项**：

```bash
# 从工作浏览器复制登录态（首次使用推荐）
uv run python tools/browser/start_debug_browser.py --copy-user-data

# 显式指定浏览器路径
uv run python tools/browser/start_debug_browser.py --browser-path "C:\Program Files\Google\Chrome\Application\chrome.exe"

# 指定端口和用户数据目录
uv run python tools/browser/start_debug_browser.py --port 9222 --user-data-dir "D:\chrome_debug"
```

**重要约束**（见 [docs/environment-constraints.md](environment-constraints.md) "调试浏览器实例"章节）：

- 不要打开用户的工作浏览器
- 不要执行 `playwright install`（已装好）
- 所有浏览器操作必须 `connect_over_cdp("http://127.0.0.1:9222")` 连接已运行实例，禁止启动新浏览器

## 启动 GUI 客户端

```bash
start_client.bat
# 或
.venv\Scripts\python.exe -m client.main
```

GUI 客户端提供 8 个面板：

| 面板 | 用途 |
|------|------|
| **Dashboard** | 后端模块状态总览（OCR / Vision / Browser / Exec / Screen 等状态） |
| **Tools** | 工具脚本启动器（自动读 `tools_manifest.json`） |
| **Keys** | LLM 密钥管理（在线编辑 keys.json） |
| **Accounting** | 记账审核（独立服务 http://127.0.0.1:8780） |
| **Monitoring** | 模块状态 + 在线配置编辑（敏感字段自动脱敏，失焦自动保存） |
| **DailySummary** | 每日活动追踪日报 |
| **Settings** | 在线编辑 `config.toml`（含字段说明） |
| **Chat** | v6-lite 对话引擎（真 SSE 流式 + 打字机三档 + 工具调用块 + steer 引导） |

## 接入 IDE

后端通过 MCP（Streamable HTTP，MCP 2025-03-26 协议）暴露能力，端点 `http://127.0.0.1:8766/mcp`。

### 方式 A：直连 Streamable HTTP

适用：Trae / CatPaw / CodeBuddy 以及任何原生支持 MCP Streamable HTTP 的客户端。

操作：客户端设置 > MCP > 手动添加，粘贴：

```json
{
  "mcpServers": {
    "localagent": {
      "type": "streamableHttp",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

### 方式 B：STDIO 桥接

适用：Claude Code / Codex / 任何只支持 STDIO MCP 的客户端。

ChatGPT 桌面客户端的"流式 HTTP"强制要求 OAuth，本地 MCP 无法满足，需用 [tools/mcp_bridge.js](../tools/mcp_bridge.js) 把 STDIO 桥接到 Streamable HTTP。前置：`npm install -g mcp-remote`。

配置：

| 字段 | 值 |
|------|-----|
| 类型 | STDIO |
| 启动命令 | `node` |
| 参数 | `tools/mcp_bridge.js` |
| 工作目录 | 项目根目录（如 `F:\<project_root>`） |

后端零改动，原有直连配置不受影响。详见 [AGENTS.md](../AGENTS.md) 的 "MCP 接入" 章节。

### 方式 C：桌面 GUI

适用：本项目自带的 PySide6 GUI 客户端（启动方式见上"启动 GUI 客户端"段）。

GUI 通过 HTTP + SSE 直连后端，不需要 MCP 配置。

## 已知卡点

部署时可能卡住的地方，按出现频率排序：

### 1. LLM API Key 缺失或配置错误

**症状**：后端能启动但所有 LLM 调用失败，`/health` 的 `llm_pool.total_keys = 0`。

**原因**：`data/llm/keys.json` 不存在 / 格式错误 / key 失效。

**解决**：

- 确认 `data/llm/keys.json` 存在且 JSON 格式正确
- 确认至少有 1 个 tier 3（默认）的 LLM key
- 用 GUI Keys 面板或 `/health` 检查 key 状态

### 2. 管理员权限缺失

**症状**：键鼠操作（`execute_action` / `screen_ocr(mode=window)`）静默失败，但 API 返回 200。

**原因**：Windows UIPI 阻止非管理员进程调 `SetCursorPos` / `SetForegroundWindow`。

**解决**：

- 用 `start.bat` 启动后端（自动 UAC 提权）
- 或手动用管理员权限的 PowerShell 启动
- 用 `/health` 检查 `screen.admin=true`

### 3. GPU / CUDA 缺失

**症状**：`uv sync` 时 PaddlePaddle-GPU 安装失败，或启动后 OCR 调用抛 CUDA 相关错误。

**原因**：无 NVIDIA GPU，或 CUDA 版本不是 12.6。

**解决**：

- 切换 CPU 模式（见上文"安装依赖"段"CPU-only 模式"）
- 或升级 NVIDIA 驱动到兼容 CUDA 12.6 的版本

### 4. Chromium 调试浏览器未启动

**症状**：浏览器相关 MCP 工具调用（`browser_navigate` / `browser_snapshot`）返回 `connect_over_cdp failed`。

**原因**：CDP 9222 端口无浏览器监听。

**解决**：

```bash
uv run python tools/browser/start_debug_browser.py
```

用 `browser_status` MCP 工具检查是否启动成功。

### 5. PaddleOCR 模型加载失败

**症状**：`/ocr/*` 调用返回 500，错误信息含 "model not found" 或 "weight load failed"。

**原因**：`[models].external_dir` 路径错误，或模型文件缺失。

**解决**：

- 检查 `config.toml [models].external_dir` 是否指向有效目录
- 留空 `""` 让回退到项目内 `weights/paddlex/` 目录
- 首次启动会自动下载 PaddleOCR 模型（需要网络）

### 6. PowerShell 终端卡住

**症状**：执行 `curl -s http://...` 或 `git log` 命令后终端不响应。

**原因**：

- `curl` 是 `Invoke-WebRequest` 的别名（PowerShell 5.1）
- `git log` / `git diff` 默认用 `less` pager，超一屏进入分页模式

**解决**：

- 用 `curl.exe` 而非 `curl`
- 用 `git --no-pager log` 或 `git log -n 20`
- 详见 [AGENTS.md](../AGENTS.md) "API 常见陷阱"段

### 7. 配置文件路径在 release 包里失效

**症状**：从 release 包部署后，部分路径包含 `<placeholder>` 或绝对路径错误。

**原因**：release engine 的 path_mapping 规则可能遗漏。

**解决**：

- 检查 `config.toml [browser].user_data_dir` 等绝对路径
- 用 GUI Settings 面板在线编辑
- 详见 [docs/release-policy.md](release-policy.md) "path_mapping 完整性"段

## 验证部署

部署完成后，按以下顺序验证：

```bash
# 1. 后端健康检查
curl.exe -s http://127.0.0.1:8766/health | python -m json.tool

# 2. MCP 端点可用
curl.exe -s http://127.0.0.1:8766/mcp -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 3. 调试浏览器状态
uv run python -c "from server.browser.session.manager import SessionManager; print(SessionManager().get_status())"

# 4. 启动 GUI 客户端
start_client.bat

# 5. 接入 IDE 后调 agent_guide
# 在 IDE 里让 LLM 调用：agent_guide(task='hello')
# 应返回候选 Skill 清单 + first_action
```

## 进一步阅读

- [README_public.md](../README_public.md) — 项目门面
- [docs/architecture.md](architecture.md) — 架构与设计决策深度论述
- [AGENTS.md](../AGENTS.md) — 项目指引、会话规范、核心陷阱、关键约束
- [docs/environment-constraints.md](environment-constraints.md) — PaddlePaddle / Playwright / 调试浏览器兼容性约束
- [docs/operations-manual.md](operations-manual.md) — 后端重启 / 终端 API / 日总结 / 挂机关机 / 浏览器经验 / 记忆系统
