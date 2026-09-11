"""端点冒烟测试基建：遍历 OpenAPI 全部 GET 端点，断言不 500

动机（2026-08 逻辑 bug 审查结论）：本项目大量链路靠字符串弱耦合
（路由/manifest/白名单/GUIDE_REGISTRY），上游改动下游没跟是历史 bug 主病灶，
且端点尾部宽泛 except Exception 常把崩溃吃成 HTTP 200 的 {"error": ...}，
静默失效无告警。本测试作为提交前机械防线：任何 agent 改完代码跑一遍，
路由级断裂直接爆炸。

设计约束：
- 只打 GET（read_only，无副作用）——遵循 docs/dev-workflow.md「危险操作测试铁律」，
  盲打 POST 可能触发 shutdown/exec 等真实副作用。
- 排除清单 EXCLUDED_PREFIXES：需真实外部资源或与 TestClient 线程模型不兼容的端点。
- 路径/必填 query 参数按 OpenAPI schema 类型自动填 dummy 值（int→1, str→"test" 等），
  因此"资源不存在"类 404/400 属预期通过；只有处理程序崩溃（≥500）才判失败。
- 已知静默失效模式（HTTP 200 + {"error": ...}）用 warnings.warn 上报，
  不判失败——warnings 即当前存量清单，修复后可逐步升级为硬断言。

用法：
    uv run python -m pytest tests/server_endpoints/test_smoke_endpoints.py -v
扩展：
- 新增需排除的前缀 → 加进 EXCLUDED_PREFIXES 并写明原因；
- 单独豁免某条路径 → 加进 PATH_EXEMPTIONS（如某 GET 首调触发重资源加载）。
"""

from __future__ import annotations

import warnings

import pytest

# 排除前缀：这些面需要真实外部资源，或与 TestClient 的 anyio 线程池不兼容。
# 每条必须写明原因，避免后人误删后踩坑。
EXCLUDED_PREFIXES: tuple[str, ...] = (
    # UIA COM 与 TestClient 线程池不兼容：ControlFromHandle 在 anyio 工作线程中
    # 触发 Windows fatal exception（access violation），整进程崩溃。
    # 详见 tests/conftest.py real_client fixture 注释——screen 端点须走 real_backend。
    "/screen",
    # 需要 CDP :9222 真实调试浏览器运行中
    "/browser",
    # PaddleOCR 模型生命周期（GPU/重资源加载）
    "/ocr",
    # VL 视觉模型
    "/vision",
    # ModelManager 模型控制面（load/unload 重资源；list 本身无害但同面前缀一并排除）
    "/models",
)

# 单独豁免的具体路径（前缀排除的例外或已知问题路径）。当前为空，留作扩展点。
PATH_EXEMPTIONS: frozenset[str] = frozenset()

# 单个请求超时（秒）：防止意外挂起拖死整个测试进程
REQUEST_TIMEOUT = 30


def _dummy_value(param_schema: dict) -> object:
    """按 OpenAPI 参数 schema 生成 dummy 值。"""
    if param_schema.get("enum"):
        return param_schema["enum"][0]
    t = param_schema.get("type")
    fmt = param_schema.get("format")
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object":
        return {}
    # string 及无 type 兜底
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "date":
        return "2026-01-01"
    return "test"


def _collect_get_targets(spec: dict) -> list[tuple[str, str]]:
    """从 OpenAPI spec 收集冒烟目标 [(path, operation_id)]。

    条件：GET 方法、不在排除前缀下、不在显式豁免单。
    """
    targets: list[tuple[str, str]] = []
    for path, methods in sorted((spec.get("paths") or {}).items()):
        op = methods.get("get")
        if not isinstance(op, dict):
            continue
        if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        if path in PATH_EXEMPTIONS:
            continue
        op_id = op.get("operationId") or path
        targets.append((path, op_id))
    return targets


def _build_url(path: str, parameters: list[dict]) -> tuple[str, dict]:
    """填充路径参数、收集必填 query 参数，返回 (url, params)。"""
    url = path
    query: dict[str, object] = {}
    for p in parameters or []:
        if not p.get("required", False):
            continue  # 可选参数不填，减少触发副作用的面积
        schema = p.get("schema") or {}
        val = _dummy_value(schema)
        if p.get("in") == "path":
            url = url.replace("{" + p["name"] + "}", str(val))
        elif p.get("in") == "query":
            query[p["name"]] = val
    return url, query


def test_smoke_all_get_endpoints(client, app):
    """遍历全部可冒烟 GET 端点：断言状态码 < 500。

    - ≥500：硬失败（路由级断裂/处理程序崩溃，正是本基建要抓的回归）
    - 200 但 body 为 {"error": ...}：warnings.warn 上报（已知静默失效模式，暂不判失败）
    """
    spec = app.openapi()
    targets = _collect_get_targets(spec)
    # 排除面应保持有界（当前约 10 条上下），若突然暴涨说明有人加了新的重资源前缀，
    # 需要人工确认排除理由而不是默默吞掉
    all_get_count = sum(
        1
        for methods in (spec.get("paths") or {}).values()
        if isinstance(methods, dict) and "get" in methods
    )
    skipped = all_get_count - len(targets)
    summary = f"smoke 覆盖 {len(targets)} 个 GET 端点（另排除 {skipped} 个需外部资源的 GET）"

    failures: list[str] = []
    print(f"\n[smoke] {summary}")
    print("[smoke] 排除前缀: " + ", ".join(EXCLUDED_PREFIXES))
    print("[smoke] 目标: " + ", ".join(op_id for _, op_id in targets))

    for path, op_id in targets:
        operation = spec["paths"][path]["get"]
        url, query = _build_url(path, operation.get("parameters") or [])
        try:
            resp = client.get(url, params=query or None, timeout=REQUEST_TIMEOUT)
        except Exception as e:  # noqa: BLE001 —— 连接层异常也视为该端点失败
            failures.append(f"{op_id}  GET {url} -> 请求异常: {type(e).__name__}: {e}")
            continue

        if resp.status_code >= 500:
            body = resp.text[:300].replace("\n", " ")
            failures.append(f"{op_id}  GET {url} -> HTTP {resp.status_code}: {body}")
        elif resp.status_code == 200:
            # 静默失效模式探测：HTTP 200 但 payload 自报错误
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("error"):
                warnings.warn(
                    f"[smoke][silent-error] {op_id} GET {url} -> 200 但 "
                    f"body.error={str(data['error'])[:200]!r}"
                    "（属已知静默失效模式，见 docs/dev-workflow.md「提交前机械防线」）",
                    stacklevel=1,
                )

    assert not failures, (
        f"{summary}\n以下端点冒烟失败（HTTP ≥500 或请求异常）：\n" + "\n".join(failures)
    )


def test_smoke_target_inventory_sanity(app):
    """元测试：确保收集逻辑没有悄悄失效（目标数为 0 时本套件形同虚设）。"""
    targets = _collect_get_targets(app.openapi())
    assert len(targets) >= 20, (
        f"smoke 目标仅 {len(targets)} 个，远低于预期（>20）。"
        "可能是 OpenAPI 结构变更或排除清单误杀，请人工检查 _collect_get_targets。"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"type": "integer"}, 1),
        ({"type": "string", "enum": ["a", "b"]}, "a"),
        ({"type": "string", "format": "date-time"}, "2026-01-01T00:00:00Z"),
        ({"type": "boolean"}, False),
        ({}, "test"),
    ],
)
def test_dummy_value_filling(value, expected):
    assert _dummy_value(value) == expected


def test_build_url_fills_path_and_required_query():
    url, query = _build_url(
        "/memory/search/traces/{trace_id}",
        [
            {"name": "trace_id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}},
            {"name": "verbose", "in": "query", "required": False, "schema": {"type": "boolean"}},
        ],
    )
    assert url == "/memory/search/traces/test"
    assert query == {"limit": 1}  # 可选参数不应被填入
