# 工程流程（Development Workflow）

从 AGENTS.md 拆出的工程化指南：功能变更检查清单、CHANGELOG 维护、发版流程、MCP 工具使用原则、MCP 接入配置、用户补充指令。AGENTS.md 仅保留每次会话必读的核心 agent 行为规范。

## 功能变更检查清单

见 `.agents/rules/project_rules.md`（Trae 镜像副本在 `.trae/rules/project_rules.md`）。

每次新增功能、修改模块、重构代码后，按清单逐项检查并更新。清单覆盖：代码变更（路由注册/Pydantic 模型/`/health`/错误处理/硬编码/ADR 评估）、待办模块变更、项目结构变更（baseline 同步）、文档更新（`_index.md`/`README.md`/`CHANGELOG.md`/skill 文档/`config.example.toml`/监控面板/`tools_manifest.json`/`AGENTS.md`/`GUIDE_REGISTRY`）、配置变更、Git、测试。

## 功能规划文档存档路径（强制）

> 从 AGENTS.md 拆出的详细路径规范、文件组织、SDD 流程衔接、`planning_notes/` 现状、历史教训。AGENTS.md 仅保留 3 条核心铁律指针。

**任何功能相关文档——构想、计划书、路线图、可行性评估、设计文档、SDD 产物（spec/tickets/checklist）——必须写入 `temp/sdd/<feature-slug>/` 子文件夹。**

**任何 HTML 展示文件**（浏览器预览、可视化报告、调试页面等）必须写入 `temp/html/<feature-name>.html` 或 `temp/html/<feature-name>/`。

**禁止在项目根目录、`planning_notes/`、`docs/`、`workspace/`、`tools/`、`server/`、`client/` 等位置创建 SDD 文档或 HTML 展示文件。**

### 历史教训

- 早期 v6-*.md 系列文件裸放在 `planning_notes/` 根目录，导致文件混杂、管理困难、难以定位
- SDD 流程产物长期堆积在 `planning_notes/` 下（30+ 子目录），反复清理反复出现，污染根目录
- 自 2026-07-30 起：所有 SDD 产物统一迁到 `temp/sdd/`（默认路径）；`planning_notes/` 允许用户手动存放规划/路线图文档（如 public-release、github-app-bot、v6 等），SDD 流程不默认写入

### 路径规范

- **SDD 路径**：`temp/sdd/<feature-slug>/`（如 `temp/sdd/recording-feature/`、`temp/sdd/llm-pool-model-health-and-unify/`）
- **HTML 路径**：`temp/html/<feature-name>.html` 或 `temp/html/<feature-name>/`（多文件时建子目录）
- **命名**：英文小写 + 连字符，与 feature 主题相关
- **历史归档**：`temp/planning_archive/sdd/` 存放 2026-07-30 之前从 `planning_notes/` 迁移的历史 SDD 文档（只读归档，不再修改）

### SDD 文件夹内文件组织

| 文件 | 用途 | 阶段 |
|------|------|------|
| `README.md` | 文档导航 + 总览 | 始终 |
| `00-overview.md` | 第一性原理分析 + 可行性评估 + 模块化分层 | 构想期 |
| `NN-<topic>.md` | 分层/分模块设计文档（如 `01-dependencies.md`、`02-block-design.md`） | 构想期→设计期 |
| `design-decisions.md` | 关键决策记录（讨论中敲定的决策 + 理由） | 构想期→设计期 |
| `grill-me.md` | grill-me skill 产出（追问澄清记录） | 构想期 |
| `spec.md` | SDD 规约 | 设计期 |
| `tickets.md` | 任务拆解（单文件汇总所有 ticket） | 设计期 |
| `checklist.md` | 验收清单 | 设计期 |
| `BLOCKED.md` | 执行阶段卡点记录 | 执行期 |

### 与 SDD 流程的衔接

- 构想期文档（`00-overview.md`、`NN-*.md`、`design-decisions.md`、`grill-me.md`）在 SDD 之前产出，作为 grill-me 的输入
- SDD 产物（`spec.md`、`tickets.md`、`checklist.md`）在 grill-me → to-spec → to-tickets 流程中产出，与构想期文档同处一个文件夹
- **不写 wip 记录**：不调 `wip_create`，文档文件本身是真源，implement skill 直接从路径读取
- **temp/ 不进 git**：`temp/` 在 `.gitignore` 中整体排除，SDD 文档不纳入版本控制。SDD 文档属"工作记忆"，跨会话恢复依赖文件本身存在；如需长期归档，由用户决策迁入 `workspace/sdd_archive/` 或类似位置

### `planning_notes/` 的现状

- 该目录原有 6 个历史中文参考文章文件夹（公众号 .txt 文章归档）
- SDD 流程产物不默认写入此目录（走 `temp/sdd/`）
- 用户手动存放的规划/路线图文档（如 public-release、github-app-bot、v6 等）允许保留在此目录，不视为违规
- task_closure / structure_diff 检测到 `planning_notes/` 下有新内容时不警告（用户手动管理的区域）
- 该目录的 `.gitignore` 规则保留（PDF/ZIP 仍排除）

### 强制禁令（反复踩坑！项目根目录已清理多轮）

- ❌ 禁止在项目根目录创建任何 SDD 文档（spec.md / tickets.md / checklist.md / design-decisions.md / 00-overview.md / NN-*.md / grill-me.md / BLOCKED.md / plan.md / design.md 等）
- ❌ 禁止在项目根目录创建任何 HTML 文件（用于展示/预览/调试的 .html 一律进 `temp/html/`）
- ⚠️ SDD 流程产物不默认写入 `planning_notes/`（走 `temp/sdd/`）；用户手动存放的规划/路线图文档允许
- ❌ 禁止在 `docs/`、`workspace/`、`tools/`、`server/`、`client/` 等位置创建 SDD 文档或 HTML 展示文件
- ✅ SDD 文档一律进 `temp/sdd/<feature-slug>/`
- ✅ HTML 展示文件一律进 `temp/html/<feature-name>.html` 或 `temp/html/<feature-name>/`

## CHANGELOG 维护

每次功能变更、Bug 修复、安全修复后，**必须**在 `CHANGELOG.md` 的 `[Unreleased]` 段对应分类下添加条目。不更新 CHANGELOG 的变更等同于未记录。`[Unreleased]` 段是发版（system.release）的输入：发版流程把它改为版本号归档；打包流程（system.public_distribution）读取它确认有变更内容。

- **分类**：Added（新增功能）/ Changed（行为变更）/ Fixed（Bug 修复）/ Deprecated（即将移除）/ Removed（已移除）/ Security（安全相关）
- **格式**：`- 简述变更（涉及文件路径，为什么改）`

## 提交前机械防线（强制，2026-08-26 逻辑 bug 审查后建立）

任何对 `server/`、`client/`、`lib/` 的代码变更，完成自测后、向用户报告"完成"之前，**必须跑以下两道机械检查**。这是防"上游改了下游没改"合同漂移复发的制度约束——审查结论见 commit `bf0c073`（f1-f10 共 16 组根因）：本项目历史 P0 bug 全部源于字符串弱耦合断链 + 宽泛 except 静默失效，人工 review 挡不住，必须机械兜底。

### 1. 端点 smoke test

```
uv run python -m pytest tests/server_endpoints/test_smoke_endpoints.py -q
```

OpenAPI 驱动遍历全部 GET 端点打真实请求：HTTP ≥500 即失败（路由级断裂直接爆炸）；HTTP 200 + `{"error": ...}` 的静默失效模式以 warnings 逐条上报（运行时清单即真相，修复后可逐步升级为硬断言）。

- 改了路由 / Pydantic 模型 / 服务层后必跑
- 新增端点自动被覆盖，无需手动登记
- 排除清单（screen/browser/ocr/vision/models 前缀，需真实外部资源）在文件头部有注释说明原因，勿随意删

### 2. pyright 增量对比

```
uv run pyright
```

配置见根目录 `pyrightconfig.json`（basic 模式扫 server/client/lib 三目录）。与基线对比规则：

- 当前基线 **0 错误**（2026-08-30 全仓归零；审查起点 459 → 435 → 0）
- **不允许新增错误**；顺手修旧错误欢迎但不强制
- ⚠️ **`include` 只列 server/client/lib**：`workspace/` 与 `tools/` 下的脚本仓库级运行**从不检查**，"全仓 0 errors" 对这些文件是假绿灯。新增/修改这类脚本必须显式传路径：`uv run pyright workspace/<...>/xxx.py`
- 基线数字以本行为准；实时错误清单直接跑 `uv run pyright` 取，不再维护快照文件

### 执行纪律

- 两道全绿才能声称代码任务完成；红了要么修复、要么向用户解释新增原因并获认可
- 注册表类改动（GUIDE_REGISTRY / manifest / 白名单 / config）额外跑 `uv run python tools/audit/drift_detector.py` 对账脚本，看有无新断链（报告写到 `temp/audit/drift_report.md`）

### 3. 硬化规则检查（2026-08-31 新增，"说过三遍的规则"→"会变红的构建"）

```
uv run python tools/check_hard_rules.py            # 全量（pytest 已挂 quick 层：tests/test_hard_rules.py）
uv run python tools/check_hard_rules.py --staged   # staged（pre-commit hook 自动跑，已生效）
```

四项：C1 tracked `.py/.md/.toml/.json` 无 UTF-8 BOM ｜ C2 源码无硬编码密钥字面量 ｜ C3 文档路径越界（.html 只许白名单位置、根目录 .md 锁定）｜ C4 import 边界（client↔server 互不许 import，白名单除外）。规则真源与白名单都在 `tools/check_hard_rules.py` 常量内——**白名单变更必须显式改代码并 commit**（这正是"硬化"的含义），不支持任何配置文件旁路。hook 安装：`uv run python tools/check_hard_rules.py install`（重克隆/换机后需重跑一次）。

配套工具：`tools/gen_feature_map.py` 生成 `data/feature_map.json`（GUI 面板→入口/控件/验证方式地图，改 client GUI 后重跑并回填手写字段）；guide 路由改动必跑 `uv run python tests/guide_eval/run_eval.py` 盲测集（<85% 红，见 `.trae/rules/project_rules.md` "Guide 关键词维护"）。

## agent_guide keywords 编写规范

新增或修改 `GUIDE_REGISTRY` 条目（`server/agent_guide_data.py` 或 `workspace/<module>/loop_actions.py`）时**必读** [docs/agent-guide-keywords.md](agent-guide-keywords.md)。该文档含匹配算法回顾、五大编写原则（精确优于泛 / entry/method 分层 / 避免共享 / 避免泛动词 / 覆盖口语动词）、已知问题清单、6 步标准编写流程。

核心原则速查：
- keyword 长度 ≥ 3 字（除非该词在 skill 上下文唯一）
- entry role 独占用户触发词，method/support role 只放方法论标识词
- 新 keyword 不能与其他 skill 共享（跑共享检查脚本）
- 不用语气助词作 keyword（"试一下"❌ → "试几种方案"✅）
- 覆盖用户口语动词（"爬/抓取/扒" 等同义场）

## 测试隔离（强制规则）

写测试时若需临时替换 `sys.modules` 中的顶层包（如 `workspace`），**禁止**用 `del sys.modules[mod]` 删除所有子模块——会导致后续测试 re-import 产生新类对象，引发 `isinstance` 身份分裂。

### 踩坑案例：VLCandidate 身份分裂

**现象**：`workspace/recorder/tests/test_recorder_consumer_e2e.py` 的 `isinstance(cand, VLCandidate)` 断言失败，即使 `cand` 实际就是 `VLCandidate` 实例。

**根因**：3 个 manifest 测试（`test_agent_guide_manifest.py`、`test_loop_manager_manifest.py`、`test_panel_registry_manifest.py`）在 setup/teardown 用以下代码清理 workspace 模块：

```python
# ❌ 错误做法：删除所有 workspace.* 模块
for mod_name in list(sys.modules.keys()):
    if mod_name.startswith("workspace"):
        del sys.modules[mod_name]
```

后续测试 re-import `workspace.recorder.consumer.types.VLCandidate` 时，Python 重新加载该模块产生**新的类对象**。但 `workspace.recorder.consumer.__init__.py` 顶层 re-export 的 `VLCandidate` 仍是旧引用（`__init__.py` 只在首次 import 时执行，不会因 types 模块重新加载而更新）。结果：`cand.__class__` 是新类，`VLCandidate`（从 `__init__` 导入的）是旧类，`isinstance` 失败。

### 正确做法：snapshot/restore 模式

```python
# ✅ 正确做法：快照-恢复模式
def _snapshot_workspace_modules():
    """快照当前 sys.modules 中的 workspace.* 模块（含 workspace 本身）。"""
    snapshot = {}
    for mod_name in list(sys.modules.keys()):
        if mod_name == "workspace" or mod_name.startswith("workspace."):
            snapshot[mod_name] = sys.modules.pop(mod_name)  # pop 到快照，不删除
    return snapshot

def _restore_workspace_modules(snapshot):
    """恢复快照：先删除测试期间新增的临时模块，再恢复快照。"""
    for mod_name in list(sys.modules.keys()):
        if mod_name == "workspace" or mod_name.startswith("workspace."):
            if mod_name not in snapshot:
                del sys.modules[mod_name]  # 只删除测试新增的临时模块（如 workspace.comp_a）
    sys.modules.update(snapshot)  # 恢复快照中的真实模块
```

**使用方式**：

```python
def test_something(self, tmp_path, monkeypatch):
    # setup：快照真实 workspace.* 模块
    ws_snapshot = _snapshot_workspace_modules()
    # 临时替换 workspace 包，扫描 tmp_path 下的临时组件
    sys.modules["workspace"] = type(sys)("workspace")
    sys.modules["workspace"].__path__ = [str(tmp_path / "workspace")]
    try:
        # 测试逻辑...
        pass
    finally:
        # teardown：恢复真实模块
        _restore_workspace_modules(ws_snapshot)
```

### 核心原则

1. **不要删除真实模块**：测试结束时真实模块必须原样恢复，否则污染后续测试
2. **pop 而非 del**：`sys.modules.pop(mod_name)` 把模块存到快照，`del` 是永久删除
3. **teardown 只删测试新增的**：恢复时只删除测试期间新增的临时模块（如 `workspace.comp_a`），保留快照中的真实模块
4. **适用范围**：任何需要临时替换 `sys.modules` 中顶层包的测试（workspace、server、client 等）

### 相关文件

- `tests/guide_loops/test_agent_guide_manifest.py` - `_snapshot_workspace_modules` / `_restore_workspace_modules` 实现
- `tests/guide_loops/test_loop_manager_manifest.py` - 同上
- `tests/componentization/test_panel_registry_manifest.py` - 同上
- `workspace/recorder/tests/test_recorder_consumer_e2e.py` - 受污染影响的测试（VLCandidate 身份分裂现象）

## 危险操作测试铁律（强制，2026-08-03 教训）

> **触发**：auto_shutdown 重构期间，`test_trigger_minimal_request` 漏传 `dry_run=True`，测试运行时真的调了 `shutdown /s /t 60`，弹窗 60s 倒计时，用户紧急 `shutdown /a` 才中止。这是严重设计失误——单个测试能单方面触发不可逆系统操作。

**核心原则：危险操作测试尽可能不实操，必须实操时要有机制级安全网。**

**危险操作清单（非穷举，按性质归类）**
- 系统级：关机 / 重启 / 注销 / 锁屏 / 休眠（`shutdown` / `Restart-Computer` / `shutdown /l` 等）
- 文件系统：递归删除 / 格式化 / 覆盖系统文件 / 写入系统目录（`rm -rf` / `Remove-Item -Recurse` / `format` / 写 `C:\Windows\`）
- 进程：强杀关键进程 / 启动恶意进程（`taskkill /f /im explorer.exe` / 启动勒索软件）
- 网络：发送恶意请求 / 修改 hosts / 关闭防火墙（`netsh advfirewall set allprofiles state off`）
- 硬件：调整分辨率 / 修改注册表硬件项 / 显卡超频
- 其他绕过审批的操作：调 `subprocess` 直接执行命令（绕过 command_guard）、调 `os.system` / `os.popen`

**写测试时的强制检查清单**

1. **能 mock 就不实操**：危险操作必须 mock，绝不真调
   - 关机/重启 → mock `subprocess.Popen` / `subprocess.run`
   - 文件删除 → mock `os.remove` / `shutil.rmtree`，或用 `tmp_path` 隔离
   - 进程操作 → mock `subprocess.Popen` / `psutil.Process`
2. **autouse 安全网 fixture**：测试模块顶部必须有 autouse fixture，默认 mock 所有危险 API，从机制上兜底
   ```python
   @pytest.fixture(autouse=True)
   def _safety_mock_dangerous_apis(monkeypatch):
       """安全网：默认 mock 所有危险 API，防任何测试意外触发"""
       monkeypatch.setattr("server.xxx.subprocess.Popen", lambda *a, **kw: object())
       monkeypatch.setattr("server.xxx.os.remove", lambda *a, **kw: None)
   ```
3. **显式传安全参数作为双保险**：即便有 autouse 兜底，测试调用时仍显式传 `dry_run=True` / `mock=True` / `safe_mode=True` 等安全参数
4. **参数默认值偏向安全**：API 设计时 `dry_run` / `safe_mode` 默认 `True`，生产场景显式传 `False`；不要让"忘传参数"等于"危险路径"
5. **静态审查清单（每次改测试必跑）**：grep 所有调危险端点的测试代码，逐个确认要么传安全参数要么 mock 了底层 API
6. **PR 自检**：测试代码改动后，问自己"这个测试最坏情况会做什么？"——如果答案是"真关机/真删文件/真杀进程"，停下来加安全网

**反面案例（auto_shutdown 教训）**

```python
# ❌ 错误：dry_run 默认 False，没 mock Popen，没 autouse 安全网
def test_trigger_minimal_request(self, client):
    resp = client.post("/auto_shutdown/trigger", json={"task_id": "minimal"})
    # → 真的调 shutdown /s /t 60，弹窗关机倒计时
```

```python
# ✅ 正确：三层防护
# 1. autouse 安全网（模块顶部）
@pytest.fixture(autouse=True)
def _safety_mock_popen(monkeypatch):
    monkeypatch.setattr("server.auto_shutdown.subprocess.Popen", lambda *a, **kw: object())

# 2. 显式 dry_run=True
def test_trigger_minimal_request(self, client):
    resp = client.post("/auto_shutdown/trigger", json={
        "task_id": "minimal",
        "dry_run": True,  # 双保险
    })

# 3. 参数默认值偏向安全（API 设计时）
class TriggerRequest(BaseModel):
    dry_run: bool = False  # ⚠️ 这种默认值不安全，应改为 True 或要求显式传
```

**判卷标准**：危险操作测试若触发真实系统行为（即便被用户中止），算测试失败，必须修复后重跑。

## 测试修复铁律（强制，2026-08-03 教训）

> **触发**：exec_python 修复 + 测试套件修复期间，多次用"巧妙"方案绕过失败测试而非查根因。用户严肃批评"你都是用'巧妙'的方案绕过了测试，这样真的好吗？是不是应该仔细考虑测试的价值以及为什么缺东西？"

**核心原则：测试失败是信号不是障碍。绕过测试 = 自欺欺人，下次同样的问题还会出现。**

### 本次踩坑案例（4 个）

#### 案例 1：用 `@pytest.mark.gpu` 跳过过时测试（最严重）

`test_e2e.py::test_small_image_ocr_then_parse` 调用 `/vision/parse` 端点，但该端点在 OmniParser 移除时已删除（`test_vision.py::TestVisionParseRemoved` 明确断言 `/vision/parse` 应返回 404）。测试本应删除/改写，却加 `@pytest.mark.gpu` 跳过——让过时测试"通过"而非诚实删除。

**为什么是绕过**：跳过让测试"绿"了，但 `/vision/parse` 端点已不存在，测试永远不执行真正断言。这等于删除测试但保留测试名假装有覆盖。正确做法是删除测试 + 注释说明为什么删。

```python
# ❌ 错误：加 @pytest.mark.gpu 跳过让过时测试"通过"
@pytest.mark.gpu
def test_small_image_ocr_then_parse(self, client, small_ui_image_base64):
    resp = client.post("/vision/parse", json={...})  # 端点都删了，跳过有什么意义？
    assert resp.status_code == 200  # 永远不会执行

# ✅ 正确：删除过时测试 + 注释说明为什么删
# 注：原 test_small_image_ocr_then_parse 已删除。
# /vision/parse 端点在 OmniParser 移除时已删除（见 test_vision.py TestVisionParseRemoved）。
# 测试本身过时，正确做法是删除而非跳过。OCR 流程已有 test_ocr.py 覆盖。
```

#### 案例 2：用 `try/except` 掩盖 Qt crash

`test_monitoring_task_authorization.py` 在 offscreen 环境下 `QDialog.exec()` 模态对话框触发 access violation，原"修复"用 `try/except SystemError` 吞掉异常假装通过。

**为什么是绕过**：crash 是真实问题（Qt 线程 + 模态对话框 + offscreen 不兼容），吞掉异常不解决问题。正确做法是 mock 掉 UI 交互，测业务逻辑（HTTP 请求参数、线程等待、超时计算）。

```python
# ❌ 错误：try/except 掩盖 crash
def test_monitoring_request_waits_for_server_prompt_timeout(qapp):
    try:
        panel._on_persistent_request_clicked()  # Qt 模态对话框 crash
    except SystemError:
        pass  # 吞掉异常假装通过

# ✅ 正确：mock UI 交互，测业务逻辑
def test_monitoring_request_waits_for_server_prompt_timeout(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    panel._on_persistent_request_clicked()  # 不弹真实对话框，测 HTTP/线程/超时逻辑
```

#### 案例 3：`ruff --unsafe-fixes` 后没跑测试验证

`ruff check --fix --unsafe-fixes` 修了 19 个 lint 错误，但 `--unsafe-fixes` 可能改变语义（删"未使用"变量但实际被 eval 引用、合并 import 导致循环导入、改字符串引号风格破坏 f-string 等）。修完只看 lint 通过就继续，没跑测试验证功能回归。

**为什么是绕过**：lint 通过 ≠ 功能正常。`--unsafe-fixes` 名字本身就警告"不安全"，必须跑全测试套件验证。

```python
# ❌ 错误：--unsafe-fixes 后只看 lint 通过
ruff check --fix --unsafe-fixes server client tests  # 修了 19 个
# 没跑 pytest，潜在功能回归未发现

# ✅ 正确：--unsafe-fixes 后必须跑全测试套件
ruff check --fix --unsafe-fixes server client tests
uv run python -m pytest tests/ -v --tb=short  # 验证没破坏功能
```

#### 案例 4：代码修复后没验证生效

`exec_python` 临时文件孤儿累积 bug 修复了 `server/exec.py`（加 `cleanup_orphan_temp_files()`），但没重启后端。用户问"exec python 的问题你真修好了吗，我看怎么还有"——后端还是 0.15.0，temp 还在堆积 90+180 个孤儿文件。

**为什么是绕过**：改了代码 ≠ 修复生效。后端不重启，新代码不执行，bug 还在。必须重启 + 验证（`/health` 查版本 + 实际行为）。

### 测试修复时的强制检查清单

- [ ] **禁止用跳过绕过过时测试**：测试因端点删除/API 变更/功能移除而失败时，**删除或改写测试**，不要加 `@pytest.mark.skip` / `@pytest.mark.gpu` 让它"通过"。跳过 = 丢失覆盖 + 假绿
- [ ] **禁止用 try/except 掩盖 crash**：测试 crash 时先查根因（环境问题？被测代码 bug？测试设计问题？），mock 掉有问题的 UI/IO 交互测业务逻辑，不要 `try/except Exception` 吞掉异常假装通过
- [ ] **禁止放宽断言让测试通过**：断言失败时先查"断言对还是实现对"，不要把 `assert x == 5` 改成 `assert x >= 1` 或删断言。UI 重构后断言确实该改的，必须在注释里说明"为什么改" + 验证新断言对应真实行为（读被测代码确认）
- [ ] **改测试前先读被测代码**：确认测试期望的行为是否还存在。端点删了？文案变了？返回结构改了？先读源码确认，再决定删/改/保留测试
- [ ] **ruff --unsafe-fixes 必须验证**：`--unsafe-fixes` 可能改变语义。**用完必须跑全测试套件**，不能只看 lint 通过就提交
- [ ] **代码修复必须验证生效**：改了后端代码必须重启后端确认修复生效（`/health` 查版本 + 实际行为验证），不能只看代码改了就声称"修好了"
- [ ] **每个测试修复都问"这是修测试还是绕测试"**：如果答案是"让测试不再报错但不验证真实行为" → 停下来，这是绕过

### 测试的价值是什么？

测试不是"为了通过而存在"，测试是**行为的规约**。当测试失败时，它在告诉你：

1. **被测代码变了**（端点删了/返回结构改了/文案变了）→ 更新测试匹配新行为，或删过时测试
2. **被测代码有 bug**（行为不符合规约）→ 修被测代码，不是改测试
3. **测试本身有 bug**（断言写错了/依赖过时的假设）→ 修测试，但要说清楚为什么旧断言错、新断言对
4. **环境问题**（缺依赖/offscreen 不兼容）→ 加 skip 标记 **并注释具体环境原因**，或 mock 掉环境依赖

**绕过测试 = 删除信号**。今天跳过的测试，明天 bug 会在生产环境爆发，而且你没防线。测试的价值在于它失败时告诉你哪里坏了——让测试"假绿"等于拆除警报器。

**判卷标准**：测试修复若让测试"通过"但不验证真实行为（跳过过时测试 / try-except 吞 crash / 盲目放宽断言），算修复失败，必须重做。

### 已知环境噪声：pytest atexit PermissionError（Windows，可忽略）

> **触发**：2026-08-04 watchdog-mode E2E 测试发现，pytest 全量测试通过后（退出码 = 0），在 atexit 阶段抛 `PermissionError: [WinError 5]`。详细诊断见 `temp/sdd/watchdog-mode/issues.md` ISSUE-002。

**现象**（识别特征，遇到立即忽略）：
- 测试主摘要 `passed/failed` 已打印完成，退出码 = 0
- 异常发生在 `Exception ignored in atexit callback` 阶段（非测试 failure/error）
- traceback 指向 `_pytest/pathlib.py` 的 `cleanup_numbered_dir` → `cleanup_dead_symlinks`
- 路径形如 `C:\Users\<user>\AppData\Local\Temp\pytest-of-<user>\pytest-current`
- 抛 `PermissionError: [WinError 5] 拒绝访问。`

**根因**（已诊断，非项目代码 bug）：
- `pytest-of-<user>\pytest-current` 是**死符号链接**（`os.path.islink()=True`、`os.path.exists()=False`）
- pytest 9.0.3 的 `cleanup_dead_symlinks` 调 `left_dir.resolve().exists()` 时，Windows 对死符号链接抛 `PermissionError` 而非返回 `False`
- 其他 `pytest-XXX` 兄弟目录都是真实目录，仅 `pytest-current` 是死链接
- 用 `--basetemp` 指向项目内目录跑子集 → 无 atexit 异常，证明根因是用户临时目录死链接

**遇到时的处理**：
- ✅ **直接忽略**：不影响测试结果，退出码仍为 0
- ✅ **确认是同一问题**：grep 日志中的 `cleanup_dead_symlinks` + `PermissionError` + `pytest-current`，三者齐全即此问题
- ❌ **不要删除** `C:\Users\<user>\AppData\Local\Temp\pytest-of-<user>\pytest-current`（违反"不操作用户临时目录"原则）
- ❌ **不要升级 pytest**：上游 `cleanup_dead_symlinks` 当前实现未对 Windows 死链接做异常处理，升级无效
- ❌ **不要加 `addopts = "--basetemp=..."`**：会绕过默认 retention 策略 + 每次清空目录，影响调试
- ❌ **不要在 conftest.py 加 hook 清理死链接**：操作用户临时目录，对没有此问题的环境引入副作用

**为什么不做项目级修复**：
- pytest 9.0.3 **没有 `tmp_path_dir` ini 配置项**（已通过 `pytest --help` + 源码检查 + 官方文档三重验证）
- 所有可行修复方案都有副作用，得不偿失
- 该异常是纯环境噪声，不影响测试退出码或测试结果可信度

**何时需要重新评估**：
- 若 atexit 异常开始伴随进程资源未释放（HTTP server/Qt 对象/后台线程持有句柄）→ 转方案 C（补 fixture teardown）
- 若异常从 atexit 阶段升级为测试 failure/error → 重新诊断
- 若 pytest 上游修复了 `cleanup_dead_symlinks` 的 Windows 死链接处理 → 可考虑升级

### 已知环境噪声：cv2 空壳模块导致 pyautogui 测试失败（Windows，可忽略）

> **触发**：2026-08-04 watchdog-mode E2E 测试发现，`tests/approval_screen/test_screen.py::TestSafety::test_safe_action_not_blocked` 和 `tests/approval_screen/test_screen.py::TestMouseKeyboard::test_type_action_safe` 因 `module 'cv2' has no attribute '__version__'` 失败。

**现象**（识别特征，遇到立即忽略）：
- 上述 2 个测试失败，错误信息含 `module 'cv2' has no attribute '__version__'`
- `cv2.__file__ is None`、`dir(cv2)` 为空（cv2 能 import 但内容为空，DLL 加载失败）
- pyautogui 内部访问 `cv2.__version__` 时抛 AttributeError，被 `/screen/action` 端点包装为"键鼠执行异常"

**根因**（环境问题，非项目代码 bug）：
- opencv-python 安装异常或 DLL 加载失败导致 cv2 为空壳模块
- `server/screen/control_grant.py` 是纯 Python + threading，不 import cv2
- `server/screen/` 下无 `import cv2`，2 个失败测试也未 import cv2，是通过 pyautogui 间接触发

**遇到时的处理**：
- ✅ **直接忽略**：与 watchdog 改动无关，是环境问题
- ✅ **确认是同一问题**：`python -c "import cv2; print(cv2.__file__, hasattr(cv2, '__version__'))"` 输出 `None False` 即此问题
- ✅ **修复方式**：`uv pip install --reinstall --no-deps opencv-python`（需用户批准，不擅自安装）
- ❌ **不要修改测试断言绕过**：是环境问题，不是测试契约问题
- ❌ **不要修改 server/screen/ 代码**：根因不在项目代码

**当前状态**（2026-08-04）：
- 已通过 `uv pip install --reinstall --no-deps numpy==2.3.5` 修复（numpy 元数据丢失导致 cv2 DLL 加载链断裂）
- 2 个测试现在通过
- conftest.py 已加 cv2 空壳检测（`HAS_PADDLE_OCR` 检查 `cv2.IMREAD_COLOR`），自动跳过 `@pytest.mark.gpu` 测试

**何时需要重新评估**：
- 若 cv2 空壳问题复现 → 检查 numpy/opencv-python 安装状态
- 若 pyautogui 升级后不再依赖 cv2.__version__ → 可移除 conftest.py 的 cv2 检查

## 测试运行与问题定位流程（避免"测→修→再测"循环）

> **痛点**：测一次发现一两个问题，修了再测又出新问题，循环浪费时间。
> **根因**：没有一次性收集全部失败，而是递归地"发现一个修一个再测"。
> **解决**：标准流程 = 一次性跑全测试拿到失败清单 → 分类 → 批量修复 → 再跑一次。

### 测试运行入口（agent 必读，禁止乱调用）

**历史问题**：agent 不知道用哪个入口，发现错了才加参数（`-v`/`--tb=short`/`-m '...'`），
反复试错浪费时间。以下三个入口**参数已统一**，按场景选一个即可，不要混搭。

| 入口 | 场景 | 命令 |
|------|------|------|
| **`tests/run_all.py`**（推荐） | 分层跑（quick 排除外部资源 / full 全跑），报告含失败复现命令 | `uv run python tests/run_all.py --quick` |
| **`tools/run_tests_collect.py`** | 一次性收集全部失败（与 run_all 共用 test_runner 层） | `uv run python tools/run_tests_collect.py` |
| **直接 pytest** | 单文件/单目录调试（看详细 traceback） | `uv run python -m pytest tests/memory -v --tb=short` |

**收集范围**（2026-08-27 起）：脚本层经 `lib.component_manifest.collect_test_dirs()`
自动扫描各组件 `manifest.toml` 的 `[tests]` 段，把 `workspace/<skill>/tests/` 一并纳入。
所以脚本命令只写 `tests/` 也会自动带上 workspace 组件测试（当前 6 个目录），
不要再手动拼 workspace 路径。

### 测试目录结构（按功能域分组，加新测试先归组）

```
tests/
├── agent_runner/       # agent runner 主循环
├── approval_screen/    # 审批+屏幕控制授权
├── archive_sdd_tickets/# SDD ticket 编号回归测试归档（历史保留，不再新增）
├── browser/ client_ui/ componentization/ files_tools/ gacha/
├── guide_loops/ headless/ lib_core/ llm_vision/ memory/
├── release_ci/ security/ server_endpoints/ skills/
└── fixtures/browser/   # browser E2E 静态资源
workspace/<skill>/tests/  # 组件测试，由 manifest.toml [tests] 声明
```

**规则**：新增测试放进对应功能域子目录（不新建平铺文件）；SDD ticket 回归测试进
archive_sdd_tickets；workspace skill 的测试放 `<skill>/tests/` 并在 `manifest.toml`
补 `[tests]` 段（有测试 enabled=true + paths=["tests"]）。

**参数一致性**（pyproject.toml `addopts` 已统一配置，不要在命令行重复加）：
- `-q -ra`：安静进度 + 失败/跳过摘要（替代旧的 `-v` 刷屏 / 纯 `-q` 看不到摘要）
- `--tb=short`：中等 traceback（单文件调试用）
- `--maxfail=500`：不在第一次失败停
- `--timeout=30 --timeout-method=thread`：防卡死
- `-n auto`：pytest-xdist 并行（**内存警告见下方"进程崩溃处理"**）
- `--junit-xml`：**只由脚本层加**（run_tests_collect.py / run_all.py），不放 addopts（避免单文件调试污染）

**脚本层覆盖**（`tools/test_runner.py` 共享逻辑，两个脚本共用）：
- 流式 tee：实时输出到控制台 + 写 `temp/test_full_log.txt`（替代 `capture_output=True` 黑盒）
- `--tb=line`：全量收集时覆盖 addopts 的 `--tb=short`（一行一个失败，最简）
- `--junit-xml=temp/test_results.xml`：结构化真源，解析 XML 而非文本（pytest-xdist 并行时文本顺序乱）
- 崩溃检测：returncode 非 0-5 标记"进程崩溃"（如 3221225477 = STATUS_ACCESS_VIOLATION），不假装"0 失败"
- 报告含**每个失败的单独复现命令**——修复验证先跑失败子集（或 `pytest --lf` 重跑上次失败），
  全过后再跑整轮 quick，不要每次验证都跑全量
- **失败清单默认强制输出**（2026-09-03）：跑完总是把完整失败/错误清单（JUnit XML 驱动，
  测试全名 + 错误摘要，一行一个）打印到控制台，**不依赖 agent 主动读 report 文件**。
  不需要时显式加 `--no-list`（不推荐：丢了清单就退回"测→修→再测"循环）

**产出**（写入 `temp/`，两个脚本产出相同）：
- `test_full_log.txt` — 完整 pytest 输出
- `test_results.xml` — JUnit XML（结构化真源）
- `test_failure_report.md` — 结构化报告（状态/摘要/失败表格/**逐条复现命令**/分类建议）

**禁止行为**：
- ❌ 用 `pytest tests/ -v` 跑全量（约 5000 测试刷屏，看不到失败摘要）
- ❌ 手动裸跑全量不加 marker —— 直接用 `tests/run_all.py --quick`（quick 已统一排除
  gpu/browser/real_backend/network/chaos/e2e/gui/migration）
- ❌ 用 `capture_output=True` 黑盒跑（看不到中间输出，崩溃时拿到空 stdout 假绿）
- ❌ 手动拼 `--tb=line`/`--tb=short`/`--maxfail` 等参数（addopts 已配，脚本层按需覆盖）
- ❌ 验证修复时反复跑全量 —— 先跑报告中列出的失败子集命令，确认全过后再跑一次全量收尾
- ❌ 跑子集/`--lf` 验证时丢了全局视野 —— 入口脚本每次跑完都打印完整失败清单（除非 `--no-list`），
  子集验证前后先对照这份清单，确认覆盖了全部失败、没有"修一批漏一批"

### 标准流程（强制）

#### Step 1: 一次性跑全测试，不中断

**推荐用脚本**（流式输出 + JUnit XML + 崩溃检测，自动生成结构化报告）：

```powershell
uv run python tools\run_tests_collect.py
```

脚本内部等价于以下手动命令（不要手动拼，参数已统一在 `tools/test_runner.py`）：

```powershell
.venv\Scripts\python.exe -m pytest tests/ `
    -m 'not gpu and not browser and not real_backend and not network and not chaos and not e2e and not gui and not migration' `
    --tb=line --junit-xml=temp\test_results.xml
```

参数说明（addopts 已配的参数，命令行不重复加）：
- `-q -ra`（addopts）：安静进度 + 失败/跳过摘要
- `-m 'not gpu and not ...'`：排除需要外部资源的测试（GPU/浏览器/真实后端/网络/混沌/E2E/GUI/数据迁移），只跑纯单元测试（约 4700 个，7 分钟左右）
- `--tb=line`（脚本层覆盖 addopts 的 `--tb=short`）：每行一个失败摘要，避免长 traceback 淹没全局
- `--maxfail=500`（addopts）：不在第一次失败时停，拿到全部失败
- `--junit-xml=temp/test_results.xml`（脚本层加）：结构化输出，解析 XML 而非文本

产出（写入 `temp/`）：
- `test_full_log.txt` — 完整 pytest 输出（流式 tee，边跑边写）
- `test_results.xml` — JUnit XML（结构化真源，工具可解析）
- `test_failure_report.md` — 结构化报告（状态/摘要/失败表格/分类建议）

#### Step 2: 分类失败（关键，禁止跳过）

拿到失败清单后，**先分类，再动手修**。失败分 4 类：

| 类别 | 判断方法 | 处理方式 |
|------|----------|----------|
| **真实 bug** | 单独跑该测试文件**也失败** | 优先修，改实现代码 |
| **测试隔离问题** | 单独跑通过，全量跑失败 | 不修实现，改测试（加 cleanup/隔离 fixture）或标记已知问题 |
| **环境问题** | 缺依赖/权限/路径错误 | 修环境，不改代码 |
| **过时测试** | 测试期望的行为已不存在（端点删除/ API 变更） | 删/改测试，参见"测试修复铁律" |

**分类方法**：对每个失败测试，单独跑确认：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_xxx.py -v --tb=short
```

- 单独跑通过 → **测试隔离问题**（不是实现 bug，不要改实现）
- 单独跑也失败 → **真实 bug 或过时测试**（读被测代码确认）

#### Step 3: 批量修复（禁止逐个修逐个测）

**禁止**每修一个就跑一次全测试（7 分钟/次，5 个失败就是 35 分钟浪费）。应：
1. 一次性修所有"真实 bug"类失败
2. 一次性修所有"过时测试"类失败
3. 测试隔离问题单独处理（加 cleanup fixture 或标记 `pytest.mark.xfail`）
4. 修完后先跑失败子集验证（见 Step 4），最后跑一次全量收尾

#### Step 4: 验证修复（先子集，后全量）

1. **先跑失败子集**：报告里每个失败都附了单独复现命令，或者用缓存重跑上次全部失败：
   ```powershell
   uv run python -m pytest --lf --tb=short        # 只重跑上次失败的测试
   ```
   （`--lf` 依赖 .pytest_cache，脚本层跑过就有；确认修复 + 无新失败即可）
2. **再跑一次全量 quick** 收尾，确认：
   - 修过的测试现在通过
   - 没有引入新失败（对比失败数）
   - ruff lint 通过（`ruff check server client tests`）

### 进程崩溃处理（returncode 非 0-5）

`tools/test_runner.py:classify_exit` 自动分类退出码。若报告显示 `crashed_NNN`：

- **3221225477 = STATUS_ACCESS_VIOLATION**：Qt access violation，conftest.py 已知问题
  （全量测试时随机崩溃，JUnit XML 无 failure/error 记录，崩溃发生在 pytest 汇总/进程退出阶段）
- **MemoryError / WinError 8（内存资源不足）/ execnet 序列化 MemoryError**：
  xdist worker 过多打爆内存。2026-08-27 教训：16 个 worker 并发时每个都要导入 app+PySide6
  （收集+序列化数 MB×16），32GB 内存机器上连续 OOM。**降并发重跑**即可：
  ```powershell
  uv run python tests/run_all.py --quick -n 4   # -n last-wins，覆盖内置的 -n auto
  ```
- **其他非 0-5 码**：Windows fatal exception，进程被系统杀死

**处理步骤**：
1. 看 `temp/test_full_log.txt` 末尾，找崩溃前最后一个输出（定位崩溃点）
2. 看 `temp/test_results.xml` 是否生成——若缺失说明崩溃在 XML 落盘前
3. 分段跑（按功能域目录）缩小崩溃范围：
   ```powershell
   uv run python -m pytest tests/memory -v --tb=short  # 只跑某一功能域
   ```
4. conftest.py 的 `pytest_runtest_teardown` hook（Qt 状态清理）已在缓解，若仍崩溃
   通常是某个测试未正确清理 QApplication 状态——单独跑该文件能复现则修测试隔离

**禁止**：看到 `crashed_NNN` 假装"0 失败"通过——崩溃时 XML 可能不完整，必须人工确认。

### 何时用 -x / --maxfail=1

- **第一次跑全测试**：用 `--maxfail=500`（大数），拿到全部失败
- **修完后再跑**：可用 `-x`（第一次失败即停），快速验证修复是否引入新问题
- **绝不用 -x 做第一次跑**：只看到第一个失败，修了再跑又出新失败，陷入循环

### 已知测试隔离问题（本项目，非实现 bug）

| 问题 | 受影响测试 | 根因 | 单独跑 |
|------|-----------|------|--------|
| Qt 跨测试累积状态 | `test_chat_panel_v2_shutdown_recovery.py`（9 个） | QApplication 共享实例 + offscreen 平台 + 累积 widget 状态；`isHidden()` 受父 widget 可见性影响 | ✅ 通过（1 秒） |
| VLCandidate 身份分裂 | `test_recorder_consumer_e2e.py` | `sys.modules` 操作导致类对象重新加载，`isinstance` 失败 | ✅ 通过 |
| numpy/cv2 元数据丢失 | `test_ocr.py` | pip 安装异常导致 cv2 空壳模块 | 跳过（`@pytest.mark.gpu`） |

这些测试单独跑通过，全量跑受污染。**不是实现 bug**，修测试隔离超出单次任务范围时应标记并跳过，不要改实现代码"迁就"测试。

### 反面案例（禁止这样做）

```powershell
# ❌ 错误：用 -x 跑全测试，只看到第一个失败
.venv\Scripts\python.exe -m pytest tests/ -x
# 修了再跑，又出新失败，又修又跑...5 个失败跑 5 次 = 15-20 分钟

# ❌ 错误：不分类，假设所有失败都是实现 bug
# 结果：改了实现代码"迁就"测试隔离问题，破坏真实行为

# ❌ 错误：每修一个跑一次全测试
pytest tests/  # 3 分钟
# 修一个
pytest tests/  # 又 3 分钟
# 5 个失败 = 15 分钟，而批量修只需跑 2 次（修前+修后）= 6 分钟

# ✅ 正确：一次性收集 + 分类 + 批量修 + 再跑一次
.venv\Scripts\python.exe tools\run_tests_collect.py  # 3-4 分钟，拿到全部失败
# 分类 + 批量修（10-20 分钟）
.venv\Scripts\python.exe tools\run_tests_collect.py  # 3-4 分钟，验证
# 总计 16-28 分钟，但只跑 2 次，且不会"测→修→再测"循环
```

## 测试文件维护准则（避免测试腐化）

> **痛点**：测试越加越多但价值没涨，过时测试堆积，隔离问题反复出现，新测试与旧测试重复。
> **根因**：没有准则，每个 agent/开发者凭感觉加测试、跳测试、留测试。
> **解决**：以下准则是 2026-08-06 测试精简复盘的产出，后续必须遵守。

### 何时删除测试（强制）

| 场景 | 判断方法 | 处理 |
|------|----------|------|
| **端点已删除** | `grep -r "端点路径" server/` 无匹配 | 删测试文件或测试类 |
| **API 行为已变更** | 测试期望的返回/状态码与当前实现不符 | 改测试期望，或删过时测试 |
| **与其他测试完全重复** | 同一行为被多个测试覆盖，断言一致 | 保留最完整的，删冗余的 |
| **测试本身过时但通过** | 测试因 mock/404 误判通过，但实际测的不存在 | 删，不要"修到通过" |

**反面案例**：`test_e2e.py` 2 个测试与 `test_screen.py` 159 个完全重复，2026-08-06 删除。

### 何时保留测试（即使看起来过时）

| 场景 | 为什么保留 | 例子 |
|------|-----------|------|
| **回归测试** | 验证已删除功能不复活 | `test_vision.py::TestVisionParseRemoved` 验证 `/vision/parse` 返回 404 |
| **核心引擎测试** | 覆盖 SessionRunner/EventStore/Reconciler 等核心 | `test_v6_lite_t01-t09`、`test_v6_streaming_t00-t05` |
| **大模块测试** | 模块复杂，测试多合理 | `test_recorder_*`（25 文件 700+ 测试）覆盖录制/编辑/消费/处理 |

### 隔离问题处理（强制，禁止 xfail 绕过）

**症状**：测试单独跑通过，全量跑失败。

**错误做法**：
- `@pytest.mark.xfail` 标记跳过（让问题"消失"但根因仍在）
- `@pytest.mark.skip` 跳过（丢失覆盖）
- 改实现代码"迁就"测试（破坏真实行为）

**正确做法**：加 autouse cleanup fixture，根治跨测试状态污染。

**本项目典型隔离问题**：
- **Qt 跨测试状态累积**：module-scoped `qapp` 共享 QApplication，widget/定时器/信号连接在 `deleteLater` 后残留，导致 `isHidden()`/`isVisible()` 断言不稳定
  - 修复：autouse fixture 每个测试后 `closeAllWindows` + 多次 `sendPostedEvents` + `gc.collect`
  - 例子：`test_chat_panel_v2_shutdown_recovery.py::_force_cleanup_qt_state_after_each_test`（2026-08-06）
- **VLCandidate 身份分裂**：`sys.modules` 操作导致类对象重新加载，`isinstance` 失败
  - 修复：snapshot/restore 模式（见"测试隔离"段）

### 测试文件大小准则

- **单文件 > 100 测试**：考虑按主题拆分（如 `test_browser.py` 175 测试可拆 `test_browser_models.py` / `test_browser_sessions.py` 等）
- **拆分是可读性优化，非减少测试**：拆分后测试总数不变，只是文件变小
- **优先级低于删过时测试和修隔离问题**：文件大但不影响测试速度

### 新加测试前必做检查

1. **grep 已有测试**：`grep -r "要测的行为" tests/` 看是否已有覆盖
2. **确认端点存在**：`grep -r "端点路径" server/` 确认端点未删除
3. **隔离考虑**：如果测试涉及 Qt/widget/共享状态，提前加 cleanup fixture
4. **不要为删过的功能加测试**：除非是回归测试（验证不复活）

### 测试精简复盘记录（2026-08-06）

本次精简结果：
- 删除 `test_e2e.py`（2 测试，与 `test_screen.py` 重复）
- 修复 `test_chat_panel_v2_shutdown_recovery.py` 9 个隔离问题（autouse fixture）
- 跳过 v6_lite schema 测试合并（重复只 2-4 个，收益低于风险）
- 跳过 test_browser.py 拆分（工作量大，收益是可读性非减少测试）
- 全量测试从 10 failed / 3600 passed → 0 failed / 3662 passed

## 发版 vs 打包（严格区分）

项目把"版本发布"和"源码打包"拆成两个独立的 task_type，**严格区分，不可混用**。用户说"发版"指前者，说"打包"指后者。

| 维度 | 发版（system.release） | 打包（system.public_distribution） |
|------|------------------------|-------------------------------------|
| **触发词** | "发版"/"发个版本"/"出版本号"/"改版本号"/"归档版本"/"release" | "打包"/"打包给朋友"/"脱敏打包"/"公开发布"/"源码分发"/"朋友版"/"public release" |
| **task_type** | `system.release` | `system.public_distribution` |
| **产出** | CHANGELOG 版本号归档 + git commit | 隐私安全的源码 ZIP（含 SHA-256 校验和、MANIFEST、DEPLOYMENT.md） |
| **是否动源码树** | 否（只改 CHANGELOG + commit） | 否（导出到 `temp/release_export_<timestamp>/`，原树保持不动） |
| **是否含 .git 历史** | 是（commit 进入 .git） | 否（扁平文件拷贝，天然剥离 .git） |
| **是否 push remote / 发 tag** | 否（项目纯本地，不发 git tag / GitHub release / push remote） | 否（只产出本地 ZIP） |
| **依赖** | 无（独立流程） | 推荐先发版再打包（让 ZIP 内的 CHANGELOG 反映最新版本号） |
| **核心文件** | `CHANGELOG.md` + `docs/changelog-archive.md` | `release/policy.toml` + `release/profiles/*.toml` + `release/audience/*.toml` + `tools/release/cli.py` + `tools/release/engine/`（spec-v2-compiler.md） |
| **Skill** | 无（system.release 无 skill_file，first_action 内联 5 步流程） | `.agents/skills/public-release/SKILL.md` |

**典型时序**：用户变更代码 → CHANGELOG 加 [Unreleased] 条目 → 用户说"发版" → system.release 流程把 [Unreleased] 归档为版本号 + git commit → 用户说"打包" → system.public_distribution 流程做审计 + 扫描 + 导出 ZIP（此时 ZIP 内的 CHANGELOG 已是最新版本号）。

> 源码分发的策略、profile 配置、隐私审计详见 `docs/release-policy.md`（friend-full / public 等 profile 与 Apache-2.0 协议说明）。

### 发版 8 步流程（system.release，agent 必须按此顺序执行）

> 本地操作：**不发 git tag、不发 GitHub release、不 push remote**。release 仅通过 CHANGELOG 文档化 + 本地 git commit。

1. **全测试套件验证（强制，2026-08-03 教训）**：发版前必须跑全测试套件，**所有测试通过或跳过有合理注释**才能发版。失败测试必须按"测试修复铁律"修复（查根因，禁止绕过）。
   ```
   uv run python -m pytest tests/ -v --tb=short
   ```
   - 测试失败 → 修复（按 `docs/dev-workflow.md` "测试修复铁律"），不能跳过发版
   - 修测试后用了 `ruff --unsafe-fixes` → 必须再跑一次全测试验证
   - 测试修复涉及后端代码 → 重启后端验证修复生效（`/health` 查版本 + 实际行为）
2. **查 [Unreleased] 段**：读 `CHANGELOG.md` 确认 `[Unreleased]` 段有实际变更内容（空段或全是 `(暂无)` 占位 → 告知用户无需 release，结束流程）
3. **定版本号**：读 `CHANGELOG.md` 找最近 release 段（如 `[0.22.0] - 2026-07-24`），按变更内容递增：
   - 常规（bug fix / 小改进）→ **patch +1**（0.22.0 → 0.22.1）
   - 重大功能（新模块 / 新功能 / 行为变更）→ **minor +1**（0.22.0 → 0.23.0）
   - 不跨 major 除非用户明确指定
4. **版本号同步**（强制，2026-08-03 教训）：跑 `uv run python tools/bump_version.py <新版本号>`，统一更新 `server/main.py` + `server/core/health.py` + `pyproject.toml` 三处 VERSION 常量。**禁止手动 Edit 单个文件**（曾导致版本号漏改）。先 `--dry-run` 预览再实际执行：
   ```
   uv run python tools/bump_version.py 0.32.1 --dry-run  # 预览
   uv run python tools/bump_version.py 0.32.1            # 执行
   ```
5. **Edit CHANGELOG.md**：把 `## [Unreleased]` 改为 `## [新版本号] - YYYY-MM-DD`（今天日期），在顶部插入新的空 `## [Unreleased]` 段（含 `### Added` / `### Changed` / `### Fixed` / `### Security` 四个子段，每段下 `- (暂无)` 占位）
6. **RunCommand 跑归档脚本**：`uv run python tools/migrate_changelog.py`（默认 `--keep 1`），把旧 release 段归档到 `docs/changelog-archive.md`（newest first，幂等）。CHANGELOG.md 仅保留 `[Unreleased]` + 最近 1 个 release，避免活跃段过长被 agent 误伤；查询完整历史需同时读 `docs/changelog-archive.md`。如需保留更多最近 release 段用 `--keep N`
7. **RunCommand 跑项目结构 baseline 同步**：`uv run python tools/sync_project_structure.py`（机械同步 `data/project_structure.json`：磁盘不存在的 baseline 条目移除、磁盘新增的路径补入 baseline）。task_closure 累积的 unknown/missing 漂移在此一次性同步，避免 baseline 与磁盘持续偏离。
   - 可先 `--verify` 看漂移摘要，再正式跑同步
   - 同步会修改 `data/project_structure.json`，需在下一步 git add 中带上此文件
8. **git add + git commit**：
   - **按目录/文件 add**（`.agents/` `client/` `server/` `docs/` `tests/` `workspace/` + 顶级文件 + `data/project_structure.json`），**不要用 `git add -A` / `git add .`**，避免误纳入 `config.toml` / `data/llm/keys.json` 等敏感文件
   - **commit 前检查 `git status`** 确认无敏感文件被 staged
   - **commit 信息**：可单条 `chore(changelog): 发版 vX.Y.Z` 标题，或按主题分多个 commit（feat/fix 类型）后再加 release commit。**PowerShell 不支持 bash HEREDOC**（`$(cat <<'EOF'...)`），多行 commit message 写到 `temp/commit_msg.txt` 用 `git commit -F temp/commit_msg.txt`

### 发版关键陷阱

- **PowerShell 不支持 `&&` 串联命令语句**：用 `;` 顺序执行或拆成多次调用
- **PowerShell 不支持 bash HEREDOC**：commit 信息用单个 `-m` 传单行标题，或多个 `-m` 传多段，或写到临时文件用 `git commit -F`
- **`git add` 不要用 `-A` / `.`**：按目录 add，避免误纳入敏感文件
- **commit 前检查 `git status`** 确认无敏感文件被 staged
- **`[Unreleased]` 段空时不要 release**
- **不发 git tag / GitHub release / push remote**：项目纯本地，release 仅通过 CHANGELOG 文档化

## MCP 工具优先原则

### 删除与高风险命令必须走可检查路线

- 任何删除、清理、递归删除、格式化、强制覆盖、强制终止进程或会丢失用户数据的操作，必须使用 LocalAgent 的 exec_cmd、exec_terminal_spawn 或 exec_python，不要使用 Codex Desktop 的直接 shell/exec_command 路径。
- 这样做不是为了故意限制或为难 Agent。模型确实可能出现幻觉、参数拼接错误或命令执行工具缺陷；dcg 和后端拦截是避免不可逆损失的最后防线，先询问用户通常没有害处。
- 直接 shell 路径可能不触发 Codex PreToolUse Hook，因此即使 C:\\Users\\admin\\.codex\\hooks.json 存在，也不能把直接 shell 当作安全路线。
- 返回 blocked=true 时，禁止改写、拆分、编码、写脚本、切换 shell、换工具或改用其他执行路径绕过。必须先说明完整命令、执行原因、目标、影响和更安全替代方案。
- 平台有原生询问工具时优先询问；否则调用 command_guard_request_approval。用户明确批准后，使用返回的、只绑定原命令/shell/cwd 且只能消费一次的 approval_token 重试。
- 删除操作必须使用精确路径。涉及 workspace/、weights/、data/、.venv/、server/、tools/、tests/ 或用户文件时，必须先列出清单并获得用户明确批准。

### 破坏性命令拦截与用户询问规范

- LocalAgent 后端使用 dcg 检查 exec_cmd、exec_terminal_spawn 和 exec_python。返回 blocked=true 时，表示命令被用户的安全规则拦截。
- 命令拦截不是为了阻碍 Agent 工作，而是为了应对模型幻觉、命令拼接错误和工具执行缺陷，是执行前的最后防线。先询问用户通常没有害处。
- 被拦截后禁止尝试通过改写命令、拆分执行、编码、写入脚本、切换 shell、调用 Python 文件 API 或其他工具绕过。
- 询问用户时必须说明：准备执行的完整命令、执行原因、可能影响、目标路径或资源，以及可用的更安全替代方案。
- 当前 Agent 平台有原生询问工具时优先使用。用户决定后调用 command_guard_record_decision，传入 approval_id、decision=approve|deny 和用户的补充理由。
- 平台没有原生询问工具时调用 command_guard_request_approval，传入 approval_id 和完整的 agent_reason，由 PySide6 弹窗收集批准或拒绝以及可选反馈。
- 批准只签发与原命令、shell、cwd 绑定的短时一次性 approval_token。重试原执行工具时原样传入该 token；不得用于其他命令。

**三层递进审批（LLM 预审）**：对通用工具类端点（exec_python/exec_cmd/exec_terminal_spawn/exec_apply_patch），审批系统会先跑静态规则扫描（拦截 subprocess/shutil.rmtree 等明确危险 API），再跑 LLM 审查（default 模型层，三态 APPROVE/DENY/MANUAL）。LLM APPROVE 时自动放行（agent 无感知，不弹窗）；LLM DENY/MANUAL/不可用时回退人审。LLM DENY 后下一次审批强制走人审（冷却期），人审通过后恢复 LLM 审查。审计日志持久化到 `data/approvals.jsonl`。配置项 `command_guard.llm_review_*` 可调（默认开启）。这层机制对 agent 透明——合法场景的 exec_python 会直接成功，只有 LLM 判断有风险时才弹窗。

**审批严格性四级配置（`command_guard.approval_level`）**：统一控制 HTTP 中间件和 MCP 网关的端点审批拦截范围，与 `enabled` 解耦（`enabled` 仅控制 dcg 二进制预检查）。四级：
- **strict**（默认）：完整审批清单——系统/loop/activity/browser/exec/terminals/mindforge/user_message/模型卸载 + PUT/DELETE 默认审批
- **moderate**：strict 减去 6 个低风险运维端点（`/loop/tasks`、`/activity/daily`、`/browser/close`、`/ocr/models/unload`、`/ocr/models/keep`、`/vision/models/unload`）—— `loop_run_task`、`loop_pause_task`、`loop_resume_task` 在此级别免审
- **loose**：仅拦截代码执行类（`/exec/python`、`/exec/apply-patch`、`/exec/cmd`），PUT/DELETE 全放行
- **none**：不拦截任何端点（仅 dcg 二进制预检查仍独立生效，受 `enabled` 控制）

实现：`safety_lookup` 仍按 strict 完整清单构建（启动时一次性），运行时由 `server/route_tags.py:classify_safety_runtime(method, path)` / `classify_safety_runtime_by_op(op)` 按当前 level 二次过滤——**用户在 GUI 改 level 立即生效，无需重启后端**。MCP 网关只有 operation_id，通过 `init_op_path_map(app)` 启动时构建 `_op_to_path` 反查表。调试「为什么 X 端点没被拦 / dcg=false 还在审批」时：先 `GET /config` 查 `command_guard.approval_level`，approval_level 才是端点审批的控制旋钮，不是 `enabled`。

#### 审批系统设计选择记录（非漏洞，从 SECURITY-RISKS.md E12 迁入）

以下两条是审批系统的设计选择（非安全缺陷），原记于 `SECURITY-RISKS.md` E12"非漏洞但记录"小节，2026-08-06 迁入此处归档：

1. **`_approved_cache` 相同代码 TTL 内免重审**（`server/approval_review.py:160-206`）：
   - 设计：用户批准一次后，相同代码（`code_sha256` 指纹相同）在 `token_ttl_seconds`（默认 120s）内自动放行，不重复弹窗
   - key 结构：`operation_id:code_sha256_16`，TTL 与审批令牌一致
   - 与重放攻击防御**无关**：此缓存是"相同代码免重审"，重放防御由 `consume_token` 的 `dict.pop` 一次性消费 + 指纹绑定 + 时间过期三层独立机制承担
   - 并发安全：`threading.Lock` 保护，单进程部署下无竞态
   - 调试入口：`GET /config` 看 `command_guard.token_ttl_seconds`，缓存 TTL 跟随此值
2. **令牌不绑定 user/session + HTTP 明文传输**：
   - 设计：LocalAgent 是个人单用户系统，令牌（`approval_token`）不绑定 user/session 标识
   - HTTP 明文（无 TLS）：理论上可被本机窃听，但令牌 120s 过期 + 一次性消费 + 指纹绑定三重防御下，窃听者只能执行用户已批准的同一操作（无法伪造新操作）
   - 不修原因：单用户场景下加 TLS / user 绑定收益有限，且引入证书管理复杂度
   - 调试入口：`server/http_guard.py:_tokens_http` / `server/command_guard.py:_tokens`（纯内存 dict，重启即吊销所有未消费令牌）

**遇到任务时，优先使用 MCP 工具，而不是自己写 Python 脚本。** 后端提供不超过 40 个直接 MCP 工具，另有高级工具网关（`localagent_advanced_tool` / `localagent_list_tools`）和模板网关（`localagent_template_tool` / `localagent_list_templates`），覆盖大部分常见操作：

| 需求 | 正确做法 | 错误做法 |
|------|---------|---------|
| 接到任务不知怎么做 | `mcp_localagent_agent_guide(task='...')` 获取指导 + 候选清单 | 读 _index.md 扫描 51 个 skill |
| 识别图片文字 | `mcp_localagent_ocr_*` | 自己写 PaddleOCR 脚本 |
| 识别 UI 元素 | `mcp_localagent_understand_image`（布局/状态描述）+ `mcp_localagent_vision_locate`（坐标兜底） | 自己写 OmniParser 脚本 |
| 截图（仅看尺寸） | `mcp_localagent_capture_screen` (format=base64, 只取 width/height) | 自己写 pyautogui 脚本 |
| 截图给多模态LLM看 | `mcp_localagent_capture_screen` (format="inline") | 用 capture_screen(format=base64) 撑爆上下文 |
| 截图特征分析（无 base64） | `mcp_localagent_screen_analyze` | 用 capture_screen(format=base64) 撑爆上下文 |
| 快速了解桌面状态 | `mcp_localagent_screen_snapshot` | 逐个调 list_windows+screen_status |
| 等待画面条件满足 | `mcp_localagent_screen_wait_for` | 手写截图+OCR+重试循环 |
| 多步键鼠操作 | `mcp_localagent_batch_actions` | 多次调 execute_action |
| 激活窗口到前台 | `mcp_localagent_focus_window` | 自己写 SetForegroundWindow 脚本 |
| 点击/输入 | `mcp_localagent_execute_action` | 自己写 pyautogui 脚本 |
| 执行代码 | `mcp_localagent_exec_python`（万能回退 ⚠️ 禁用于发 HTTP 调本地 API，见下方决策树） | 自己起 Python 进程 |
| 修改源码/应用补丁 | `mcp_localagent_exec_apply_patch`（UTF-8、dry-run、失败回滚） | PowerShell here-string / `.bat %*` 传多行补丁 |
| LLM 对话/评分 | `mcp_localagent_agent_chat` / `agent_score` | 自己调 OpenAI API |
| 浏览器操作（基础） | `mcp_localagent_browser_*`（status/tabs/navigate/close/screenshot/console_logs） | 自己写 CDP 脚本 |
| 浏览器点击元素 | `mcp_localagent_browser_action`（action=click, target={css/text/...}） | exec_python 写 Playwright |
| 浏览器填表单 | `mcp_localagent_browser_action`（action=fill/press_sequentially, target={css/text/...}） | exec_python 写 Playwright |
| 浏览器等加载 | `mcp_localagent_browser_wait_for`（wait_type=load/domcontentloaded/networkidle/...） | exec_python 写 Playwright |
| 浏览器提取文本 | `mcp_localagent_browser_evaluate`（document.body.innerText 等 read-only 表达式） | 截图+OCR（准确度低） |
| 浏览器元素截图 | `mcp_localagent_browser_screenshot`（shot_type=element, selector=...） | 全页截图再裁剪 |
| 查询窗口 | `mcp_localagent_list_windows` | 自己写 win32 脚本 |
| 读写记忆 | `mcp_localagent_memory_*` | 自己写文件读写脚本 |
| 查询到期任务/WIP | `mcp_localagent_todos_due` / `mcp_localagent_wip_list`（summary=true 只看摘要）/ `mcp_localagent_wip_get`（单个详情） | 自己写 SQL 查询 |
| 查看 Loop 任务状态 | `mcp_localagent_loop_list_tasks` / `loop_status` | 自己读 data/loops/tasks.json |
| 手动触发/暂停 Loop | `mcp_localagent_loop_run_task` / `loop_pause_task` / `loop_resume_task` | 自己写 HTTP 脚本 |
| 查看收件箱 | `mcp_localagent_inbox_list` / `inbox_get` | 自己写 SQL 查询 inbox.db |
| 处理收件箱条目 | `mcp_localagent_inbox_update` / `inbox_delete` | exec_python 写 SQL |
| 调用非直连 REST 端点（读详情/写/运维） | `mcp_localagent_localagent_advanced_tool` | exec_python 发 HTTP |
| 常见工作流（截图+OCR等） | `mcp_localagent_localagent_template_tool` | exec_python 写重复代码 |

**只有当 MCP 工具无法满足需求时，才写自定义脚本，且必须写到 `temp/` 目录。**

### 工具选择决策树（遇到任何"要调后端能力"的需求时按此顺序）

1. **直连 MCP 工具（白名单）** → 直接用。查白名单：`localagent_list_tools()` 或看上方表格
2. **`localagent_advanced_tool` 网关** → 适用于**所有非直连的 REST 端点**（含只读详情如 `todos_get`/`memory_search`/`apikey_history`，以及 CRUD/运维）。调用：`localagent_advanced_tool(tool=<operation_id>, params={...})`
3. **`localagent_template_tool`** → 适用于预定义工作流（截图+OCR、浏览器导航+等待等）
4. **`exec_python`** → 仅用于**真正需要运行代码**的场景：运行项目脚本、复杂数据处理、import 项目模块、截图+OCR 一步完成（避免 base64 撑爆上下文）

**关键事实**：`advanced_tool` 调 GET 类端点**免审批**（safety 绑定到目标 operation，GET=read_only）；`exec_python` 是 approval_required，**预审不通过会触发用户审批**。

### 响应大小保护与精简模式

MCP 网关层有 **50KB 硬上限 size guard**：单个 TextContent 超阈值时头尾截断，marker 文案明确指示"禁止静默尝试其它方案"+"请立即向用户报告"。**触发截断时必须上报用户，不要自行缩小参数重试或换工具。**

高频大返回端点提供精简参数，agent 默认应优先使用：

| 端点 | 精简参数 | 说明 |
|------|---------|------|
| `localagent_list_tools` | 默认精简（无 parameters） | 需要参数 schema 时显式传 `verbose=true` |
| `llm_pool_status` | `summary=true` | 省略 keys 详情数组，只返回聚合统计 |
| `wip_list` | `summary=true` | 只返回 id/title/status/priority/progress |
| `memory_search` | `top_k=10`（默认） | 最大 100，按需调 |
| `inbox_list` | `limit=200`（默认） | 最大 1000 |

### 新增 REST 端点与 MCP 暴露策略（防膨胀）

**加端点前必须先判断是否要进 MCP，避免工具列表无限膨胀。** MCP 工具列表过长会让 agent 选择困难、上下文污染、`localagent_list_tools` 返回变慢。

判断流程（按顺序）：

1. **agent 日常会用吗？** → 进 `DIRECT_TOOLS` 白名单（第一层，直连 MCP 工具）
   - 例：`inbox_list` / `inbox_get`（agent 查询待审查条目）
2. **agent 偶尔会用、但属于通用能力？** → 不进白名单，自动收进 `localagent_advanced_tool` 网关（第二层）
   - 例：`todos_get` / `wip_create` / `memory_search`
3. **只给监控面板/脚本/GUI 用，agent 不该调？** → 加入 `GATEWAY_EXCLUDE`（从 MCP 网关排除，REST 仍可用）
   - 例：`inbox_batch`（监控面板批量管理）、`user_message_*`（GUI 管理）、`activity_daily_*`（日报系统）

**强制规则**：
- 新增 REST 端点后，必须在 `server/mcp_whitelist.py` 显式决定：进 `DIRECT_TOOLS` 还是加 `GATEWAY_EXCLUDE`，不能"默认不处理"（默认会进网关，可能膨胀）
- 运维/批量/管理类端点（GUI/面板/脚本调用）一律 `GATEWAY_EXCLUDE`
- 高频只读端点才考虑 `DIRECT_TOOLS`（agent 每次会话都可能用）
- 修改 `mcp_whitelist.py` 后跑 `uv run python -m pytest tests/server_endpoints/test_mcp.py -v` 验证白名单一致性

**当前已排除的运维端点**（见 `server/mcp_whitelist.py` 的 `GATEWAY_EXCLUDE`）：
shutdown/health/config/llm_pool_*/activity_daily_*/user_message_*/inbox_batch/各类 form 端点/零调用状态端点

### 禁止反模式：用 exec_python 发 HTTP 调本地 8766 API

`exec_python` 发 urllib/requests 调本地 API 是**反模式**，原因：
- `/exec/python` 是 approval_required，**会触发用户审批**
- 绕过了 HTTP 端点的 safety 分类（read_only/safe/approval_required）
- advanced_tool 调 GET 类端点**免审批**

**例外**：截图+OCR 一步完成（`capture_screen` 返回 base64 不便走网关），见 AGENTS.md "截图+OCR 的正确流程"。

| 错误做法 | 正确做法 |
|---------|---------|
| `exec_python` 写 urllib 调 `GET /wip/{id}` | `mcp_localagent_wip_get(task_id="wip_xxx")`（直连，免审批） |
| `exec_python` 写 urllib 调 `GET /todos/{id}` | `localagent_advanced_tool(tool="todos_get", params={"todo_id": "todo_xxx"})` |
| `exec_python` 写 urllib 调 `POST /memory/search` | `localagent_advanced_tool(tool="memory_search", params={...})` |
| `exec_python` 写 urllib 调 `GET /apikey/history` | `localagent_advanced_tool(tool="apikey_history", params={...})` |

### Windows UTF-8 与项目环境规则

- 禁止通过 PowerShell here-string、管道或 `.bat %*` 向 `apply_patch`、Python、Node 传递包含中文的源码或测试代码。
- 修改源码优先使用 `exec_apply_patch(patch, cwd, dry_run)`；它直接调用 Codex 补丁入口，支持中文、多文件拆分、dry-run、修改文件清单和失败回滚。
- `exec_python` 默认使用 LocalAgent 自身 Python。执行其他项目代码时必须传 `cwd` + `environment="workspace_venv"`，或显式传 `python_path`；返回值会显示实际解释器和工作目录。
- `exec_python` 通过 stdin 管道将代码送入子解释器执行（不落临时文件），并把 `cwd` 注入 `sys.path/PYTHONPATH`，无需 PowerShell 转义。子进程 `__file__` 为 `<stdin>`，依赖 `__file__` 推路径的代码请改用 `exec_cmd` 跑 `python tools/xxx.py` 或 `exec_apply_patch` 写到具体位置。
- 请求 `shell="powershell"` 时优先使用 PowerShell 7 (`pwsh.exe`)，不可用才回退 Windows PowerShell；响应会返回实际 shell 路径、家族和版本。
- Windows PowerShell 5.1 不支持 `&&`；必须使用 `$LASTEXITCODE` 显式判断，或改用 PowerShell 7。
- 中文断言不要依赖终端显示编码；优先从 UTF-8 源文件读取期望值，必要时使用 Unicode 码点。路径测试优先使用 `pathlib` 或正斜杠。

> 三层 MCP 架构详解、工具排除清单、REST→MCP 映射规则详见 `docs/mcp-reference.md`。

## MCP 接入

后端统一提供 **Streamable HTTP** MCP 端点（`http://127.0.0.1:8766/mcp`），各客户端按自身支持情况选择直连或桥接：

### A. CatPaw / Trae（直连 Streamable HTTP）

两个 IDE 使用**相同的 MCP 配置 JSON**，只是操作入口不同：

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

| IDE | 操作入口 |
|-----|---------|
| **CatPaw** | 设置 > MCP > 手动添加，粘贴上述 JSON |
| **Trae** | 设置 > MCP > 添加 > 手动添加，粘贴上述 JSON |

这两个 IDE 均支持 STDIO、SSE、Streamable HTTP 三种 MCP 传输方式，直接走 Streamable HTTP 即可。

### B. ChatGPT 桌面客户端（STDIO 桥接）

ChatGPT 桌面客户端的"流式 HTTP"类型**强制要求 OAuth 流程**，本地 MCP 服务无法满足（后端日志会看到 `GET /.well-known/oauth-protected-resource` 404 探测，握手成功但客户端报"不可用"）。

解决方案：用 [tools/mcp_bridge.js](file:///<project_root>/tools/mcp_bridge.js) 把 STDIO 协议桥接到现有的 Streamable HTTP 端点（基于 `mcp-remote`）。**后端零改动**，原有的 Trae/CatPaw 直连配置不受影响。

前置条件：
1. 后端已启动（`start.bat`）
2. 本机已安装 Node.js 18+ 并在 PATH 中
3. 已全局安装 mcp-remote：`npm install -g mcp-remote`

ChatGPT 配置：

| 字段 | 值 |
|------|-----|
| **类型** | STDIO |
| **启动命令** | `node` |
| **参数** | `tools/mcp_bridge.js` |
| **工作目录** | 项目根目录（如 `<project_root>`） |
| 环境变量 | （可选）`LOCALAGENT_MCP_URL=http://127.0.0.1:8766/mcp`，自定义端点时使用 |

桥接脚本会自动按以下优先级查找 mcp-remote：
1. `npm root -g` 返回路径
2. `%APPDATA%\npm\node_modules\`
3. `%APPDATA%\TRAE SOLO CN\ModularData\ai-agent\vm\tools\node\node_modules\`（TRAE 自带 Node 路径）
4. `%LOCALAPPDATA%\npm\`、`%USERPROFILE%\.npm-global\`、`.volta` 等常见位置
5. 全部失败 → 回退用 `npx -y mcp-remote` 启动

验证桥接是否可用（终端手动运行，看到 `Proxy established successfully` 即正常，Ctrl+C 退出）：
```
node tools\mcp_bridge.js
```

### 通用排查

如连接失败，按顺序检查：
1. 后端是否运行：`curl.exe -s http://127.0.0.1:8766/health`
2. MCP 端点是否可达：浏览器访问 `http://127.0.0.1:8766/mcp` 应返回 406 或 MCP 响应（不是连接错误）
3. （仅 ChatGPT 桥接）`node --version` 应 ≥ 18；`npm ls -g mcp-remote` 应能看到包
4. 防火墙是否阻止 localhost 连接

> **平台迁移指南**：如需在 CatPaw/Trae/ChatGPT 之间切换或迁移到其他 IDE，请参阅 `.agents/<data_drive>:\Documents/platform-migration-guide.md`。

## 用户补充指令注入

用户可以在 agent 工作期间通过 HTTP 端点或 PySide6 GUI 发送补充指令，agent 会在下次调用非工作端点时收到指令。

### 工作原理

1. 用户通过 `POST /user/message` 发送文本指令，指令暂存在内存队列
2. 后端中间件在非工作端点的 JSON 响应中注入 `_user_supplement` 字段
3. MCP monkey-patch 在工具返回结果中追加 TextContent 块，标记为用户补充
4. agent 收到后可以继续当前任务，同时考虑用户的补充需求

### 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/user/message` | GET | 获取待发送指令列表 |
| `/user/message` | POST | 发送新指令 `{"text": "..."}` |
| `/user/message` | DELETE | 清空所有待发送指令 |
| `/user/message/{id}` | DELETE | 取消指定指令 |

### 排除的工作端点

以下端点不注入用户消息（脚本/系统调用，非 agent 直接使用）：

- `/llm/pool/*` - LLM 并发池代理（脚本调用）
- `/mcp/*` - MCP 协议端点（通过 monkey-patch 单独处理）
- `/user/*` - 用户消息端点自身（避免自反馈）
- `/static/*` `/output/*` `/docs` `/openapi.json` `/redoc` - 静态资源/文档

**设计原则**：用户消息的存在意义就是让 agent 收到，所以排除清单应最小化。工作端点（`/exec` `/ocr` `/vision` `/screen` `/browser` `/mindforge` 等）不排除——agent 在执行这些操作时也应能收到用户的补充指令。

### GUI 工具

- `tools/user_message_gui.py` - PySide6 界面，支持发送/取消指令
