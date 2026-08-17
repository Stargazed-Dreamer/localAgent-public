"""分层运行测试，流式输出 + JUnit XML + 崩溃检测 + 结构化报告。

用法:
    uv run python tests/run_all.py              # 默认 quick：仅纯单元测试
    uv run python tests/run_all.py --quick      # 同上
    uv run python tests/run_all.py --full       # 跑全部测试（含 E2E）
    uv run python tests/run_all.py --full --tb=short  # 全量 + 详细 traceback

分层策略对齐 docs/dev-workflow.md "测试运行与问题定位流程"：
- quick: 排除 gpu/browser/real_backend/network/chaos（约 3-4 分钟跑完纯逻辑测试）
- full: 跑全部（E2E 测试依赖 CDP:9222 + 后端:8766，不可连时 conftest 自动 skip）

产出（写入 temp/）:
  - test_full_log.txt       完整 pytest 输出
  - test_results.xml        JUnit XML（结构化真源）
  - test_failure_report.md  结构化报告

与 tools/run_tests_collect.py 共享 tools/test_runner.py 逻辑：
  - 流式 tee（实时输出到控制台 + 写 log，替代 capture_output 黑盒）
  - JUnit XML 解析（比文本正则可靠，pytest-xdist 并行时不受输出顺序影响）
  - 崩溃检测（returncode 非 0-5 标记"进程崩溃"，不假装"0 失败"假绿）
"""
import sys
from pathlib import Path

# 共享运行器（在 tools/ 下，tests/ 需补路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
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
    args = sys.argv[1:]
    filtered = []
    for a in args:
        if a in ("--quick", "--full"):
            mode = a
        elif a.startswith("--tb="):
            tb_mode = a.split("=", 1)[1]
        else:
            filtered.append(a)

    if mode not in ("--quick", "--full"):
        print(f"未知参数: {mode}（支持 --quick / --full / --tb=MODE）", file=sys.stderr)
        return 2

    markers = QUICK_MARKERS if mode == "--quick" else None
    # --durations=10 输出最慢测试，帮定位慢测试
    cmd = build_pytest_cmd(
        extra_args=["--durations=10"] + filtered,
        markers=markers,
        xml_path=XML_FILE,
        tb_mode=tb_mode,
    )
    print(
        f"[run_all] mode={mode}, "
        f"markers={markers or '(none)'}, tb={tb_mode}",
        flush=True,
    )
    return run_and_report(
        cmd,
        log_path=LOG_FILE,
        xml_path=XML_FILE,
        report_path=REPORT_FILE,
        cwd=PROJECT_ROOT,
    )


if __name__ == "__main__":
    sys.exit(main())
