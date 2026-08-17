"""Skill 一致性校验脚本。

检查三处真源是否同步（仅适用于核心 skill）：
  1. 文件系统：.agents/skills/ 下的 SKILL.md 和扁平 .md（排除 _vendor/_deprecated）
  2. _index.md：人工维护的 skill 索引（核心 skill 段）
  3. GUIDE_REGISTRY：server/agent_guide.py 中手写的路由元数据

检查项（核心 skill）：
  - frontmatter 完整性：每个 skill 文件必须有 name + description + task_type
  - _index.md 与文件系统一致：每个 skill 文件在 _index.md 中有对应条目
  - GUIDE_REGISTRY 与文件系统一致：每个 skill 文件的 task_type 在 REGISTRY 中有对应条目
  - task_type 唯一性：同一 task_type 不能对应多个文件

组件化 skill（manifest 声明的，位于 workspace/<module>/SKILL.md）不参与"三处真源"校验：
  - 文件系统：不位于 .agents/skills/，由 manifest 真源管理
  - _index.md：由 tools/generate_index.py 程序化生成"组件化模块"段，格式与核心段不同
  - GUIDE_REGISTRY：由 _load_optional_guide_entries() 运行时合并，源文件不含硬编码条目
  本脚本对组件化 skill 单独做 frontmatter 完整性校验，不报"三处真源"差异。

用法：
    uv run python -m tools.check_skills          # 退出码 0=通过，1=有警告，2=有错误
    uv run python -m tools.check_skills --json   # JSON 输出（CI 友好）
"""
from __future__ import annotations

import re
import sys
import json
import argparse
from pathlib import Path

# 项目根目录（脚本位于 tools/check_skills.py）
ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".agents" / "skills"
INDEX_FILE = SKILLS_DIR / "_index.md"
REGISTRY_FILE = ROOT / "server" / "agent_guide.py"

# 排除的目录（vendor 副本、弃用占位）
EXCLUDE_DIRS = {"_vendor", "_deprecated"}


# ========== frontmatter 解析 ==========

_FRONTMATTER_RE = re.compile(r"^\ufeff*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _strip_quotes(s: str) -> str:
    """去除 YAML 值首尾的引号（单引号或双引号）。"""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def parse_frontmatter(content: str) -> dict | None:
    """解析 YAML frontmatter（轻量级，不依赖 PyYAML）。

    返回 dict（可能为空）或 None（无 frontmatter）。
    只提取 name、description、task_type 三个字段，description 支持多行（> 或 |）。
    容忍文件开头多个 UTF-8 BOM（\ufeff，某些历史文件有重复 BOM）。
    单行值会去除首尾引号。
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None
    body = m.group(1)
    result: dict = {}
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        # 跳过空行和注释
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        # key: value 形式
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = _strip_quotes(value.strip())
            # 多行值（> 或 |）
            if value in (">", "|"):
                multiline: list[str] = []
                i += 1
                while i < len(lines) and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                    multiline.append(lines[i].strip())
                    i += 1
                result[key] = " ".join(multiline)
                continue
            # 单行值
            result[key] = value
        i += 1
    return result


# ========== 扫描文件系统 ==========

def scan_skill_files() -> list[dict]:
    """扫描 .agents/skills/ 下所有 skill 文件（排除 _vendor/_deprecated）。

    返回 list of dict，每个 dict 含 path, rel_path, frontmatter, has_frontmatter。
    同时收集 SKILL.md（子目录式）和 .md（扁平式，排除 _index.md）。
    """
    skills: list[dict] = []
    for md_file in SKILLS_DIR.rglob("*.md"):
        # 排除 _vendor 和 _deprecated
        if any(part in EXCLUDE_DIRS for part in md_file.parts):
            continue
        # 排除 _index.md
        if md_file.name == "_index.md":
            continue
        # 排除 references/ 下的参考文档（不是 skill 文件）
        if "references" in md_file.parts:
            continue
        # 排除 bucket README
        if md_file.name == "README.md":
            continue
        # 排除 sites/ 下的网站经验文档（browser_lessons/sites/）
        if "sites" in md_file.parts:
            continue
        # 排除 _template.md
        if md_file.name == "_template.md":
            continue

        # skill 文件判定规则：
        #   1. 子目录式：文件名为 SKILL.md（如 browser_lessons/SKILL.md、dev/tdd/SKILL.md）
        #   2. 变体式：文件名为 SKILL_*.md（如 office_docs/SKILL_docx.md、SKILL_xlsx.md）
        #   3. 扁平式：直接在 .agents/skills/ 根目录下的 .md 文件（如 accounting.md）
        # 其他 .md 文件（如 dev/tdd/mocking.md、daily/teach/MISSION-FORMAT.md）是参考文档，不是 skill
        is_root_flat = md_file.parent == SKILLS_DIR
        is_skill_md = md_file.name == "SKILL.md"
        is_skill_variant = md_file.name.startswith("SKILL_") and md_file.name.endswith(".md")
        if not (is_root_flat or is_skill_md or is_skill_variant):
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = parse_frontmatter(content)
        rel = md_file.relative_to(ROOT).as_posix()
        skills.append({
            "path": str(md_file),
            "rel_path": rel,
            "name": fm.get("name") if fm else None,
            "task_type": fm.get("task_type") if fm else None,
            "has_frontmatter": fm is not None,
            "has_name": bool(fm and fm.get("name")),
            "has_description": bool(fm and fm.get("description")),
            "has_task_type": bool(fm and fm.get("task_type")),
        })
    return skills


# ========== 解析 _index.md ==========

def parse_index() -> dict:
    """解析 _index.md，提取所有 skill 条目。

    返回 {skill_name_or_slug: {"section": "### N. xxx (slug)", "line": N}}。
    用 ### 标题中的括号内 slug 作为 key（如 "### 1. 记账 (accounting)" → key="accounting"）。
    slug 允许字母、数字、连字符；标题末尾可能有 [scope] 标记。
    """
    if not INDEX_FILE.exists():
        return {}
    content = INDEX_FILE.read_text(encoding="utf-8")
    result: dict = {}
    for m in re.finditer(r"^### \d+\.\s+[^()]*?\(([\w\-]+)\)\s*(?:\[[\w]+\]\s*)*$", content, re.MULTILINE):
        slug = m.group(1)
        result[slug] = {
            "section": m.group(0).strip(),
            "line": content[:m.start()].count("\n") + 1,
        }
    return result


# ========== 解析 GUIDE_REGISTRY ==========

def parse_registry() -> dict:
    """解析 server/agent_guide.py 的 GUIDE_REGISTRY，提取 task_type → skill_file 映射。

    返回 {task_type: {"skill_file": str, "name": str}}。
    用正则匹配，避免 import agent_guide（可能失败）。
    """
    if not REGISTRY_FILE.exists():
        return {}
    content = REGISTRY_FILE.read_text(encoding="utf-8")
    result: dict = {}
    # 匹配 "task_type": { ... "skill_file": "..." ... }
    # 由于条目结构复杂，用宽松匹配：找到每个 "xxx.yyy": { 后取其 skill_file 字段
    entry_re = re.compile(
        r'"([\w]+\.[\w_]+)":\s*\{[^}]*?"skill_file":\s*"([^"]+)"[^}]*?\}',
        re.DOTALL,
    )
    for m in entry_re.finditer(content):
        task_type = m.group(1)
        skill_file = m.group(2)
        result[task_type] = {"skill_file": skill_file}
    return result


# ========== 组件化 skill 扫描（manifest 声明，不参与三处真源校验） ==========

def scan_component_skills() -> list[dict]:
    """扫描 manifest 声明的组件化 skill，单独做 frontmatter 校验。

    返回 list of dict，每个 dict 含 path/rel_path/name/task_type/has_frontmatter 等字段。
    不参与"三处真源"校验：
      - 文件系统：不位于 .agents/skills/
      - _index.md：由 tools/generate_index.py 程序化生成"组件化模块"段
      - GUIDE_REGISTRY：由 _load_optional_guide_entries() 运行时合并，源文件不含硬编码
    """
    try:
        from server.component_manifest import load_manifests
        manifests = load_manifests()
    except Exception:
        return []

    result: list[dict] = []
    for name, m in manifests.items():
        if m.skill is None:
            continue
        skill_file = m.workspace_dir / m.skill.file
        if not skill_file.exists():
            result.append({
                "name": name,
                "path": str(skill_file),
                "rel_path": f"workspace/{name}/{m.skill.file}",
                "has_frontmatter": False,
                "has_name": False,
                "has_description": False,
                "has_task_type": False,
                "name_in_fm": None,
                "task_type_in_fm": None,
                "exists": False,
                "manifest_task_type": m.skill.task_type,
            })
            continue
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = parse_frontmatter(content)
        result.append({
            "name": name,
            "path": str(skill_file),
            "rel_path": f"workspace/{name}/{m.skill.file}",
            "has_frontmatter": fm is not None,
            "has_name": bool(fm and fm.get("name")),
            "has_description": bool(fm and fm.get("description")),
            "has_task_type": bool(fm and fm.get("task_type")),
            "name_in_fm": fm.get("name") if fm else None,
            "task_type_in_fm": fm.get("task_type") if fm else None,
            "exists": True,
            "manifest_task_type": m.skill.task_type,
        })
    return result


def check_component_skills(component_skills: list[dict]) -> tuple[list[str], list[str]]:
    """对组件化 skill 做独立的 frontmatter 完整性校验。

    返回 (errors, warnings)。
    """
    errors: list[str] = []
    warnings: list[str] = []
    for s in component_skills:
        if not s["exists"]:
            errors.append(f"组件化 skill 文件不存在: {s['rel_path']}（manifest 声明但文件缺失）")
            continue
        if not s["has_frontmatter"]:
            errors.append(f"组件化 skill 无 frontmatter: {s['rel_path']}")
            continue
        if not s["has_name"]:
            errors.append(f"组件化 skill frontmatter 缺 name: {s['rel_path']}")
        elif s["name_in_fm"] != s["name"]:
            warnings.append(
                f"组件化 skill name 不一致: manifest={s['name']} frontmatter={s['name_in_fm']} ({s['rel_path']})"
            )
        if not s["has_description"]:
            warnings.append(f"组件化 skill frontmatter 缺 description: {s['rel_path']}")
        if not s["has_task_type"]:
            warnings.append(f"组件化 skill frontmatter 缺 task_type: {s['rel_path']}")
        elif s["task_type_in_fm"] != s["manifest_task_type"]:
            errors.append(
                f"组件化 skill task_type 不一致: manifest={s['manifest_task_type']} "
                f"frontmatter={s['task_type_in_fm']} ({s['rel_path']})"
            )
    return errors, warnings


# ========== 校验逻辑 ==========

def check_all() -> dict:
    """运行全部校验，返回结构化报告。

    核心 skill 走"三处真源"校验（文件系统 + _index.md + GUIDE_REGISTRY 源文件）。
    组件化 skill（manifest 声明的）走独立 frontmatter 校验，不参与"三处真源"。
    """
    skills = scan_skill_files()
    index = parse_index()
    registry = parse_registry()
    component_skills = scan_component_skills()

    errors: list[str] = []
    warnings: list[str] = []

    # 1. 核心 skill frontmatter 完整性
    for s in skills:
        if not s["has_frontmatter"]:
            errors.append(f"无 frontmatter: {s['rel_path']}")
        elif not s["has_name"]:
            errors.append(f"frontmatter 缺 name: {s['rel_path']}")
        elif not s["has_description"]:
            warnings.append(f"frontmatter 缺 description: {s['rel_path']}")
        elif not s["has_task_type"]:
            warnings.append(f"frontmatter 缺 task_type: {s['rel_path']}")

    # 2. 核心 skill task_type 唯一性
    tt_to_files: dict[str, list[str]] = {}
    for s in skills:
        if s["task_type"]:
            tt_to_files.setdefault(s["task_type"], []).append(s["rel_path"])
    for tt, files in tt_to_files.items():
        if len(files) > 1:
            errors.append(f"task_type 重复: {tt} → {files}")

    # 3. _index.md 覆盖检查（仅核心 skill；组件化 skill 在自动生成段，格式不同）
    fs_names = set()
    for s in skills:
        if s["name"]:
            fs_names.add(s["name"])
        else:
            p = Path(s["rel_path"])
            if p.name == "SKILL.md":
                fs_names.add(p.parent.name)
            else:
                fs_names.add(p.stem)
    index_slugs = set(index.keys())
    missing_in_index = fs_names - index_slugs
    extra_in_index = index_slugs - fs_names
    for name in sorted(missing_in_index):
        warnings.append(f"_index.md 缺失条目: {name}")
    for slug in sorted(extra_in_index):
        warnings.append(f"_index.md 多余条目: {slug}")

    # 4. GUIDE_REGISTRY 覆盖检查（仅核心 skill；组件化 skill 在运行时合并，源文件不含）
    fs_task_types = set(tt_to_files.keys())
    registry_task_types = set(registry.keys())
    missing_in_registry = fs_task_types - registry_task_types
    extra_in_registry = registry_task_types - fs_task_types
    for tt in sorted(missing_in_registry):
        warnings.append(f"GUIDE_REGISTRY 缺失 task_type: {tt}")
    for tt in sorted(extra_in_registry):
        reg_file = registry[tt].get("skill_file", "")
        if reg_file:
            full_path = ROOT / reg_file
            if not full_path.exists():
                errors.append(f"GUIDE_REGISTRY 指向的文件不存在: {tt} → {reg_file}")
            else:
                warnings.append(f"GUIDE_REGISTRY 有 {tt}，但对应文件 frontmatter 缺 task_type 字段: {reg_file}")

    # 5. 组件化 skill 独立校验（不参与"三处真源"）
    comp_errors, comp_warnings = check_component_skills(component_skills)
    errors.extend(comp_errors)
    warnings.extend(comp_warnings)

    return {
        "summary": {
            "skill_files": len(skills),
            "component_skill_files": len(component_skills),
            "index_entries": len(index),
            "registry_entries": len(registry),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "errors": errors,
        "warnings": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description="Skill 一致性校验")
    parser.add_argument("--json", action="store_true", help="JSON 输出（CI 友好）")
    args = parser.parse_args()

    report = check_all()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        s = report["summary"]
        print(f"Skill 一致性校验报告")
        print(f"  核心 skill 文件:        {s['skill_files']}")
        print(f"  组件化 skill 文件:      {s['component_skill_files']}")
        print(f"  _index.md 核心段条目:   {s['index_entries']}")
        print(f"  GUIDE_REGISTRY 源文件条目: {s['registry_entries']}")
        print(f"  错误: {s['errors']}")
        print(f"  警告: {s['warnings']}")
        print()
        if report["errors"]:
            print("=== 错误 ===")
            for e in report["errors"]:
                print(f"  [E] {e}")
            print()
        if report["warnings"]:
            print("=== 警告 ===")
            for w in report["warnings"]:
                print(f"  [W] {w}")
            print()
        if s["errors"] == 0 and s["warnings"] == 0:
            print("✓ 全部通过")

    # 退出码：有错误 → 2，只有警告 → 1，全通过 → 0
    if report["summary"]["errors"] > 0:
        sys.exit(2)
    elif report["summary"]["warnings"] > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
