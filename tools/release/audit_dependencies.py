"""扫描 pyproject.toml 中每个依赖在 git-tracked 源码中的真实使用情况。

输出 JSON 报告：
- used_in_core: 在 server/ 或 client/ 下被 import（必须保留）
- used_in_workspace_only: 仅在 workspace/ 下被 import（按需依赖）
- used_in_tests_only: 仅在 tests/ 下被 import（dev 依赖）
- unused: 完全未被任何 git-tracked 文件 import（可移除）

跳过：
- references/ 已 hard-excluded，不算使用
- .venv/ / temp/ / node_modules 等非源码目录
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(r"f:\<project_root>")
PYPROJECT = ROOT / "pyproject.toml"
OUTPUT = ROOT / "release" / "dependency_audit.json"

# 标准库 + 项目内模块（非第三方包，跳过）
STDLIB_AND_INTERNAL = {
    # 标准库
    "abc", "argparse", "asyncio", "base64", "collections", "concurrent",
    "contextlib", "ctypes", "datetime", "decimal", "enum", "functools",
    "hashlib", "http", "importlib", "inspect", "io", "itertools", "json",
    "logging", "math", "os", "pathlib", "platform", "re", "shutil",
    "signal", "socket", "sqlite3", "string", "subprocess", "sys", "tempfile",
    "threading", "time", "traceback", "typing", "urllib", "uuid", "warnings",
    "weakref", "xml", "zipfile", "argparse", "ast", "binascii", "calendar",
    "configparser", "copy", "csv", "difflib", "email", "fnmatch", "fractions",
    "getpass", "glob", "gzip", "hashlib", "html", "http", "imghdr", "ipaddress",
    "locale", "mimetypes", "multiprocessing", "operator", "pathlib", "pdb",
    "pickle", "pkgutil", "platform", "pprint", "queue", "random", "secrets",
    "shlex", "site", "smtplib", "ssl", "stat", "struct", "textwrap",
    "timeit", "tomllib", "trace", "types", "unicodedata", "unittest",
    "urllib", "venv", "warnings", "wsgiref", "zipimport", "zlib",
    # 项目内模块
    "server", "client", "workspace", "tools", "tests",
}

# pyproject.toml dependency name → 实际 import 名映射
# （有些包名和 import 名不一样）
PACKAGE_TO_IMPORT = {
    "paddleocr": "paddleocr",
    "paddlepaddle-gpu": "paddle",
    "pillow": "PIL",
    "pyside6": "PySide6",
    "pywin32": ["win32gui", "win32con", "win32ui", "win32api", "win32process", "pywintypes"],
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
    "onnxruntime-gpu": "onnxruntime",
    "pymupdf": ["fitz", "pymupdf"],
}


def parse_pyproject_dependencies():
    """解析 pyproject.toml 的 [project].dependencies 列表

    使用 tomllib 直接解析，避免正则解析的不准确。
    返回的每个 entry 形如 "paddleocr[doc-parser]>=3.7.0"，去掉版本约束后
    得到 "paddleocr[doc-parser]"（含 extras）或 "akshare"（不含 extras）。
    """
    import tomllib
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    raw_deps = data.get("project", {}).get("dependencies", [])
    pkgs = []
    for entry in raw_deps:
        # entry 形如 "paddleocr[doc-parser]>=3.7.0" 或 "akshare>=1.18.65"
        # 去掉版本约束（>= / == / < 等），保留包名 + extras
        pkg = re.split(r"[><=!~\[]", entry)[0]
        # 如果原 entry 含 [extras]，重新附加
        m = re.match(r"^([a-zA-Z0-9_\-]+)(\[[a-zA-Z0-9_\-]+\])?", entry)
        if m:
            pkg = m.group(1) + (m.group(2) or "")
        if pkg:
            pkgs.append(pkg)
    return pkgs


def get_import_names(pkg_name: str) -> list[str]:
    """返回该包对应的所有可能的 import 名

    输入可能含 [extras]（如 "paddleocr[doc-parser]"），去掉 extras 后再查映射表。
    """
    # 去掉 [extras] 部分
    base_name = re.split(r"\[", pkg_name)[0]
    if base_name in PACKAGE_TO_IMPORT:
        v = PACKAGE_TO_IMPORT[base_name]
        return v if isinstance(v, list) else [v]
    return [base_name.replace("-", "_")]


def find_usage_in_paths(import_names: list[str], search_paths: list[Path]) -> dict[str, list[str]]:
    """对每个 import 名，在指定路径下 grep import 语句"""
    usage = {}
    for imp_name in import_names:
        # 正则：行首（可选空格）import pkg 或 from pkg
        pattern = rf"^\s*(?:import\s+{re.escape(imp_name)}\b|from\s+{re.escape(imp_name)}\b)"
        cmd = ["rg", "-n", "--glob", "*.py", "-l", pattern]
        for path in search_paths:
            if not path.exists():
                continue
            cmd_with_path = cmd + [str(path)]
            try:
                result = subprocess.run(cmd_with_path, capture_output=True, text=True, cwd=ROOT, encoding="utf-8")
                files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
                if files:
                    usage.setdefault(imp_name, []).extend(files)
            except Exception:
                pass
    return usage


def main():
    print(f"[scan] pyproject.toml dependencies")
    deps = parse_pyproject_dependencies()
    print(f"[scan] {len(deps)} packages in pyproject.toml")
    print()

    # 定义搜索路径（不含 references/，因为已 hard-excluded）
    core_paths = [ROOT / "server", ROOT / "client"]
    workspace_path = ROOT / "workspace"
    tools_path = ROOT / "tools"
    tests_path = ROOT / "tests"
    agents_path = ROOT / ".agents"
    docs_path = ROOT / "docs"

    report = {
        "generated_at": None,
        "summary": {},
        "dependencies": {},
    }

    from datetime import datetime, timezone
    report["generated_at"] = datetime.now(timezone.utc).isoformat()

    categories = {
        "used_in_core": [],      # 必须保留
        "used_in_workspace_only": [],  # 按需依赖
        "used_in_tools_only": [],      # 构建工具依赖
        "used_in_tests_only": [],      # dev 依赖
        "unused": [],                  # 完全未使用，可移除
    }

    for pkg in deps:
        import_names = get_import_names(pkg)
        core_usage = find_usage_in_paths(import_names, core_paths)
        workspace_usage = find_usage_in_paths(import_names, [workspace_path])
        tools_usage = find_usage_in_paths(import_names, [tools_path])
        tests_usage = find_usage_in_paths(import_names, [tests_path])
        agents_usage = find_usage_in_paths(import_names, [agents_path])
        docs_usage = find_usage_in_paths(import_names, [docs_path])

        # 汇总
        all_usage = {
            "core": core_usage,
            "workspace": workspace_usage,
            "tools": tools_usage,
            "tests": tests_usage,
            "agents": agents_usage,
            "docs": docs_usage,
        }

        # 分类
        core_count = sum(len(v) for v in core_usage.values())
        workspace_count = sum(len(v) for v in workspace_usage.values())
        tools_count = sum(len(v) for v in tools_usage.values())
        tests_count = sum(len(v) for v in tests_usage.values())
        agents_count = sum(len(v) for v in agents_usage.values())
        docs_count = sum(len(v) for v in docs_usage.values())
        total = core_count + workspace_count + tools_count + tests_count + agents_count + docs_count

        if core_count > 0:
            category = "used_in_core"
        elif workspace_count > 0:
            category = "used_in_workspace_only"
        elif tools_count > 0:
            category = "used_in_tools_only"
        elif tests_count > 0:
            category = "used_in_tests_only"
        elif agents_count > 0 or docs_count > 0:
            category = "used_in_docs_or_agents"  # 仅文档/skill 引用，不强制需要
        else:
            category = "unused"

        categories.setdefault(category, []).append(pkg)

        # 详细信息（截断长文件列表）
        def truncate(d, max_files=5):
            out = {}
            for k, v in d.items():
                out[k] = v[:max_files] if len(v) > max_files else v
                if len(v) > max_files:
                    out[k].append(f"... ({len(v) - max_files} more)")
            return out

        report["dependencies"][pkg] = {
            "import_names": import_names,
            "category": category,
            "counts": {
                "core": core_count,
                "workspace": workspace_count,
                "tools": tools_count,
                "tests": tests_count,
                "agents": agents_count,
                "docs": docs_count,
                "total": total,
            },
            "usage": truncate(all_usage),
        }

    # 汇总
    report["summary"] = {
        "total_packages": len(deps),
        "by_category": {k: len(v) for k, v in categories.items()},
        "categories": categories,
    }

    # 打印报告
    print("=" * 80)
    print("依赖审计报告")
    print("=" * 80)
    print()
    print(f"总包数: {len(deps)}")
    for cat, pkgs in categories.items():
        if pkgs:
            print(f"\n【{cat}】({len(pkgs)} 个)")
            for pkg in pkgs:
                info = report["dependencies"][pkg]
                counts = info["counts"]
                print(f"  - {pkg}  core={counts['core']} workspace={counts['workspace']} tools={counts['tools']} tests={counts['tests']} agents={counts['agents']} docs={counts['docs']}")

    # 保存 JSON
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] full report → {OUTPUT}")


if __name__ == "__main__":
    main()
