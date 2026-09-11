# project_essentials - LocalAgent 项目改进精华

> LocalAgent 项目在 anthropics/skills 基础上的改进与本土化汇总。
> 适用于新项目启动时直接落地，避免重复踩坑。

## 1. 概述

本文件汇总 LocalAgent 项目（`<project_root>\`）在 anthropics/skills 基础上做的改进与本土化：

1. **anti-AI-slop 实战经验**：合并 brand-guidelines + frontend-design 精华 + 本项目用户偏好
2. **Reconnaissance-Then-Action 模式**：动态页面调试铁律
3. **三层 MCP 架构**：DIRECT_TOOLS + advanced_tool 网关 + GATEWAY_EXCLUDE
4. **Tool Annotations 落地**：4 hint × 全工具映射表
5. **Skill 编写规范**：500 行软上限 / Domain organization / pushy description / evals 必备
6. **用户补充指令注入**：让用户能在 agent 工作中补充指令
7. **终端会话 API**：长时间运行命令管理
8. **响应大小保护**：base64 撑爆上下文的预防
9. **磁盘清理强制规则**：先清单审核再执行
10. **审批 token 三层递进**：静态规则 + LLM 预审 + 人审

**与 anthropics/skills 的关系**：
- anthropic 是**通用规范**，适合所有项目
- 本文件是**本土化改进**，针对个人 AI Agent 场景做的优化
- 两者互补：anthropic 给骨架，本文件给血肉

## 2. anti-AI-slop 设计原则

合并 anthropic brand-guidelines + frontend-design 精华 + 本项目实战。

### 2.1 七大反模式（必须避免）

| # | 反模式 | 为什么是 slop | 正确做法 |
|---|--------|--------------|---------|
| 1 | **过度居中** | 视觉呆板、无层级、无方向感 | 主轴对齐 + 次轴左对齐；用列宽和留白制造节奏 |
| 2 | **紫色渐变**（紫蓝/紫粉） | AI 默认色板，所有模型都偏这个 | 选与主题相关的具象色（如绿色对应自然/医疗，赤陶对应陶艺） |
| 3 | **统一圆角**（所有元素 8px/12px） | 无差别，丢失层级 | 区分：图直角 / 卡片 8px / 按钮 4px / 输入框 2px |
| 4 | **Inter 字体**（无衬线、Inter 单一字族） | AI 默认字体，毫无个性 | 配对：display 用衬线/grotesk/mono；body 用 Lora/Source Serif/思源 |
| 5 | **emoji 堆砌**（🚀✨🎯 当图标） | 没设计图标时的偷懒 | 用 SVG icon 或字体图标（Lucide/Heroicons） |
| 6 | **均匀间距**（所有 margin/padding 16px） | 无视觉重点、无呼吸 | 8/16/24/40 多档间距；主标题留白 > 段落 > 行内 |
| 7 | **纯文本骨架**（只有 h1+p+ul） | 像论文不像界面 | 加分隔线/编号眉标/数据卡/引文块 |

### 2.2 三种 AI 默认 look（要主动跳出）

来自 anthropic frontend-design：
1. **暖米色背景（#F4F1EA）+ 高对比衬线显示字 + 赤陶强调** —— 适合陶艺/工艺品 brief
2. **近黑背景 + 酸绿或朱红单一强调** —— 适合 cyber/tech brief
3. **报纸式布局 + 发丝分割线 + 零圆角 + 密集列** —— 适合新闻/报告 brief

> 这三种都是**默认**而非**选择**——任何 brief 都会产出来。brief 没指明时，不要把自由度花在这些默认上。

### 2.3 Dominance over Equality（主色 60-70%）

设计要有主从：主色占 60-70%，副色 20-30%，强调色 5-10%。三色均分 = AI slop。

```css
:root {
  --color-primary: #2e7d32;      /* 主色 70% */
  --color-secondary: #788c5d;    /* 副色 20% */
  --color-accent: #d97757;       /* 强调 10% */
}
```

### 2.4 设计 risk：每个 brief 至少冒一个险

> 让 signature 元素是唯一让人记住的东西，其他保持克制纪律。**不冒险本身也可能是冒险**——做成默认样子等于没做。

**signature 例子**：
- 独特的字体处理（大号下沉首字母）
- 非典型的色彩组合（医疗用品用陶土橙 + 苔藓绿）
- 手工感装饰元素（手绘 SVG 分隔符）
- 交互时刻（滚动触发的有节奏揭示）

### 2.5 用户偏好清单（本项目实测）

| 偏好 | 说明 | 实现 |
|------|------|------|
| **绿色主色** | 用户偏好自然系绿色，不要紫色 | `--color-primary: #2e7d32;` 系列 |
| **避免紫色** | 紫色被 AI 滥用，用户反感 | 主题色板禁用紫蓝/紫粉 |
| **PySide6 优于 web** | 桌面应用首选 PySide6，不做 Web 版 | 项目内 `client/` 用 PySide6 |
| **未读数指示** | 列表/标签要有未读计数徽章 | `<span class="badge">5</span>` |
| **按钮宽度** | 主按钮宽度固定，不要撑满 | `width: 120px;` 或 `min-width: 80px;` |
| **只读状态** | 不可操作元素有视觉提示 | `opacity: 0.5; cursor: not-allowed;` |

### 2.6 反感清单（不要做）

- 紫色 / 紫蓝 / 紫粉渐变
- 过度动画（每次点击都弹跳）
- 模糊背景（backdrop-filter: blur）滥用
- 满屏 emoji 图标
- 单一 Inter 字体
- 所有元素统一 12px 圆角
- 全居中布局
- AI 默认的"暖米色 + 赤陶 + 衬线"组合（除非 brief 明确要）

## 3. Reconnaissance-Then-Action 模式

来自 anthropic webapp-testing + 本项目强化。

### 3.1 核心原则：先侦察再行动

**铁律**：在动态 Web 应用上做任何 DOM 操作之前，必须先：
1. 导航到目标页面
2. 等待 `networkidle`
3. 截图或读 DOM 确认渲染状态
4. 从渲染状态识别选择器
5. 用发现的选择器执行操作

**为什么**：动态 SPA（React/Vue/Svelte）在 JS 执行前 DOM 是空的或骨架屏，提前找选择器会失败或找到错误元素。

### 3.2 networkidle CRITICAL

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173')
    page.wait_for_load_state('networkidle')  # ⚠️ CRITICAL
    # 现在才能安全 inspect DOM
    content = page.content()
```

**`networkidle` 含义**：等待 500ms 内没有网络请求（页面已加载完成、JS 已执行、异步请求已结束）。

**何时不用**：长轮询/WebSocket 应用（永远不 idle）、持续刷新页面。此时用 `domcontentloaded` + 显式 `wait_for_selector`。

### 3.3 4 步流程

```
1. Navigate + wait
   page.goto(url)
   page.wait_for_load_state('networkidle')

2. Inspect（侦察）
   page.screenshot(path='/tmp/inspect.png', full_page=True)
   content = page.content()
   buttons = page.locator('button').all()

3. Identify（识别选择器）
   从渲染状态识别：role > text > id > data-testid > CSS class

4. Execute（执行）
   page.get_by_role('button', name='提交').click()
```

### 3.4 决策树

```
用户任务 → 是静态 HTML 吗？
│
├─ 是 → 直接读 HTML 文件识别选择器
│  ├─ 成功 → 写 Playwright 脚本用选择器
│  └─ 失败/不完整 → 当成动态处理
│
└─ 否（动态 SPA）→ 服务器已运行吗？
   │
   ├─ 否 → 启动服务器，等待就绪后执行
   │
   └─ 是 → Reconnaissance-Then-Action 4 步流程
```

### 3.5 常见踩坑

- ❌ 不等 `networkidle` 就找元素 → 找不到
- ❌ 用动态 CSS class（如 `.css-1abc23`） → 下次 build 就变
- ❌ 用 sleep 等待渲染 → 不可靠
- ❌ 测试间状态泄漏（共享 cookie/localStorage） → 测试互相影响

## 4. 三层 MCP 架构

来自本项目实战，详见 `<project_root>\server\mcp_whitelist.py`。

### 4.1 三层暴露策略

```
┌─────────────────────────────────────────────────────┐
│ 第一层：DIRECT_TOOLS（直连 MCP 工具）              │
│ - agent 日常高频用                                  │
│ - 直接暴露为独立 MCP 工具                           │
│ - 例：memory_get / list_files / exec_python         │
├─────────────────────────────────────────────────────┤
│ 第二层：网关（advanced_tool）                       │
│ - agent 偶尔用 / 通用能力                           │
│ - 通过单一网关工具调用，operation_id 作参数         │
│ - 例：todos_get / wip_create / memory_search        │
├─────────────────────────────────────────────────────┤
│ 第三层：GATEWAY_EXCLUDE（仅 REST 可用）             │
│ - 给 GUI / 脚本 / 监控面板用，agent 不该调          │
│ - 不暴露给 MCP，REST 接口仍可用                     │
│ - 例：shutdown / health / config / batch ops        │
└─────────────────────────────────────────────────────┘
```

### 4.2 工具选择决策树（给 agent 用）

```
遇到"要调后端能力"的需求
│
├─ 1. 直连 MCP 工具（白名单）→ 直接用
│  查白名单：list_tools()
│
├─ 2. advanced_tool 网关 → 适用于非直连的 REST 端点
│  调 GET 类端点免审批（safety=read_only）
│
├─ 3. template_tool → 适用于预定义工作流
│  例：截图+OCR、浏览器导航+等待
│
└─ 4. exec_python → 仅用于真正需要运行代码
   ⚠️ 反模式：用 exec_python 发 HTTP 调本地 API
   正确：用 advanced_tool 调 GET 类端点免审批
```

### 4.3 50KB 响应大小保护

MCP 网关层有 50KB 硬上限 size guard：
- 单个 TextContent 超阈值 → 头尾截断
- marker 文案明确指示"禁止静默尝试其它方案"+"请立即向用户报告"
- **触发截断时必须上报用户**，不要自行缩小参数重试或换工具

高频大返回端点提供精简参数，agent 默认应优先使用：

| 端点 | 精简参数 | 说明 |
|------|---------|------|
| `list_tools` | 默认精简（无 parameters） | 需要参数 schema 时显式传 `verbose=true` |
| `llm_pool_status` | `summary=true` | 省略 keys 详情数组 |
| `wip_list` | `summary=true` | 只返回 id/title/status/priority/progress |
| `memory_search` | `top_k=10`（默认） | 最大 100，按需调 |
| `inbox_list` | `limit=200`（默认） | 最大 1000 |

### 4.4 防膨胀策略（GATEWAY_EXCLUDE）

加端点前必须先判断是否要进 MCP：

```
新增 REST 端点 → 判断是否要进 MCP
│
├─ agent 日常会用吗？ → DIRECT_TOOLS（第一层）
│  例：inbox_list / inbox_get（agent 查询待审查条目）
│
├─ agent 偶尔用、属于通用能力？ → 自动收进网关（第二层）
│  例：todos_get / wip_create / memory_search
│
└─ 只给 GUI/面板/脚本用？ → GATEWAY_EXCLUDE（第三层）
   例：inbox_batch / user_message / activity_daily
```

**强制规则**：
- 新增 REST 端点后，必须在 `mcp_whitelist.py` 显式决定：进 `DIRECT_TOOLS` 还是加 `GATEWAY_EXCLUDE`
- 运维/批量/管理类端点（GUI/面板/脚本调用）一律 `GATEWAY_EXCLUDE`
- 高频只读端点才考虑 `DIRECT_TOOLS`（agent 每次会话都可能用）

### 4.5 本项目实际数据

来自 `<project_root>\server\mcp_whitelist.py`：
- DIRECT_TOOLS：~35 个直连工具
- 网关自动收纳：~50 个工具（通过 `localagent_advanced_tool` 访问）
- GATEWAY_EXCLUDE：~20 个排除工具（仅 REST 可用）

总 REST 端点 ~105 个，但 MCP 工具列表只暴露 ~37 个（35 直连 + 2 网关），避免膨胀。

## 5. Tool Annotations 4 hint

来自 MCP 协议规范 + 本项目落地。

### 5.1 四个 hint

```python
TOOL_ANNOTATIONS = {
    "tool_name": {
        "readOnlyHint": True,      # 工具无副作用（纯查询），可安全重复调用
        "destructiveHint": False, # 工具会破坏数据/状态（删除/终止/覆盖）
        "idempotentHint": True,    # 相同参数重复调用效果相同
        "openWorldHint": False,    # 工具与外部世界交互（网络/文件系统/进程）
    }
}
```

| hint | true 含义 | 典型工具 |
|------|----------|---------|
| `readOnlyHint` | 纯读无副作用 | `list_files` / `get_user` / `search_docs` |
| `destructiveHint` | 破坏数据/状态 | `delete_file` / `kill_process` / `drop_table` |
| `idempotentHint` | 重复调用同效 | `set_user_name` / `create_if_not_exists` |
| `openWorldHint` | 与外部交互 | `call_api` / `send_email` / `read_file` |

**默认全 false**（保守策略，agent 需自行判断）。

### 5.2 与 safety 分类的区别

| 维度 | Tool Annotations | safety 分类 |
|------|-----------------|-------------|
| 性质 | 软提示（语义标记） | 硬约束（是否需要审批） |
| 字段 | 4 个 hint | `read_only` / `safe` / `approval_required` |
| 用途 | agent 选择工具参考 | 是否拦截执行 |

**两者互补**：一个工具可以 `readOnlyHint=true`（语义只读）但 `approval_required=true`（读敏感数据要审批）。

### 5.3 本项目全工具映射参考

来自 `<project_root>\server\mcp_whitelist.py`：

```python
TOOL_ANNOTATIONS = {
    # === 纯只读查询（readOnly + idempotent） ===
    "agent_guide":               {"readOnlyHint": True,  "idempotentHint": True},
    "list_tools":                {"readOnlyHint": True,  "idempotentHint": True},
    "exec_status":               {"readOnlyHint": True,  "idempotentHint": True},
    "memory_get":                {"readOnlyHint": True,  "idempotentHint": True},
    "memory_list":               {"readOnlyHint": True,  "idempotentHint": True},
    "list_windows":              {"readOnlyHint": True,  "idempotentHint": True},
    "browser_list_tabs":         {"readOnlyHint": True,  "idempotentHint": True},

    # === 幂等写（idempotent + openWorld） ===
    "memory_set":                {"idempotentHint": True, "openWorldHint": True},

    # === 破坏性（destructive + openWorld） ===
    "exec_terminal_kill":        {"destructiveHint": True, "openWorldHint": True},
    "exec_terminal_delete":      {"destructiveHint": True, "openWorldHint": True},

    # === 开放世界交互（openWorld） ===
    "exec_python":               {"openWorldHint": True},
    "execute_action":            {"openWorldHint": True},
    "browser_open":              {"openWorldHint": True},
    "agent_chat":                {"openWorldHint": True},
}
```

## 6. Skill 编写规范

来自本项目强化版 `skill-creator.md`。

### 6.1 核心理念

1. **Skill 是文件夹，不是文件**：`SKILL.md` + 可选 `scripts/` / `references/` / `assets/`
2. **可执行的知识**：不是文档，而是工作流指令
3. **渐进式细化**：先骨架，再测试，再迭代

### 6.2 500 行软上限

接近上限时按以下策略分层：

1. **拆到 references/**：详细映射表 / API 参数说明 / 长示例代码移到 `references/*.md`
2. **Domain organization**：按变体分文件（如 ocr skill 下分 paddleocr.md / qwen_vl.md）
3. **大参考文件加 TOC**：>300 行的 reference 文件顶部加目录
4. **scripts/ 封装确定性逻辑**：可执行流程放 `scripts/*.py`，agent 调用而非现场重写

### 6.3 Description pushy 原则

> Claude 倾向于 undertrigger skills。为对抗这点，description 要主动"推"。

**pushy 三原则**：
1. 明确列全触发场景
2. 用"当用户提到 X / Y / Z 时使用此 skill"的主动句式
3. 覆盖边界场景 + 中英文双语触发词

**反例**（不够 pushy）：
> 创建 HTML 工具页面的 skill。

**正例**（pushy）：
> 编写和调试 HTML 工具页面。当用户要求创建或修改 HTML 页面（如可视化 Dashboard、监控面板、审核页面、工具页面）时触发。当用户提到"写个页面"、"前端"、"Dashboard"、"可视化"时也使用此 skill。

### 6.4 evals 必备

有客观可验证输出的 skill 必须写 `evals/evals.json`：
- 2-5 个用例覆盖正常流程 + 1 个边界场景
- 用 5 种断言类型（contains / not_contains / regex / file_exists / json_field）
- prompt 像真人说话，不写"测试 X 功能"
- 详见 `workspace/dev_toolkit/evals_template.json`

### 6.5 写作风格权衡

- 安全规则用"必须/绝不/禁止"硬约束（如"删除操作必须用户确认"）
- 工作流指引优先用"解释为什么这样做"而非堆砌 MUST
- **理解 why 的 agent 能在未覆盖的场景做出正确判断**
- 只会照搬 MUST 的 agent 遇到新情况就僵在轨道上

## 7. 用户补充指令注入

来自本项目 `/user/message` 端点。

### 7.1 工作原理

1. 用户通过 `POST /user/message` 发送文本指令，指令暂存在内存队列
2. 后端中间件在非工作端点的 JSON 响应中注入 `_user_supplement` 字段
3. MCP monkey-patch 在工具返回结果中追加 TextContent 块，标记为用户补充
4. agent 收到后可以继续当前任务，同时考虑用户的补充需求

### 7.2 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/user/message` | GET | 获取待发送指令列表 |
| `/user/message` | POST | 发送新指令 `{"text": "..."}` |
| `/user/message` | DELETE | 清空所有待发送指令 |
| `/user/message/{id}` | DELETE | 取消指定指令 |

### 7.3 排除的工作端点

以下端点不注入用户消息（脚本/系统调用，非 agent 直接使用）：

- `/llm/pool/*` - LLM 并发池代理（脚本调用）
- `/mcp/*` - MCP 协议端点（通过 monkey-patch 单独处理）
- `/user/*` - 用户消息端点自身（避免自反馈）
- `/static/*` `/output/*` `/docs` `/openapi.json` `/redoc` - 静态资源/文档

**设计原则**：用户消息的存在意义就是让 agent 收到，所以排除清单应最小化。工作端点（`/exec` `/ocr` `/vision` `/screen` `/browser` `/mindforge` 等）不排除——agent 在执行这些操作时也应能收到用户的补充指令。

### 7.4 GUI 工具

- `tools/user_message_gui.py` - PySide6 界面，支持发送/取消指令

### 7.5 实现要点

- **暂存队列**：内存队列，避免持久化（用户消息是临时的）
- **注入时机**：在非工作端点的 JSON 响应中注入
- **MCP 兼容**：MCP 工具返回结果通过 monkey-patch 追加 TextContent 块
- **标记清晰**：注入的内容明确标记为"用户补充指令"，与原响应内容区分

## 8. 响应大小保护

来自本项目实战教训。

### 8.1 禁止 base64 污染上下文（强制规则）

> 所有代码、脚本、MCP 工具调用都不得返回大段 base64 数据（截图、文件、音频等）到 LLM 上下文。

**违反此规则会导致上下文窗口被数 MB 的 base64 字符串撑爆，LLM 无法继续工作**。

### 8.2 正确做法

| 场景 | 错误做法 | 正确做法 |
|------|---------|---------|
| 截图给多模态 LLM | `capture_screen(format="base64")` | `capture_screen(format="inline")`（返回 ImageContent，由 MCP 协议处理） |
| 截图特征分析 | base64 进上下文 | `screen_analyze`（返回纯文本的颜色/亮度/异常分析） |
| 截图+OCR | base64 + 调 OCR | `screen_ocr`（直接返回 text + bbox） |
| 文件读取 | base64 编码返回 | `docviewer_read` 或 `exec_python` 提取文本 |

### 8.3 截图+OCR 正确流程

```
localagent_advanced_tool(
    tool="screen_ocr",
    params={"mode":"window", "hwnd":123, "engine":"ocr"}
)
```

一步截图并返回 `text + details[].box`，不返回图片数据。

### 8.4 输出截断规则

- `/exec/python` 返回的 stdout/stderr 超过 8000 字符时自动截断，保留头部+尾部
- 截断时返回 `exec_id`、`stdout_truncated=true`、`stdout_total_chars` 等字段
- **查看完整输出**：`POST /output/{exec_id}` 指定 `action=range` + 字符区间
- **搜索输出内容**：`POST /output/{exec_id}` 指定 `action=search` + 搜索词，返回匹配位置及上下文
- 缓冲区保留最近 20 条执行输出，超出自动淘汰

### 8.5 50KB 网关 size guard

MCP 网关层有 50KB 硬上限：
- 单个 TextContent 超阈值 → 头尾截断
- marker 文案明确指示"禁止静默尝试其它方案"+"请立即向用户报告"
- **触发截断时必须上报用户**，不要自行缩小参数重试或换工具

## 9. 终端会话 API

来自本项目 `/exec/terminal/*` 端点，用于长时间运行命令管理。

### 9.1 为什么需要

`/exec/python` 有 30s 默认超时，不适合：
- 模型下载（几分钟到几小时）
- pip 安装（30s 到几分钟）
- 批量处理（不可预测时长）

终端会话 API 提供非阻塞的长时间运行命令管理。

### 9.2 端点

| MCP 工具 | 用途 |
|---------|------|
| `exec_terminal_spawn` | 启动后台终端（返回 tid，不阻塞） |
| `exec_terminals_list` | 列出所有终端（状态/PID/输出字符数） |
| `exec_terminal_detail` | 查看终端详情（stdout/stderr，支持 tail 截取） |
| `exec_terminal_output` | 按字节游标增量读取完整 stdout/stderr（单次最多 256 KiB） |
| `exec_terminal_input` | 向运行中终端发送输入（交互式命令/Ctrl+C） |
| `exec_terminal_kill` | 终止运行中终端 |
| `exec_terminal_delete` | 删除已结束终端记录 |

### 9.3 典型流程

1. `exec_terminal_spawn(cmd="python download.py", label="download_model")` → 获取 tid
2. 轮询 `exec_terminal_detail(tid, tail=3000)` 查看近期进度
3. 如需交互：`exec_terminal_input(tid, text="y")` 回应提示
4. 需要完整日志时，用 `exec_terminal_output(tid, channel="stdout", offset=0, limit=65536)` 按 `next_offset` 分页读取
5. 完成后 `exec_terminal_delete(tid)` 清理会话和磁盘日志

### 9.4 实现要点

- 终端输出以 `temp/terminals/` 下的日志文件为真源
- 内存只保留每通道最近 64 KiB
- stdout/stderr 会并发排空，避免高输出命令因管道写满死锁
- 运行中终端默认上限为 10 个，防止 Agent 无界创建后台进程

### 9.5 终端监控

通过 GUI 客户端（`start_client.bat`）的 Monitoring 面板查看终端管理标签页，实时查看所有终端输出。

## 10. 磁盘清理强制规则

来自本项目实战教训。

### 10.1 强制规则

> 任何形式的磁盘清理——包括但不限于删除模型缓存、pip/uv 缓存、临时文件、旧环境、下载残留——必须先拉取清单给用户审核，获得明确同意后才可执行。

### 10.2 流程

1. **清理前**：列出所有待删除文件的完整路径和大小，生成清单表格
2. **审核**：将清单提交给用户，等待用户逐项确认或整体批准
3. **执行**：只删除用户确认的文件，删除完成后报告释放的空间
4. **例外**：Agent 自己临时创建的脚本文件（`temp/` 目录下的临时脚本）无需审核，可直接删除

**违反此规则可能导致用户重要数据/模型被误删，是不可接受的错误。**

### 10.3 删除与高风险命令必须走可检查路线

- 任何删除、清理、递归删除、格式化、强制覆盖、强制终止进程或会丢失用户数据的操作，必须使用项目内的 exec_cmd / exec_terminal_spawn / exec_python，**不要使用平台的直接 shell/exec_command 路径**
- 直接 shell 路径可能不触发 PreToolUse Hook，因此即使有 hooks.json 也不能把直接 shell 当作安全路线
- 返回 `blocked=true` 时，禁止改写、拆分、编码、写脚本、切换 shell、换工具或改用其他执行路径绕过
- 必须先说明完整命令、执行原因、目标、影响和更安全替代方案

## 11. 审批 token 三层递进

来自本项目 `command_guard` 模块。

### 11.1 三层递进审批

对通用工具类端点（exec_python / exec_cmd / exec_terminal_spawn / exec_apply_patch），审批系统会先跑：

#### Layer 1：静态规则扫描

拦截 `subprocess` / `shutil.rmtree` / `os.remove` 等明确危险 API。

#### Layer 2：LLM 预审

- default 模型层，三态：APPROVE / DENY / MANUAL
- **APPROVE** → 自动放行（agent 无感知，不弹窗）
- **DENY / MANUAL / 不可用** → 回退人审
- LLM DENY 后下一次审批强制走人审（冷却期），人审通过后恢复 LLM 审查
- 审计日志持久化到 `data/approvals.jsonl`
- 配置项 `command_guard.llm_review_*` 可调（默认开启）

#### Layer 3：人审弹窗

- 平台有原生询问工具时优先使用
- 否则调用 `command_guard_request_approval`，由 PySide6 弹窗收集批准或拒绝
- 用户决定后调用 `command_guard_record_decision`，传入 approval_id、decision=approve|deny 和用户的补充理由
- 批准只签发与原命令、shell、cwd 绑定的短时一次性 approval_token
- 重试原执行工具时原样传入该 token；不得用于其他命令

### 11.2 设计原则

- 命令拦截**不是为了阻碍 Agent 工作**，而是为了应对模型幻觉、命令拼接错误和工具执行缺陷
- **先询问用户通常没有害处**
- 被拦截后禁止尝试通过改写命令、拆分执行、编码、写入脚本、切换 shell、调用 Python 文件 API 或其他工具绕过
- 询问用户时必须说明：准备执行的完整命令、执行原因、可能影响、目标路径或资源，以及可用的更安全替代方案

### 11.3 配置项

`command_guard.llm_review_*` 配置项（在 `config.toml`）：
- `llm_review_enabled`（默认 true）：是否启用 LLM 预审
- `llm_review_model`：用哪个 LLM 模型预审
- `llm_review_cooldown_minutes`：DENY 后冷却期时长

## 12. 实施清单

新项目落地这些改进精华时按此清单逐项确认：

- [ ] 设计三层 MCP 架构（DIRECT_TOOLS + 网关 + EXCLUDE）
- [ ] 配置 Tool Annotations（4 个 hint × 全工具）
- [ ] 实现 50KB 响应大小保护（网关层 size guard）
- [ ] 截图/文件类操作禁止返回 base64（用 inline / 文本分析）
- [ ] 实现磁盘清理强制规则（先清单审核再执行）
- [ ] 实现三层递进审批（静态规则 + LLM 预审 + 人审）
- [ ] 实现用户补充指令注入（`/user/message` 端点 + 中间件）
- [ ] 实现终端会话 API（长时间运行命令管理）
- [ ] 输出截断规则（8000 字符上限 + exec_id 查完整）
- [ ] Skill 编写规范（500 行软上限 + pushy description + evals）
- [ ] anti-AI-slop 检查清单（7 反模式 + 用户偏好）
- [ ] Reconnaissance-Then-Action 模式（动态页面 networkidle 必等）

## 13. 参考资源

- **LocalAgent 项目主入口**：`<project_root>\AGENTS.md`
- **功能变更检查清单**：`<project_root>\.trae\rules\project_rules.md`
- **MCP 白名单 + Tool Annotations**：`<project_root>\server\mcp_whitelist.py`
- **MCP 网关实现**：`<project_root>\server\core\mcp_gateway.py`
- **强化版 skill-creator**：`<project_root>\.agents\skills\skill-creator.md`
- **强化版 html-dev-debug**：`<project_root>\.agents\skills\html-dev-debug.md`
- **MCP 三层架构文档**：`<project_root>\docs\mcp-reference.md`
- **三层记忆系统**：`<project_root>\docs\memory-system.md`
- **LLM 并发池架构**：`<project_root>\docs\llm-pool.md`
- **环境兼容性约束**：`<project_root>\docs\environment-constraints.md`
- **工具脚本指南**：`<project_root>\docs\tools-guide.md`
- **本工具包其他文件**：
  - [README.md](file:///<project_root>/workspace/dev_toolkit/README.md)
  - [html_toolkit.md](file:///<project_root>/workspace/dev_toolkit/html_toolkit.md)
  - [mcp_builder.md](file:///<project_root>/workspace/dev_toolkit/mcp_builder.md)
  - [webapp_testing.md](file:///<project_root>/workspace/dev_toolkit/webapp_testing.md)
  - [skill_creator.md](file:///<project_root>/workspace/dev_toolkit/skill_creator.md)
  - [evals_template.json](file:///<project_root>/workspace/dev_toolkit/evals_template.json)
