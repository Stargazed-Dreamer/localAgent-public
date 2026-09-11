"""硬化规则检查（"说过三遍的规则"→"会变红的构建"）。

四个检查项（详见 temp/sdd/agent-trust-guardrails/spec.md 2.1）：
  C1 BOM：tracked 的 .py/.md/.toml/.json 文件头不得为 UTF-8 BOM（EF BB BF）
  C2 硬编码密钥：源码中 api_key/token/secret/password 等字面量赋值即违规
  C3 文档路径越界：tracked .html / 根目录 .md 必须在白名单内
  C4 import 边界：client/** 不许 import server/**；server/** 不许 import client/**（白名单除外）

用法：
  uv run python tools/check_hard_rules.py            # 全量检查（CI/测试语义）
  uv run python tools/check_hard_rules.py --staged   # 只查 staged 文件（pre-commit 语义）
  uv run python tools/check_hard_rules.py install    # git config core.hooksPath scripts/hooks
  uv run python tools/check_hard_rules.py uninstall  # 恢复默认 hooksPath

设计约束：stdlib-only，不引入新依赖；文件清单以 git ls-files 为准（天然排除
gitignored 的 temp/ 等）；白名单变更必须显式改本文件并 commit（这就是"硬化"）。
"""
from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 文件头 3 字节为 UTF-8 BOM
BOM = b"\xef\xbb\xbf"
# C1 检查的扩展名
BOM_EXTENSIONS = {".py", ".md", ".toml", ".json"}

# C2 白名单（相对路径，支持 fnmatch 通配）：这些文件合法接触密钥形态文本
SECRET_ALLOW_FILES = [
    "lib/secret/*",
    "server/llm_pool/key_store.py",
    "tools/migrate_secrets.py",
    "tools/check_hard_rules.py",  # 本文件自带检测模式字符串
    "tests/*",
    "docs/*",
    "workspace/*",
    "planning_notes/*",
    "展示文档/*",
]
# C2 占位值（不视为泄漏）
SECRET_PLACEHOLDER_SUBSTRINGS = [
    "xxx", "your", "placeholder", "example", "dummy", "changeme",
    "todo", "<", ">", "test", "sample", "redacted",
]
# C2 检测的密钥形字段名（用于 .py 行内赋值/字典键；需词边界防 "backups/secrets" 这类路径误报）
SECRET_KEY_PATTERN = r"(?:api[_-]?key|api[_-]?secret|token|secret|password|passwd|access[_-]?key)"
# 赋值形态：key = "字面量"（≥8 字符才算，避免空串/短占位误报）；(?<![\w]) 防字段名误中字符串内容
RE_SECRET_ASSIGN = re.compile(
    r"(?<![\w])" + SECRET_KEY_PATTERN + r"\s*[\"']?\s*=\s*[\"']([^\"']{8,})[\"']", re.IGNORECASE
)
# 字典形态："key": "字面量"
RE_SECRET_DICT = re.compile(
    "[\"']" + SECRET_KEY_PATTERN + r"[\"']\s*:\s*[\"']([^\"']{8,})[\"']", re.IGNORECASE
)
# config.example.toml：字段名（锚定在 = 前）含密钥形字段 且 值为 8+ 字符字面量 → 违规
RE_TOML_SECRET = re.compile(
    r"^\s*[\w*]*" + SECRET_KEY_PATTERN + r"[\w*]*\s*=\s*[\"']([^\"']{8,})[\"']", re.IGNORECASE
)

# C3：tracked .html 白名单（temp/html/ 因 gitignore 天然不入清单）
HTML_ALLOW = [
    "tools/release/templates/*",
    "tests/*",       # 测试夹具（browser fixtures 等）
    "workspace/*",   # workspace 组件自带资源（gacha/accounting/dashboard 等）
    "展示文档/*",
]
# C3：根目录 tracked .md 白名单（新增顶级 .md 必须显式改这里并 commit）
ROOT_MD_ALLOW = [
    "AGENTS.md",
    "CHANGELOG.md",
    "NOTICE.md",
    "README.md",
    "README_public.md",
    "SECURITY-RISKS.md",
    "USER_ONLY_PROJECT_OPERATIONS_AGENT_DO_NOT_READ_OR_EDIT.md",
]

# C4：import 边界。双向各有白名单例外（现存合法跨层复用，新增例外必须显式改这里并 commit）
IMPORT_ALLOW_SERVER_TO_CLIENT = [
    "server/probe/runner.py",                  # 探针运行器复用 client.core.agent
    "server/activity_tracker/headless_runner.py",  # headless 会话复用 client.core.agent
]
IMPORT_ALLOW_CLIENT_TO_SERVER = [
    "client/core/agent/compactor.py",  # 压缩器复用 server.llm_pool.compression（有本地粗估 fallback）
]


def _git(args: list[str]) -> str:
    r = subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, check=True
    )
    return r.stdout.decode("utf-8", errors="replace")


def _tracked_files() -> list[str]:
    out = _git(["ls-files", "-z"])
    return [f for f in out.split("\0") if f]


def _staged_files() -> list[str]:
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"])
    return [f for f in out.split("\0") if f]


def _fnmatch_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def _is_secret_placeholder(value: str) -> bool:
    low = value.lower()
    return any(s in low for s in SECRET_PLACEHOLDER_SUBSTRINGS)


# ---------- C1 BOM ----------

def check_bom(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        if Path(f).suffix.lower() not in BOM_EXTENSIONS:
            continue
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        try:
            with open(p, "rb") as fh:
                if fh.read(3) == BOM:
                    violations.append(f"C1 BOM: {f}")
        except OSError:
            continue
    return violations


# ---------- C2 硬编码密钥 ----------

def check_secrets(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        if _fnmatch_any(f, SECRET_ALLOW_FILES):
            continue
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        suffix = Path(f).suffix.lower()
        try:
            if suffix == ".py":
                text = p.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    for m in (*RE_SECRET_ASSIGN.finditer(line), *RE_SECRET_DICT.finditer(line)):
                        if not _is_secret_placeholder(m.group(1)):
                            violations.append(f"C2 硬编码密钥: {f}:{lineno}（字段形如 {SECRET_KEY_PATTERN}）")
                            break
            elif f == "config.example.toml":
                text = p.read_text(encoding="utf-8", errors="replace")
                for lineno, line in enumerate(text.splitlines(), 1):
                    m = RE_TOML_SECRET.match(line)
                    if m and not _is_secret_placeholder(m.group(1)):
                        violations.append(f"C2 硬编码密钥: {f}:{lineno}（token/key 字段不得有真实值）")
                        break
        except OSError:
            continue
    return violations


# ---------- C3 文档路径越界 ----------

def check_path_boundary(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        low = f.lower()
        if low.endswith(".html") and not _fnmatch_any(f, HTML_ALLOW):
            violations.append(f"C3 路径越界: {f}（.html 展示文件只允许白名单位置）")
        elif "/" not in f and low.endswith(".md") and f not in ROOT_MD_ALLOW:
            violations.append(f"C3 路径越界: {f}（根目录新增 .md 必须显式加入 ROOT_MD_ALLOW）")
    return violations


# ---------- C4 import 边界 ----------

def _iter_import_modules(tree: ast.AST) -> list[str]:
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                modules.append(node.module)
            # 相对 import（level>0）不跨包，忽略
    return modules


def check_import_boundary(files: list[str]) -> list[str]:
    violations = []
    py_files = [f for f in files if f.startswith(("client/", "server/")) and f.endswith(".py")]
    for f in py_files:
        p = PROJECT_ROOT / f
        if not p.is_file():
            continue
        try:
            source = p.read_bytes().decode("utf-8-sig", errors="replace")
            tree = ast.parse(source)
        except SyntaxError:
            continue  # C1/C2 已覆盖内容问题，这里只管边界
        in_client = f.startswith("client/")
        for mod in _iter_import_modules(tree):
            if in_client and mod.startswith("server."):
                if not _fnmatch_any(f, IMPORT_ALLOW_CLIENT_TO_SERVER):
                    violations.append(
                        f"C4 import 边界: {f} import {mod}（client 不许 import server，"
                        f"例外需加入 IMPORT_ALLOW_CLIENT_TO_SERVER）"
                    )
            elif not in_client and mod.startswith("client."):
                if not _fnmatch_any(f, IMPORT_ALLOW_SERVER_TO_CLIENT):
                    violations.append(
                        f"C4 import 边界: {f} import {mod}（server 不许 import client，"
                        f"例外需加入 IMPORT_ALLOW_SERVER_TO_CLIENT）"
                    )
    return violations


# ---------- 主入口 ----------

ALL_CHECKS = [check_bom, check_secrets, check_path_boundary, check_import_boundary]


def run_checks(staged: bool = False) -> list[str]:
    files = _staged_files() if staged else _tracked_files()
    violations: list[str] = []
    for check in ALL_CHECKS:
        violations.extend(check(files))
    return sorted(set(violations))


def cmd_check(staged: bool) -> int:
    violations = run_checks(staged=staged)
    scope = "staged" if staged else "全量 tracked"
    if violations:
        print(f"[check_hard_rules] {scope} 检查发现 {len(violations)} 处违规：")
        for v in violations:
            print(f"  {v}")
        print("修复或显式更新 tools/check_hard_rules.py 白名单（白名单变更必须 commit）")
        return 1
    print(f"[check_hard_rules] {scope} 检查通过（C1 BOM / C2 密钥 / C3 路径 / C4 import）")
    return 0


def cmd_install() -> int:
    subprocess.run(
        ["git", "config", "core.hooksPath", "scripts/hooks"],
        cwd=PROJECT_ROOT, check=True,
    )
    print("[check_hard_rules] 已设置 core.hooksPath=scripts/hooks，pre-commit 生效")
    return 0


def cmd_uninstall() -> int:
    r = subprocess.run(
        ["git", "config", "--unset", "core.hooksPath"],
        cwd=PROJECT_ROOT, capture_output=True,
    )
    if r.returncode == 0:
        print("[check_hard_rules] 已恢复默认 hooksPath")
    else:
        print("[check_hard_rules] 本来就没有自定义 hooksPath")
    return 0


def main(argv: list[str]) -> int:
    if "--staged" in argv:
        return cmd_check(staged=True)
    if "install" in argv:
        return cmd_install()
    if "uninstall" in argv:
        return cmd_uninstall()
    return cmd_check(staged=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
