# mcp_builder - MCP 服务构建

> 整合自 anthropics/skills 的 mcp-builder + LocalAgent 项目三层 MCP 架构实战经验。
> 适用于新项目要构建 MCP 服务让 LLM 调用工具的场景。

## 1. 概述

**MCP（Model Context Protocol）** 是 Anthropic 提出的开放协议，让 LLM 能标准化地调用外部工具、资源、提示词。本质是给 LLM 装一个"USB 接口"，让任何 MCP 兼容的客户端（Claude Desktop / Trae / CatPaw / 自研 Agent）都能调用你的服务。

**MCP 服务质量衡量标准**（来自 anthropic mcp-builder）：
> 一个 MCP 服务的好坏，看它能让 LLM 多好地完成真实任务。

**适用场景**：
- 你有一组 API/工具想让 LLM 调用
- 你想让多个 LLM 客户端共用同一套工具
- 你想给 LLM 提供结构化数据访问（数据库 / 文件 / API）

**不适用场景**：
- 一次性脚本（直接让 LLM 写 Python 跑就行）
- 没有 LLM 客户端消费的纯后端服务（用普通 REST 就行）

## 2. MCP 协议核心概念

### 2.1 三类暴露物

| 类型 | 说明 | 例子 |
|------|------|------|
| **Tools** | LLM 可调用的函数（最常用） | `get_weather(city)` / `send_email(to, subject)` |
| **Resources** | LLM 可读取的数据源（只读） | `file:///path/to/log` / `db://users` |
| **Prompts** | 预定义的提示词模板 | `prompt://code-review?lang=python` |

新项目 90% 精力放在 **Tools** 上，Resources 和 Prompts 是补充。

### 2.2 两种传输方式

| 传输 | 适用 | 实现 |
|------|------|------|
| **stdio** | 本地服务（同机进程） | LLM 客户端启动子进程，stdin/stdout 通信 |
| **Streamable HTTP** | 远程服务（可水平扩展） | HTTP POST + SSE，支持鉴权 |

**选择原则**（来自 anthropic mcp-builder）：
- 本地工具 / 命令行 → stdio
- 远程 / 多用户 / 需鉴权 → Streamable HTTP（用 stateless JSON，不用 stateful session）
- 不确定 → Streamable HTTP，更通用

### 2.3 协议规范参考

- 官方文档：`https://modelcontextprotocol.io/`
- 协议规范：`https://modelcontextprotocol.io/specification`
- 从 sitemap 入手：`https://modelcontextprotocol.io/sitemap.xml`
- 抓具体页面用 `.md` 后缀：`https://modelcontextprotocol.io/specification/draft.md`

## 3. Tool Annotations 规范

来自 MCP 协议规范 + LocalAgent 项目落地实践。

### 3.1 四个 hint

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

### 3.2 与 safety 分类的区别

| 维度 | Tool Annotations | safety 分类 |
|------|-----------------|-------------|
| 性质 | 软提示（语义标记） | 硬约束（是否需要审批） |
| 字段 | 4 个 hint | `read_only` / `safe` / `approval_required` |
| 用途 | agent 选择工具参考 | 是否拦截执行 |
| 例子 | `readOnlyHint=true` 是只读 | `read_only` 不需要审批 |

**两者互补**：一个工具可以 `readOnlyHint=true`（语义只读）但 `approval_required=true`（因为读敏感数据要审批）。

### 3.3 LocalAgent 全工具映射（参考）

来自 `f:\<project_root>\server\mcp_whitelist.py`：

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

## 4. 用 Python + fastapi-mcp 构建

来自 LocalAgent 项目实践（`f:\<project_root>\server\core\mcp_gateway.py`）。

### 4.1 项目结构

```
my_mcp_server/
├── main.py              # FastAPI 入口
├── mcp_gateway.py       # MCP 网关配置
├── mcp_whitelist.py     # 工具白名单 + 排除清单 + Tool Annotations
├── route_tags.py        # safety 分类（read_only/safe/approval_required）
├── tools/
│   ├── user_tools.py    # 业务工具实现
│   └── ...
└── pyproject.toml       # 依赖：fastapi + fastapi-mcp + uvicorn
```

### 4.2 最小服务示例

```python
# main.py
from fastapi import FastAPI
from fastapi_mcp import FastApiMCP

app = FastAPI()

@app.get("/hello", operation_id="hello", tags=["test"])
async def hello(name: str = "world"):
    """打招呼。

    Args:
        name: 名字
    """
    return {"message": f"Hello, {name}!"}

# 挂载 MCP
mcp = FastApiMCP(app)
mcp.mount_http()
```

启动：`uvicorn main:app --port 8000`

### 4.3 用 Pydantic 定义工具 schema

```python
from pydantic import BaseModel, Field

class SendEmailRequest(BaseModel):
    to: str = Field(..., description="收件人邮箱")
    subject: str = Field(..., min_length=1, max_length=100, description="主题")
    body: str = Field(..., description="正文")
    cc: list[str] | None = Field(None, description="抄送")

@app.post("/email/send", operation_id="send_email")
async def send_email(req: SendEmailRequest):
    """发送邮件。"""
    # 业务逻辑
    return {"sent": True, "to": req.to}
```

fastapi-mcp 自动从 Pydantic 模型生成 JSON Schema，传给 LLM。

### 4.4 operation_id 是 LLM 看到的工具名

- `operation_id="send_email"` → LLM 看到工具 `send_email`
- 不写 operation_id，FastAPI 默认用函数名（如 `send_email_send_email_post`），对 LLM 不友好
- **规则**：每个 endpoint 必须显式写 `operation_id`

## 5. 用 TypeScript + @modelcontextprotocol/sdk 构建

来自 anthropic mcp-builder 推荐路径。

### 5.1 项目结构

```
my-mcp-server/
├── src/
│   └── index.ts         # 服务入口
├── package.json         # 依赖：@modelcontextprotocol/sdk + zod
└── tsconfig.json
```

### 5.2 最小服务示例

```typescript
// src/index.ts
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

const server = new Server(
  { name: "my-mcp-server", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// 定义工具 schema
const SendEmailSchema = z.object({
  to: z.string().email().describe("收件人邮箱"),
  subject: z.string().min(1).max(100).describe("主题"),
  body: z.string().describe("正文"),
});

// 列出工具
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "send_email",
      description: "发送邮件",
      inputSchema: {
        type: "object",
        properties: {
          to: { type: "string", format: "email" },
          subject: { type: "string" },
          body: { type: "string" },
        },
        required: ["to", "subject", "body"],
      },
      // Tool Annotations
      annotations: {
        openWorldHint: true,  // 与外部邮件服务器交互
      },
    },
  ],
}));

// 调用工具
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name === "send_email") {
    const args = SendEmailSchema.parse(request.params.arguments);
    // 业务逻辑
    return {
      content: [{ type: "text", text: `已发送邮件到 ${args.to}` }],
    };
  }
  throw new Error(`未知工具: ${request.params.name}`);
});

// 启动 stdio 传输
const transport = new StdioServerTransport();
await server.connect(transport);
```

### 5.3 Streamable HTTP 传输

```typescript
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";

const app = express();

app.post("/mcp", async (req, res) => {
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  await server.connect(transport);
  await transport.handleRequest(req, res);
});

app.listen(3000);
```

### 5.4 Python vs TypeScript

| 维度 | Python (fastapi-mcp) | TypeScript (SDK) |
|------|----------------------|------------------|
| 学习曲线 | 低（FastAPI 上手快） | 中（要懂 MCP 协议） |
| 类型安全 | Pydantic 运行时校验 | Zod 编译时 + 运行时 |
| 性能 | 中 | 高 |
| 部署 | uvicorn / gunicorn | node / bun |
| 社区 | 中（增长中） | 大（官方主推） |
| 推荐 | 已有 FastAPI 项目 / 数据科学场景 | 新项目 / 高性能场景 |

**anthropic mcp-builder 推荐 TypeScript**（SDK 支持好 + 模型生成 TS 代码质量高 + MCPB 兼容性）。

## 6. 工具命名规范

### 6.1 命名风格

- **verb_noun**（动词_名词）：`send_email` / `get_user` / `list_files`
- **kebab-case**：`send-email` / `get-user`（部分客户端偏好）
- **operation_id**：FastAPI 中通过 `operation_id=` 参数指定，是 LLM 看到的工具名

### 6.2 一致前缀

```python
# ❌ 不一致
"send_email", "fetch_user", "get_files", "delete_record"

# ✅ 一致前缀（GitHub 风格）
"github_create_issue", "github_list_repos", "github_get_user"
```

### 6.3 命名反模式

- **过长**：`send_email_to_external_recipient` → `send_email`
- **过短**：`go` / `do` → 无语义
- **模糊**：`process` / `handle` / `manage` → 不知道做什么
- **缩写**：`get_usr` → `get_user`（除非已是行业标准如 `id` / `url`）

## 7. 工具粒度决策（三层架构）

来自 LocalAgent 项目实践。

### 7.1 三层暴露策略

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

### 7.2 决策流程（加端点前必走）

```
新增 REST 端点 → 判断是否要进 MCP
│
├─ agent 日常会用吗？ → DIRECT_TOOLS（第一层）
│  例：memory_get / inbox_list（每次会话可能用）
│
├─ agent 偶尔用、属于通用能力？ → 自动收进网关（第二层）
│  例：todos_get / wip_create（按需调用）
│
└─ 只给 GUI/面板/脚本用？ → GATEWAY_EXCLUDE（第三层）
   例：inbox_batch / user_message（批量管理）
```

### 7.3 为什么要分层（防膨胀）

来自 LocalAgent 实战教训：

| 问题 | 后果 |
|------|------|
| 工具列表 > 50 个 | agent 选择困难、上下文污染 |
| `list_tools` 返回变慢 | 每次会话开始都要等 |
| LLM 工具选择准确率下降 | 太多相似工具导致误选 |
| 维护成本爆炸 | 改一个工具要改 N 处文档 |

### 7.4 LocalAgent 实际数据

来自 `f:\<project_root>\server\mcp_whitelist.py`：
- DIRECT_TOOLS：~35 个直连工具
- 网关自动收纳：~50 个工具（通过 `localagent_advanced_tool` 访问）
- GATEWAY_EXCLUDE：~20 个排除工具（仅 REST 可用）

总 REST 端点 ~105 个，但 MCP 工具列表只暴露 ~37 个（35 直连 + 2 网关），避免膨胀。

### 7.5 工具选择决策树（给 agent 用）

```
遇到"要调后端能力"的需求
│
├─ 1. 直连 MCP 工具（白名单）→ 直接用
│  查白名单：localagent_list_tools()
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

## 8. 测试与调试

### 8.1 MCP Inspector

来自 anthropic mcp-builder：
```bash
# TypeScript
npx @modelcontextprotocol/inspector

# Python
mcp dev server.py
```

Inspector 提供：
- 工具列表可视化
- 单工具调用测试
- 请求/响应原始 JSON 查看
- 错误信息高亮

### 8.2 curl 测试（Streamable HTTP）

```bash
# 列出工具
curl -X POST http://127.0.0.1:8766/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# 调用工具
curl -X POST http://127.0.0.1:8766/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc":"2.0","id":2,"method":"tools/call",
    "params":{"name":"hello","arguments":{"name":"world"}}
  }'
```

### 8.3 evals（量化评估）

来自 anthropic mcp-builder 第 4 阶段：

**目的**：测试 LLM 能否有效使用你的 MCP 服务回答真实的复杂问题。

**生成 10 个 eval 问题**：
1. 列出所有可用工具，理解能力
2. 用只读操作探索可用数据
3. 生成 10 个复杂、真实的问题
4. 自己解答每个问题，验证答案

**eval 要求**：
- 独立（不依赖其他问题）
- 只读（不破坏数据）
- 复杂（需要多次工具调用 + 深度探索）
- 真实（基于真实用例）
- 可验证（单一明确答案，字符串比较即可）
- 稳定（答案不随时间变化）

**输出 XML 格式**：
```xml
<evaluation>
  <qa_pair>
    <question>找到所有未完成的 TODO，按优先级排序</question>
    <answer>high:3,medium:5,low:2</answer>
  </qa_pair>
</evaluation>
```

详见 `workspace/dev_toolkit/evals_template.json` 模板。

## 9. 反模式

### 9.1 工具过多导致选择困难

**反模式**：把所有 REST 端点都暴露为 MCP 工具
- 100+ 工具 → agent 选择准确率下降
- 工具列表响应慢
- 维护成本爆炸

**正确**：用三层架构（DIRECT_TOOLS + 网关 + EXCLUDE）控制暴露数量

### 9.2 响应过大撑爆上下文

**反模式**：`get_all_files()` 返回 10MB JSON
- LLM 上下文被占满
- 后续对话失效
- 上下文窗口爆炸

**正确**：
- 默认分页（`limit` / `offset`）
- 提供精简参数（如 `summary=true`）
- 大返回（截图/文件）用 base64 + `inline` 引用而非 TextContent
- 50KB size guard：超阈值头尾截断 + 强制上报用户

详见 `workspace/dev_toolkit/project_essentials.md` § 8。

### 9.3 审批不严导致破坏性操作

**反模式**：`delete_user` 没有任何审批
- LLM 误调用 → 数据丢失
- LLM 幻觉 → 删错数据
- 命令拼接错误 → 不可逆损失

**正确**：三层递进审批
1. **静态规则扫描**：拦截 `subprocess` / `shutil.rmtree` 等明确危险 API
2. **LLM 预审**（default 模型层，三态 APPROVE/DENY/MANUAL）
   - APPROVE → 自动放行（agent 无感知）
   - DENY/MANUAL → 回退人审
3. **人审弹窗**：用户决定，签发一次性 approval_token

详见 `workspace/dev_toolkit/project_essentials.md` § 9。

### 9.4 工具描述模糊

**反模式**：
```json
{
  "name": "process_data",
  "description": "处理数据"
}
```

**正确**：
```json
{
  "name": "extract_text_from_pdf",
  "description": "从 PDF 文件提取文本内容。当用户需要 OCR PDF / 提取文档文字 / 解析 PDF 内容时使用此工具。支持中英文双语 PDF，保留段落结构。"
}
```

描述包含：
- 做什么（提取文本）
- 何时用（OCR / 提取 / 解析）
- 输入输出（PDF → 文本）
- 边界（支持中英文，保留段落）

### 9.5 错误信息不actionable

**反模式**：
```json
{"error": "失败"}
```

**正确**：
```json
{
  "error": "PDF 文件大小超过 50MB 限制",
  "suggestion": "请先用 split_pdf 工具拆分，或压缩后重试",
  "current_size_mb": 78.3,
  "limit_mb": 50
}
```

错误信息应：
- 明确说什么失败
- 提示具体解决方法
- 给出相关数据
- 不暴露内部细节（如堆栈、SQL）

## 10. 实施清单

新项目构建 MCP 服务时按此清单逐项确认：

- [ ] 选定语言（Python / TypeScript）
- [ ] 选定传输方式（stdio / Streamable HTTP）
- [ ] 设计三层工具暴露策略（DIRECT / 网关 / EXCLUDE）
- [ ] 每个 REST 端点都写 `operation_id`（动词_名词）
- [ ] 每个工具都有清晰的 description（含触发场景）
- [ ] 工具参数用 Pydantic / Zod 定义 schema
- [ ] 工具响应限制大小（< 50KB TextContent）
- [ ] 破坏性工具走审批流程
- [ ] 配置 Tool Annotations（4 个 hint）
- [ ] 写 10 个 eval 问题测试 LLM 可用性
- [ ] 用 MCP Inspector 调试
- [ ] 文档化工具列表 + 调用示例

## 11. 参考资源

- **MCP 官方文档**：`https://modelcontextprotocol.io/`
- **协议规范**：`https://modelcontextprotocol.io/specification`
- **TypeScript SDK**：`https://github.com/modelcontextprotocol/typescript-sdk`
- **Python SDK**：`https://github.com/modelcontextprotocol/python-sdk`
- **fastapi-mcp**：`https://github.com/jlowin/fastapi-mcp`
- **anthropics/skills mcp-builder**：`https://github.com/anthropics/skills/blob/main/skills/mcp-builder/SKILL.md`
- **LocalAgent 项目 MCP 网关**：`f:\<project_root>\server\core\mcp_gateway.py`
- **LocalAgent 项目白名单**：`f:\<project_root>\server\mcp_whitelist.py`
- **LocalAgent 三层架构文档**：`f:\<project_root>\docs\mcp-reference.md`
