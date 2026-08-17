#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享测试运行器：流式 tee 输出 + JUnit XML 解析 + 崩溃检测 + 报告生成。

被 tools/run_tests_collect.py 和 tests/run_all.py 共用，消除"查不到错误"的根因：

1. 流式 tee：subprocess.Popen 实时读 stdout，边写 log 文件边 echo 控制台。
   跑测试时能实时看到进度和失败，跑完有完整 log（替代 capture_output=True 黑盒）。
2. JUnit XML：--junit-xml 结构化输出，解析 XML 而非文本（pytest-xdist 并行时
   文本输出顺序乱，正则解析易漏；XML 是结构化真源）。
3. 崩溃检测：检查 returncode，非 0/1/2/3/4/5 时标记"进程崩溃"（如
   3221225477 = STATUS_ACCESS_VIOLATION，conftest.py:95-122 已知问题），
   不假装"0 失败"假绿。
4. 报告生成：解析 XML 生成 test_failure_report.md，含崩溃状态 + 失败表格。

退出码分类（pytest 官方 + Windows 崩溃）：
  0  = 全过
  1  = 有失败
  2  = 中断（KeyboardInterrupt）
  3  = 内部错误
  4  = 用法错误
  5  = 没收集到测试
  其他 = 进程崩溃（STATUS_ACCESS_VIOLATION 等致命错误，XML 可能不完整或缺失）

详见 docs/dev-workflow.md "测试运行与问题定位流程" 段。
"""
import re
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT_ROOT / "temp" / "test_full_log.txt"
XML_FILE = PROJECT_ROOT / "temp" / "test_results.xml"
REPORT_FILE = PROJECT_ROOT / "temp" / "test_failure_report.md"

# 排除需要外部资源的 marker，只跑纯单元测试
QUICK_MARKERS = "not gpu and not browser and not real_backend and not network and not chaos"


def classify_exit(rc: int) -> tuple[str, str]:
    """分类 pytest 退出码。

    返回 (status, detail)：
    - passed: 全过
    - failed: 有失败（正常失败，XML 应完整）
    - pytest_error_N: pytest 自身错误（中断/内部/用法/无测试）
    - crashed_N: 进程崩溃（致命错误，XML 可能不完整或缺失，禁止假装"0 失败"）
    """
    if rc == 0:
        return "passed", "全部通过"
    if rc == 1:
        return "failed", "有测试失败"
    error_map = {
        2: "中断（KeyboardInterrupt 或 collection 错误）",
        3: "内部错误",
        4: "用法错误",
        5: "没收集到测试",
    }
    if rc in error_map:
        return f"pytest_error_{rc}", error_map[rc]
    # Windows fatal exception（如 3221225477 = STATUS_ACCESS_VIOLATION）
    # conftest.py:95-122 已知：全量测试时随机崩溃，JUnit XML 无 failure/error 记录
    return f"crashed_{rc}", f"进程崩溃（exit={rc}，可能是 STATUS_ACCESS_VIOLATION 等 Windows fatal exception）"


def parse_junit_xml(xml_path: Path) -> tuple[dict, list[dict]] | tuple[None, list]:
    """解析 JUnit XML，返回 (summary, failures)。

    summary: {tests, failures, errors, skipped, passed, time}
    failures: [{name, classname, file, message, type}, ...]

    XML 不存在或解析失败时返回 (None, [])，调用方应据此标记"XML 未生成"。
    """
    if not xml_path.exists():
        return None, []

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        return None, [{"parse_error": str(e)}]

    root = tree.getroot()
    # pytest junit-xml: <testsuites><testsuite ...><testcase ...><failure/></testcase></testsuite></testsuites>
    # 兼容 root 是 <testsuite> 或 <testsuites>
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    total = failures = errors = skipped = 0
    total_time = 0.0
    failure_list: list[dict] = []

    for suite in suites:
        total += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        try:
            total_time += float(suite.get("time", 0))
        except (ValueError, TypeError):
            pass

        for tc in suite.findall("testcase"):
            # failure 节点 = 测试断言失败
            fail_node = tc.find("failure")
            err_node = tc.find("error")
            if fail_node is not None or err_node is not None:
                node = fail_node if fail_node is not None else err_node
                kind = "failure" if fail_node is not None else "error"
                name = tc.get("name", "<unknown>")
                classname = tc.get("classname", "")
                file = tc.get("file", "")
                message = node.get("message", "") or (node.text or "").strip()
                # 截断长 message
                if len(message) > 200:
                    message = message[:197] + "..."
                failure_list.append({
                    "kind": kind,
                    "name": name,
                    "classname": classname,
                    "file": file,
                    "message": message,
                    "type": node.get("type", ""),
                })

    passed = total - failures - errors - skipped
    return {
        "tests": total,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "passed": passed,
        "time": total_time,
    }, failure_list


def generate_report(
    xml_path: Path,
    rc: int,
    elapsed: float,
    log_path: Path,
    report_path: Path,
    cmd: list[str],
) -> str:
    """生成 test_failure_report.md，返回报告内容。"""
    status, detail = classify_exit(rc)
    summary, failures = parse_junit_xml(xml_path)

    report = ["# 测试报告\n"]
    report.append("## 运行状态\n")
    report.append(f"- **状态**：`{status}` — {detail}")
    report.append(f"- **退出码**：{rc}")
    report.append(f"- **耗时**：{elapsed:.1f}s")
    report.append(f"- **命令**：`{' '.join(cmd)}`\n")

    if summary is None:
        report.append("## 结果摘要\n")
        report.append("> JUnit XML 未生成或解析失败（进程崩溃或被中断）。")
        report.append(f"> 无法解析结构化结果，**不能假设\"0 失败\"**。")
        report.append(f"> 完整输出见 `{log_path.relative_to(PROJECT_ROOT)}`。\n")
    else:
        report.append("## 结果摘要\n")
        report.append(
            f"```\n{summary['passed']} passed, {summary['failures']} failed, "
            f"{summary['errors']} errors, {summary['skipped']} skipped, "
            f"{summary['tests']} total in {summary['time']:.1f}s\n```"
        )

        report.append(f"\n## 失败/错误测试 ({len(failures)} 个)\n")
        if failures:
            report.append("| # | 类型 | 测试 | 错误摘要 |")
            report.append("|---|------|------|----------|")
            for i, f in enumerate(failures, 1):
                # 测试全名：classname::name（classname 含模块路径）
                full = f"{f['classname']}::{f['name']}" if f["classname"] else f["name"]
                msg = f["message"] if f["message"] else f["type"] or "(无消息)"
                # 转义 markdown 表格分隔符
                msg = msg.replace("|", "\\|").replace("\n", " ")
                report.append(f"| {i} | {f['kind']} | `{full}` | {msg} |")
        else:
            if status == "passed":
                report.append("（无失败）")
            else:
                report.append(
                    f"> 状态={status} 但 XML 解析出 0 失败。"
                    f"可能是崩溃发生在 pytest 汇总阶段（conftest.py:95-122 已知问题），"
                    f"XML 未记录崩溃。完整输出见 log。"
                )

    report.append(f"\n## 完整 log\n见 `{log_path.relative_to(PROJECT_ROOT)}`\n")
    if xml_path.exists():
        report.append(f"## JUnit XML\n见 `{xml_path.relative_to(PROJECT_ROOT)}`\n")

    report.append("\n## 分类建议\n")
    report.append("- **真实 bug**：单独跑该测试文件也失败 → 改实现代码")
    report.append("- **测试隔离问题**：单独跑通过，全量跑失败 → 改测试（加 cleanup/隔离 fixture）或标记已知问题")
    report.append("- **环境问题**：缺依赖/权限/路径 → 修环境")
    report.append("- **过时测试**：测试期望的行为已不存在 → 删/改测试")
    report.append("- **进程崩溃**：returncode 非 0-5，多为 Qt access violation → 看 log 末尾，分段跑定位\n")
    report.append("单独跑确认：")
    report.append("```powershell")
    report.append(".venv\\Scripts\\python.exe -m pytest tests/test_xxx.py -v --tb=short")
    report.append("```\n")

    content = "\n".join(report)
    report_path.write_text(content, encoding="utf-8")
    return content


def run_pytest_streaming(
    cmd: list[str],
    log_path: Path = LOG_FILE,
    xml_path: Path | None = None,
    cwd: Path = PROJECT_ROOT,
) -> tuple[int, float]:
    """流式 tee 运行 pytest。

    - subprocess.Popen + stdout=PIPE + stderr=STDOUT（合并）
    - 实时逐行读：边写 log 文件边 echo 控制台（用户能看到进度和失败）
    - 不用 capture_output=True（黑盒），不用 timeout=600（卡死等 10 分钟）

    返回 (returncode, elapsed_seconds)。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 清空旧 log（避免上次残留混淆）
    log_path.write_text("", encoding="utf-8")

    print(f"[run] {' '.join(cmd)}", flush=True)
    print(f"[run] log -> {log_path}", flush=True)
    if xml_path:
        print(f"[run] xml -> {xml_path}", flush=True)
    print(f"[run] 实时输出中（同时写 log 文件）...\n", flush=True)

    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout，避免顺序错乱
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,  # 行缓冲，实时输出
    )

    # 主线程逐行读（Popen.stdout.readline 阻塞，读到 EOF 返回 ""）
    with open(log_path, "a", encoding="utf-8") as log_f:
        for line in iter(proc.stdout.readline, ""):
            log_f.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()

    proc.stdout.close()
    proc.wait()
    elapsed = time.monotonic() - start
    print(f"\n[done] exit={proc.returncode}, elapsed={elapsed:.1f}s", flush=True)
    return proc.returncode, elapsed


def build_pytest_cmd(
    extra_args: list[str] | None = None,
    markers: str | None = None,
    xml_path: Path | None = XML_FILE,
    tb_mode: str = "line",
    python_exe: str | None = None,
) -> list[str]:
    """构造 pytest 命令。

    - markers: -m 表达式（如 QUICK_MARKERS），None 表示不筛选
    - xml_path: --junit-xml 路径，None 表示不生成 XML
    - tb_mode: --tb 模式（line/short/long），默认 line（全量收集最简）
    - python_exe: python 路径，None 用 sys.executable

    注意：不在此加 -q/-ra/--maxfail/--timeout 等——这些走 pyproject.toml addopts，
    避免重复。脚本层只补 --tb（覆盖 addopts 的 --tb=short）和 --junit-xml。
    """
    if python_exe is None:
        python_exe = sys.executable
    cmd = [python_exe, "-m", "pytest", "tests/"]
    if markers:
        cmd.extend(["-m", markers])
    cmd.extend(["--tb", tb_mode])
    if xml_path:
        cmd.extend(["--junit-xml", str(xml_path)])
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def run_and_report(
    cmd: list[str],
    log_path: Path = LOG_FILE,
    xml_path: Path = XML_FILE,
    report_path: Path = REPORT_FILE,
    cwd: Path = PROJECT_ROOT,
) -> int:
    """完整流程：流式运行 → 解析 XML → 生成报告 → 返回退出码。"""
    rc, elapsed = run_pytest_streaming(cmd, log_path, xml_path, cwd)
    content = generate_report(xml_path, rc, elapsed, log_path, report_path, cmd)
    print(f"[done] report -> {report_path}", flush=True)
    # 打印报告关键摘要到控制台
    status, detail = classify_exit(rc)
    summary, failures = parse_junit_xml(xml_path)
    print(f"[result] status={status} ({detail})", flush=True)
    if summary:
        print(
            f"[result] {summary['passed']} passed, {summary['failures']} failed, "
            f"{summary['errors']} errors, {summary['skipped']} skipped",
            flush=True,
        )
    elif failures and "parse_error" in failures[0]:
        print(f"[result] XML 解析失败：{failures[0]['parse_error']}", flush=True)
    else:
        print("[result] XML 未生成（进程崩溃或被中断），见 log 末尾", flush=True)
    return rc
