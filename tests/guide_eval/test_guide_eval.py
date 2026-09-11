"""pytest 包装：guide 路由盲测（quick 层）。

规则真源：tests/guide_eval/run_eval.py + cases.jsonl。
改 GUIDE_REGISTRY keywords 后必须回测（project_rules.md "Guide 关键词维护"）。
"""
import subprocess
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent


def test_guide_route_eval():
    r = subprocess.run(
        [sys.executable, str(EVAL_DIR / "run_eval.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(EVAL_DIR.parent.parent),
    )
    assert r.returncode == 0, f"guide 路由盲测未达标：\n{r.stdout}\n{r.stderr}"
