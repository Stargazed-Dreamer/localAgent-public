"""导出副本的个人残留断言（2026-09-20 加）。

背景：v0.49.0 首发把一条真机安装目录路径和真机主机名发了出去。根因不是"规则没写"，
而是"规则写了却静默不命中"：

1. 引擎用第三方 `toml` 包读 profile，它对 basic string 里反斜杠的转义层数与标准
   `tomllib` 不一致，所以按直觉写出的双反斜杠键，在引擎里拿到的是多一层的字符串，
   `re.escape` 之后永远匹配不上源文本里的单反斜杠形态；
2. 为了让增量分诊报"干净"，同一行又被加了 line_skips，于是坏规则 + 遮蔽恰好配成一对；
3. prepare 期扫描跑在**源码原状**上，build 期替换跑在 **staging 副本**上，
   两者之间项目原本没有任何一道"替换后复检"（build 按设计不重跑 scan）。

本测试就是那道复检：对全部入包文件在内存里跑完整替换链（path_mapping → literal →
post_process_remove_lines），断言 profile `[scan]` 里列的 `residue_markers` 与
`personal_identifiers` 一个都不剩。违规只报 (文件, 标记)，不回显行内容。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_PROFILE = Path("release/profiles/public-full.toml")
_ROOT = Path(".")


def _scan_config() -> tuple[list[str], list[str]]:
    data = tomllib.loads(_PROFILE.read_text(encoding="utf-8"))
    scan = data.get("scan", {}) or {}
    return list(scan.get("personal_identifiers") or []), list(scan.get("residue_markers") or [])


def _apply_chain(
    text: str,
    path_mapping: dict[str, str],
    literals: list[dict],
    identifiers: list[str],
) -> str:
    """复刻 build_release 的替换顺序：path_mapping → literal → 个人标识清除"""
    out = text
    for find, repl in path_mapping.items():
        out = re.sub(re.escape(find), lambda m, r=repl: r, out, flags=re.IGNORECASE)
    for rule in literals:
        out = re.sub(re.escape(rule["find"]), lambda m, r=rule["replace"]: r, out, flags=re.DOTALL)
    for ident in identifiers:
        out = re.sub(re.escape(ident), "<dev_machine>", out, flags=re.IGNORECASE)
    return out


def _drop_removed_lines(rel_path: str, text: str, remove_lines: dict[str, list[str]]) -> str:
    patterns = remove_lines.get(rel_path) or []
    if not patterns:
        return text
    compiled = [re.compile(p) for p in patterns]
    kept = [ln for ln in text.splitlines() if not any(c.search(ln) for c in compiled)]
    return "\n".join(kept)


def test_sanitized_copy_has_no_personal_residue() -> None:
    """入包文件应用完整替换链后，不得残留 profile 声明的个人路径片段与主机名。"""
    try:
        from tools.release.engine.build import (
            _load_literal_replacements,
            _load_path_mapping,
            _load_post_process_remove_lines,
        )
        from tools.release.engine.prepare import prepare_release
    except ImportError as e:  # pragma: no cover
        pytest.skip(f"release engine 不可用: {e}")

    identifiers, markers = _scan_config()
    assert identifiers and markers, "profile [scan] 缺 personal_identifiers / residue_markers"

    try:
        plan = prepare_release(profile="public-full", source="HEAD", audience="public")
    except Exception as e:  # 需要真实仓库状态时按既有 release_ci 惯例跳过
        pytest.skip(f"prepare_release 需要真实仓库状态: {e}")

    path_mapping = _load_path_mapping("public-full")
    literals = _load_literal_replacements("public-full")
    remove_lines = _load_post_process_remove_lines("public-full")

    checks = [(f"identifier:{i}", re.compile(re.escape(i), re.IGNORECASE)) for i in identifiers]
    checks += [(f"marker:{m}", re.compile(m, re.IGNORECASE)) for m in markers]

    violations: list[str] = []
    for entry in plan.file_entries:
        src = _ROOT / entry.rel_path
        if not src.is_file():
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        sanitized = _drop_removed_lines(
            entry.rel_path,
            _apply_chain(text, path_mapping, literals, identifiers),
            remove_lines,
        )
        for name, pat in checks:
            n = len(pat.findall(sanitized))
            if n:
                violations.append(f"{entry.rel_path} [{name}] x{n}")

    assert not violations, "替换链后仍残留个人标识（说明有规则静默 no-op）：\n  " + "\n  ".join(
        sorted(violations)[:25]
    )


def test_personal_hostname_rule_is_high_and_satisfiable(monkeypatch, tmp_path) -> None:
    """personal-hostname 规则：给了标识就 HIGH 命中，不给标识就看不见（证明它确实是新盲区补丁）。"""
    from tools.release.engine import prepare as prep
    from tools.release.engine.models import FileEntry

    identifiers, _ = _scan_config()
    host = identifiers[0]
    (tmp_path / "sample.md").write_text(f"部署到 {host} 上验证\n", encoding="utf-8")
    entries = (FileEntry(rel_path="sample.md", source="tracked", sha256="0" * 64, size=1),)

    monkeypatch.setattr(prep, "_PROJECT_ROOT", tmp_path)

    hits, scanned, _skipped = prep.collect_sensitive_hits(entries, {}, None, [host])
    assert scanned == 1
    assert [h["rule"] for h in hits] == ["personal-hostname"]
    assert hits[0]["severity"] == "HIGH", "主机名命中必须 HIGH，否则 gate 不会 fail-closed"

    blind, _s2, _k2 = prep.collect_sensitive_hits(entries, {}, None, None)
    assert blind == [], "未配置标识时该类命中不存在——正是 v0.49.0 首发的实际处境"

