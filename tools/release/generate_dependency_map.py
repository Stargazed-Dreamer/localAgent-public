"""扫描 pyproject.toml 依赖在 git-tracked 源码中的真实使用情况，
生成机械的依赖映射表 release/dependency_map.toml。

设计原则：
- 脚本机械生成，agent 不编辑此文件（除非补 notes 字段）
- 每次运行覆盖整个文件，确保与源码同步
- 输出三个文件：
  - release/dependency_map.toml — 机器可读的依赖映射表（profile 裁剪依据）
  - release/dependency_audit.json — 详细使用位置报告（供 agent 调查）
  - stdout 摘要 — 供 agent 快速检查

工作流：
1. 解析 pyproject.toml 的 [project].dependencies
2. 对每个依赖，在 git-tracked .py 文件中 grep import 语句
3. 按使用位置分类：core / workspace_only / tools_only / tests_only / unused
4. 对 workspace_only 依赖，列出"被哪些 workspace 模块使用"（用于 profile 裁剪）
5. 生成 dependency_map.toml + dependency_audit.json

使用：
    uv run python tools/release/generate_dependency_map.py
    uv run python tools/release/generate_dependency_map.py --check-profile friend-full
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
OUTPUT_MAP = PROJECT_ROOT / "release" / "dependency_map.toml"
OUTPUT_AUDIT = PROJECT_ROOT / "release" / "dependency_audit.json"

# === 包名 → import 名映射 ===
# pyproject.toml 的包名（含 [extras]）→ 实际 import 用的模块名
# 一个包可能对应多个 import 名（如 pywin32 → win32gui/win32con/...）
PACKAGE_TO_IMPORT = {
    "paddleocr": "paddleocr",
    "paddlepaddle-gpu": "paddle",
    "pillow": "PIL",
    "pyside6": "PySide6",
    "pywin32": ["win32gui", "win32con", "win32ui", "win32api", "win32process", "pywintypes", "pythoncom"],
    "python-docx": "docx",
    "python-pptx": "pptx",
    "python-multipart": "multipart",
    "huggingface-hub": "huggingface_hub",
    "pyautogui": "pyautogui",
    "pynput": "pynput",
    "pyperclip": "pyperclip",
    "uiautomation": "uiautomation",
    "playwright-stealth": "playwright_stealth",
    "playwright": "playwright",
    "transformers": "transformers",
    "torchvision": "torchvision",
    "accelerate": "accelerate",
    "fastapi-mcp": "fastapi_mcp",
    "onnxruntime-gpu": ["onnxruntime", "ort"],
    "pymupdf": ["fitz", "pymupdf"],
    "tokenizers": "tokenizers",
    "huggingface-hub": "huggingface_hub",
    "openai": "openai",
    "supervision": "supervision",
    "timm": "timm",
    "orjson": "orjson",
    "jieba": "jieba",
    "akshare": "akshare",
    "pandas": "pandas",
    "setuptools": "setuptools",
    "numpy": "numpy",
    "mss": "mss",
    "toml": "toml",
    "uvicorn": "uvicorn",
    "fastapi": "fastapi",
    "openpyxl": "openpyxl",
    "ultralytics": "ultralytics",
}

# === 人工 notes（补充脚本无法识别的真实状态） ===
# 脚本只看 git-tracked .py 文件的显式 import 语句，无法识别：
# 1. 未 git-tracked 的 workspace 模块 → 由 manifest [release].extra_deps 声明
# 2. 框架间接依赖（如 python-multipart 被 FastAPI 用于 form 解析）
# 3. 通过 importlib / 字符串引用的包
# 4. 辅助生态包（transformers 自动加载 accelerate）
# 组件专属依赖（如某组件的 akshare/pandas）由 manifest [release].extra_deps 声明，
# 不再在此硬编码。此字典仅保留框架级间接依赖说明。
KNOWN_NOTES: dict[str, str] = {
    "python-multipart": "FastAPI 间接依赖：处理 multipart/form-data 请求体时自动加载（/upload 等端点必需）",
    "orjson": "FastAPI 可选 JSON 编码器：若安装则自动替换默认 json 库，加速响应序列化",
    "accelerate": "transformers 辅助包：model.from_pretrained() 在加载大模型时自动探测并使用，不显式 import",
    "timm": "torchvision 辅助包：部分 ViT 模型权重加载时需要",
    "supervision": "ultralytics 辅助包：YOLO 推理结果可视化时使用",
    "setuptools": "Python 打包工具：runtime 不需要，仅构建时使用；可考虑移到 dev 依赖",
    "pyperclip": "项目源码 0 使用，可移除",
    "jieba": "三层记忆系统 v2 中文分词，server/memory/embedder.py 使用（lazy import）",
}


def parse_pyproject_dependencies() -> list[str]:
    """解析 pyproject.toml 的 [project].dependencies 列表

    返回形如 ["paddleocr[doc-parser]", "akshare", ...] 的列表（含 extras，去版本约束）。
    """
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    raw_deps = data.get("project", {}).get("dependencies", [])
    pkgs = []
    for entry in raw_deps:
        # entry 形如 "paddleocr[doc-parser]>=3.7.0" 或 "akshare>=1.18.65"
        m = re.match(r"^([a-zA-Z0-9_\-]+(?:\[[a-zA-Z0-9_\-]+\])?)", entry)
        if m:
            pkgs.append(m.group(1))
    return pkgs


def _load_manifest_extra_deps() -> dict[str, list[str]]:
    """从 workspace manifests 加载组件专属依赖声明。

    返回 dict[component_name] -> list[extra_dep_package_name]。
    这些依赖不在 git-tracked 源码的 import 扫描中体现（如未 git-tracked 的模块），
    但在部署该组件时必需。
    """
    import sys as _sys
    _server_dir = str(PROJECT_ROOT / "server")
    if _server_dir not in _sys.path:
        _sys.path.insert(0, _server_dir)
    try:
        from component_manifest import load_manifests
        manifests = load_manifests()
    except Exception:
        return {}

    result: dict[str, list[str]] = {}
    for name, m in manifests.items():
        if m.release and m.release.extra_deps:
            result[name] = list(m.release.extra_deps)
    return result


def get_import_names(pkg_name: str) -> list[str]:
    """返回该包对应的所有 import 名（去掉 [extras]）"""
    base_name = re.split(r"\[", pkg_name)[0]
    if base_name in PACKAGE_TO_IMPORT:
        v = PACKAGE_TO_IMPORT[base_name]
        return v if isinstance(v, list) else [v]
    return [base_name.replace("-", "_")]


_GIT_FILES_CACHE: list[str] | None = None


def _list_tracked_files() -> list[str]:
    """缓存 git ls-files 结果，避免重复调用"""
    global _GIT_FILES_CACHE
    if _GIT_FILES_CACHE is not None:
        return _GIT_FILES_CACHE
    result = subprocess.run(
        ["git", "ls-files", "*.py", "*.pyx"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    # 过滤掉 references/ 和 .venv/ 等（保险，git ls-files 一般不会列出这些）
    files = [
        f for f in files
        if not f.startswith("references/")
        and not f.startswith(".venv/")
        and not f.startswith("temp/")
    ]
    _GIT_FILES_CACHE = files
    return files


# 预编译匹配 import 语句的正则（POSIX ERE 不支持 \b，所以用 Python re 更可靠）
_IMPORT_PATTERNS: dict[str, re.Pattern] = {}


def _get_import_pattern(imp_name: str) -> re.Pattern:
    """缓存编译后的正则"""
    if imp_name not in _IMPORT_PATTERNS:
        # 匹配 `import pkg` 或 `from pkg` 在行首（含缩进）
        # 用 \b 在边界上锚定（避免 import fastapi_xxx 误匹配）
        # re.MULTILINE 让 ^ 匹配每行行首（默认只匹配字符串开头）
        pat = rf"^\s*(?:import\s+{re.escape(imp_name)}\b|from\s+{re.escape(imp_name)}(?:\.|\s|$))"
        _IMPORT_PATTERNS[imp_name] = re.compile(pat, re.MULTILINE)
    return _IMPORT_PATTERNS[imp_name]


def find_import_usage(import_names: list[str]) -> dict[str, list[str]]:
    """对每个 import 名，在所有 git-tracked .py 文件中搜索 import 语句

    用 Python re 模块搜索（避免 git grep 的 POSIX ERE 限制）。
    跳过：references/（hard-excluded）、.venv/、temp/
    返回 dict[import_name] -> list[relative_path]，路径相对 PROJECT_ROOT。
    """
    tracked = _list_tracked_files()
    usage: dict[str, list[str]] = {imp: [] for imp in import_names}
    for rel_path in tracked:
        abs_path = PROJECT_ROOT / rel_path
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for imp_name in import_names:
            pat = _get_import_pattern(imp_name)
            if pat.search(text):
                usage[imp_name].append(rel_path)
    return usage


def classify_path(path: str) -> str:
    """分类文件路径所属区域"""
    if path.startswith("server/") or path.startswith("client/"):
        return "core"
    if path.startswith("workspace/"):
        # 取 workspace/<module>/ 部分
        parts = path.split("/", 2)
        if len(parts) >= 2:
            return f"workspace/{parts[1]}"
        return "workspace"
    if path.startswith("tools/"):
        return "tools"
    if path.startswith("tests/"):
        return "tests"
    if path.startswith(".agents/"):
        return "agents"
    if path.startswith("docs/"):
        return "docs"
    return "other"


def categorize_dependency(usage: dict[str, list[str]]) -> tuple[str, list[str]]:
    """根据使用位置分类依赖

    返回 (category, used_in_workspace_modules)
    category: core / workspace_only / tools_only / tests_only / unused
    used_in_workspace_modules: 仅当 category=workspace_only 时有意义，
        列出依赖被哪些 workspace 模块用（如 ["workspace/<component>"]）
    """
    all_files = []
    for files in usage.values():
        all_files.extend(files)

    if not all_files:
        return "unused", []

    areas = [classify_path(f) for f in all_files]
    has_core = any(a == "core" for a in areas)
    workspace_areas = {a for a in areas if a.startswith("workspace/")}

    if has_core:
        return "core", sorted(workspace_areas)
    if workspace_areas:
        return "workspace_only", sorted(workspace_areas)
    if "tools" in areas:
        return "tools_only", []
    if "tests" in areas:
        return "tests_only", []
    return "unused", []


def generate_map():
    """生成 dependency_map.toml 和 dependency_audit.json"""
    deps = parse_pyproject_dependencies()
    print(f"[scan] pyproject.toml has {len(deps)} dependencies")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pyproject_path": str(PYPROJECT.relative_to(PROJECT_ROOT)),
        "total_packages": len(deps),
        "dependencies": {},
    }

    entries = []  # for TOML output
    summary = defaultdict(list)

    # Load manifest extra_deps to supplement import-scan classification
    manifest_extra_deps = _load_manifest_extra_deps()
    # Build reverse map: pkg_name → list of component names that declare it
    extra_dep_owners: dict[str, list[str]] = {}
    for comp_name, dep_list in manifest_extra_deps.items():
        for dep in dep_list:
            extra_dep_owners.setdefault(dep, []).append(comp_name)

    for pkg in deps:
        import_names = get_import_names(pkg)
        usage = find_import_usage(import_names)
        category, workspace_modules = categorize_dependency(usage)

        # Supplement with manifest extra_deps: if a component declares this pkg
        # as extra_deps, add it to workspace_modules and upgrade unused→workspace_only
        manifest_owners = extra_dep_owners.get(pkg, [])
        if manifest_owners:
            for owner in manifest_owners:
                ws_mod = f"workspace/{owner}"
                if ws_mod not in workspace_modules:
                    workspace_modules.append(ws_mod)
            if category == "unused":
                category = "workspace_only"

        # 收集每个 import 名的所有使用位置（按区域分组）
        usage_by_area = defaultdict(list)
        for imp_name, files in usage.items():
            for f in files:
                area = classify_path(f)
                usage_by_area[area].append({"import": imp_name, "path": f})

        counts = {area: len(items) for area, items in usage_by_area.items()}
        total_count = sum(counts.values())

        # Build notes: KNOWN_NOTES + manifest extra_deps annotation
        notes = KNOWN_NOTES.get(pkg, "")
        if manifest_owners:
            manifest_note = f"manifest extra_deps 声明 by: {', '.join(manifest_owners)}"
            notes = f"{notes}; {manifest_note}" if notes else manifest_note

        summary[category].append(pkg)

        report["dependencies"][pkg] = {
            "import_names": import_names,
            "category": category,
            "used_in_workspace_modules": workspace_modules,
            "counts_by_area": counts,
            "total_usage_count": total_count,
            "usage": dict(usage_by_area),
        }

        # 生成 TOML 条目
        entries.append({
            "package": pkg,
            "import_names": import_names,
            "category": category,
            "used_in_workspace_modules": workspace_modules,
            "usage_count": total_count,
            "notes": notes,
        })

    report["summary"] = {
        "total_packages": len(deps),
        "by_category": {k: len(v) for k, v in summary.items()},
        "categories": dict(summary),
    }

    # === 写 dependency_map.toml ===
    write_map_toml(entries, summary)
    # === 写 dependency_audit.json ===
    OUTPUT_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_AUDIT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # === 打印摘要 ===
    print()
    print("=" * 78)
    print("依赖映射表生成报告")
    print("=" * 78)
    print(f"\n总包数: {len(deps)}")
    for cat in ["core", "workspace_only", "tools_only", "tests_only", "unused"]:
        pkgs = summary.get(cat, [])
        if pkgs:
            print(f"\n【{cat}】({len(pkgs)} 个)")
            for pkg in pkgs:
                info = report["dependencies"][pkg]
                counts = info["counts_by_area"]
                ws = info["used_in_workspace_modules"]
                ws_str = f" → {ws}" if ws else ""
                area_str = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"  - {pkg}  ({area_str}){ws_str}")

    print(f"\n[output] {OUTPUT_MAP}")
    print(f"[output] {OUTPUT_AUDIT}")
    print()
    print("agent 可调用 generate_dependency_map.py --check-profile <profile> 检查 profile 裁剪建议")


def write_map_toml(entries: list[dict], summary: dict[str, list[str]]):
    """生成 release/dependency_map.toml"""
    lines = [
        "# 依赖映射表（机器可读）",
        "#",
        "# 由 tools/release/generate_dependency_map.py 自动生成，请勿手动编辑。",
        "# 如需补充 notes 字段，请运行 `--add-notes` 模式（待实现）或修改",
        "# generate_dependency_map.py 的 KNOWN_NOTES 常量。",
        "#",
        "# 字段说明：",
        "#   package             pyproject.toml 中的包名（含 [extras]，去版本约束）",
        "#   import_names        实际 import 时使用的模块名（一个包可能多个）",
        "#   category            依赖分类：",
        "#                        core           - server/client 用，必须始终保留",
        "#                        workspace_only - 仅 workspace 模块用，按 profile 裁剪",
        "#                        tools_only     - 仅 tools/ 用（构建工具）",
        "#                        tests_only     - 仅 tests/ 用（dev 依赖）",
        "#                        unused         - 项目源码完全未使用，可移除",
        "#   used_in_workspace_modules  workspace_only 类专属：列出使用该依赖的 workspace 模块",
        "#   usage_count         总 import 位置数（git-tracked 文件）",
        "#   notes               人工标注（KNOWN_NOTES 常量）：说明脚本无法识别的真实状态，",
        "#                       如未 git-tracked 的 workspace 依赖、框架间接依赖、辅助生态包等",
        "#",
        f"# 最后生成时间: {datetime.now(timezone.utc).isoformat()}",
        f"# 总包数: {len(entries)}",
        f"# 分类: core={len(summary.get('core', []))} "
        f"workspace_only={len(summary.get('workspace_only', []))} "
        f"tools_only={len(summary.get('tools_only', []))} "
        f"tests_only={len(summary.get('tests_only', []))} "
        f"unused={len(summary.get('unused', []))}",
        "",
    ]

    # 按分类分组输出，便于人类阅读
    for cat in ["core", "workspace_only", "tools_only", "tests_only", "unused"]:
        cat_entries = [e for e in entries if e["category"] == cat]
        if not cat_entries:
            continue
        lines.append(f"# === {cat} ({len(cat_entries)} 个) ===")
        for e in cat_entries:
            lines.append(f"[[dependency]]")
            lines.append(f'package = "{e["package"]}"')
            import_str = ", ".join(f'"{n}"' for n in e["import_names"])
            lines.append(f"import_names = [{import_str}]")
            lines.append(f'category = "{e["category"]}"')
            if e["used_in_workspace_modules"]:
                ws_str = ", ".join(f'"{m}"' for m in e["used_in_workspace_modules"])
                lines.append(f"used_in_workspace_modules = [{ws_str}]")
            else:
                lines.append("used_in_workspace_modules = []")
            lines.append(f'usage_count = {e["usage_count"]}')
            if e["notes"]:
                # 用 TOML basic string，转义反斜杠和双引号
                safe_notes = e["notes"].replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'notes = "{safe_notes}"')
            lines.append("")
        lines.append("")

    OUTPUT_MAP.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MAP.write_text("\n".join(lines), encoding="utf-8")


def check_profile(profile_name: str):
    """检查给定 profile 下哪些 workspace_only 依赖是冗余的"""
    profile_path = PROJECT_ROOT / "release" / "profiles" / f"{profile_name}.toml"
    if not profile_path.exists():
        print(f"[err] profile not found: {profile_path}", file=sys.stderr)
        sys.exit(2)

    # 读取 dependency_map.toml
    if not OUTPUT_MAP.exists():
        print(f"[err] dependency_map.toml not found, run generate first", file=sys.stderr)
        sys.exit(2)

    with open(OUTPUT_MAP, "rb") as f:
        dep_map = tomllib.load(f)

    # 读取 profile 的 include/exclude
    with open(profile_path, "rb") as f:
        profile = tomllib.load(f)

    include_paths = profile.get("scope", {}).get("include_paths", [])
    exclude_paths = profile.get("scope", {}).get("exclude_paths", [])
    include_components = set(profile.get("scope", {}).get("include_components", []))

    # 判断 workspace 模块是否被包含
    # 启用条件：include_components 中含该模块名，或 include_paths 中含该模块的 .py 文件路径
    # 关闭条件：exclude_paths 中含该模块的整体前缀
    def is_workspace_module_included(module_name: str) -> bool:
        """判断 workspace/<module>/ 是否被 profile 包含"""
        # Check component-level selector first
        if module_name in include_components:
            return True
        # Check explicit include_paths
        for p in include_paths:
            if p.startswith(f"workspace/{module_name}/"):
                return True
            if p == f"workspace/{module_name}":
                return True
        # 整体前缀在 include 中（罕见，但 workspace/dev_toolkit/ 这种）
        for p in include_paths:
            if p.rstrip("/") == f"workspace/{module_name}":
                return True
        return False

    def is_workspace_module_excluded(module_name: str) -> bool:
        """判断 workspace/<module>/ 是否被 profile 整体排除"""
        for p in exclude_paths:
            if p.rstrip("/") == f"workspace/{module_name}":
                return True
            # 整体前缀形式
            if p == f"workspace/{module_name}/":
                return True
        return False

    # 收集 workspace_only 依赖的裁剪建议
    workspace_only_deps = [d for d in dep_map.get("dependency", []) if d["category"] == "workspace_only"]
    unused_deps = [d for d in dep_map.get("dependency", []) if d["category"] == "unused"]

    print(f"Profile: {profile_name}")
    print(f"=" * 78)
    print(f"include_paths: {len(include_paths)} 项")
    print(f"exclude_paths: {len(exclude_paths)} 项")
    print()

    # === 冗余依赖检查 ===
    print("【冗余 workspace_only 依赖】（required_by 的 workspace 模块全部未 include）")
    redundant = []
    for dep in workspace_only_deps:
        required_modules = dep["used_in_workspace_modules"]
        if not required_modules:
            continue
        # 检查每个 required_module 是否被 include
        included = []
        excluded = []
        for mod_path in required_modules:
            # mod_path 形如 "workspace/<component>"
            module_name = mod_path.split("/", 1)[1] if "/" in mod_path else mod_path
            if is_workspace_module_included(module_name):
                included.append(mod_path)
            else:
                excluded.append(mod_path)
        # 如果所有 required_modules 都未 include，则该依赖冗余
        if not included and excluded:
            redundant.append((dep, excluded))
            print(f"  - {dep['package']}  (required_by: {excluded}, all excluded)")
    if not redundant:
        print("  (none)")
    print()

    # === unused 依赖分类（看 notes 区分真的可移除 vs 间接依赖） ===
    truly_removable = []
    keep_with_notes = []
    for dep in unused_deps:
        notes = dep.get("notes", "")
        # notes 中含"可移除"字样才认为真的可移除
        if "可移除" in notes:
            truly_removable.append(dep)
        else:
            keep_with_notes.append(dep)

    print("【未使用依赖 - 真的可移除】（项目源码 0 使用 + notes 标注可移除）")
    for dep in truly_removable:
        print(f"  - {dep['package']}  → 可从 pyproject.toml 移除")
        notes = dep.get("notes", "")
        if notes:
            print(f"      notes: {notes}")
    if not truly_removable:
        print("  (none)")
    print()

    print("【未使用依赖 - 建议保留】（脚本未识别但有 notes 说明的间接依赖/辅助包）")
    for dep in keep_with_notes:
        notes = dep.get("notes", "(无 notes，请人工核实)")
        print(f"  - {dep['package']}  → 保留")
        print(f"      notes: {notes}")
    if not keep_with_notes:
        print("  (none)")
    print()

    # === 裁剪建议汇总 ===
    trim_count = len(redundant) + len(truly_removable)
    print(f"【裁剪建议】共 {trim_count} 个依赖可从导出 pyproject.toml 中移除")
    print(f"  - workspace_only 冗余: {len(redundant)}")
    print(f"  - unused 真的可移除: {len(truly_removable)}")
    print(f"  - unused 建议保留（看 notes）: {len(keep_with_notes)}")
    print()
    print("提示：v2 compiler engine（tools/release/engine/）目前未自动应用 pyproject.toml 裁剪；")
    print("      导出后需手动按本报告移除冗余依赖，再重新计算 sha256 入 MANIFEST.json。")


def main():
    parser = argparse.ArgumentParser(description="生成依赖映射表")
    parser.add_argument(
        "--check-profile",
        metavar="PROFILE",
        help="检查给定 profile 下哪些依赖是冗余的",
    )
    args = parser.parse_args()

    if args.check_profile:
        check_profile(args.check_profile)
    else:
        generate_map()


if __name__ == "__main__":
    main()
