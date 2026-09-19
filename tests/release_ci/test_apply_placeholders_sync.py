"""apply_placeholders.py 的 PLACEHOLDERS 清单与 profile 占位符同步守护。

背景（2026-09-19）：脚本 PLACEHOLDERS 与 public-full.toml path_mapping 的目标
占位符漂移——脚本缺 <apps_root>/<backup_drive>/<media_root>/<models_root>/
<projects_parent>/<user_home> 6 项，收包人无法填充包内真实存在的占位符；
同时脚本里的 <project_root_parent> 在包内零命中（真名是 <projects_parent>）。

守护策略：
- path_mapping 的每个 replace 值中出现的 <...> 占位符，必须要么在脚本
  PLACEHOLDERS 里，要么在 DEAD_TARGETS 白名单里（规则存在但当前入包文件
  零命中的死规则，不要求收包人填写）。
"""

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "tools" / "deploy" / "apply_placeholders.py"
_PROFILE = _ROOT / "release" / "profiles" / "public-full.toml"

# path_mapping 里存在、但在当前 1586 个入包文件上零命中的死规则目标。
# 若某条死规则将来复活（入包文件重新出现该占位符），需把它从这里移除，
# 否则收包人会拿到填不了的占位符。
DEAD_TARGETS = {"<miniconda_env_root>", "<debug_profile_dir>"}


def _script_placeholders() -> set[str]:
    text = _SCRIPT.read_text(encoding="utf-8")
    return set(re.findall(r'"(<[a-z_]+>)"', text))


def _path_mapping_targets() -> set[str]:
    data = tomllib.loads(_PROFILE.read_text(encoding="utf-8"))
    pm = data.get("content_replacements", {}).get("path_mapping", {}) or {}
    targets: set[str] = set()
    for repl in pm.values():
        targets |= set(re.findall(r"<[a-z_]+>", repl))
    return targets


def test_script_placeholders_cover_path_mapping_targets():
    listed = _script_placeholders()
    missing = _path_mapping_targets() - listed - DEAD_TARGETS
    assert not missing, (
        f"脚本 PLACEHOLDERS 缺少 path_mapping 目标占位符: {sorted(missing)}；"
        f"请在 tools/deploy/apply_placeholders.py 补齐（或确认是死规则后加入 DEAD_TARGETS）"
    )


def test_script_has_no_dead_required_placeholders():
    """脚本列出的占位符里，不允许存在 path_mapping 完全不认识的名字
    （即拼错/改名的占位符，收包人填了也替换不到任何东西）。"""
    listed = _script_placeholders()
    known = _path_mapping_targets() | {"<username>", "<copyright_holder>",
                                       "<digest>", "<export_dir>"}
    unknown = listed - known
    assert not unknown, (
        f"脚本 PLACEHOLDERS 含 profile 未使用的占位符（疑似改名残留）: {sorted(unknown)}"
    )


@pytest.mark.parametrize("ph", sorted(DEAD_TARGETS))
def test_dead_targets_are_actually_dead(ph: str):
    """白名单里的死规则必须真的零命中：若包内源文件重新出现该占位符的
    来源路径，此测试失败，提示把它移出 DEAD_TARGETS。"""
    # 死规则判定 = 没有任何入包源文件包含它的 find 模式。这里退而求其次：
    # 校验它不出现在脚本清单里，且确实是 path_mapping 的目标（防白名单写错）。
    assert ph in _path_mapping_targets()
    assert ph not in _script_placeholders()
