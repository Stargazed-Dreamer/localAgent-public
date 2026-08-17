"""Jinja2 模板渲染器（spec-v2-compiler.md Solution 节"Jinja2 模板系统"）。

配置：autoescape=False / trim_blocks=True / lstrip_blocks=True / keep_trailing_newline=True
渲染 DEPLOYMENT.md / RELEASE_NOTES.md，模板变量不含 zip_size（user 方案 B）。
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .models import PreparedRelease

# 项目根目录（templates.py 在 tools/release/engine/，需 4 级 parent 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_TEMPLATES_DIR = _PROJECT_ROOT / "tools" / "release" / "templates"

# 模块级 Jinja2 Environment（spec Solution 节"Jinja2 配置"）
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,             # 生成 .md 不是 HTML
    trim_blocks=True,             # 去除块标签后的第一个换行
    lstrip_blocks=True,           # 去除块标签前的空白
    keep_trailing_newline=True,   # 保留模板末尾换行（.md 文件末尾换行）
    undefined=StrictUndefined,    # 未定义变量报错（防止悄悄渲染空值）
)


def _build_context(plan: PreparedRelease) -> dict:
    """构造模板渲染 context（不含 zip_size，spec Anti-Cheat 硬约束）"""
    # 组件信息：从 components tuple 构造 {name, version, description} 列表
    # components 是 tuple[str, ...]，但渲染时需要更多元信息
    # 这里简单构造，version/description 可在调用前从 manifest 加载注入
    components_info = []
    # plan 不直接存 component 元信息，调用方应通过 _ COMPONENTS_META 注入
    # 默认行为：仅用 component name 构造
    for name in plan.components:
        components_info.append({"name": name, "version": "0.0.0", "description": ""})

    # 文件统计
    file_count = len(plan.file_entries)
    total_size = sum(e.size for e in plan.file_entries)

    # 豁免信息
    exemptions_info = [{"path": e.path, "reason": e.reason} for e in plan.exemptions]

    return {
        "profile_id": plan.profile_id,
        "audience": plan.audience,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "components": components_info,
        "file_count": file_count,
        "total_size": total_size,
        "exemptions": exemptions_info,
        "created_at": plan.created_at,
        # ★ 故意不含 zip_size（user 方案 B，spec Anti-Cheat 硬约束）
    }


def render_deployment_md(plan: PreparedRelease, components_meta: dict | None = None) -> str:
    """渲染 DEPLOYMENT.md

    Args:
        plan: PreparedRelease 实例
        components_meta: 可选，{component_name: {version, description}} 注入更丰富的组件信息
    """
    ctx = _build_context(plan)
    if components_meta:
        for c in ctx["components"]:
            meta = components_meta.get(c["name"], {})
            c["version"] = meta.get("version", c["version"])
            c["description"] = meta.get("description", c["description"])
    template = _env.get_template("DEPLOYMENT.md.j2")
    return template.render(**ctx)


def render_release_notes_md(plan: PreparedRelease, components_meta: dict | None = None) -> str:
    """渲染 RELEASE_NOTES.md

    Args:
        plan: PreparedRelease 实例
        components_meta: 可选，{component_name: {version, description}} 注入更丰富的组件信息
    """
    ctx = _build_context(plan)
    if components_meta:
        for c in ctx["components"]:
            meta = components_meta.get(c["name"], {})
            c["version"] = meta.get("version", c["version"])
            c["description"] = meta.get("description", c["description"])
    template = _env.get_template("RELEASE_NOTES.md.j2")
    return template.render(**ctx)
