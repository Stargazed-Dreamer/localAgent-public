#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Token 消耗统计报告

数据来源：
  1. data/llm/stats/llm_pool_stats.json  — 并发池持久化的 per-project token 统计（实时）
  2. workspace/community_review/社群/reports/summarize_direct_checkpoint_all.json — 帖子总结的历史 token 数据
  3. temp/mimo_comment_state.json — 代码注释进度（无 token 数据，仅显示处理量）
  4. temp/mimo_lrc_state.json — LRC 分析进度（无 token 数据，仅显示处理量）

用法:
  uv run python tools/llm/llm_token_stats.py              # 显示统计
  uv run python tools/llm/llm_token_stats.py --import      # 将历史 checkpoint token 导入池统计文件
  uv run python tools/llm/llm_token_stats.py --watch       # 持续刷新（每10秒）
"""

import json
import time
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

POOL_STATS_FILE = PROJECT_ROOT / "data" / "llm" / "stats" / "llm_pool_stats.json"
# 各脚本独立的 stats 文件（避免多进程写冲突）—— 动态扫描 stats 目录
_STATS_DIR = PROJECT_ROOT / "data" / "llm" / "stats"
POOL_STATS_FILES = sorted(_STATS_DIR.glob("*.json")) if _STATS_DIR.exists() else [POOL_STATS_FILE]
POSTS_CHECKPOINT = PROJECT_ROOT / "output" / "社群" / "reports" / "summarize_direct_checkpoint_all.json"
COMMENT_STATE = PROJECT_ROOT / "temp" / "mimo_comment_state.json"
LRC_STATE = PROJECT_ROOT / "temp" / "mimo_lrc_state.json"
LRC_RESULTS = PROJECT_ROOT / "temp" / "mimo_lrc_results.json"


def load_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def get_posts_checkpoint_tokens():
    """从帖子总结 checkpoint 提取历史 token 数据"""
    data = load_json(POSTS_CHECKPOINT)
    if not data:
        return None
    results = data.get("results", [])
    pt = sum(x.get("usage", {}).get("prompt_tokens", 0) for x in results)
    ct = sum(x.get("usage", {}).get("completion_tokens", 0) for x in results)
    tt = sum(x.get("usage", {}).get("total_tokens", 0) for x in results)
    calls = sum(1 for x in results if x.get("usage", {}).get("total_tokens", 0) > 0)
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "total_tokens": tt,
        "calls": calls,
        "posts_count": len(results),
    }


def get_comment_progress():
    """代码注释进度"""
    data = load_json(COMMENT_STATE)
    if not data:
        return None
    files = data.get("files", {})
    completed = sum(1 for v in files.values() if v.get("completed"))
    units = sum(v.get("units_processed", 0) for v in files.values())
    # 按项目路径分组
    by_project = {}
    for filepath, info in files.items():
        # 从路径推断项目名
        parts = Path(filepath).parts
        project = "unknown"
        for known in ("MuseArc", "MusePlayer", "MelodyMark", "ElasticBreath", "server"):
            if known in parts:
                project = known
                break
        if project not in by_project:
            by_project[project] = {"files": 0, "units": 0}
        by_project[project]["files"] += 1
        by_project[project]["units"] += info.get("units_processed", 0)
    return {
        "total_files": len(files),
        "completed_files": completed,
        "total_units": units,
        "by_project": by_project,
    }


def get_lrc_progress():
    """LRC 分析进度"""
    state = load_json(LRC_STATE)
    results = load_json(LRC_RESULTS)
    return {
        "processed": len(state) if state else 0,
        "results": len(results) if results else 0,
    }


def import_historical_tokens():
    """将帖子总结 checkpoint 中的历史 token 数据导入对应的 stats 文件"""
    posts_stats_file = PROJECT_ROOT / "data" / "llm" / "stats" / "llm_pool_stats_posts.json"
    pool_data = load_json(posts_stats_file) or {"projects": {}}
    projects = pool_data.get("projects", {})

    posts_tokens = get_posts_checkpoint_tokens()
    if posts_tokens and posts_tokens["total_tokens"] > 0:
        proj = "社群帖子总结"
        # 如果池统计中已有该项目的数据，只补充差额（避免重复计算）
        existing = projects.get(proj, {})
        existing_tt = existing.get("total_tokens", 0)
        if existing_tt < posts_tokens["total_tokens"]:
            # 池统计中的数据是本次运行新增的，checkpoint 是总量
            # 差额 = checkpoint总量 - 池已有量
            diff_pt = posts_tokens["prompt_tokens"] - existing.get("prompt_tokens", 0)
            diff_ct = posts_tokens["completion_tokens"] - existing.get("completion_tokens", 0)
            diff_tt = posts_tokens["total_tokens"] - existing_tt
            diff_calls = posts_tokens["calls"] - existing.get("calls", 0)

            if diff_tt > 0:
                if proj not in projects:
                    projects[proj] = {"prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0, "calls": 0}
                projects[proj]["prompt_tokens"] += diff_pt
                projects[proj]["completion_tokens"] += diff_ct
                projects[proj]["total_tokens"] += diff_tt
                projects[proj]["calls"] += diff_calls
                print(f"  导入 {proj}: +{diff_tt} tokens (差额: 池={existing_tt}, checkpoint={posts_tokens['total_tokens']})")
            else:
                print(f"  跳过 {proj}: 池统计已包含全部 token ({existing_tt} >= {posts_tokens['total_tokens']})")
        else:
            print(f"  跳过 {proj}: 池统计已包含全部 token ({existing_tt} >= {posts_tokens['total_tokens']})")

    pool_data["projects"] = projects
    pool_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    posts_stats_file.parent.mkdir(parents=True, exist_ok=True)
    posts_stats_file.write_text(json.dumps(pool_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保存到 {posts_stats_file}")


def merge_pool_stats():
    """合并所有独立的 stats 文件（每个脚本一个文件，避免多进程写冲突）"""
    merged = {}
    sources = []
    for stats_file in POOL_STATS_FILES:
        data = load_json(stats_file)
        if not data or not data.get("projects"):
            continue
        sources.append(str(stats_file.name))
        for proj, st in data["projects"].items():
            if proj not in merged:
                merged[proj] = {"prompt_tokens": 0, "completion_tokens": 0,
                                "total_tokens": 0, "calls": 0}
            merged[proj]["prompt_tokens"] += st.get("prompt_tokens", 0)
            merged[proj]["completion_tokens"] += st.get("completion_tokens", 0)
            merged[proj]["total_tokens"] += st.get("total_tokens", 0)
            merged[proj]["calls"] += st.get("calls", 0)
    return merged, sources


def print_report():
    """打印 token 统计报告"""
    projects, sources = merge_pool_stats()
    posts_tokens = get_posts_checkpoint_tokens()
    comment_prog = get_comment_progress()
    lrc_prog = get_lrc_progress()

    print(f"\n{'='*70}")
    print(f"  LLM Token 消耗统计报告  (更新于 {time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{'='*70}")

    # ---- Per-Project Token 统计（合并多个 stats 文件） ----
    print(f"\n  ┌─ Per-Project Token 统计（合并 {len(sources)} 个 stats 文件）")
    print(f"  │  来源: {', '.join(sources) if sources else '(无)'}")
    if projects:
        print(f"  │  {'Project':<22s} {'Calls':>7s} {'Prompt':>12s} {'Completion':>12s} {'Total':>12s}")
        print(f"  │  {'-'*67}")
        total_pt = total_ct = total_tt = total_cl = 0
        for proj, st in sorted(projects.items(), key=lambda x: -x[1].get("total_tokens", 0)):
            cl = st.get("calls", 0)
            pt = st.get("prompt_tokens", 0)
            ct = st.get("completion_tokens", 0)
            tt = st.get("total_tokens", 0)
            print(f"  │  {proj:<22s} {cl:7d} {pt:12,d} {ct:12,d} {tt:12,d}")
            total_pt += pt
            total_ct += ct
            total_tt += tt
            total_cl += cl
        print(f"  │  {'-'*67}")
        print(f"  │  {'TOTAL':<22s} {total_cl:7d} {total_pt:12,d} {total_ct:12,d} {total_tt:12,d}")
    else:
        print(f"  │  (暂无数据)")

    # ---- 帖子总结 Checkpoint（独立验证） ----
    print(f"\n  ┌─ 社群帖子总结 Checkpoint（独立验证）")
    print(f"  │  来源: {POSTS_CHECKPOINT}")
    if posts_tokens:
        print(f"  │  已处理帖子: {posts_tokens['posts_count']:,d}")
        print(f"  │  Prompt tokens:     {posts_tokens['prompt_tokens']:>12,d}")
        print(f"  │  Completion tokens: {posts_tokens['completion_tokens']:>12,d}")
        print(f"  │  Total tokens:      {posts_tokens['total_tokens']:>12,d}")
        print(f"  │  有效调用数:        {posts_tokens['calls']:>12,d}")
        if posts_tokens["posts_count"] > 0:
            avg = posts_tokens["total_tokens"] / posts_tokens["posts_count"]
            print(f"  │  平均每篇 tokens:   {avg:>12,.0f}")
    else:
        print(f"  │  (暂无数据)")

    # ---- 代码注释进度 ----
    print(f"\n  ┌─ 代码注释进度")
    print(f"  │  来源: {COMMENT_STATE}")
    if comment_prog:
        print(f"  │  总文件: {comment_prog['total_files']}  已完成: {comment_prog['completed_files']}  处理单元: {comment_prog['total_units']}")
        print(f"  │  按项目分组:")
        for proj, info in sorted(comment_prog["by_project"].items()):
            print(f"  │    {proj:<20s} files={info['files']:4d}  units={info['units']:4d}")
    else:
        print(f"  │  (暂无数据)")

    # ---- LRC 分析进度 ----
    print(f"\n  ┌─ LRC 歌词分析进度")
    print(f"  │  来源: {LRC_STATE}")
    if lrc_prog:
        print(f"  │  已处理: {lrc_prog['processed']}  结果数: {lrc_prog['results']}")
    else:
        print(f"  │  (暂无数据)")

    # ---- 汇总 ----
    print(f"\n  ┌─ 汇总")
    pool_tt = sum(v.get("total_tokens", 0) for v in projects.values())
    posts_tt = posts_tokens["total_tokens"] if posts_tokens else 0
    # 帖子总结的 token 在 pool stats 和 checkpoint 中可能重复，取较大值
    grand_total = max(pool_tt, posts_tt)
    print(f"  │  池统计 Total tokens:  {pool_tt:>12,d}")
    print(f"  │  Checkpoint Total:     {posts_tt:>12,d}")
    print(f"  │  ─────────────────────────────────")
    print(f"  │  累计消耗（取较大值）: {grand_total:>12,d}")
    if grand_total > 0:
        # mimo-v2.5-pro 2x credit, 估算消耗的 credit
        print(f"  │  估算消耗 credit:      {grand_total*2:>12,d} (pro模型2x)")
    print(f"\n{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="LLM Token 消耗统计报告")
    parser.add_argument("--import", dest="import_data", action="store_true",
                        help="将历史 checkpoint token 导入池统计文件")
    parser.add_argument("--watch", action="store_true",
                        help="持续刷新（每10秒）")
    args = parser.parse_args()

    if args.import_data:
        print("导入历史 token 数据...")
        import_historical_tokens()
        print()
        print_report()
        return

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")  # 清屏
                print_report()
                time.sleep(10)
        except KeyboardInterrupt:
            print("\n已停止")
        return

    print_report()


if __name__ == "__main__":
    main()
