"""Audience policy 加载器（spec-v2-compiler.md Solution 节"audience policy"）。

读 release/audience/<name>.toml → 解析为 dict。
status="reserved" 的 audience → raise（占位检查，供未来新 audience 占位用）。
phase 3 起 public audience 已升级为完整实现（status 字段已移除），不再触发 reserved 检查。
"""
from __future__ import annotations

from pathlib import Path

import toml

# 项目根目录（audience.py 在 tools/release/engine/，需 4 级 parent 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_AUDIENCE_DIR = _PROJECT_ROOT / "release" / "audience"


def load_audience_policy(name: str) -> dict:
    """加载 audience policy 文件

    Args:
        name: audience 名称，如 "friend" / "public"

    Returns:
        解析后的 toml dict，含 schema_version / audience.{name, profile, components, export_set, gates.static, gates.build_time}

    Raises:
        FileNotFoundError: audience 文件不存在
        ValueError: audience.status == "reserved"（占位检查，供未来新 audience 占位用）
        ValueError: 必填字段缺失
    """
    path = _AUDIENCE_DIR / f"{name}.toml"
    if not path.exists():
        raise FileNotFoundError(f"audience policy not found: {path}")

    policy = toml.load(path)

    # 校验 schema_version
    if policy.get("schema_version") != 1:
        raise ValueError(f"unsupported audience schema_version: {policy.get('schema_version')}")

    audience = policy.get("audience")
    if not audience or not isinstance(audience, dict):
        raise ValueError(f"audience policy missing [audience] section: {path}")

    # reserved 检查（占位 audience 守卫；phase 3 起 public 已升级为完整实现，不触发此 raise）
    if audience.get("status") == "reserved":
        raise ValueError(f"audience '{name}' is reserved, not implemented yet")

    # 必填字段校验
    required_fields = ["name", "profile", "components", "export_set"]
    for field in required_fields:
        if field not in audience:
            raise ValueError(f"audience policy missing required field audience.{field}: {path}")

    # gates 段校验
    gates = audience.get("gates", {})
    if not isinstance(gates, dict):
        raise ValueError(f"audience.gates must be a table: {path}")
    if "static" not in gates or "build_time" not in gates:
        raise ValueError(f"audience.gates must contain static and build_time lists: {path}")

    # core_files 段校验（可选，spec-v4 决策 #4/#6：audience policy 各自定义 core_files）
    # core_files: list[str] glob 模式（包含清单），如 ["server/**", "client/**", "lib/**"]
    # core_files_exclude: list[str] glob 模式（排除清单），如 [".agents/wip/**", "release/profiles/**"]
    # 两者都可选；若存在则必须是 list[str]
    core_files = audience.get("core_files", [])
    if not isinstance(core_files, list):
        raise ValueError(f"audience.core_files must be a list: {path}")
    for item in core_files:
        if not isinstance(item, str):
            raise ValueError(f"audience.core_files items must be strings: {path}")

    core_files_exclude = audience.get("core_files_exclude", [])
    if not isinstance(core_files_exclude, list):
        raise ValueError(f"audience.core_files_exclude must be a list: {path}")
    for item in core_files_exclude:
        if not isinstance(item, str):
            raise ValueError(f"audience.core_files_exclude items must be strings: {path}")

    return policy


def list_available_audiences() -> list[str]:
    """扫描 release/audience/*.toml，返回可用的 audience 名称列表

    不含 reserved 状态的 audience（如 public）。
    """
    if not _AUDIENCE_DIR.exists():
        return []
    result: list[str] = []
    for path in sorted(_AUDIENCE_DIR.glob("*.toml")):
        try:
            policy = toml.load(path)
            audience = policy.get("audience", {})
            if audience.get("status") == "reserved":
                continue
            name = audience.get("name")
            if name:
                result.append(name)
        except Exception:
            # 跳过解析失败的文件
            continue
    return result
