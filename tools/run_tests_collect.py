#!/usr/bin/env python3
"""一次性跑全测试，流式输出 + JUnit XML + 崩溃检测 + 结构化报告。

避免"测→发现一个错→修→再测→又发现新错"循环，也避免"跑半天查不到错误"。
策略：--maxfail=500 + --tb=line 拿到全部失败摘要，--junit-xml 结构化输出，
实时 tee 到控制台 + 写 log 文件，崩溃时检测 returncode 标记状态。

用法:
  uv run python tools/run_tests_collect.py           # 默认 quick：仅纯单元测试
  uv run python tools/run_tests_collect.py --full    # 跑全部测试（含 E2E）
  uv run python tools/run_tests_collect.py --full --tb=short  # 全量 + 详细 traceback
  uv run python tools/run_tests_collect.py --no-list # 不打印失败清单（默认总是打印）

产出:
  - temp/test_full_log.txt       完整 pytest 输出（含 stderr，合并到 stdout）
  - temp/test_results.xml        JUnit XML（结构化真源，工具可解析）
  - temp/test_failure_report.md  结构化报告（状态/摘要/失败表格/分类建议）

排除需要外部资源的 marker（gpu/browser/real_backend/network/chaos），只跑纯单元测试。
如需跑这些，加 --full。

详见 docs/dev-workflow.md "测试运行与问题定位流程" 段。
"""
import sys
from pathlib import Path

# 共享运行器（流式 tee + XML 解析 + 崩溃检测 + 报告生成）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runner import (  # noqa: E402
    LOG_FILE,
    PROJECT_ROOT,
    QUICK_MARKERS,
    REPORT_FILE,
    XML_FILE,
    build_pytest_cmd,
    run_and_report,
)


def main() -> int:
    # 解析参数
    mode = "--quick"
    tb_mode = "line"
    show_list = True  # 默认总是输出失败清单（agent 拿全量错误的机械防线），--no-list 显式关闭
    args = sys.argv[1:]
    filtered = []
    for a in args:
        if a in ("--quick", "--full"):
            mode = a
        elif a == "--no-list":
            show_list = False
        elif a.startswith("--tb="):
            tb_mode = a.split("=", 1)[1]
        else:
            filtered.append(a)

    markers = QUICK_MARKERS if mode == "--quick" else None
    # -n auto：xdist 并行（addopts 已移除 -n auto，脚本层显式加；受限沙箱可用 -n0 覆盖）
    cmd = build_pytest_cmd(
        extra_args=["-n", "auto"] + filtered,
        markers=markers,
        xml_path=XML_FILE,
        tb_mode=tb_mode,
    )
    print(
        f"[run_tests_collect] mode={mode}, "
        f"markers={markers or '(none)'}, tb={tb_mode}, "
        f"failure_list={'on' if show_list else 'off'}",
        flush=True,
    )
    return run_and_report(
        cmd,
        log_path=LOG_FILE,
        xml_path=XML_FILE,
        report_path=REPORT_FILE,
        cwd=PROJECT_ROOT,
        show_failure_list=show_list,
    )


if __name__ == "__main__":
    sys.exit(main())
