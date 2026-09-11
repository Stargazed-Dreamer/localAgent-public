"""guide 路由盲测评测器（P3）。

用法：
  uv run python tests/guide_eval/run_eval.py                 # 纯关键词路径（确定性，无需后端）
  uv run python tests/guide_eval/run_eval.py --with-embedding  # 启用向量化（需后端在线）
  uv run python tests/guide_eval/run_eval.py --threshold 0.85  # 自定义红阈值

判定：
  - expected_top1 非 null：top1 strong_match 候选 == expected 即通过
  - expected_top1 为 null：无 strong_match 候选（或无候选）即通过（weak match 容忍）
  - known_gap=true 的用例不计入阈值（已知路由缺口，单独报告），防止基线永久红
退出码：top1 命中率 < 阈值（默认 0.85）→ 1（让构建变红）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CASES_FILE = Path(__file__).parent / "cases.jsonl"
DEFAULT_THRESHOLD = 0.85


def load_cases() -> list[dict]:
    cases = []
    with open(CASES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("//"):
                cases.append(json.loads(line))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--with-embedding", action="store_true",
                    help="不 monkeypatch 向量化（需后端在线）；默认纯关键词路径保证确定性")
    ap.add_argument("--show-pass", action="store_true", help="显示通过用例明细")
    args = ap.parse_args()

    if not args.with_embedding:
        # 关键词确定性路径：屏蔽向量化（match_task_candidates 在函数内懒 import）
        import server.agent_guide_embedder as _emb
        _emb.semantic_match = lambda *a, **k: []

    from server.agent_guide import match_task_candidates

    cases = load_cases()
    passed = failed = skipped = 0
    mismatches: list[str] = []

    for case in cases:
        query, expected = case["query"], case.get("expected_top1")
        cands = match_task_candidates(query, top_n=3)
        strong_top1 = next(
            (c["task_type"] for c in cands if c.get("strong_match")), None
        )
        ok = (strong_top1 == expected) if expected else (strong_top1 is None)
        if case.get("known_gap"):
            skipped += 1
            if not ok:
                mismatches.append(f"[known_gap] {query!r} 期望 {expected} 实际 {strong_top1}")
        elif ok:
            passed += 1
            if args.show_pass:
                print(f"  PASS {query!r} → {strong_top1}")
        else:
            failed += 1
            scores = ", ".join(f"{c['task_type']}={c['score']}" for c in cands)
            mismatches.append(f"{query!r}\n    期望 {expected}  实际 {strong_top1}  候选[{scores}]")

    total = passed + failed
    acc = passed / total if total else 0.0
    print(f"\n[guide_eval] 用例 {len(cases)}（known_gap 跳过 {skipped}）"
          f" 命中 {passed}/{total} = {acc:.1%}（红阈值 {args.threshold:.0%}）")
    if mismatches:
        print("串台/未命中明细：")
        for m in mismatches:
            print(f"  {m}")
    if acc < args.threshold:
        print("[guide_eval] 命中率低于阈值，构建红（改 keywords 后必须回测）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
