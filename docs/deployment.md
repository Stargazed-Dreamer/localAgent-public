# 部署指南

本文档面向**从 GitHub clone 源码包**的部署者，目标是**先让后端跑起来，再按需增强**。
全程分 4 个阶段，每个阶段有明确的验收判据——**阶段 1 结束你就能看到后端活着，阶段 2 结束才算"能用"**。

> 遇到卡点先看 [已知卡点](#已知卡点) 与 [public 版能力边界](#public-版能力边界) 两节。
> 部署前请先读一遍 [环境要求](#环境要求)，确认你的机器满足必需项。

---

## 0. 部署路线图

| 阶段 | 做什么 | 大致耗时 | 通过判据 |
|------|--------|----------|----------|
| **阶段 0** | clone + 替换路径占位符 + 前置自检 | 2 分钟 | `preflight_check.py` 无 ❌ |
| **阶段 1** | 轻量安装依赖 + 启动后端 | 5–15 分钟 | `/health` 返回 `status: ok` |
| **阶段 2** | REST 验证能力就绪 + 接入 IDE | 5 分钟 | `/guide?task=...` 返回候选 Skill |
| **阶段 3** | 可选增强（OCR / 嵌入 / STT / GUI / 浏览器） | 按需 | 各项自行验证 |

**关键原则**：阶段 0、1、2 只装运行核心所必需的依赖，**不会下载几个 GB 的本地推理模型**。
OCR、本地嵌入模型、语音转写、GUI 面板全部是阶段 3 的可选项，不装不影响后端启动与 MCP 接入。

---

## 1. 环境要求

### 必需

| 项 | 要求 | 说明 |
|----|------|------|
| **操作系统** | Windows 10/11 | 键鼠操控走 Win32 API + UIA，浏览器调试实例路径假设 Windows 文件系统。换平台需要重写一整层 |
| **Python** | 3.12+ | 系统安装即可（用于跑自检脚本）；项目依赖由 uv 管理，不需要手动装 |
| **uv** | 最新版 | 包管理器。安装：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **Git** | 任意版本 | 用于 clone |
| **管理员权限** | 启动后端时需要 | 键鼠操控走 Win32 `SetCursorPos` / `SetForegroundWindow`，无权限会被 Windows UIPI 静默阻止。`start.bat` 会自动 UAC 提权 |
| **LLM API Key** | 至少 1 个 | 后端在**无 key 时也能启动**（依赖 key 的功能会安全停止并报告），但 Agent 实际干活需要。tier 3 为默认层 |

### 可选但推荐

| 项 | 用途 | 缺失影响 |
|----|------|----------|
| **NVIDIA GPU + CUDA 12.6** | PaddleOCR-GPU 推理 / 本地嵌入模型 | 退化为 CPU 推理，OCR 慢 5–10 倍；嵌入模型仍可跑（onnxruntime CPU）。**不装 OCR 也能跑后端** |
| **Chromium 内核浏览器**（Chrome / Edge / Brave / Vivaldi 任一） | 浏览器自动化 | 浏览器类任务不可用。脚本会自动探测，也可显式指定路径 |
| **远程 VL API Key**（ModelScope） | 文档解析、图像描述、无文字元素定位兜底 | 远程 VL 相关工具不可用；OCR bbox 仍能承担文字定位 |
| **第二显示器** | 双屏配置 | 单屏也能跑，但活动追踪的截屏 VL 会拼接所有显示器（`mss.monitors[0]`） |

### 平台兼容性约束

PaddlePaddle / Playwright / 调试浏览器实例的兼容性约束详见 [docs/environment-constraints.md](environment-constraints.md)。

---

## 2. 阶段 0 — clone 与前置自检（不装任何依赖）

### 2.1 克隆仓库

```bash
git clone https://github.com/Stargazed-Dreamer/localagent-public.git localAgent
cd localAgent
```

> clone 到**任意目录**都可以，脚本内部一律用相对路径定位项目根。
> 路径里**不要有中文或空格**，能省掉一批工具链的转义麻烦。

### 2.2 替换路径占位符（clone 后必做）

发布包出于脱敏需要，把原作者的机器路径替换成了占位符（`<project_root>` / `<username>` / `<data_drive>` 等）。
**部署第一步就是把它们换成你本机的真实路径**，否则配置文件里的路径会指向不存在的位置。

先看一眼会改哪些文件（不写入）：

```bash
python tools/deploy/apply_placeholders.py . --dry-run ^
    --project-root D:\code\localAgent ^
    --username your_name ^
    --data-drive D
```

确认清单没问题后，去掉 `--dry-run` 实际写入：

```bash
python tools/deploy/apply_placeholders.py . ^
    --project-root D:\code\localAgent ^
    --username your_name ^
    --data-drive D
```

| 参数 | 含义 | 注意 |
|------|------|------|
| `--project-root` | LocalAgent 项目根的绝对路径 | 就是上一步 `git clone` 出来的目录 |
| `--username` | 你的 Windows 用户名 | 用于少数含用户目录的路径 |
| `--data-drive` | 数据盘盘符**字母**（如 `D`） | **不要带冒号**。传 `D:` 会得到 `D::\...` 双冒号异常路径，脚本会报错退出 |

不传参数则进入交互式问答；可选占位符直接回车跳过。脚本只处理文本文件、保留原换行符风格，且**只用 Python 标准库**，不需要先装依赖。

### 2.3 前置自检

```bash
python tools/deploy/preflight_check.py
```

> ⚠️ 这里用**系统 Python**直跑，**不要写 `uv run python`** —— `uv run` 会先触发 `uv sync`，那就等于直接跳过本阶段开始下载依赖了。

脚本检查 7 项并给出 ✅ / ⚠️ / ❌ 与修复命令：

| 检查项 | 说明 |
|--------|------|
| Python 版本 ≥ 3.12 | 优先识别 `.venv` / uv 管理的解释器 |
| uv 已安装 | 缺失则给出安装命令 |
| 磁盘剩余空间 ≥ 5 GB | 项目所在盘 |
| `config.toml` 存在 | 阶段 1 会创建，此时报 ⚠️ 属正常 |
| `data/llm/keys.json` 有效 JSON | 阶段 1 会创建，此时报 ⚠️ 属正常 |
| 占位符残留检测 | 应为 0；若不为 0，回到 2.2 检查参数 |
| GPU 探测 / 端口 8766 占用 | 无 GPU 只报 ⚠️；端口被占需先释放 |

**退出码**：全过或仅 ⚠️ → `0`；存在 ❌ → `1`。

**判据：没有 ❌ 才进入阶段 1。** 把失败拦在下载任何依赖之前，是这个阶段存在的意义。

---

## 3. 阶段 1 — 轻量安装 + 启动后端

### 3.1 轻量安装依赖

主依赖（`[project.dependencies]`）只含启动必需的轻量包。本地推理栈（`torch` cu126 一个就 4.17 GB、
`paddlepaddle-gpu` 0.98 GB）与行情 / 文档栈放在 `[project.optional-dependencies]` 的 5 个 extras 组里
（`ocr` / `inference` / `stt` / `quant` / `office`，与阶段 3 对照表一一对应），`uv sync` 默认**不装**：

```bash
uv sync
```

`uv sync` 会自动创建 `.venv/`。想一步到位装完整版（含 GPU 推理）用 `uv sync --all-extras`，只是第一次会慢很多。

> **为什么可以跳过**：后端的启动路径与本地推理依赖是解耦的——`paddle` / `paddleocr` / `onnxruntime` / `torch`
> 全部是**函数内延迟导入**，`server/` 顶层没有任何重型 import。实测四个启动入口
> （`server.main` / `client.main` / `client.approval_panel` / 录制器）的**启动期硬依赖只有 PySide6**，
> 所以主依赖**保留了** `pyside6` 与 `playwright`——GUI 与浏览器层保持开箱可用。

> **⚠️ 作者机注意**：主依赖不含重型包，裸 `uv sync`（不带 `--all-extras`）会把环境里已装的重型包**卸载**。
> 作者机日常请用 `uv sync --all-extras --inexact`（或至少加 `--inexact` 只增不删）。
> 日常 `uv run` 不受影响——`uv run` 默认是**非 exact** 同步（只补缺失、不删多余），`--exact` 才会清理。

> **⚠️ 缺包不会报错，是静默降级**：`server/memory/embeddings.py` 捕获异常后退回 BM25-only（只打一句 warning）、
> `server/ocr.py` 的 `paddle` 是首次调用才懒加载、`server/docviewer.py` 与 `server/browser/` 同理。
> 所以**不能靠「看启动日志报缺失模块名」判断该补哪个包**——请在阶段 3 按下面的对照表主动补装。

### 3.2 创建 config.toml

```bash
cp config.example.toml config.toml
```

`config.toml` 管**非敏感配置**（端口 / 路由策略 / Loop 调度 / 浏览器 / 屏幕安全），每个字段在 `config.example.toml` 里都有内嵌注释。
首次部署通常只需要确认两处：

```toml
[browser]
user_data_dir = "D:\\code\\localAgent\\chrome_debug"   # 独立用户数据目录（绝对路径，注意双反斜杠）

[models]
external_dir = ""        # 空字符串 = 用项目内 weights/ 目录；设绝对路径可把模型集中到数据盘（如 "D:\\ai_models"）
```

其余保持默认即可。完整字段说明见 [配置文件详解](#6-配置文件详解)。

### 3.3 创建 data/llm/keys.json

LLM / VL 密钥统一存在这里（schema v9）。**最小可用只需 1 个 LLM provider**：

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
    }
  ]
}
```

tier 说明：1 最轻量 / 2 轻量 / **3 默认** / 4 较强 / 5 最强大（复杂推理 / VL / AIGC）。
`use_case` 的 `default_tier` 决定调用层级；tier 是硬匹配，model 是软偏好（同 tier 内可 fallback）。

需要远程 VL（图像描述 / 布局理解）时，再加一个 `scope: ["vl"]` 的 provider：

```json
{
  "id": "modelscope-vl",
  "label": "ModelScope VL",
  "key": "ms-your-vl-key",
  "base_url": "https://api-inference.modelscope.cn/v1",
  "models": [{"name": "<当前可用的 VL 模型 ID>", "scope": ["vl"], "tier": 2}],
  "vision": {"max_concurrency": 1, "timeout": 60, "rate_limit_cooldown": 60}
}
```

> ModelScope 会不定期下架模型 ID。运行时用 `GET /vision/status` 看实际生效的 `vl_model`。

### 3.4 创建 data/secret/secrets.toml（可选）

非 LLM 密钥（tushare_token / github_token 等）：

```toml
[tokens]
# tushare_token = "your-tushare-token"        # 股票数据用，不需要可省
# github_token = "your-github-pat"            # GitHub 私有镜像监控用，不需要可省
```

只填你需要的，其他可省略。**密钥读取统一经 `lib/secret` 中转**，禁止硬编码进代码或 `config.toml`。
`data/secret/` 与 `data/llm/` 已被 `.gitignore` 排除，不会进仓库。

### 3.5 启动后端

```bash
start.bat
```

`start.bat` 会：① 检测管理员权限，无则自动 UAC 提权 → ② 杀掉占用 8766 端口的旧后端 → ③ 设置 UTF-8 stdio → ④ 启动 `.venv\Scripts\python.exe -m server.main`。

> 🚫 **不要修改 `start*.bat`**。它们内部用 `%~dp0` + 相对 `.venv` 路径，**clone 到任何目录都开箱可用**，没有任何需要你改的地方。
> `.bat` 在中文 Windows 上对编码（GBK + CRLF + 无 BOM）和行尾极其敏感，用编辑器/agent"顺手改一下"很容易把脚本改坏，
> 症状是满屏乱码或报 `'XXX' 不是内部或外部命令`。**确实需要改就先读 `.agents/skills/bat_writing/SKILL.md`。**

手动启动（管理员权限的 PowerShell）：

```bash
.venv\Scripts\python.exe -m server.main
```

启动成功后控制台输出：

```
[INFO] Starting LocalAgent...
[INFO] API: http://127.0.0.1:8766
[INFO] Dashboard: http://127.0.0.1:8766/static/index.html
[INFO] MCP: http://127.0.0.1:8766/mcp
```

### 3.6 阶段 1 验收

```bash
curl.exe -s http://127.0.0.1:8766/health
```

期望 `"status": "ok"`。`/health` 返回各模块状态：

```json
{
  "status": "ok",
  "version": "0.37.0",
  "screen": {"admin": true, "...": "..."},
  "browser": {"cdp_port": 9222, "...": "..."},
  "llm_pool": {"total_keys": 1, "...": "..."},
  "memory": {"db_path": "...", "...": "..."}
}
```

再看一份**逐项解读**（会明确指出"某模块是可选项未安装"而不是笼统报错）：

```bash
python tools/deploy/diagnose.py
```

**判据：`/health` 返回 200 且 `status: ok`；`diagnose.py` 无 ❌。** 到这一步后端已经活着了。

> **PowerShell 陷阱**：在 PowerShell 里写 `curl -s http://...` 会触发 `Invoke-WebRequest`（`curl` 是它的别名），
> 把 `-s` 解析为 `-Session` 然后卡住等输入。**用 `curl.exe` 强制调真正的 curl，或用 `Invoke-RestMethod`**。

---

## 4. 阶段 2 — 验证能力就绪 + 接入 IDE

### 4.1 先用 REST 验证（推荐路径）

**在接 IDE 之前，先用一条 REST 请求确认"后端 + 任务路由 + Skill 注册表"是活的**：

```bash
curl.exe -s "http://127.0.0.1:8766/guide?task=hello"
```

期望返回一座 JSON，含 `task_categories` / `mcp_tool_categories` / `environment_notes` 等字段
（`task=hello` 无匹配时返回 GeneralGuide + `match_hint`，这本身也是"路由工作正常"的证明）。

再试一条明确任务的：

```bash
curl.exe -s "http://127.0.0.1:8766/guide?task=整理下载文件夹"
```

期望返回匹配到的 TaskGuide + `candidates` 候选清单 + `first_action`。

> **为什么优先用 REST 验证**：MCP 客户端**不是热重载的**——你在 IDE 里新加完 MCP server 配置后，
> 通常需要**重启 IDE 或新开一个会话**才会真正加载到这份工具列表。当轮对话里 `agent_guide` 调不到，
> 不代表后端有问题。用 `/guide` 这条 REST 端点验证，能立刻把"后端问题"和"IDE 未重载"区分开。
> 同理，`/health`、`/vision/status`、`/browser/status` 这些 GET 端点都适合用于排障。

### 4.2 接入 IDE（三种方式）

后端通过 MCP（Streamable HTTP，MCP 2025-03-26 协议）暴露能力，端点 `http://127.0.0.1:8766/mcp`。

**方式 A：直连 Streamable HTTP** — 适用 Trae / CatPaw / CodeBuddy 等原生支持 MCP Streamable HTTP 的客户端。

客户端设置 > MCP > 手动添加，粘贴：

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

**方式 B：STDIO 桥接** — 适用 Claude Code / Codex 等只支持 STDIO MCP 的客户端。

ChatGPT 桌面客户端的"流式 HTTP"强制要求 OAuth，本地 MCP 无法满足，需用 `tools/mcp_bridge.js` 把 STDIO 桥接到 Streamable HTTP。
前置：`npm install -g mcp-remote`。

| 字段 | 值 |
|------|-----|
| 类型 | STDIO |
| 启动命令 | `node` |
| 参数 | `tools/mcp_bridge.js` |
| 工作目录 | 项目根目录的绝对路径 |

**方式 C：桌面 GUI** — 本项目自带的 PySide6 客户端（见 [启动 GUI 客户端](#8-启动-gui-客户端)）。
GUI 通过 HTTP + SSE 直连后端，**不需要 MCP 配置**。

### 4.3 阶段 2 验收

在 IDE 里**新开一个会话**（让 MCP 配置生效），让 LLM 调用：

```
agent_guide(task='hello')
```

返回候选 Skill 清单 + `first_action` + 注入的记忆 → **这一步过了才算"能用"**。

---

## 5. 阶段 3 — 可选增强（不装不阻塞）

以上三个阶段跑完，核心后端 + MCP + 记忆（BM25）+ 屏幕 + 浏览器控制已经可用。
下面每一项都是**独立可选**的增强，按需安装即可：

| 增强项 | 装了能得到什么 | 不装会怎样 | 大致代价 |
|--------|----------------|------------|----------|
| **PaddleOCR + CUDA 12.6** | 截图文字定位（Computer Use 主路径），bbox 误差 1–3px | 截图类任务的文字定位不可用；远程 VL 仍可做布局/状态描述 | `paddlepaddle-gpu` 0.98 GB + `paddleocr`/`paddlex` + 首次模型下载 |
| **本地嵌入模型**（bge-small-zh-v1.5） | 三层记忆的语义检索 | 记忆**静默**退化为 BM25-only（不报错，只是召回变弱） | `torch` 4.17 GB + `onnx`/`onnxruntime-gpu`/`transformers`/`tokenizers`，合计约 4.5 GB |
| **远程 VL key**（ModelScope） | 文档解析、图像描述、无文字元素定位兜底 | 相关工具不可用 | 只需一个 API key（在 `keys.json` 配） |
| **STT**（faster-whisper） | 录制器语音转写 | 录制器的音频层不可用 | `faster-whisper` + `ctranslate2`，约 65 MB |
| **行情 / 数据模块**（`workspace/stock_advisor` 等） | 股票数据源、回测、本地行情库 | 该工作区模块**导入即失败**（`panel.py` 顶层 import pandas / matplotlib） | `pandas`/`matplotlib`/`scipy`/`pyarrow`/`duckdb`/`akquant`/`tushare`/`baostock`，约 390 MB |
| **文档解析**（docviewer） | 预览 PDF / Word / PPT / Excel | 文档预览不可用 | `pymupdf`/`python-docx`/`python-pptx`/`openpyxl`，约 52 MB |
| **PySide6 GUI 客户端** | 图形面板（对话 / 监控 / 设置 / 密钥）+ 独立审批面板 | 纯 MCP + HTTP 也够用；但 `start_client.bat` 与审批面板**无法启动** | **679 MB**（Addons 458 + Essentials 213 + 其余 8） |
| **调试浏览器**（CDP 9222） | 浏览器自动化 | 浏览器类任务不可用 | 0（复用已有的 Chrome / Edge） |

> 主依赖已保留 `pyside6` 与 `playwright`（GUI 三入口的启动硬依赖），所以上表前 6 项才是需要补装的对象。
> 各增强项对应 extras 组：`ocr` / `inference` / `stt` / `quant` / `office`（`office` 不含 `openpyxl`——
> 它是纯 Python 轻量包，已在主依赖里，文档预览开箱可用）。

补装方式是**重新跑一次同步**——`--inexact` 保证只增不删：

```bash
uv sync --all-extras --inexact          # 装齐全部（含 GPU 推理、GUI、行情组件）
```

或只补某几组（示例：补 OCR + 嵌入链路，仍不要行情 / STT / 文档）：

```bash
uv sync --inexact --extras ocr --extras inference
```

extras 组名与上表的对应：`ocr`=PaddleOCR、`inference`=本地嵌入模型、`stt`=STT、
`quant`=行情 / 数据模块、`office`=文档解析（`pymupdf`/`python-docx`/`python-pptx`）。

装完**重启后端**（重跑 `start.bat`），再用 `python tools/deploy/diagnose.py` 确认对应模块已就绪。

> **无 GPU 也能跑**：把 `config.toml [models] paddle_device` 设为 `cpu`（模板默认值），
> 并把 `pyproject.toml` 的 `[tool.uv.sources]` 里 `paddlepaddle-gpu` 换成 CPU 版 `paddlepaddle`、
> 删掉 `torch` 的 cu126 索引。详见 [docs/environment-constraints.md](environment-constraints.md)。

---

## 6. 配置文件详解

LocalAgent 用三个配置文件分工管理配置和密钥：

| 文件 | 用途 | 是否进 git | 模板 |
|------|------|-----------|------|
| `config.toml` | 非敏感配置（端口 / 路由策略 / Loop 调度 / 浏览器 / 屏幕安全） | ❌（gitignore） | `config.example.toml` |
| `data/llm/keys.json` | LLM / VL / AIGC 密钥 + tier 系统 + provider 配置 | ❌（gitignore） | 见 `config.example.toml` 注释 |
| `data/secret/secrets.toml` | 非 LLM 密钥（tushare_token / github_token 等） | ❌（gitignore） | 见 `config.example.toml` 注释 |

`config.example.toml` 每个字段都有内嵌注释，这里只列常用的几个段：

```toml
[server]
host = "127.0.0.1"        # 监听地址（仅本机访问）
port = 8766               # 监听端口

[browser]
debug_port = 9222         # 调试浏览器 CDP 端口
user_data_dir = "D:\\code\\localAgent\\chrome_debug"   # 独立用户数据目录（绝对路径，注意双反斜杠）

[models]
external_dir = ""         # 模型存储根目录。空 = 用项目内 weights/；填绝对路径可集中到数据盘

[llm.pool]
retry_count = 3                          # 失败重试次数
rate_limit_cooldown_seconds = 60.0       # 429 后冷却时间
```

### 模型权重路径（可选）

`[models].external_dir` 非空时，所有 AI 模型集中到该目录下按子目录分类：

```
external_dir = "D:\\ai_models"
├── paddleocr/      - PaddleOCR 模型
├── embeddings/     - 嵌入模型 bge-small-zh-v1.5
├── faster_whisper/ - Whisper STT 模型
├── huggingface/    - HF 缓存（HF_HOME 指向）
└── modelscope/     - ModelScope 缓存（MODELSCOPE_CACHE 指向）
```

留空 `""` 时各模块回退到项目内 `weights/` 目录（发布模式默认）。细节见 [docs/model-paths.md](model-paths.md)。

**安全约束**（见 [.agents/rules/project_rules.md](../.agents/rules/project_rules.md) "密钥统一管理"段）：

- 禁止把密钥硬编码进代码或写入 `config.toml`
- 所有密钥读取必须经 `lib/secret` 中转
- 新增密钥存到 `secrets.toml [tokens]` 或 `data/llm/keys.json`（LLM 密钥）

---

## 7. 启动调试浏览器

涉及浏览器操作时（DOM 操作 / 截图 / 表单填写），先启动调试浏览器实例：

```bash
uv run python tools/browser/start_debug_browser.py
```

脚本会：自动探测 Chrome / Edge / Brave / Vivaldi（按此顺序）→ 用独立的 `chrome_debug/` 用户数据目录（与你的工作浏览器隔离）→ 启动 CDP 调试端口 9222 → 非交互模式。

选项：

```bash
# 从工作浏览器复制登录态（首次使用推荐）
uv run python tools/browser/start_debug_browser.py --copy-user-data

# 显式指定浏览器路径
uv run python tools/browser/start_debug_browser.py --browser-path "C:\Program Files\Google\Chrome\Application\chrome.exe"

# 指定端口和用户数据目录
uv run python tools/browser/start_debug_browser.py --port 9222 --user-data-dir "D:\code\localAgent\chrome_debug"
```

**重要约束**（见 [environment-constraints.md](environment-constraints.md) "调试浏览器实例"章节）：

- 不要打开你的工作浏览器
- 不要执行 `playwright install`（`start_debug_browser.py` 已处理）
- 所有浏览器操作必须 `connect_over_cdp("http://127.0.0.1:9222")` 连接已运行实例，禁止启动新浏览器

---

## 8. 启动 GUI 客户端

```bash
start_client.bat
# 或
.venv\Scripts\python.exe -m client.main
```

> `pyside6` 已包含在阶段 1 的默认清单里（GUI 三入口的启动硬依赖，见 [阶段 1](#3-阶段-1--轻量安装--启动后端)）。
> 纯 MCP / REST 用法不需要 GUI。

GUI 面板由 `PanelRegistry` 扫描 `client/panels/` 自动发现，按 main / monitor / advanced 三组排列：

| 分组 | 面板（概览） |
|------|------|
| **main** | 对话（v6-lite 引擎：真 SSE 流式 + 打字机三档 + 工具调用块 + steer 引导）、概览、待办 hub、工具、收件箱、到期任务、WIP 任务 |
| **monitor** | 状态监控、终端、Loop、模型池、记忆、日总结 |
| **advanced** | 设置（在线编辑 `config.toml`）、密钥（在线编辑 keys.json）、系统工具、入站管理 |

另有 workspace manifest 声明的**组件面板**（如记账审核）按需加载；**独立进程审批面板**用 `start_approval_panel.bat` 单独启动。
完整面板清单以 `client/panels/` 目录与 `client/core/panel_registry.py` 的发现结果为准。

---

## 9. 已知卡点

按出现频率排序。

### 1. LLM API Key 缺失或配置错误

**症状**：后端能启动但所有 LLM 调用失败，`/health` 的 `llm_pool.total_keys = 0`。

**原因**：`data/llm/keys.json` 不存在 / JSON 格式错误 / key 失效。

**解决**：确认文件存在且 JSON 合法 → 确认至少有 1 个 tier 3 的 LLM key → 用 GUI 密钥面板或 `/health` 检查 key 状态。

### 2. 管理员权限缺失

**症状**：键鼠操作（`execute_action` / `screen_ocr(mode=window)`）静默失败，但 API 返回 200。

**原因**：Windows UIPI 阻止非管理员进程调 `SetCursorPos` / `SetForegroundWindow`。

**解决**：用 `start.bat` 启动（自动 UAC 提权），或手动用管理员 PowerShell 启动；用 `/health` 确认 `screen.admin=true`。

### 3. GPU / CUDA 缺失

**症状**：装 PaddlePaddle-GPU 时失败，或启动后 OCR 调用抛 CUDA 相关错误。

**原因**：无 NVIDIA GPU，或 CUDA 版本不是 12.6。

**解决**：切 CPU 模式（见 [阶段 3](#5-阶段-3--可选增强不装不阻塞) 末尾），或升级 NVIDIA 驱动到兼容 CUDA 12.6 的版本。
**若你还没装 OCR（阶段 3 之前），这条与核心功能无关。**

### 4. Chromium 调试浏览器未启动

**症状**：浏览器工具调用返回 `connect_over_cdp failed`。

**原因**：CDP 9222 端口无浏览器监听。

**解决**：`uv run python tools/browser/start_debug_browser.py`。

### 5. PaddleOCR 模型加载失败

**症状**：`/ocr/*` 调用返回 500，错误信息含 `model not found` 或 `weight load failed`。

**原因**：`[models].external_dir` 路径错误，或模型文件缺失。

**解决**：检查 `config.toml [models].external_dir`；留空 `""` 回退到项目内 `weights/paddlex/`；
首次启动会自动下载 PaddleOCR 模型（需要网络）。

### 6. PowerShell 终端卡住

**症状**：执行 `curl -s http://...` 或 `git log` 后终端不响应。

**原因**：`curl` 是 `Invoke-WebRequest` 的别名（PowerShell 5.1）；`git log` 默认用 `less` 分页。

**解决**：用 `curl.exe` 而非 `curl`；用 `git --no-pager log`。

### 7. 配置里仍有 `<placeholder>` 残留

**症状**：`preflight_check.py` 报占位符残留，或运行时提示路径不存在。

**原因**：阶段 0 的占位符替换没跑，或某个占位符被跳过了。

**解决**：重跑 `python tools/deploy/apply_placeholders.py . --dry-run ...` 看清单，
确认参数（尤其 `--data-drive` 不要带冒号）后去掉 `--dry-run` 执行；
也可以直接用 GUI 设置面板在线编辑 `config.toml`。

### 8. 记忆系统静默退化为 BM25-only

**症状**：`/health` 中 `memory.embedding_ready` 为 `false`，语义检索不可用，且**没有任何报错**。

**原因**：目标机器三者全无 —— 本地无 `model.onnx` + 无 `onnxruntime`、本地权重 + 无 `torch`/`transformers`、也无 `sentence-transformers`。

**解决**：装上阶段 3 的"本地嵌入模型"链路（`torch` + `transformers` 或 `onnxruntime`），或接受 BM25-only。
详见 [其他机器部署常见问题](#其他机器部署常见问题)。

---

## 10. public 版能力边界

本包是**完全开源版**（除个人数据外全发），包含核心代码与下列 10 个 workspace 组件：

| 组件 | 说明 |
|------|------|
| `disk_manager` | 磁盘清理与备份 |
| `recorder` | 操作录制器（L0–L4 五层架构） |
| `modelscope_model_update` | ModelScope 模型库更新 |
| `accounting` | 记账（识别 + 审核） |
| `dev_toolkit` | 工程化开发工具集 |
| `life_design` | 人生设计对话 |
| `arknights_gacha` / `wuwa_gacha` / `endfield_gacha` / `yihuan_gacha` | 4 个抽卡记录采集模块 |

**不在本包内的内容**：原作者的**个人业务模块**（如股票 / 社群内容 / 图文整理 / 求职 / 考试复习等个人场景的 skill 与工作区）、
个人运行数据、浏览器登录态、密钥、Git 历史、内部发布流程文件。

> 判断标准很简单：**通用工具与游戏组件在包内；绑定个人生活场景的模块不在。**
> 因此 README 里"60+ Skill"的完整清单在公开版会少一部分——这是有意的裁剪，不是缺文件。

---

## 其他机器部署常见问题

### 嵌入模型下载后仍然不可用

`BAAI/bge-small-zh-v1.5` 官方仓库（HuggingFace 与 ModelScope 一致）**不含任何 ONNX 文件**，
只有 `model.safetensors` / `pytorch_model.bin` 及配套配置。
所以下载阶段拿不到 `model.onnx` 是**正常的**，必须靠本地 `torch` 转换，或装 `sentence-transformers` 走降级路径。

目标机器满足以下任意一条，嵌入才可用：

| 条件 | 走的路径 |
|------|----------|
| 本地已有 `model.onnx` + 装了 `onnxruntime` | ONNX 直接推理（最快） |
| 本地有权重 + 装了 `torch` + `transformers` | 本地导出 ONNX（不联网） |
| 装了 `sentence-transformers` | ST 降级推理 |

三者全无 → 静默退化为 BM25-only（见 [已知卡点 8](#8-记忆系统静默退化为-bm25-only)）。

### PaddleOCR 模型缓存位置

`server/ocr.py` 通过 `PADDLE_PDX_CACHE_HOME` 让 `config.toml [models].external_dir` 接管 OCR 模型缓存，
默认落在 `<external_dir>/paddleocr/`；留空 `external_dir` 则回退到项目内 `weights/paddlex/`。
细节见 [docs/model-paths.md](model-paths.md)。

### HF / ModelScope 下载慢

设置镜像环境变量后再触发下载（首次 OCR / 嵌入模型下载时尤其明显）：

```bash
set HF_ENDPOINT=https://hf-mirror.com
set MODELSCOPE_CACHE=D:\ai_models\modelscope
```

---

## 部署后检查清单

- [ ] `python tools/deploy/preflight_check.py` 无 ❌
- [ ] `curl.exe -s http://127.0.0.1:8766/health` 返回 `status: ok`
- [ ] `python tools/deploy/diagnose.py` 无 ❌
- [ ] `curl.exe -s "http://127.0.0.1:8766/guide?task=hello"` 返回 JSON
- [ ] IDE 里新开会话后 `agent_guide(task='hello')` 有返回
- [ ] （可选）`config.toml` 的 `[browser].user_data_dir` 指向本机真实路径
- [ ] （可选）`data/llm/keys.json` 里至少 1 个 tier 3 key 可用

---

## 进一步阅读

- [README.md](../README.md) — 项目门面与能力一览
- [docs/architecture.md](architecture.md) — 架构与设计决策深度论述
- [AGENTS.md](../AGENTS.md) — 项目指引、会话规范、核心陷阱、关键约束
- [docs/environment-constraints.md](environment-constraints.md) — PaddlePaddle / Playwright / 调试浏览器兼容性约束
- [docs/operations-manual.md](operations-manual.md) — 后端重启 / 终端 API / 日总结 / 挂机关机 / 浏览器经验 / 记忆系统
- [docs/model-paths.md](model-paths.md) — 模型路径管理
- [SECURITY-RISKS.md](../SECURITY-RISKS.md) — 已知安全风险登记（个人单机使用尺度）
