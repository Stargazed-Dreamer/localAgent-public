"""GUI Feature Map 生成器（P2）：AST 解析 client/panels/*.py → data/feature_map.json。

目的：给智能体一份"自己 GUI 的地图"——每个面板从用户视角怎么进、关键控件
objectName、连接的信号槽、调用的后端 API——改 GUI 代码后可据此自查/验证。

提取（纯 AST，不 import client/PySide6，无运行时依赖）：
  - PanelBase 子类 + PANEL_META(id/title/icon/order/category/requires_backend)
  - 模块 docstring（面板功能摘要）
  - setObjectName("...") 常量调用 → object_names
  - self._xxx.connect(...) → connected_attrs
  - 形如 "/activity/daily" 的 URL 字面量 → api_endpoints

回填保护（重跑不丢手写）：entry / user_perspective / verify_howto 三个手写
字段从现有 JSON 原样保留；缺失的面板在输出末尾列出"待回填清单"。

用法：uv run python tools/gen_feature_map.py
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PANELS_DIR = PROJECT_ROOT / "client" / "panels"
OUTPUT = PROJECT_ROOT / "data" / "feature_map.json"

# 手写字段（生成器永不覆盖）
HANDWRITTEN_FIELDS = ("entry", "user_perspective", "verify_howto")

META_FIELDS = ("id", "title", "icon", "order", "category", "requires_backend")

RE_URL_LIKE = "/"


def _literal(node: ast.AST):
    """安全取常量值（含前缀负号）。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        return -node.operand.value
    return None


def _extract_meta(cls_node: ast.ClassDef) -> dict:
    meta = {}
    for node in cls_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PANEL_META":
                    if isinstance(node.value, ast.Call):
                        for kw in node.value.keywords:
                            if kw.arg in META_FIELDS:
                                meta[kw.arg] = _literal(kw.value)
    return meta


def _walk_class_and_module(tree: ast.AST) -> tuple[list[str], set[str], set[str], list[str]]:
    """返回 (panel_base 子类名, object_names, connected_attrs, api_endpoints)。"""
    classes: list[str] = []
    object_names: set[str] = set()
    connected: set[str] = set()
    endpoints: list[str] = []

    for node in ast.walk(tree):
        # 类收集：基类名含 PanelBase
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else (
                    base.attr if isinstance(base, ast.Attribute) else None
                )
                if name and "PanelBase" in name:
                    classes.append(node.name)
        # setObjectName("x")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setObjectName" and node.args:
                val = _literal(node.args[0])
                if isinstance(val, str):
                    object_names.add(val)
            # self._xxx.connect(...)
            if node.func.attr == "connect" and isinstance(node.func.value, ast.Attribute):
                connected.add(node.func.value.attr)
        # URL 字面量（以 / 开头的字符串常量，排除纯格式串）
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v.startswith("/") and len(v) > 1 and " " not in v and "\n" not in v:
                endpoints.append(v)

    return classes, object_names, connected, sorted(set(endpoints))


def extract_panel(py_file: Path) -> dict | None:
    source = py_file.read_bytes().decode("utf-8-sig", errors="replace")
    tree = ast.parse(source)
    doc = ast.get_docstring(tree) or ""
    # docstring 只取首段（到第一个空行），避免整段塞进 JSON
    summary = doc.split("\n\n")[0].strip() if doc else ""

    classes, object_names, connected, endpoints = _walk_class_and_module(tree)
    meta = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in classes:
            meta = _extract_meta(node)
            break

    return {
        "class": classes[0] if classes else None,
        "bases": ["PanelBase"] if classes else [],
        "meta": meta,
        "doc": summary,
        "object_names": sorted(object_names),
        "connected_attrs": sorted(connected),
        "api_endpoints": endpoints,
    }


def main() -> int:
    existing: dict = {}
    if OUTPUT.exists():
        try:
            existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"[warn] 现有 {OUTPUT.name} 解析失败，手写字段将丢失")

    old_panels = existing.get("panels", {})
    new_panels: dict = {}
    missing_handwritten: list[str] = []

    for py_file in sorted(PANELS_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        info = extract_panel(py_file)
        if info is None:
            continue
        old = old_panels.get(py_file.name, {})
        # 回填保护：手写字段原样保留
        for field in HANDWRITTEN_FIELDS:
            info[field] = old.get(field, "")
        if any(not info[f] for f in HANDWRITTEN_FIELDS):
            missing = [f for f in HANDWRITTEN_FIELDS if not info[f]]
            missing_handwritten.append(f"{py_file.name}: 缺 {', '.join(missing)}")
        new_panels[py_file.name] = info

    result = {
        "_meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "generator": "tools/gen_feature_map.py（重跑不覆盖 entry/user_perspective/verify_howto 手写字段）",
            "purpose": "GUI feature map：改 client GUI 后智能体据此自查面板入口/控件/验证方式",
        },
        "panels": new_panels,
    }
    OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"[gen_feature_map] 写出 {len(new_panels)} 个面板 → {OUTPUT.relative_to(PROJECT_ROOT)}")
    if missing_handwritten:
        print("待回填清单（手写字段缺失）：")
        for m in missing_handwritten:
            print(f"  {m}")
    else:
        print("手写字段全部已回填")
    return 0


if __name__ == "__main__":
    sys.exit(main())
