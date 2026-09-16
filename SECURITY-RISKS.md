# SECURITY-RISKS.md

> LocalAgent 项目已知安全风险与查证结论。
> 本文件由 `temp/sdd/chat-engine-safety-fixes/` SDD 流程产出，对应 spec D18/D19/D25 决策。
>
> 风险等级：
> - **高危**：攻击者接触本机即可利用，建议尽快修复
> - **中危**：需特定条件利用，建议排期修复
> - **低危**：理论风险或仅影响可用性，可暂缓
> - **非漏洞**：查证后确认安全，仅记录测试/文档缺口

---

## 1. 后端无认证即可连接（高危，D18）

### 描述

LocalAgent 后端 FastAPI 监听 `127.0.0.1:8766`，**无任何认证**。任何能访问本机的进程都可调用全部 API（含 exec_python / exec_cmd / 文件读写 / 屏幕操控 / 关机等高危操作）。

### 利用条件

- 攻击者能在本机执行任意代码（如恶意软件、被入侵的浏览器扩展利用 SSRF）
- 或本机多用户环境中其他用户能访问 127.0.0.1:8766

### 影响

- 完全系统沦陷：执行任意命令、读取任意文件、操控屏幕、关机
- 凭据泄露：可读取 `config.toml` 中的 API keys、`keys.json` 中的 LLM tier keys

### 缓解措施

- 个人单用户使用场景下风险可控
- 不在多用户环境运行后端
- 防止恶意软件运行（标准安全卫生）
- 未来可选：加 token 认证 / Uvicorn socket 权限控制

### 当前处置

**个人用暂不修。** 用户明确决定（D18）。

---

## 2. 凭据明文存储在 EventStore（中危，D18）

### 描述

v6-lite chat 引擎的 EventStore（SQLite）持久化所有对话消息，包括用户消息中可能包含的 API keys、tokens、密码等敏感凭据。这些数据**明文存储**，无加密、无过滤。

### 存储位置

- `data/client/agent.db` 的 `messages` 表
- `data/client/agent.db` 的 `events` 表（streaming events）
- `data/client/agent.db` 的 `tool_calls` 表（tool 调用参数与结果）

### 利用条件

- 攻击者能读取 `data/client/agent.db` 文件
- 或攻击者能调用 `/sessions/{id}/messages` 等 REST 端点

### 影响

- 历史对话中出现的所有凭据泄露
- 工具调用参数中的敏感数据（如 exec_python 代码中的密码变量）泄露

### 缓解措施

- 依靠文件系统权限保护 `data/` 目录（E9 已对 artifact 文件设 0o600，但 agent.db 本身未设）
- 不在对话中粘贴敏感凭据
- 定期清理 `data/client/agent.db`

### 当前处置

**个人用暂不修。** 用户明确决定（D18）。

---

## 3. 路径白名单缺失（中危，D19）

### 描述

`file_edit` / `file_write` 等工具可写任意路径，无工作区白名单约束。Agent 可被诱导修改用户主目录下任意文件（如 `~/.ssh/authorized_keys`、`~/.bashrc`）。

### 利用条件

- Agent 被提示注入攻击（用户让 agent 处理恶意内容，agent 误改关键文件）
- 或 agent 决策错误（误删重要文件）

### 影响

- 关键配置文件被改写
- SSH 公钥被注入（无密码登录后门）
- 用户 shell 启动脚本被改写（持久化恶意代码）

### 缓解措施

- 危险路径写入仍走 command_guard 审批（exec_python 路径）
- 用户监督 agent 行为

### 当前处置

**记入 wip。** 用户决定（D19）后续在 GUI 改进中加"工作区"概念：新建会话时选定工作区，agent 提示词中告知工作区路径。其他路径不强制限制（个人辅助场景需要 agent 到处干活）。

---

## 4. 内部 MCP 调用审批机制（非漏洞，E10 查证）

### 描述

调查问题：MCP 工具调用是否绕过 PermissionPolicy？

### 查证结论

**MCP 内部调用不绕过 PermissionPolicy**，但审批由独立机制强制，而非 HTTP 中间件。

### 机制说明

- `fastapi-mcp` 用 `httpx.ASGITransport` 把 MCP 工具调用转为对 FastAPI app 的内部 HTTP 请求
- `server/core/mcp_gateway.py:51-321` 的 `_patched_execute_api_tool` 包装层在调原始 fastapi-mcp 之前先查 `_operation_safety_map[effective_op]`
- `approval_required` 工具走三层递进审批（静态规则 → LLM → 人审）+ token 验证
- `localagent_advanced_tool` 网关：patch 检查 `arguments.tool`（目标 op）的 safety，避免"一次网关审批解锁所有端点"提权
- handler 内 `command_guard.check_command()` 在 MCP 路径上也生效（与调用来源无关）
- 不存在 MCP-only 工具：所有 44 个 DIRECT_TOOLS 都有 REST 后端

### 发现的非安全缺陷

1. **未知 op 默认放行**（理论缺口，已补回归测试）：`safety_map.get(unknown_op)` 返回 None → 不进 approval 分支。受 DIRECT_TOOLS 硬编码 + advanced_tool registry 守门限制，运行时无法实际利用。2026-08-06 补 `TestUnknownOpDefaultPass` 回归测试（4 测试，`tests/test_mcp.py`），锁定"默认放行 + 多层守门"行为，防止未来重构时误改 `build_operation_safety_map` 漏掉路由导致该路由绕过审批。

> 已修复并移出（2026-08-06）：C1（http_guard.py:6 注释过时）、C3（mcp_gateway.py:145-156 人审 token 丢弃，已被 _approved_cache 治本缓解）—— 详见 CHANGELOG.md [Unreleased]。

### 当前处置

**安全模型有效，无需修复。** C1/C3 已修复（2026-08-06），子项 1 仍为理论缺口，已补回归测试锁定行为契约。

---

## 5. memory 路径校验（非漏洞，E11 查证）

### 描述

调查问题：memory_key 参数是否存在路径遍历漏洞？

### 查证结论

**memory 路径校验对路径遍历攻击是充分的**（三层独立防护层叠加）。

### 三层防护

1. **白名单正则** `_KEY_PATTERN = r'^[a-zA-Z0-9_-]+$'` 在 FastAPI 路由层拒绝所有可疑字符（`server/memory/router.py:19, 392, 408, 444`）
2. **SQLite 参数化查询** 阻断 SQL 注入（`server/memory/store.py:297-311` upsert_fact 等）
3. **架构上 key 从不参与文件路径构造** — 即使前两层都被绕过，也无法触发文件系统遍历

### 攻击向量核查

所有以下向量均被白名单阻断：空字符串 / 路径分隔符 `/` / 反斜杠 `\` / 点段 `.` `..` / Unicode 全角 `．．／` / URL 编码 `%2e%2e%2f` / Null 字节 / 空格 / 特殊字符 / Shell 元字符。

### 存储类型

**SQLite（DB-based，低风险）**，非文件系统。key 仅作 SQLite 列值。

### 发现的非安全缺陷

1. **缺口 C（测试覆盖，已补）**：测试套件原无针对 `_KEY_PATTERN` 的回归测试。2026-08-06 补 `TestKeyPatternValidation` 回归测试（74 测试，`tests/test_memory.py`），覆盖 4 个验证点（GET/POST/DELETE Path 参数 + POST /memory/record body 字段），按 URL 解析行为分类（path 端点能进 pattern 的字符 vs URL/路由提前阻断的字符），防止未来误删 `pattern=` 参数无测试会失败。

> 已修复并移出（2026-08-06）：C4（MemoryRecordRequest.key 加 `pattern=_KEY_PATTERN`）、C5（_KEY_PATTERN 加 `{1,128}` 长度上限）—— 详见 CHANGELOG.md [Unreleased]。

### 当前处置

**无路径遍历漏洞。** 缺口 A/B 已修复（2026-08-06），缺口 C 已补回归测试（74 测试全过）。

---

## 6. 审批 token 重放攻击（非漏洞，E12 查证）

### 描述

调查问题：approval_token 是否可被重放？两套审批系统（http_guard + command_guard）。

### 查证结论

**审批令牌系统不受重放攻击威胁**（三层防御 + 无竞态条件）。

### 三层防御

1. **一次性使用**：`consume_token` 用 `dict.pop(token, None)` 原子取出并删除令牌
   - `http_guard.py:100`：`record = _tokens_http.pop(token, None)`
   - `command_guard.py:111`：`record = _tokens.pop(token, None)`
   - 第二次调用必返回 None → False

2. **指纹绑定**：
   - http_guard: `hash(method.upper() + path + sha256(body)[:16])`（`http_guard.py:33-37`）
   - command_guard: `hash(command + shell + resolved(cwd))`（`command_guard.py:70-72`）
   - 指纹校验在 pop **之后**：窃取令牌用于不同命令时，令牌被销毁而非复用（安全正向设计）

3. **时间过期**：`token_ttl_seconds` 默认 120s（`http_guard.py:128`、`command_guard.py:201`），`_cleanup` 在每次 consume/record 前清理过期条目

### 无竞态条件

- `consume_token` 是同步函数（无 `await`），不会在执行中途让出事件循环
- `dict.pop()` 在 GIL 下原子
- 单进程部署（`uvicorn.run(app, ...)` 单进程），无多进程令牌 store 副本

### 存储方式

纯内存 dict，无 DB 持久化。服务重启即吊销所有未消费令牌（正向）。

### 发现的非安全缺陷

> 已修复并移出（2026-08-06）：C7（test_http_guard.py 核心逻辑 40 测试）、C8（并发/跨端点/过期令牌重放 11 测试）—— 详见 CHANGELOG.md [Unreleased]。
> 已迁出（2026-08-06）：原"非漏洞但记录"子项 3（`_approved_cache` 设计）和子项 4（令牌不绑定 user/session + HTTP 明文）属设计选择，非安全缺陷，已迁到 `docs/dev-workflow.md`「审批系统设计选择记录」小节归档。

### 当前处置

**无重放攻击漏洞。** 缺口 A/B 已修复（2026-08-06，51 测试全过），原"非漏洞但记录"子项 3/4 已迁到 `docs/dev-workflow.md` 归档（非 SECURITY-RISKS.md 范畴）。

---

## 7. lib/secret 密钥明文存储 at rest（中危，lib/secret 改造）

### 描述

`data/secret/secrets.toml` 和 `data/llm/keys.json` 均为明文存储，依赖 `.gitignore` 兜底。lib/secret 作为统一读取入口，但本轮不实现加密层。

### 影响

本地文件系统被访问时（恶意软件、误共享目录、备份泄露），密钥直接暴露。

### 缓解措施

- `.gitignore` 排除 `data/`、`data/secret/`、`data/llm/`
- lib/secret 作为统一读取入口，便于未来扩展加密层
- 文件系统权限依赖 Windows 用户隔离

### 当前处置

**个人用暂不修。** 本轮 lib/secret 改造决策（不加密 at rest）。未来可扩展 lib/secret v2 增加 encrypt/decrypt 层，用 Windows Credential Manager 或 keyring 库。

---

## 8. config.toml 明文 token（中危，lib/secret 改造，迁移后消除）

### 描述

`tushare_token` / `github_token` 原在 config.toml 明文存储。

### 影响

config.toml 被访问时 token 暴露。

### 缓解措施

本轮迁移到 `data/secret/secrets.toml`（仍明文，但集中管理）。

### 当前处置

**迁移后消除。** 迁移脚本 `tools/migrate_secrets.py` 负责搬迁。

---

## 9. chrome_debug/ 浏览器凭据（中危，无法消除）

### 描述

浏览器保存的网站登录凭据、cookies、表单数据在 `chrome_debug/` 目录。

### 影响

目录被访问时凭据暴露。

### 缓解措施

- `.gitignore` 排除 `chrome_debug/`
- 独立用户数据目录（与工作浏览器隔离）
- Chrome 自身的 SQLite 加密（DPAPI 保护）

### 当前处置

**无法消除。** 浏览器凭据是 SQLite 数据库，Chrome 用 DPAPI 加密，无法被 lib/secret 读取。依赖文件系统权限和 Chrome 自身加密。

---

## 10. 发布包脱敏依赖 path_mapping 规则完整性（低危）

### 描述

release engine 的 path_mapping 规则遗漏会导致个人路径泄露到发布包。

### 影响

发布包接收者可看到真实本地路径（如 `C:\<user_home>\`）。

### 缓解措施

- 每轮发布前跑 `uv run python -m tools.release.cli scan --profile friend-full` 验证
- path_mapping 覆盖 4 种字面形式（单反斜杠/双反斜杠/正斜杠/转义）× 大小写变体
- build_release 是扁平拷贝，不含 `.git` 历史

### 当前处置

**低危，每轮验证。** 规则已成熟。

---

## 11. 二级敏感内容（逻辑泄露）（中危）

### 描述

代码/文档中非明文但逻辑上可识别身份的内容：
- 硬编码的个人邮箱/用户名/机器名
- 文档中提到的私人项目结构、具体工作流描述
- 注释中的个人习惯、内部约定
- skill 文件中描述的私人场景（如"每天 9 点分析持仓"暴露交易习惯）
- 测试数据中的伪个人数据
- 错误信息/日志格式中可能暴露的系统配置

### 影响

发布包接收者可推断项目所有者身份和习惯。

### 缓解措施

- 本轮 LLM 语义审查（subagent 并行扫描所有 git-tracked 文件）
- release engine scan 的 `private-repository-reference` 规则覆盖部分场景

### 当前处置

**中危，本轮审查。** 无法完全消除，依赖审查覆盖度。

---

## 12. git 历史可能含历史密钥（中危）

### 描述

历史 commit 中可能提交过密钥（gitignore 后加的）。

### 影响

clone 完整 git 历史可获取历史密钥。

### 缓解措施

- 发布包扁平拷贝不含 `.git` 历史
- 定期轮换密钥（特别是 GitHub PAT）
- git secrets / pre-commit hook（未实施，未来改进）

### 当前处置

**中危，密钥轮换。** 历史密钥需轮换消除。

---

## 风险登记时间线

| 日期 | 风险项 | 处置 |
|------|--------|------|
| 2026-08-06 | 后端无认证（D18） | 个人用暂不修 |
| 2026-08-06 | 凭据明文存储（D18） | 个人用暂不修 |
| 2026-08-06 | 路径白名单缺失（D19） | 记入 wip，GUI 加工作区概念 |
| 2026-08-06 | E10 MCP 审批 | 查证安全（C1/C3 已修复 2026-08-06；子项 1"未知 op 默认放行"理论缺口，已补 `TestUnknownOpDefaultPass` 回归测试 4 测试） |
| 2026-08-06 | E11 memory 路径校验 | 查证安全（C4/C5 已修复 2026-08-06；缺口 C 已补 `TestKeyPatternValidation` 回归测试 74 测试全过） |
| 2026-08-06 | E12 token 重放 | 查证安全（C7/C8 已修复 2026-08-06，51 测试；子项 3/4 设计记录已迁到 `docs/dev-workflow.md`） |
| 2026-08-06 | lib/secret 密钥明文 at rest | 个人用暂不修（lib/secret 改造决策，未来 v2 加密） |
| 2026-08-06 | config.toml 明文 token | 迁移后消除（迁到 secrets.toml） |
| 2026-08-06 | chrome_debug 浏览器凭据 | 无法消除（SQLite + DPAPI，依赖 gitignore） |
| 2026-08-06 | path_mapping 完整性 | 低危，每轮验证 |
| 2026-08-06 | 二级敏感内容（逻辑泄露） | 中危，本轮 LLM 语义审查 |
| 2026-08-06 | git 历史密钥 | 中危，密钥轮换 |
| 2026-08-07 | route_tags fail-open | 低危，设计选择（安全靠 safety+approval） |
| 2026-08-07 | server 侧 OCR 路径遍历 | 中危，工具通用性优先不改 |
| 2026-08-18 | 密钥备份明文存储（第 15 项） | 中危，设计选择（个人用暂不处理；明文副本 + 双位置 + 保留策略 + 仅手动恢复） |
| 2026-08-18 | workspace 个人数据明文备份（第 16 项） | 中危，设计选择（与密钥备份同频次 + 独立配置目录和保留策略；ADR-0029） |
| 2026-09-03 | 入站请求/响应正文明文落库（第 17 项） | 中危，本机开启 + 默认关 + 窗口上限（50 条/500MB/单条 100MB）+ 无读取端点 |
| 2026-09-13 | apply_patch 不进三层审批静态规则（第 18 项） | 中危，用户决策记录暂不修（code review 1-2） |
| 2026-09-13 | 内置工具不经审批链（第 19 项） | 中危，用户决策记录暂不修（code review 8-10） |
| 2026-09-13 | apikey unmasked 明文无鉴权（第 20 项） | 低危，风险实质由第 1 项覆盖（code review 5-15） |

---

## 13. route_tags fail-open 默认暴露（低危，设计选择）

### 描述

`server/route_tags.py` 的 `_is_agent_callable` 函数采用 fail-open 策略：新端点默认 `x-agent-callable=True`，只有显式加入 `GATEWAY_EXCLUDE` 的端点才不被标记为 agent 可调。

### 影响

新端点自动暴露给 agent。如果新端点有危险操作但未配置 `classify_safety` + `approval_level`，agent 可直接调用。

### 缓解措施

- 安全防护靠 `classify_safety` → `approval_level` → HTTP 中间件拦截，不靠 `x-agent-callable` 标志
- 危险端点（如 auto_shutdown）应在 `classify_safety` 中标为 `danger` + `approval_required`
- `x-agent-callable` 当前仅写入 OpenAPI 元数据，运行时消费方为 ToolRegistry

### 当前处置

**设计选择，不改。** 加 MCP 端点的目的就是给 agent 用，fail-open 是有意设计。docstring 已修正（T09）。

---

## 14. server 侧 OCR/VL/docviewer/mindforge 路径端点无白名单（中危，工具通用性优先）

### 描述

`server/ocr.py` 的 `vl_file_json`/`ocr_file_json`、`server/mindforge*.py` 的路径端点接受任意文件路径，仅检查 `p.exists()`，无工作区白名单。与 `client/core/agent/builtin_tools/base.py` 的 `is_path_safe`（4 重校验 + PROJECT_ROOT 白名单）形成防护不一致。

### 影响

agent 通过 `localagent_advanced_tool` 网关可调这些端点，传 `C:\<user_home>\.ssh\id_rsa` 等敏感路径。虽然 `Image.open` 限制只能读图片格式，但攻击者可通过截图/PDF 间接泄露敏感信息。`docviewer_read`/`mindforge_convert` 可能接受更多文档格式，风险更高。

### 缓解措施

- `vl_file_json`/`ocr_file_json` 用 `Image.open(p)` → 只能读图片格式，文本文件会报错
- 端点在 `GATEWAY_EXCLUDE` 中的情况需确认（fail-open 默认暴露）
- `classify_safety` + `approval_level` 提供一级防护

### 当前处置

**不改，工具通用性优先。** OCR 工具需读任意路径的图片（桌面截图、D 盘 PDF 等），加白名单会限制工具通用性。记入风险文档。

---

## 15. 密钥备份明文存储（中危，备份方案设计选择）

### 描述

`tools/backup_secrets.py` 把 `data/llm/keys.json`、`data/secret/secrets.toml`、`config.toml` 三个明文敏感文件**明文副本**备份到两个位置：

- 项目内 `backups/secrets/`（已在 .gitignore 排除，不进 git 历史，不进 release 包）
- 项目外目录（绝对路径，配置在 config.toml `[secret_backup].external_dir`；建议不同物理盘防单盘故障）

备份文件名格式：`<basename>_<YYYYMMDD>_<HHMMSS>.<ext>`，保留策略「最近 N 个版本 + M 天内所有版本」取并集（默认 N=10, M=20）。

### 触发场景

2026-08-18：agent 工作时误删 `keys.json`，因 .gitignore 排除且无备份机制难以恢复。用户决策加本地备份方案：双重位置 + 5 天定时 + 手动触发 + 明文 + 仅手动恢复。

### 影响

- 备份目录被未授权访问（如外部盘被挂载到其他系统、共享目录误开）→ 所有密钥直接泄露
- 备份目录的明文副本数量是原文件的 N 倍（保留 10 个版本即 10 份明文密钥），增加泄露面
- 与原文件 at-rest 风险等级相同（详见第 7 项 lib/secret 明文存储），但备份位置脱离了项目根的 .gitignore 兜底

### 缓解措施

- `backups/` 在 .gitignore 排除，不进 git 历史
- `config.toml` 在 .gitignore 排除，外部路径不入 release 包
- `tools/backup_secrets.py` / `tools/restore_secrets.py` 源码不硬编码具体外部路径，通过 `lib.secret.get_*_path()` + `lib.config_reader.load_config()` 读取 `[secret_backup]` 段，避免 release 包泄露本机路径
- 保留策略限制备份副本数量（≤ N 个版本 + M 天）
- 项目外目录建议放在不同物理盘防单盘故障
- 用户对外部备份目录有文件系统级访问控制（Windows 用户隔离）

### 未来可扩展

- 加密层：PBKDF2 + AES-256-GCM（恢复需输密码）
- 机器绑定加密：Windows DPAPI（本机本用户免密解密，换机不可读）
- 写入时 hook：在 `_write_raw_keys_file` 前快照，防 mid-write 损坏

### 当前处置

**个人用暂不处理。** 用户明确决策（2026-08-18）：明文副本 + 记入风险文档。设计为后续可平滑升级加密层（备份文件名格式不变，仅内容从明文改为密文）。

---

## 16. workspace 个人数据明文备份（中危，备份方案设计选择）

### 描述

`tools/backup_workspace.py` 按 `workspace/<component>/manifest.toml [backup]` 段声明的路径，把各组件的关键个人数据（股票 watchlist / 持仓 db / 账单 / 配置等）**明文副本**备份到两个位置：

- 项目内 `backups/workspace/<component>/`（已在 .gitignore 排除，不进 git 历史，不进 release 包）
- 项目外目录（绝对路径，配置在 config.toml `[workspace_backup].external_dir`；建议不同物理盘防单盘故障）

文件路径 → 原子拷贝；目录路径（以 "/" 结尾或 is_dir）→ 递归打 zip 后原子写。备份文件名格式 `<basename>_<YYYYMMDD>_<HHMMSS>.<ext|zip>`，保留策略「最近 N 个版本 ∪ M 天内所有版本」（默认 N=7, M=14，比密钥更激进）。

### 触发场景

2026-08-18：用户反馈"workspace 还有很多关键数据未被跟踪（如股票的个人数据），实际上非常值得备份"，希望"manifest.toml 里加一条说明本 work 下哪些是需要备份的文件，然后 loop 任务一起处理好"。决策：与 secret_backup 对称设计但独立配置目录和保留策略（ADR-0029）。

### 影响

- 备份目录被未授权访问 → 所有个人数据直接泄露（持仓信息、账单、配置等）
- workspace 备份多是大文件（duckdb 几十 MB），泄露面比密钥更大（含行为数据）
- 备份目标由各组件 manifest 声明，组件作者加 `[backup]` 段时需自行评估敏感性
- 与第 15 项密钥备份同类风险，但密钥脱密后影响更严重（财务账户被盗用 vs 交易行为被窥探）

### 缓解措施

- `backups/` 在 .gitignore 排除，不进 git 历史
- `config.toml` 在 .gitignore 排除，外部路径不入 release 包
- `tools/backup_workspace.py` 源码不硬编码外部路径，通过 `lib.config_reader.load_config()` 读取 `[workspace_backup]` 段，避免 release 包泄露本机路径
- zip 内 arcname 用相对路径不暴露本机绝对路径
- 保留策略限制备份副本数量（默认 ≤ 7 个版本 + 14 天）
- 项目外目录建议放在不同物理盘防单盘故障
- 用户对外部备份目录有文件系统级访问控制（Windows 用户隔离）
- 备份目标声明权在组件作者（manifest [backup] 段），不强行扫描全部组件数据

### 未来可扩展

- 加密层：PBKDF2 + AES-256-GCM（与第 15 项共享同一加密层，备份文件名格式不变）
- 机器绑定加密：Windows DPAPI
- 选择性加密：只对 `[backup].sensitive = true` 的组件加密（与 backup 段可加敏感标志字段配套）

### 当前处置

**个人用暂不处理。** 用户明确决策（2026-08-18）：与密钥备份同频次（5 天）+ 独立配置 + 明文副本 + 记入风险文档。设计为后续可平滑升级加密层（备份文件名格式不变，仅内容从明文改为密文）。

---

## 17. 入站请求/响应正文明文落库（中危，可开关 detail logging）

### 描述

入站网关新增可开关的请求/响应正文明细留存：当 `[inbound.detail_logging].enabled = true` 时，所有进入转发（pool.call/stream）的 `/v1/chat/completions` 调用，其**请求 JSON 原文**与**响应正文**（非流式 content / 流式聚合正文含 thinking）明文存到 `data/inbound_calls.db` 的 `inbound_call_details` 表。正文可能含用户提示、业务数据、系统提示片段等敏感内容，**无加密、无自动脱敏**。

### 存储位置

- `data/inbound_calls.db` 的 `inbound_call_details` 表（`request_body` / `response_body`）
- 独立快照表，自带定位字段（key/model/status/error），生命周期与统计主表 `inbound_calls` **完全解耦**（不联动删除）
- 窗口上限：整体 ≤ 50 条 / ≤ 500 MB（超出丢最老让位）；单条 ≤ 100 MB（超限写入前截断，置 `truncated` 标记）

### 利用条件

- 攻击者能读取 `data/inbound_calls.db`（该库与主表同目录，未设文件级 0o600 权限）
- 或本机多用户环境其他用户能读 `data/` 目录
- 开关默认关；仅本机（config.toml）显式开启

### 影响

- 明文泄露最近转发流量的请求/响应全文（提示词、业务内容、潜在系统提示片段）
- 单库最多约 500 MB 明文正文（窗口自收敛）

### 缓解措施

- 开关默认 `false`（config.example.toml 文档 + 代码内置默认），仅本机 config.toml 置 true
- 明细**不暴露给任何 REST/MCP 读取端点**；需要时自写脚本直连库 SELECT
- 明细窗口自动让位清最老，总量被 500 MB / 50 条封顶
- `data/` 依赖文件系统权限与 .gitignore 兜底（不进 git / release 包）
- 自读脚本若需传播，脱敏由脚本侧自行处理（本项目未引入自动脱敏层）

### 当前处置

**个人用调试需要开启**（2026-09-03 本机 config.toml 置 true）。中危，接受并记录。开关由 `[inbound.detail_logging]` 控制，无需时置 false 即关闭。


---

## 18. exec_apply_patch 的 patch 内容不进三层审批静态规则与缓存（中危，code review 1-2）

### 描述

三层递进审批（`server/approval_review.py`）的静态危险 API 扫描、人审批准缓存 key、审查 prompt 主字段均只取 `code`/`cmd`/`command` 三个键；`exec_apply_patch` 的载荷在 `patch` 键——静态规则对 patch 内容永远扫描空串（危险删除类 patch 不可见）、patch 永不命中审批缓存（LLM deny 后每次重复人审）、大 patch 走 prompt 的 `other` 区块不截断。

### 影响

- 破坏性 patch（删库/删目录）不被 Layer1 静态规则拦截，全靠 Layer2 LLM 审查与 Layer3 人审兜底
- apply_patch 每次被 LLM 拒后都要重复人审（无缓存复用）

### 缓解措施

- Layer2 LLM 审查仍能看到 patch 内容（untrusted_data 区块），deny/manual 会转人审
- 一次性 token + 请求指纹绑定、dcg 二进制命令拦截在子进程层仍然生效

### 当前处置

**用户决策（2026-09-13）：记录暂不修。** 权衡：纳入静态扫描会让 apply_patch 人审频率明显上升（patch 几乎总含"写文件"形态）。未来若纳入，需同步扩展 `_compute_approval_cache_key` / `_static_review` / `_build_review_prompt` 三处的键集合。

---

## 19. 内置工具（builtin_executor）不经审批链（中危，code review 8-10）

### 描述

v6-lite runner 对 `builtin_executor.has_tool(name)` 命中的工具**直接本地执行**（runner.py 约 L1048-1052），不经过 HTTP 中间件/MCP 网关的 `x-tool-safety=approval_required` 审批链——该标注对内置工具无强制执行力。

### 利用条件

模型被 prompt 注入或幻觉驱使，选择用内置工具执行危险本地操作。

### 影响

绕过三层审批的内置工具可做本地危险操作；但内置工具集合有限，shell 类危险操作实际走 exec_*（有审批 + dcg 拦截）。

### 缓解措施

- 危险 shell/代码执行不在内置工具集（走 exec_python/exec_cmd，有三层审批 + dcg）
- 工具结果回灌与 DoomLoopDetector 提供行为层兜底

### 当前处置

**用户决策（2026-09-13）：记录暂不修。** 未来可对 `destructiveHint` 类内置工具在 runner 层强制走 facade 审批回调。

---

## 20. apikey 端点支持 unmasked=true 返回明文 key 且无独立鉴权（低危，code review 5-15）

### 描述

`/keys`、`/keys/{key_id}` 端点支持 `unmasked=true` 查询参数返回**明文** key，无 token/审批校验。后端本身无认证（见第 1 项），任何能访问 127.0.0.1:8766 的本机进程都可读取全部 key 明文。

### 影响

与第 1 项"后端无认证"攻击前提完全相同，属其子集——能打到本端点的进程本来就能调 exec_python 读 `keys.json` 原文件。

### 缓解措施

- 仅监听 127.0.0.1
- GUI 密钥面板的"显示明文"功能依赖此参数

### 当前处置

**用户确认记入风险文档（2026-09-13），不独立加固。** 风险实质被第 1 项覆盖；若未来后端引入认证，本端点应纳入同一鉴权体系。
