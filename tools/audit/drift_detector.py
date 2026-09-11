"""漂移对账脚本（logic bug 审查 第2层）

检测项目中的"字符串弱耦合断链"：
  A. 运行时 OpenAPI 真源（import server.main）
  B. client/workspace 发出的 HTTP 调用 vs 服务端实际路由（前后端没对接）
  C. tools_manifest.json 的 script/command 路径存在性
  D. GUIDE_REGISTRY 条目：skill_file 存在性、manifest [skill].task_type 一致性、mcp_tools_priority 工具名有效性
  E. mcp_whitelist 的 DIRECT_TOOLS/GATEWAY_EXCLUDE/TOOL_ANNOTATIONS ⊆ OpenAPI operationIds
  F. config.example.toml ↔ data/config_descriptions.json 双向对账
  G. workspace/*/manifest.toml 声明的 file/entries_var/class/glob 存在性
  H. docs/api-reference.md ↔ OpenAPI 路由双向对账

用法: uv run python tools/audit/drift_detector.py
输出: 控制台摘要 + temp/audit/drift_report.md（可再生产物，故仍写 temp/）
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

REPORT_LINES: list[str] = []
FINDINGS: dict[str, list[str]] = {}


def emit(section: str, severity: str, msg: str) -> None:
    FINDINGS.setdefault(section, []).append(f"[{severity}] {msg}")
    REPORT_LINES.append(f"- **[{severity}]** {msg}")


# ---------------------------------------------------------------- A. OpenAPI 真源
def load_openapi() -> tuple[dict, set[tuple[str, str]], set[str]]:
    """返回 (openapi_schema, {(METHOD, path)}, {operation_id})。失败时抛异常。"""
    from server.main import app  # noqa: PLC0415  导入即注册全部路由，uvicorn 仅在 __main__ 启动

    schema = app.openapi()
    routes: set[tuple[str, str]] = set()
    opids: set[str] = set()
    for path, methods in schema.get("paths", {}).items():
        for method, op in methods.items():
            if method.startswith("/") or method == "parameters":
                continue
            routes.add((method.upper(), path))
            oid = op.get("operationId")
            if oid:
                opids.add(oid)
    return schema, routes, opids


# ---------------------------------------------------------------- B. 客户端调用 vs 路由
HTTP_CALL_RE = re.compile(
    r'(?:_http|http|http_client)\s*\.\s*'
    r'(get|post|put|patch|delete)\(\s*(f?)"(/[^"]*)"',
    re.IGNORECASE,
)


def norm_path(p: str) -> str:
    """把 {expr} / {param} 统一为 {*}，忽略尾斜杠差异。"""
    p = re.sub(r"\{[^}]*\}", "{}", p)
    return p.rstrip("/")


def extract_client_calls() -> list[tuple[Path, int, str, str]]:
    calls: list[tuple[Path, int, str, str]] = []
    scan_dirs = [ROOT / "client", ROOT / "workspace"]
    for base in scan_dirs:
        for py in base.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                m = HTTP_CALL_RE.search(line)
                if not m:
                    continue
                method, raw_path = m.group(1).lower(), m.group(3)
                # 带 base_url= 覆盖的调用（如 FAKE_PROXY_URL）不是打本服务端的，跳过
                if "base_url" in line:
                    continue
                calls.append((py, i, method, norm_path(raw_path)))
    return calls


def check_client_routes(routes: set[tuple[str, str]]) -> None:
    path_index: dict[str, set[str]] = {}
    for method, path in routes:
        path_index.setdefault(norm_path(path), set()).add(method)

    calls = extract_client_calls()
    REPORT_LINES.append(f"\n### B. 客户端 HTTP 调用 vs 服务端路由（共扫到 {len(calls)} 处）\n")
    for py, lineno, method, npath in calls:
        rel = py.relative_to(ROOT)
        if npath not in path_index:
            emit("B", "HIGH", f"{rel}:{lineno} 调用 {method.upper()} \"{npath}\" —— 服务端不存在该路径")
        elif method.upper() not in path_index[npath]:
            allowed = "/".join(sorted(path_index[npath]))
            emit("B", "HIGH", f"{rel}:{lineno} 方法不匹配：客户端 {method.upper()} \"{npath}\"，服务端只支持 {allowed}")
    if not any(x.startswith("[HIGH]") for x in FINDINGS.get("B", [])):
        REPORT_LINES.append("- 无断链")


# ---------------------------------------------------------------- C. tools_manifest.json
def check_tools_manifest() -> None:
    mf = ROOT / "tools_manifest.json"
    if not mf.exists():
        emit("C", "MEDIUM", "tools_manifest.json 不存在")
        return
    data = json.loads(mf.read_text(encoding="utf-8"))
    REPORT_LINES.append(f"\n### C. tools_manifest.json（{len(data.get('tools', []))} 个工具）\n")
    for tool in data.get("tools", []):
        tid = tool.get("id", "?")
        script = tool.get("script")
        if script and not (ROOT / script).exists():
            emit("C", "HIGH", f"工具 {tid}: script 不存在 → {script}")
        cmd = tool.get("command") or ""
        # command 形如 ".venv\\Scripts\\python.exe tools/xxx.py"，提取其后的脚本路径
        parts = cmd.split()
        for seg in parts:
            if seg.endswith(".py") and not seg.startswith("-"):
                if not (ROOT / seg.replace("\\", "/")).exists():
                    emit("C", "HIGH", f"工具 {tid}: command 引用脚本不存在 → {seg}")
                break


# ---------------------------------------------------------------- D. GUIDE_REGISTRY
def import_module_from_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        emit("D", "HIGH", f"模块加载失败 {path.relative_to(ROOT)}: {e!r}")
        return None
    return mod


def check_guide_registry(opids: set[str]) -> dict[str, dict]:
    from server.agent_guide_data import GUIDE_REGISTRY  # noqa: PLC0415

    entries_all = dict(GUIDE_REGISTRY)
    ws_dir = ROOT / "workspace"
    manifest_task_types: dict[str, tuple[Path, Path]] = {}  # task_type -> (manifest路径, skill文件路径)
    manifests = sorted(ws_dir.glob("*/manifest.toml"))
    for mf in manifests:
        try:
            conf = tomllib.loads(mf.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            emit("D", "HIGH", f"{mf.relative_to(ROOT)} 解析失败: {e}")
            continue
        skill = conf.get("skill") or {}
        comp_dir = mf.parent
        tt = skill.get("task_type")
        if tt:
            manifest_task_types[tt] = (mf, comp_dir / skill.get("file", "SKILL.md"))

    REPORT_LINES.append("\n### D. GUIDE_REGISTRY 条目一致性\n")

    # workspace 组件的 GUIDE_REGISTRY_ENTRIES
    ws_keys: dict[str, list[str]] = {}
    for mf in manifests:
        conf = tomllib.loads(mf.read_text(encoding="utf-8"))
        ag = conf.get("agent_guide") or {}
        if not ag:
            continue
        mod_path = mf.parent / ag.get("file", "loop_actions.py")
        var_name = ag.get("entries_var", "GUIDE_REGISTRY_ENTRIES")
        mod = import_module_from_path(f"ws_{mf.parent.name}", mod_path)
        entries = getattr(mod, var_name, None) if mod else None
        if entries is None:
            emit("D", "HIGH", f"{mf.parent.name}: [agent_guide] {var_name} 在 {mod_path.name} 中不存在或不可加载")
            continue
        ws_keys[mf.parent.name] = list(entries.keys())
        entries_all.update(entries)

    # skill_file 存在性
    for tt, entry in sorted(entries_all.items()):
        sf = entry.get("skill_file")
        if sf and not (ROOT / sf).exists():
            emit("D", "HIGH", f"条目 {tt}: skill_file 不存在 → {sf}")

    # manifest [skill].task_type ↔ 组件条目 key
    for tt, (mf, skill_file) in manifest_task_types.items():
        comp = mf.parent.name
        if comp not in ws_keys:
            continue
        if tt not in ws_keys[comp]:
            emit("D", "HIGH", f"{comp}: manifest [skill].task_type={tt!r} 与 GUIDE_REGISTRY_ENTRIES keys {ws_keys[comp]} 不一致")
        if not skill_file.exists():
            emit("D", "MEDIUM", f"{comp}: [skill].file 不存在 → {skill_file.relative_to(ROOT)}")

    # mcp_tools_priority 引用的工具名有效性
    known_tools = set(opids)
    tool_re = re.compile(r"^([a-z_][a-z0-9_]*)\s*\(")
    unknown_refs: set[str] = set()
    for tt, entry in sorted(entries_all.items()):
        for ref in entry.get("mcp_tools_priority", []) or []:
            m = tool_re.match(ref.strip())
            if m and m.group(1) not in known_tools:
                if m.group(1) not in {"localagent_advanced_tool", "localagent_template_tool"}:
                    unknown_refs.add(f"{tt}: {m.group(1)}")
    for u in sorted(unknown_refs):
        emit("D", "LOW", f"mcp_tools_priority 引用了未知工具名（可能已改名/删除）→ {u}")
    return entries_all


# ---------------------------------------------------------------- E. mcp_whitelist
def check_mcp_whitelist(opids: set[str]) -> None:
    from server.mcp_whitelist import DIRECT_TOOLS, GATEWAY_EXCLUDE, TOOL_ANNOTATIONS  # noqa: PLC0415

    REPORT_LINES.append("\n### E. mcp_whitelist ⊆ OpenAPI operationIds\n")
    for name, group in (("DIRECT_TOOLS", DIRECT_TOOLS), ("GATEWAY_EXCLUDE", GATEWAY_EXCLUDE), ("TOOL_ANNOTATIONS", TOOL_ANNOTATIONS.keys())):
        missing = sorted(set(group) - opids)
        for m in missing:
            emit("E", "HIGH", f"{name} 中的 {m!r} 不在 OpenAPI operationIds 里（接口改名/删除后白名单没跟）")


# ---------------------------------------------------------------- F. config 对账
def flatten_toml(prefix: str, node: dict, out: set) -> None:
    for k, v in node.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.add(key)
            flatten_toml(key, v, out)
        else:
            out.add(key)


def check_config() -> None:
    ex = ROOT / "config.example.toml"
    dj = ROOT / "data" / "config_descriptions.json"
    if not ex.exists() or not dj.exists():
        emit("F", "MEDIUM", "config.example.toml 或 data/config_descriptions.json 缺失")
        return
    toml_data = tomllib.loads(ex.read_text(encoding="utf-8"))
    toml_paths: set[str] = set()
    flatten_toml("", toml_data, toml_paths)
    desc = json.loads(dj.read_text(encoding="utf-8"))
    desc_keys = {k for k in desc if not k.startswith("_")}

    def desc_covers(key: str) -> bool:
        if key in desc_keys:
            return True
        parts = key.split(".")
        # 逐级把某一段换成 *（如 llm.models.*.provider 覆盖 llm.models.foo.provider）
        for i in range(len(parts)):
            wild = ".".join(parts[:i] + ["*"] + parts[i + 1 :])
            if wild in desc_keys:
                return True
        # 连续多级折叠为单个 *（如 llm.models.* 覆盖 llm.models.foo.bar）
        for i in range(len(parts)):
            for j in range(i + 2, len(parts)):
                wild = ".".join(parts[:i] + ["*"] + parts[j:])
                if wild in desc_keys:
                    return True
        return False

    REPORT_LINES.append("\n### F. config.example.toml ↔ config_descriptions.json\n")
    uncovered = sorted(k for k in toml_paths if not desc_covers(k))
    for k in uncovered:
        emit("F", "LOW", f"config 键缺少说明（Settings 面板会显示空说明）→ {k}")
    stale = sorted(k for k in desc_keys if k not in toml_paths and "*" not in k)
    for k in stale:
        emit("F", "MEDIUM", f"description 指向不存在的 config 键（配置项已删但说明没删）→ {k}")


# ---------------------------------------------------------------- G. workspace manifests
def check_workspace_manifests(entries_by_comp: dict[str, list[str]]) -> None:
    REPORT_LINES.append("\n### G. workspace/*/manifest.toml 声明存在性\n")
    for mf in sorted((ROOT / "workspace").glob("*/manifest.toml")):
        conf = tomllib.loads(mf.read_text(encoding="utf-8"))
        comp = mf.parent.name

        def resolve(fname: str) -> Path:
            return mf.parent / fname

        cp = conf.get("client_panel") or {}
        if cp:
            f = cp.get("file")
            cls = cp.get("class")
            if f and not resolve(f).exists():
                emit("G", "HIGH", f"{comp}: [client_panel].file 不存在 → {f}")
            if f and cls:
                text = resolve(f).read_text(encoding="utf-8", errors="replace")
                if not re.search(rf"^class\s+{cls}\b", text, re.MULTILINE):
                    emit("G", "HIGH", f"{comp}: [client_panel].class {cls!r} 未在 {f} 中定义")

        lt = conf.get("loop_tasks") or {}
        if lt:
            mod = import_module_from_path(f"g_{comp}", resolve(lt.get("file", "")))
            var = lt.get("entries_var", "LOOP_TASK_DEFS")
            if mod is not None and not hasattr(mod, var):
                emit("G", "HIGH", f"{comp}: [loop_tasks].entries_var {var!r} 不在 {lt.get('file')} 中")

        exports = ((conf.get("exports") or {}).get("source") or {}).get("files") or []
        for pat in exports:
            hits = list(ROOT.glob(pat)) if not pat.startswith("workspace/") else list(ROOT.glob(pat))
            if not hits:
                emit("G", "MEDIUM", f"{comp}: [exports.source] glob 未命中任何文件 → {pat}")


# ---------------------------------------------------------------- H. api-reference.md
DOC_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([A-Z/]+)\s*\|")


def check_api_docs(routes: set[tuple[str, str]]) -> None:
    doc = ROOT / "docs" / "api-reference.md"
    if not doc.exists():
        emit("H", "MEDIUM", "docs/api-reference.md 不存在")
        return
    doc_routes: set[tuple[str, str]] = set()
    for line in doc.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.replace("\\|", "¦")  # 表格转义的 | 先还原成占位符
        m = DOC_ROW_RE.match(line)
        if not m:
            continue
        path, methods = m.group(1).replace("¦", "|"), m.group(2)
        for mm in methods.split("/"):
            doc_routes.add((mm.strip().upper(), path))

    REPORT_LINES.append("\n### H. docs/api-reference.md ↔ 实际路由\n")
    idx: dict[str, set[str]] = {}
    for m_, p_ in routes:
        idx.setdefault(norm_path(p_), set()).add(m_)
    doc_norm = {norm_path(dp) for _, dp in doc_routes}
    for m, p in sorted(doc_routes):
        np_ = norm_path(p)
        if np_ not in idx:
            emit("H", "MEDIUM", f"文档中的端点服务端已不存在（幽灵文档）→ {m} {p}")
        elif m not in idx[np_]:
            emit("H", "MEDIUM", f"文档方法与实现不符 → 文档 {m} {p}，实现 {'/'.join(sorted(idx[np_]))}")
    impl_only = sorted(f"{m} {p}" for m, p in routes - doc_routes if norm_path(p) not in doc_norm)
    REPORT_LINES.append(f"- 实现了但文档未收录的端点 {len(impl_only)} 个（信息级，不逐条列为 bug）")


# ---------------------------------------------------------------- I. config.py 读取函数 ↔ example.toml
SECTION_VAR_RE = re.compile(r'(\w+)\s*=\s*config\.get\(\s*"([^"]+)"')
CHAINED_RE = re.compile(r'config\.get\(\s*"([^"]+)"[^)]*\)\s*\.\s*get\(\s*"([^"]+)"')
VAR_GET_RE = re.compile(r'\b(\w+)\.get\(\s*"([^"]+)"')


def extract_config_reads() -> set[tuple[str, str]]:
    """从 server/config.py 源码提取 get_*_config 函数实际读取的 (段, 键) 集合。

    识别两种模式：
    1. 变量中转：browser = config.get("browser", {}) → 后续 browser.get("key")
    2. 链式直读：config.get("server", {}).get("port")
    只对账字符串字面量键；动态拼接键（f-string/变量）无法静态解析，跳过。
    """
    src = (ROOT / "server" / "config.py").read_text(encoding="utf-8")
    reads: set[tuple[str, str]] = set()
    # 链式直读
    for sec, key in CHAINED_RE.findall(src):
        reads.add((sec, key))
    # 变量中转：先建立 变量→段名 映射（同一函数内变量名可能复用不同段，
    # 简化为全局映射——config.py 中同名变量跨函数指向同段是既成事实）
    var2sec: dict[str, str] = {}
    for var, sec in SECTION_VAR_RE.findall(src):
        var2sec.setdefault(var, sec)
    for var, key in VAR_GET_RE.findall(src):
        if var in var2sec:
            reads.add((var2sec[var], key))
    return reads


def check_config_code_reads() -> None:
    """I 节：代码实际读取的 config 键 vs config.example.toml 双向对账。

    补 F 节缺失的第三条腿：example 有键但代码从不读取（僵尸配置），
    以及代码读取但 example 缺失（仅靠默认值运行，Settings 面板不可见）。
    """
    REPORT_LINES.append("\n### I. server/config.py 读取函数 ↔ config.example.toml\n")
    code_reads = extract_config_reads()
    ex = ROOT / "config.example.toml"
    toml_data = tomllib.loads(ex.read_text(encoding="utf-8"))
    toml_paths: set[str] = set()
    flatten_toml("", toml_data, toml_paths)
    read_secs = {sec for sec, _ in code_reads}

    # 方向1：代码读取但 example 没有 → 仅靠默认值运行
    for sec, key in sorted(code_reads):
        if f"{sec}.{key}" not in toml_paths:
            emit(
                "I",
                "LOW",
                f"代码读取 [{sec}].{key} 但 config.example.toml 无此键"
                "（仅靠默认值运行，Settings 面板不可见）",
            )

    # 方向2：example 叶子键不被代码字面量读取 → 僵尸配置或动态/其他方式消费
    for sec, body in toml_data.items():
        if not isinstance(body, dict):
            continue
        for k, v in body.items():
            if isinstance(v, dict):
                continue  # 嵌套表不是叶子配置键；动态消费段整层跳过避免误报
            if sec in read_secs and (sec, k) not in code_reads:
                emit(
                    "I",
                    "MEDIUM",
                    f"config.example.toml 定义 [{sec}].{k} 但 server/config.py 无字面量读取"
                    "（僵尸配置或被其他模块直接 load_config 消费，需人工确认）",
                )


def main() -> None:
    print("== 加载运行时 OpenAPI（import server.main）...")
    try:
        _schema, routes, opids = load_openapi()
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: OpenAPI 构建失败（这本身就是一个高优先级发现）: {e!r}")
        sys.exit(2)
    print(f"   路由 {len(routes)} 条, operationId {len(opids)} 个")

    REPORT_LINES.append("# 漂移对账报告\n")
    REPORT_LINES.append("## 发现明细\n")

    check_client_routes(routes)
    check_tools_manifest()
    entries = check_guide_registry(opids)
    check_mcp_whitelist(opids)
    check_config()
    check_config_code_reads()
    entries_by_comp: dict[str, list[str]] = {}
    check_workspace_manifests(entries_by_comp)
    check_api_docs(routes)

    # 汇总
    total = sum(len(v) for v in FINDINGS.values())
    sev_high = sum(1 for v in FINDINGS.values() for x in v if "[HIGH]" in x)
    summary = [
        "\n## 汇总\n",
        f"- 总计 {total} 条发现（HIGH {sev_high} / MEDIUM "
        f"{sum(1 for v in FINDINGS.values() for x in v if '[MEDIUM]' in x)} / LOW "
        f"{sum(1 for v in FINDINGS.values() for x in v if '[LOW]' in x)}）",
        "- 各节：" + ", ".join(f"{k}={len(v)}" for k, v in FINDINGS.items()),
    ]
    out = ROOT / "temp" / "audit" / "drift_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(REPORT_LINES + summary), encoding="utf-8")
    print("\n".join(summary))
    print(f"\n完整报告: {out}")


if __name__ == "__main__":
    main()
