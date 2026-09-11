"""硬化规则检查的 pytest 包装（quick 层）。

规则真源：tools/check_hard_rules.py。任何 C1-C4 违规让测试变红，
对应"说过三遍的规则→会变红的构建"（详见 temp/sdd/agent-trust-guardrails/spec.md）。
"""
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from check_hard_rules import run_checks  # noqa: E402


def test_hard_rules_all_tracked_files():
    violations = run_checks(staged=False)
    assert not violations, (
        "硬化规则违规（修复文件或显式更新 tools/check_hard_rules.py 白名单）：\n"
        + "\n".join(violations)
    )
