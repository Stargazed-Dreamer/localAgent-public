"""代码执行路由 - 通过子进程实时运行Python代码片段

Python 片段默认通过 stdin 送入子解释器执行，不落临时脚本文件。
输出超过阈值时自动截断，保留完整输出供后续按需查看。
"""

import asyncio
import codecs
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request

from lib.schema import BaseSchema
from server.command_guard import AGENT_BLOCK_MESSAGE, check_command

logger = logging.getLogger("localagent.exec")
router = APIRouter(prefix="/exec", tags=["代码执行"])
terminal_router = APIRouter(prefix="/terminals", tags=["终端会话"])
output_router = APIRouter(prefix="/output", tags=["执行输出"])

# 临时脚本目录
TEMP_DIR = Path(__file__).parent.parent / "temp"
TEMP_DIR.mkdir(exist_ok=True)

# 输出截断阈值
MAX_OUTPUT_CHARS = 8000  # 超过此长度截断返回
TRUNCATE_TAIL_CHARS = 2000  # 截断时保留尾部字符数

# 输出缓冲区：存储完整输出供按需查看
_output_buffers: dict[str, dict] = {}  # exec_id -> {stdout, stderr, code, timestamp}
_MAX_BUFFERS = 20  # 最多保留最近20条

# 执行历史（最近10条摘要）
_exec_history: list[dict] = []
_MAX_HISTORY = 10

# ========== 终端会话管理 ==========
# 持久化的后台命令会话，支持状态轮询和浏览器监控
_terminals: dict[str, dict] = {}  # tid -> session dict
_MAX_TERMINALS = 20  # 最多保留20个终端会话
_MAX_RUNNING_TERMINALS = 10
_spawn_inflight = 0  # 4-8: spawn await 窗口内的占位计数（上限检查原子化用）
_TERMINAL_MEMORY_TAIL_CHARS = 64 * 1024
_TERMINAL_DETAIL_DEFAULT_CHARS = 8000
_TERMINAL_OUTPUT_MAX_BYTES = 256 * 1024
TERMINAL_DIR = TEMP_DIR / "terminals"
TERMINAL_DIR.mkdir(exist_ok=True)


def get_status() -> dict:
    """Exec 状态概览（供 /health 调用，不暴露 raw 私有 dict）"""
    import time as _time
    running = [t for t in _terminals.values() if t["status"] == "running"]
    return {
        "temp_dir": str(TEMP_DIR),
        "history_count": len(_exec_history),
        "terminals_total": len(_terminals),
        "terminals_running": len(running),
        "terminals": [
            {
                "tid": t["tid"],
                "label": t["label"],
                "cmd": t["cmd"][:80],
                "status": t["status"],
                "pid": t["pid"],
                "elapsed": (t["finished_at"] or _time.time()) - t["started_at"],
                "stdout_chars": t["stdout_chars"],
                "stderr_chars": t["stderr_chars"],
                "output_bytes": t["stdout_bytes"] + t["stderr_bytes"],
            }
            for t in sorted(_terminals.values(), key=lambda x: x["started_at"], reverse=True)[:10]
        ],
    }


def reset_state() -> None:
    """重置模块级状态（测试隔离用）"""
    _exec_history.clear()
    _terminals.clear()
    _output_buffers.clear()

def _utf8_process_env(cwd: str | None = None) -> dict:
    """子进程统一使用 UTF-8，降低 Windows 控制台编码影响。"""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if cwd:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = cwd if not existing else cwd + os.pathsep + existing
    return env


def _resolve_work_dir(cwd: str) -> Path:
    work_dir = Path(cwd).expanduser() if cwd else Path(__file__).parent.parent
    work_dir = work_dir.resolve()
    if not work_dir.is_dir():
        raise ValueError(f"工作目录不存在: {work_dir}")
    return work_dir


def _workspace_python(work_dir: Path) -> Path | None:
    candidates = [
        work_dir / ".venv" / "Scripts" / "python.exe",
        work_dir / ".venv" / "bin" / "python",
        work_dir / "venv" / "Scripts" / "python.exe",
        work_dir / "venv" / "bin" / "python",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _resolve_python_executable(
    work_dir: Path,
    environment: str,
    python_path: str,
) -> tuple[Path, str]:
    if python_path:
        executable = Path(python_path).expanduser().resolve()
        if not executable.is_file():
            raise ValueError(f"Python 解释器不存在: {executable}")
        return executable, "explicit"
    if environment == "workspace_venv":
        executable = _workspace_python(work_dir)
        if not executable:
            raise ValueError(f"未找到项目虚拟环境: {work_dir / '.venv'}")
        return executable, "workspace_venv"
    if environment == "system":
        system_python = shutil.which("python") or shutil.which("python3")
        if not system_python:
            raise ValueError("系统 PATH 中未找到 Python")
        return Path(system_python).resolve(), "system"
    return Path(sys.executable).resolve(), "localagent"


def _resolve_shell(shell: str, cmd: str) -> tuple[list[str], dict]:
    """构建命令并返回实际解释器元数据。"""
    requested = (shell or "cmd").lower()
    if requested == "python":
        command = [sys.executable, "-X", "utf8", "-u"]
        command += ["-c", cmd] if cmd.strip() else ["-i"]
        return command, {"requested_shell": requested, "shell_executable": sys.executable}
    if requested in ("powershell", "pwsh"):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise ValueError("未找到 PowerShell（pwsh.exe 或 powershell.exe）")
        return [executable, "-NoProfile", "-NonInteractive", "-Command", cmd], {
            "requested_shell": requested,
            "shell_executable": executable,
            "shell_family": "pwsh" if Path(executable).stem.lower() == "pwsh" else "windows_powershell",
        }
    if requested == "bash" and sys.platform != "win32":
        executable = shutil.which("bash") or "bash"
        return [executable, "-c", cmd], {"requested_shell": requested, "shell_executable": executable}
    if sys.platform == "win32":
        executable = os.environ.get("COMSPEC", "cmd.exe")
        return [executable, "/d", "/s", "/c", f"chcp 65001>nul & {cmd}"], {
            "requested_shell": requested,
            "shell_executable": executable,
            "shell_family": "cmd",
        }
    executable = shutil.which("sh") or "sh"
    return [executable, "-c", cmd], {"requested_shell": requested, "shell_executable": executable}


@lru_cache(maxsize=16)
def _shell_version(executable: str, shell_family: str) -> str:
    try:
        if shell_family in ("pwsh", "windows_powershell"):
            command = [executable, "-NoProfile", "-NonInteractive", "-Command",
                       "$PSVersionTable.PSVersion.ToString()"]
        elif shell_family == "cmd":
            command = [executable, "/d", "/c", "ver"]
        elif shell_family == "python":
            command = [executable, "--version"]
        else:
            command = [executable, "--version"]
        # 用 bytes + _decode_output 多编码尝试，避免 cmd.exe GBK 输出被 UTF-8 误解码为乱码
        # （中文 Windows cmd.exe ver 命令输出 "版本" 是 GBK 字节，UTF-8 解码 → "ï¿½æ±¾"）
        result = subprocess.run(command, capture_output=True, timeout=3)
        text = _decode_output(result.stdout or result.stderr)
        return text.strip().splitlines()[0] if text else "unknown"
    except Exception:
        return "unknown"


def _decode_output(data: bytes) -> str:
    """解码子进程输出。优先 UTF-8，失败时回退到 Windows 常见编码。"""
    if not data:
        return ""
    candidates = ["utf-8", "gbk", "mbcs"]
    best = None
    best_bad = 1 << 30  # 哨兵：首个候选必然入选（原 None 触发比较类型错）
    for enc in candidates:
        try:
            text = data.decode(enc, errors="replace")
        except LookupError:
            continue
        bad = text.count("\ufffd")
        if best is None or bad < best_bad:
            best = text
            best_bad = bad
        if bad == 0:
            return text
    return best or data.decode("utf-8", errors="replace")


def _build_shell_command(shell: str, cmd: str) -> list[str]:
    """构建命令行。cmd 默认切到 UTF-8 code page，python 模式绕开 shell。"""
    return _resolve_shell(shell, cmd)[0]


def _store_output(exec_id: str, code: str, stdout: str, stderr: str):
    """存储完整输出到缓冲区"""
    _output_buffers[exec_id] = {
        "stdout": stdout,
        "stderr": stderr,
        "code": code,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 超出上限时删除最旧的
    while len(_output_buffers) > _MAX_BUFFERS:
        oldest_key = next(iter(_output_buffers))
        del _output_buffers[oldest_key]


def _is_base64_content(text: str, sample_size: int = 500) -> bool:
    """检测文本是否主要是 base64 编码内容"""
    sample = text[:sample_size]
    if len(sample) < 100:
        return False
    # base64 字符集：A-Z, a-z, 0-9, +, /, =, 换行
    import re
    non_b64 = re.sub(r'[A-Za-z0-9+/=\n\r\s]', '', sample)
    # 如果非 base64 字符占比很低，认为是 base64 内容
    return len(non_b64) / len(sample) < 0.05


def _truncate_output(text: str, label: str = "输出") -> tuple[str, bool, int]:
    """截断过长输出，返回 (截断后文本, 是否已截断, 原始长度)

    对 base64 等不可读内容采用更激进的截断策略：
    只保留前后各 200 字符的预览，避免大量无意义数据污染上下文。
    """
    original_len = len(text)
    if original_len <= MAX_OUTPUT_CHARS:
        return text, False, original_len

    # 检测是否为 base64 等不可读内容
    if _is_base64_content(text):
        preview_head = text[:200]
        preview_tail = text[-200:]
        marker = (
            f"\n\n... [{label}已截断: 检测到 base64/二进制内容，"
            f"共 {original_len} 字符，省略 {original_len - 400} 字符。"
            f"使用 exec_output(action=range) 查看完整内容] ...\n\n"
        )
        return preview_head + marker + preview_tail, True, original_len

    # 普通文本：保留头部 + 尾部，中间用省略标记
    head = text[:MAX_OUTPUT_CHARS - TRUNCATE_TAIL_CHARS - 200]
    tail = text[-TRUNCATE_TAIL_CHARS:]
    truncated_len = original_len - len(head) - len(tail)
    marker = f"\n\n... [{label}已截断: 省略 {truncated_len} 字符，共 {original_len} 字符。使用 exec_output(action=range) 查看完整内容] ...\n\n"
    return head + marker + tail, True, original_len


def _find_codex_executable() -> Path:
    configured = os.environ.get("CODEX_EXECUTABLE", "")
    candidates = [Path(configured)] if configured else []
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(Path(discovered))
    if sys.platform == "win32":
        candidates.extend(Path(r"C:\Program Files\WindowsApps").glob(
            r"OpenAI.Codex_*\app\resources\codex.exe"
        ))
    executable = next((path.resolve() for path in candidates if path.is_file()), None)
    if not executable:
        raise ValueError("未找到 Codex 可执行文件；可通过 CODEX_EXECUTABLE 指定")
    return executable


def _parse_patch_targets(patch: str, work_dir: Path) -> list[Path]:
    targets = []
    for line in patch.splitlines():
        match = re.match(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", line)
        move_match = re.match(r"^\*\*\* Move to: (.+)$", line)
        m = match or move_match
        raw_path = m.group(1).strip() if m else ""
        if not raw_path:
            continue
        relative = Path(raw_path)
        if relative.is_absolute():
            raise ValueError(f"补丁文件路径必须相对 cwd: {raw_path}")
        resolved = (work_dir / relative).resolve()
        try:
            resolved.relative_to(work_dir)
        except ValueError as exc:
            raise ValueError(f"补丁路径越界: {raw_path}") from exc
        if resolved not in targets:
            targets.append(resolved)
    if not targets:
        raise ValueError("补丁中没有找到 Add/Update/Delete File 声明")
    return targets


def _split_codex_patch(patch: str, max_chars: int = 24000) -> list[str]:
    """按文件段拆分补丁，绕开 Windows 单命令行长度限制。"""
    normalized = patch.replace("\r\n", "\n").strip("\ufeff\n")
    if not normalized.startswith("*** Begin Patch\n") or not normalized.endswith("\n*** End Patch"):
        raise ValueError("补丁必须以 *** Begin Patch 开始并以 *** End Patch 结束")
    body = normalized[len("*** Begin Patch\n"):-len("\n*** End Patch")]
    starts = [match.start() for match in re.finditer(
        r"(?m)^\*\*\* (?:Update|Add|Delete) File: ", body
    )]
    if not starts or starts[0] != 0:
        raise ValueError("补丁文件段格式无效")
    sections = [body[start:starts[index + 1] if index + 1 < len(starts) else len(body)].rstrip("\n")
                for index, start in enumerate(starts)]
    chunks = []
    current = []
    current_chars = len("*** Begin Patch\n\n*** End Patch")
    for section in sections:
        section_chars = len(section) + 1
        if section_chars + len("*** Begin Patch\n\n*** End Patch") > max_chars:
            header = section.splitlines()[0]
            raise ValueError(f"单个补丁文件段过大，无法安全传递: {header}")
        if current and current_chars + section_chars > max_chars:
            chunks.append("*** Begin Patch\n" + "\n".join(current) + "\n*** End Patch")
            current = []
            current_chars = len("*** Begin Patch\n\n*** End Patch")
        current.append(section)
        current_chars += section_chars
    if current:
        chunks.append("*** Begin Patch\n" + "\n".join(current) + "\n*** End Patch")
    return chunks


def _snapshot_paths(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore_paths(snapshot: dict[Path, bytes | None], work_dir: Path | None = None) -> None:
    for path, content in snapshot.items():
        if content is None:
            path.unlink(missing_ok=True)
            parent = path.parent
            while work_dir and parent != work_dir:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


class ApplyPatchRequest(BaseSchema):
    """安全应用 Codex UTF-8 补丁。"""
    patch: str
    cwd: str = ""
    dry_run: bool = False


@router.post("/apply-patch", operation_id="exec_apply_patch")
async def exec_apply_patch(req: ApplyPatchRequest):
    """直接调用 Codex 补丁入口，不经过 PowerShell、BAT 或文本管道。"""
    started = time.perf_counter()
    work_dir = None
    snapshot = None
    try:
        work_dir = _resolve_work_dir(req.cwd)
        targets = _parse_patch_targets(req.patch, work_dir)
        chunks = _split_codex_patch(req.patch)
        executable = _find_codex_executable()
        snapshot = _snapshot_paths(targets)
        outputs = []
        for index, chunk in enumerate(chunks, 1):
            proc = await asyncio.create_subprocess_exec(
                str(executable), "--codex-run-as-apply-patch", chunk,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=_utf8_process_env(str(work_dir)),
            )
            stdout, stderr = await proc.communicate()
            stdout_text = _decode_output(stdout)
            stderr_text = _decode_output(stderr)
            outputs.append({
                "chunk": index,
                "exit_code": proc.returncode or 0,
                "stdout": stdout_text,
                "stderr": stderr_text,
            })
            if proc.returncode:
                _restore_paths(snapshot, work_dir)
                return {
                    "success": False,
                    "error": stderr_text or stdout_text or f"补丁分段 {index} 应用失败",
                    "failed_chunk": index,
                    "rolled_back": True,
                    "cwd": str(work_dir),
                    "codex_executable": str(executable),
                    "chunks": outputs,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
        changed_files = [str(path.relative_to(work_dir)).replace("\\", "/") for path in targets
                         if (path.read_bytes() if path.is_file() else None) != snapshot[path]]
        if req.dry_run:
            _restore_paths(snapshot, work_dir)
        return {
            "success": True,
            "dry_run": req.dry_run,
            "rolled_back": req.dry_run,
            "changed_files": changed_files,
            "chunk_count": len(chunks),
            "cwd": str(work_dir),
            "codex_executable": str(executable),
            "chunks": outputs,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        rolled_back = False
        if snapshot is not None and work_dir is not None:
            _restore_paths(snapshot, work_dir)
            rolled_back = True
        return {
            "success": False,
            "error": str(exc),
            "rolled_back": rolled_back,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }


class ExecRequest(BaseSchema):
    """exec_python 请求（spec D1-D3：统一 terminal 机制，异步执行）。

    code 写入 temp/exec_<uuid>.py，通过 terminal_spawn 起后台子进程，立即返回 terminal_id。
    长任务用 exec_inspect(tid, timeout=N) 等结果；想纯等待用 wait(N) + exec_inspect(tid, timeout=0)。
    """
    code: str
    cwd: str = ""  # 工作目录，空=LocalAgent项目根
    environment: Literal["localagent", "workspace_venv", "system"] = "localagent"
    python_path: str = ""  # 显式解释器路径，优先级最高
    approval_token: str = ""

class ExecResponse(BaseSchema):
    """exec_python 响应（v16：默认内联等待 inline_wait_secs 秒）。

    - status="done"：子进程在 inline_wait_secs 内结束，直接返回完整 stdout/stderr/exit_code
    - status="running"：子进程仍在运行（inline_wait_secs 超时或 inline_wait_secs=0 禁用），
      返回 terminal_id，agent 用 exec_inspect(tid, timeout=N) 查状态
    - 失败：success=False, error 描述原因
    - guard 拒绝：blocked=True, approval_id, guard_reason

    v15 D3（立即返回）由 v16 演进：默认内联等待 10s，0=禁用回退 v15 立即返回。
    """
    success: bool
    terminal_id: str = ""
    status: str = "running"  # "running"（仍运行/禁用）或 "done"（内联等待完成）
    temp_file: str = ""
    pid: int = 0
    error: str | None = None
    blocked: bool = False
    approval_id: str = ""
    guard_reason: str = ""
    agent_instruction: str = ""
    python_executable: str = ""
    cwd: str = ""
    environment: str = ""
    # v16 内联等待 done 路径字段（status="done" 时填充，running 时为默认空值，向后兼容）
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    elapsed: float = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_total_chars: int = 0
    stderr_total_chars: int = 0

class ExecStatusResponse(BaseSchema):
    """用于表示执行状态的响应模型。

    该类用于封装和管理与执行任务相关的状态信息，例如临时目录位置、历史执行记录数量等。

    Attributes:
        temp_dir (str): 用于存放执行过程中临时文件的目录路径。
        history_count (int): 已保存的历史执行记录总数。
        last_exec (dict | None): 最近一次执行任务的详细信息字典，若无记录则为 None。
        buffered_outputs (int): 当前在缓冲区中等待处理的完整输出数量。
    """
    temp_dir: str
    history_count: int
    last_exec: dict | None = None
    buffered_outputs: int  # 缓冲区中的完整输出数量


# ========== T00: exec_inspect / exec_kill / exec_send_input / wait 工具 ==========

class ExecInspectRequest(BaseSchema):
    """exec_inspect 请求：查询终端状态，可选阻塞等待（spec D4）。

    timeout=0（默认）：立即返回当前快照。
    timeout>0：新输出/子进程结束/N秒到，任一触发立即返回。
    """
    tid: str
    timeout: int = 0


class ExecKillRequest(BaseSchema):
    """exec_kill 请求：终止终端子进程（SIGKILL 等价，spec D9）。"""
    tid: str


class ExecSendInputRequest(BaseSchema):
    """exec_send_input 请求：向终端 stdin 发送文本（spec D9）。"""
    tid: str
    text: str


class WaitRequest(BaseSchema):
    """wait 请求：纯 sleep，不查 terminal 状态（spec Anti-Cheat 11）。

    通用工具，不限于 terminal 场景。LLM 想纯等待时用 wait(N) + exec_inspect(tid, timeout=0)。
    """
    seconds: int


@router.get("/status", response_model=ExecStatusResponse, operation_id="exec_status")
async def exec_status():
    """查询exec模块状态"""
    return ExecStatusResponse(
        temp_dir=str(TEMP_DIR),
        history_count=len(_exec_history),
        last_exec=_exec_history[-1] if _exec_history else None,
        buffered_outputs=len(_output_buffers),
    )


@router.post("/python", response_model=ExecResponse, operation_id="exec_python")
async def exec_python(req: ExecRequest):
    """Execute Python code asynchronously via terminal mechanism (spec D1-D3).

    Code is written to temp/exec_<uuid>.py and executed in a terminal session.
    Returns immediately with terminal_id — does NOT block for execution result.

    This is the UNIVERSAL FALLBACK tool: any operation without a dedicated MCP tool
    (file ops, batch processing, multi-step automation, one-off queries) should use this.

    After calling, use these tools to manage the running terminal:
    - exec_inspect(tid, timeout=N): wait for new output/completion/N seconds (any triggers)
    - exec_inspect(tid, timeout=0): instant snapshot
    - exec_kill(tid): terminate the process
    - exec_send_input(tid, text): send stdin input
    - wait(seconds): pure sleep (use with exec_inspect(tid, timeout=0) for "sleep then check")

    Capabilities:
    - Defaults to LocalAgent's Python environment.
    - Set cwd + environment="workspace_venv" to use <cwd>/.venv automatically.
    - Set python_path for an explicit interpreter; the resolved path is returned.
    - Sync or async code both work (asyncio.run applied via -u flag in terminal).
    - cwd is inserted into sys.path/PYTHONPATH for project imports.

    Common templates:
    - Screenshot + OCR in one step (AVOID capture_screen base64 which overflows context):
        import requests, base64, os, tempfile
        API='http://127.0.0.1:8766'
        cap=requests.post(f'{API}/screen/capture',json={'mode':'window','window_title':'XXX','format':'base64'}).json()
        p=os.path.join(tempfile.gettempdir(),'s.png')
        open(p,'wb').write(base64.b64decode(cap['image']))
        ocr=requests.post(f'{API}/ocr/path/json',json={'path':p}).json()
        for it in ocr.get('details',[]): print(it.get('text'),it.get('box'))
    - Run a project tool script: subprocess or `uv run python tools/xxx.py`.
    - Call any REST endpoint not exposed as MCP (see AGENTS.md "MCP 工具排除清单").
    """
    try:
        work_dir = _resolve_work_dir(req.cwd)
        python_executable, resolved_environment = _resolve_python_executable(
            work_dir, req.environment, req.python_path,
        )

        # Guard: check code content (synthetic -c command for content inspection)
        guard_command = f'{python_executable} -c {json.dumps(req.code, ensure_ascii=False)}'
        guard = await check_command(
            guard_command, "python", str(work_dir), req.approval_token,
        )
        if guard.blocked:
            return ExecResponse(
                success=False, error="command_blocked_by_user_rule",
                blocked=True, approval_id=guard.approval_id,
                guard_reason=guard.reason, agent_instruction=AGENT_BLOCK_MESSAGE,
                python_executable=str(python_executable),
                cwd=str(work_dir), environment=resolved_environment,
            )

        # Write code to temp file (spec D2: temp/exec_<uuid>.py)
        temp_file = TEMP_DIR / f"exec_{uuid.uuid4().hex[:12]}.py"
        temp_file.write_text(req.code, encoding="utf-8")

        # T04: 参数使用率统计（spec D10，best-effort，失败不影响 exec_python）
        try:
            from server.tool_usage_stats import record_exec_python_call
            record_exec_python_call(
                code=req.code,
                environment=resolved_environment,
                cwd=req.cwd,
            )
        except Exception:
            pass  # 统计失败不影响 exec_python 正常工作

        # Build command list (绕过 cmd.exe 引号陷阱，路径含空格时安全)
        # runner 支持 top-level await（asyncio.run），替代旧的 _PYTHON_STDIN_RUNNER
        runner_path = Path(__file__).parent / "_exec_runner.py"
        cmd_list = [
            str(python_executable), "-X", "utf8", "-u",
            str(runner_path), str(temp_file), str(work_dir),
        ]

        # Call terminal_spawn internally (spec D1: unified terminal mechanism)
        spawn_req = TerminalSpawnRequest(
            cmd_list=cmd_list,  # 直接命令列表模式，绕过 shell
            cwd=req.cwd,
            label="exec_python",
            timeout=0,  # no timeout (spec D5: exec_python has no timeout parameter)
            approval_token=req.approval_token,
        )
        spawn_result = await terminal_spawn(spawn_req)

        if not spawn_result.get("success"):
            # terminal_spawn failed (guard reject / capacity / spawn error)
            # 修复 bug：spawn 失败时 temp_file 已写入但未清理，导致 temp/exec_*.py 累积
            try:
                temp_file.unlink(missing_ok=True)
            except Exception:
                pass
            _record_history(
                req.code[:200], False, 0,
                error=spawn_result.get("error", "terminal_spawn failed")[:200],
            )
            return ExecResponse(
                success=False,
                error=spawn_result.get("error", "terminal_spawn failed"),
                blocked=spawn_result.get("blocked", False),
                approval_id=spawn_result.get("approval_id", ""),
                guard_reason=spawn_result.get("guard_reason", ""),
                agent_instruction=spawn_result.get("agent_instruction", ""),
                python_executable=str(python_executable),
                cwd=str(work_dir), environment=resolved_environment,
            )

        # Record temp_file in terminal session for TTL cleanup (spec D2 + T00 TTL)
        tid = spawn_result["tid"]
        if tid in _terminals:
            _terminals[tid]["temp_file"] = str(temp_file)

        _record_history(req.code[:200], True, 0, error=None)

        # v16: 内联等待 — 默认等 inline_wait_secs 秒，子进程结束则直接返回完整结果
        # （spec Solution）。inline_wait_secs=0 时跳过等待，回退 v15 立即返回行为。
        from server.config import get_server_config
        inline_wait_secs = int(get_server_config().get("exec_python_inline_wait_secs", 10))

        if inline_wait_secs > 0:
            done = await _wait_terminal_for_inline(tid, inline_wait_secs)
            if done:
                session = _terminals.get(tid, {})
                stdout_full = _read_terminal_full(session, "stdout")
                stderr_full = _read_terminal_full(session, "stderr")
                stdout_display, stdout_trunc, stdout_len = _truncate_output(stdout_full, "stdout")
                stderr_display, stderr_trunc, stderr_len = _truncate_output(stderr_full, "stderr")
                started_at = session.get("started_at", 0)
                finished_at = session.get("finished_at") or time.time()

                # v16.1 修复残留泄露（2026-08-03）：done 路径完成后立即删 temp_file。
                # 之前依赖 30 分钟 TTL 清理，但 agent/loop 高频调用时 TTL 跟不上累积速度，
                # temp/exec_*.py 持续堆积（实测一天 45+ 个文件）。
                # 子进程已结束 → runner 已读完 temp_file（_exec_runner.py line 27-28
                # with open(...) as f: f.read() 后文件句柄已关闭）→ 删除安全。
                # running 路径不删：子进程可能还在读，且用户代码可能通过 __file__ 重读。
                try:
                    temp_file.unlink(missing_ok=True)
                    if tid in _terminals:
                        _terminals[tid]["temp_file"] = None
                except Exception:
                    pass

                return ExecResponse(
                    success=True,
                    terminal_id=tid,
                    status="done",
                    temp_file=str(temp_file),
                    pid=spawn_result.get("pid", 0),
                    python_executable=str(python_executable),
                    cwd=str(work_dir), environment=resolved_environment,
                    stdout=stdout_display,
                    stderr=stderr_display,
                    exit_code=session.get("exit_code"),
                    elapsed=max(0.0, finished_at - started_at),
                    stdout_truncated=stdout_trunc,
                    stderr_truncated=stderr_trunc,
                    stdout_total_chars=stdout_len,
                    stderr_total_chars=stderr_len,
                )
            # 超时仍 running → 走 running 路径（同 v15）

        # running 路径（inline_wait_secs=0 禁用 或 内联等待超时）
        return ExecResponse(
            success=True,
            terminal_id=tid,
            status="running",
            temp_file=str(temp_file),
            pid=spawn_result.get("pid", 0),
            python_executable=str(python_executable),
            cwd=str(work_dir), environment=resolved_environment,
        )
    except Exception as e:
        _record_history(req.code[:200], False, 0, error=str(e)[:200])
        return ExecResponse(
            success=False, error=str(e),
        )


# ========== 输出查看接口 ==========

class OutputQueryRequest(BaseSchema):
    """查看执行输出的统一请求（action 区分模式）

    参数:
        action: full | range | search
        channel: stdout / stderr / code
        start: range 模式起始字符位置（含）
        end: range 模式结束位置（不含），-1=到末尾
        query: search 模式搜索词
        context_chars: search 模式上下文字符数
        max_matches: search 模式最大匹配数
    """
    action: str = "full"
    channel: str = "stdout"
    start: int = 0
    end: int = -1
    query: str = ""
    context_chars: int = 200
    max_matches: int = 5

class OutputRangeResponse(BaseSchema):
    """这是一个OutputRangeResponse类，用于表示输出范围的响应数据。

    功能：该类继承自BaseModel，用于定义输出范围响应的数据结构，支持数据验证和序列化。
    参数：
        exec_id (str): 执行ID，标识执行过程。
        channel (str): 通道，指明数据通道。
        start (int): 起始位置，范围的起始索引。
        end (int): 结束位置，范围的结束索引。
        total_chars (int): 总字符数，范围内字符的总数。
        content (str): 内容，实际的数据内容。
    返回值：当实例化时，返回一个OutputRangeResponse对象，包含上述字段。
    """
    exec_id: str  # 执行ID
    channel: str  # 通道
    start: int  # 起始位置
    end: int  # 结束位置
    total_chars: int  # 总字符数
    content: str  # 内容

class OutputSearchMatch(BaseSchema):
    """用于表示输出中搜索匹配结果的类。

    功能：存储在完整输出字符串中匹配到的目标子串及其上下文信息。
    参数：无显式参数，通过类属性定义。
    返回值：该类的实例，包含匹配位置和上下文字符串。
    """
    position: int  # 匹配在完整输出中的起始位置
    context: str  # 匹配位置前后 context_chars 字符

class OutputSearchResponse(BaseSchema):
    """封装搜索响应的结果数据。

    功能：
        用于结构化存储搜索操作返回的完整响应信息，包括本次查询的标识、查询条件、来源渠道以及匹配结果的详细信息。

    参数：
        exec_id (str): 标识本次搜索执行的唯一ID。
        query (str): 本次搜索所使用的原始查询字符串。
        channel (str): 执行搜索所使用的来源渠道（例如：“web”, “api”）。
        total_matches (int): 满足查询条件的匹配结果总数。
        shown_matches (int): 在本次响应中实际返回的匹配结果条数。
        matches (list[OutputSearchMatch]): 包含本次返回的具体匹配项详细信息的列表。

    返回值：
        本类的实例（object）。
    """
    exec_id: str  # 本次搜索执行的唯一标识
    query: str  # 用户提交的原始查询词
    channel: str  # 搜索请求的来源渠道
    total_matches: int  # 总匹配数
    shown_matches: int  # 本次返回的匹配数
    matches: list[OutputSearchMatch]  # 包含详细信息的匹配项列表


@output_router.post("/{exec_id}", operation_id="exec_output")
async def query_output(exec_id: str, req: OutputQueryRequest):
    """查看执行输出（action=full|range|search）

    - full: 返回完整输出（channel 指定通道，可能很长）
    - range: 按字符区间查看（start/end 参数）
    - search: 搜索文本（query 参数，返回匹配位置及上下文）
    """
    buf = _output_buffers.get(exec_id)
    if buf is None:
        available = list(_output_buffers.keys())[-5:]
        return {"error": f"输出缓冲区不存在: {exec_id}（可能已过期）", "available_ids": available}

    content = buf.get(req.channel, "")

    if req.action == "range":
        total = len(content)
        start = max(0, req.start)
        end = total if req.end < 0 else min(req.end, total)
        return OutputRangeResponse(
            exec_id=exec_id, channel=req.channel,
            start=start, end=end, total_chars=total,
            content=content[start:end],
        )

    if req.action == "search":
        positions = [m.start() for m in re.finditer(re.escape(req.query), content)]
        total_matches = len(positions)
        shown = positions[:req.max_matches]
        matches = []
        for pos in shown:
            ctx_start = max(0, pos - req.context_chars)
            ctx_end = min(len(content), pos + len(req.query) + req.context_chars)
            matches.append(OutputSearchMatch(
                position=pos,
                context=content[ctx_start:ctx_end],
            ))
        return OutputSearchResponse(
            exec_id=exec_id, query=req.query, channel=req.channel,
            total_matches=total_matches, shown_matches=len(matches),
            matches=matches,
        )

    # full (默认)
    return {"exec_id": exec_id, "channel": req.channel, "total_chars": len(content), "content": content}


class CmdRequest(BaseSchema):
    """Shell 命令执行请求"""
    cmd: str  # 要执行的命令
    shell: str = "cmd"  # cmd / powershell(优先pwsh) / bash / python
    cwd: str = ""  # 工作目录，空=项目根
    timeout: float = 60.0  # 超时秒数
    approval_token: str = ""


class CmdResponse(BaseSchema):
    """Shell 命令执行响应"""
    success: bool
    stdout: str
    stderr: str
    error: str | None = None
    exit_code: int = 0
    elapsed_ms: int = 0
    exec_id: str = ""
    stdout_truncated: bool = False
    stdout_total_chars: int = 0
    stderr_truncated: bool = False
    stderr_total_chars: int = 0
    shell_executable: str = ""
    shell_family: str = ""
    shell_version: str = ""
    cwd: str = ""
    blocked: bool = False
    approval_id: str = ""
    guard_reason: str = ""
    agent_instruction: str = ""


@router.post("/cmd", response_model=CmdResponse, operation_id="exec_cmd")
async def exec_cmd(req: CmdRequest):
    """Execute a shell command (cmd/powershell) and return stdout/stderr.

    Bypasses IDE terminal PowerShell issues — runs directly via subprocess.
    Uses cmd.exe by default on Windows for clean, predictable output.

    Common uses:
    - pip/uv install commands
    - file operations (dir, copy, del)
    - git commands
    - running .bat/.ps1 scripts
    """
    # 4-9: exec_id 换 uuid4().hex——秒级时间戳 + id(req) 对象地址在地址复用时可碰撞，
    # 覆盖 _output_buffers 中的历史输出
    exec_id = f"cmd_{uuid.uuid4().hex[:12]}"

    try:
        work_dir = _resolve_work_dir(req.cwd)
        full_cmd, shell_info = _resolve_shell(req.shell, req.cmd)
        shell_family = shell_info.get("shell_family", shell_info["requested_shell"])
        shell_version = await asyncio.to_thread(
            _shell_version, shell_info["shell_executable"], shell_family,
        )
    except Exception as exc:
        return CmdResponse(success=False, stdout="", stderr=str(exc), error=str(exc), exit_code=-1)

    guard = await check_command(req.cmd, req.shell, str(work_dir), req.approval_token)
    if guard.blocked:
        return CmdResponse(
            success=False, stdout="", stderr=AGENT_BLOCK_MESSAGE,
            error="command_blocked_by_user_rule", exit_code=126,
            exec_id=exec_id, shell_executable=shell_info["shell_executable"],
            shell_family=shell_family, shell_version=shell_version,
            cwd=str(work_dir), blocked=True, approval_id=guard.approval_id,
            guard_reason=guard.reason, agent_instruction=AGENT_BLOCK_MESSAGE,
        )

    t0 = time.perf_counter()

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=_utf8_process_env(str(work_dir)),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=req.timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            elapsed = int((time.perf_counter() - t0) * 1000)
            _store_output(exec_id, req.cmd, "", f"执行超时（{req.timeout}秒）")
            return CmdResponse(
                success=False, stdout="", stderr="",
                error=f"执行超时（{req.timeout}秒）", exit_code=-1,
                elapsed_ms=elapsed, exec_id=exec_id,
                shell_executable=shell_info["shell_executable"],
                shell_family=shell_family, shell_version=shell_version,
                cwd=str(work_dir),
            )

        stdout_str = _decode_output(stdout)
        stderr_str = _decode_output(stderr)
        exit_code = proc.returncode or 0
        elapsed = int((time.perf_counter() - t0) * 1000)

        _store_output(exec_id, f"$ {req.cmd}", stdout_str, stderr_str)

        stdout_display, stdout_trunc, stdout_len = _truncate_output(stdout_str, "stdout")
        stderr_display, stderr_trunc, stderr_len = _truncate_output(stderr_str, "stderr")

        _record_history(f"$ {req.cmd[:150]}", exit_code == 0, elapsed,
                        error=stderr_str[:200] if exit_code != 0 else None)

        return CmdResponse(
            success=exit_code == 0,
            stdout=stdout_display,
            stderr=stderr_display,
            exit_code=exit_code,
            elapsed_ms=elapsed,
            exec_id=exec_id,
            stdout_truncated=stdout_trunc,
            stdout_total_chars=stdout_len,
            stderr_truncated=stderr_trunc,
            stderr_total_chars=stderr_len,
            shell_executable=shell_info["shell_executable"],
            shell_family=shell_family, shell_version=shell_version,
            cwd=str(work_dir),
        )
    except Exception as e:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return CmdResponse(
            success=False, stdout="", stderr=str(e),
            exit_code=-1, elapsed_ms=elapsed, exec_id=exec_id,
        )


# ========== 终端会话 API ==========

class TerminalSpawnRequest(BaseSchema):
    """启动后台终端会话

    cmd_list（可选）：直接传命令列表，绕过 shell 解析（避免 cmd.exe 引号陷阱）。
    用于 exec_python 内部调用，路径含空格时安全。
    """
    cmd: str = ""
    cmd_list: list[str] = []  # 优先于 cmd+shell，直接作为命令列表
    shell: str = "cmd"  # cmd / powershell(优先pwsh) / bash / python
    cwd: str = ""
    label: str = ""  # 可选标签，方便识别
    timeout: float = 0  # 0=不超时，>0=N秒后自动kill
    approval_token: str = ""


def _terminal_log_path(tid: str, channel: str) -> Path:
    return TERMINAL_DIR / f"{tid}.{channel}.log"


def _wake_output_waiters(session: dict) -> None:
    """唤醒该终端的所有 SSE 流消费者。

    每消费者持有独立 Event（session["_consumer_events"]），生产侧广播 set——
    共享单 Event 时先醒的消费者 clear() 会丢掉其他消费者的唤醒
    （2026-09-13 code review 4-5）。
    """
    for ev in list(session.get("_consumer_events") or ()):
        ev.set()


def _append_terminal_text(session: dict, channel: str, text: str) -> None:
    """将终端输出写入磁盘真源，并只在内存保留有界尾部。"""
    if not text:
        return
    encoded = text.encode("utf-8")
    with session[f"_{channel}_path"].open("ab") as handle:
        handle.write(encoded)
    session[channel] = (session[channel] + text)[-_TERMINAL_MEMORY_TAIL_CHARS:]
    session[f"{channel}_chars"] += len(text)
    session[f"{channel}_bytes"] += len(encoded)
    session["output_version"] += 1
    # 唤醒 SSE 等待者（事件驱动，避免 0.5s 轮询）
    _wake_output_waiters(session)


def _read_terminal_tail(session: dict, channel: str, tail_chars: int) -> str:
    """从日志文件尾部读取文本，避免把完整日志载入内存。"""
    if tail_chars <= 0:
        tail_chars = _TERMINAL_DETAIL_DEFAULT_CHARS
    path = session[f"_{channel}_path"]
    if not path.exists():
        return session.get(channel, "")[-tail_chars:]
    read_bytes = min(path.stat().st_size, max(tail_chars * 4, 4096))
    with path.open("rb") as handle:
        handle.seek(-read_bytes, os.SEEK_END)
        data = handle.read()
    return data.decode("utf-8", errors="replace")[-tail_chars:]


def _read_terminal_full(session: dict, channel: str) -> str:
    """读取 channel 全量日志文本（v16 内联等待 done 路径用）。

    与 _read_terminal_tail 不同：不做尾部截断，读全量后由 _truncate_output 统一截断。
    日志文件不存在时回退到内存尾部。
    """
    path = session.get(f"_{channel}_path")
    if not path or not path.exists():
        return session.get(channel, "")
    return path.read_text(encoding="utf-8", errors="replace")


async def _wait_terminal_for_inline(tid: str, secs: int) -> bool:
    """等待终端子进程在 secs 秒内完全结束（含输出收集后处理）。

    v16 exec_python 内联等待用：spawn 成功后调用，secs 内 collector task 完成
    （proc.wait + stdout/stderr gather + status/exit_code 设置）→ 返回 True；
    超时仍 running → 返回 False（agent 走 exec_inspect 两步流程）。

    用 asyncio.shield 保护 collector 不被 wait_for 超时取消（spec Solution：事件驱动，
    collector finally 会 set _output_event；这里直接等 collector task 更精确，避免
    _output_event 在每次输出时被 set 的干扰）。
    """
    session = _terminals.get(tid)
    if not session:
        return False
    # 已结束（含异常/done/killed/timeout）：直接返回
    if session.get("status") != "running":
        return True
    collector = session.get("_collector_task")
    if not collector or collector.done():
        return session.get("status") != "running"
    try:
        await asyncio.wait_for(asyncio.shield(collector), timeout=secs)
        return True
    except TimeoutError:
        return False
    except Exception:
        # collector 抛异常（终端以 error 结束）→ 视为已结束，done 路径读已落盘的输出
        return True


async def wait_terminal_completion(tid: str, timeout_secs: float) -> dict:
    """等待 terminal 子进程结束（或超时），返回 {done, stdout, stderr, exit_code, missing}。

    公共 helper（2026-09-13 code review 4-1）：browser/_execute_playwright 等
    模块在 exec_python 内联等待超时（status="running"）后，需要按自己的 timeout
    继续等待并取回完整输出，不应各自触碰 _terminals 内部结构。
    missing=True 表示 tid 对应会话已不存在（被清理）；done=False 表示超时仍在运行。
    """
    done = await _wait_terminal_for_inline(tid, max(1, int(timeout_secs)))
    session = _terminals.get(tid)
    if session is None:
        return {"done": False, "stdout": "", "stderr": "", "exit_code": None, "missing": True}
    return {
        "done": done,
        "stdout": _read_terminal_full(session, "stdout"),
        "stderr": _read_terminal_full(session, "stderr"),
        "exit_code": session.get("exit_code"),
        "missing": False,
    }


def _read_terminal_range(session: dict, channel: str, offset: int, limit: int) -> dict:
    """按字节游标读取日志，适合 Agent 增量消费海量输出。"""
    path = session[f"_{channel}_path"]
    total_bytes = session[f"{channel}_bytes"]
    safe_offset = max(0, min(offset, total_bytes))
    safe_limit = max(1, min(limit, _TERMINAL_OUTPUT_MAX_BYTES))
    if not path.exists() or safe_offset >= total_bytes:
        data = b""
    else:
        with path.open("rb") as handle:
            handle.seek(safe_offset)
            data = handle.read(safe_limit)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(data, final=False)
    buffered_tail = decoder.getstate()[0]
    consumed_bytes = len(data) - len(buffered_tail)
    if consumed_bytes == 0 and data:
        content = data.decode("utf-8", errors="replace")
        consumed_bytes = len(data)
    next_offset = safe_offset + consumed_bytes
    return {
        "channel": channel,
        "offset": safe_offset,
        "next_offset": next_offset,
        "total_bytes": total_bytes,
        "content": content,
        "has_more": next_offset < total_bytes,
        "limit_capped": limit > _TERMINAL_OUTPUT_MAX_BYTES,
    }


async def _pump_terminal_stream(
    session: dict,
    stream: asyncio.StreamReader | None,
    channel: str,
) -> None:
    """独立排空 stdout/stderr，避免任一管道写满导致子进程死锁。"""
    if stream is None:
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        _append_terminal_text(session, channel, decoder.decode(chunk))
    _append_terminal_text(session, channel, decoder.decode(b"", final=True))


async def _terminate_terminal_process(session: dict, force: bool = True) -> None:
    """终止整个进程树，避免 shell 子进程遗留。"""
    proc = session.get("_proc")
    if not proc or proc.returncode is not None:
        return
    if sys.platform == "win32":
        args = ["taskkill", "/T", "/PID", str(session["pid"])]
        if force:
            args.insert(1, "/F")
        killer = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(session["pid"], signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return


def _delete_terminal_files(session: dict) -> None:
    """统一删除 terminal 关联的磁盘文件（temp_file + stdout/stderr 日志）。

    修复 bug：原 _cleanup_old_terminals / terminal_delete 只删日志文件不删 temp_file，
    导致 temp/exec_*.py 累积。
    """
    temp_file = session.get("temp_file")
    if temp_file:
        try:
            Path(temp_file).unlink(missing_ok=True)
        except Exception:
            pass
    for channel in ("stdout", "stderr"):
        path = session.get(f"_{channel}_path")
        if path:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _cleanup_old_terminals():
    """清理已结束且超过保留数量的终端"""
    finished = [(tid, t) for tid, t in _terminals.items() if t["status"] in ("done", "killed", "timeout", "error")]
    # 按完成时间排序，保留最近的
    finished.sort(key=lambda x: x[1].get("finished_at", 0), reverse=True)
    for tid, session in finished[_MAX_TERMINALS:]:
        _delete_terminal_files(session)
        del _terminals[tid]


def _verify_terminal_owner(tid: str, owner_token: str) -> bool:
    """验证终端 owner_token：agent 自建终端凭 token 免审后续操作。

    Returns:
        True = token 匹配，免审放行
        False = token 不匹配/缺失/终端不存在，需走审批流程
    """
    if not owner_token:
        return False
    t = _terminals.get(tid)
    if not t:
        return False
    stored = t.get("owner_token", "")
    return bool(stored) and secrets.compare_digest(stored, owner_token)


def _is_gui_request(request: Request | None) -> bool:
    """判断请求是否来自 GUI/用户（而非 agent）。

    agent 请求带 X-Agent-Caller: true 头；GUI/用户请求不带此头。
    GUI 请求免 owner_token 检查（已有自己的确认弹窗）。
    request 为 None 时（内部/测试直接调用）视为可信，免 owner_token 检查。
    """
    if request is None:
        return True
    return request.headers.get("X-Agent-Caller", "").lower() != "true"


def _check_terminal_permission(tid: str, owner_token: str, request: Request | None, action: str) -> dict | None:
    """统一权限检查：GUI 请求放行；agent 请求需 owner_token。

    Returns:
        None = 权限通过
        dict = 权限拒绝，返回该错误响应（含 approval_id）
    """
    if _is_gui_request(request):
        return None  # GUI 请求免审
    if not _verify_terminal_owner(tid, owner_token):
        return _terminal_approval_required(tid, action)
    return None


def _terminal_approval_required(tid: str, action: str) -> dict:
    """owner_token 不匹配时返回审批要求（不是不给过，是走审批流程）。

    agent 丢失 token 或非 agent 调用时，创建 approval_id 让用户审批。
    """
    from server.http_guard import create_pending
    approval_id = create_pending("POST", f"/terminals/{tid}/{action}", b"")
    return {
        "success": False,
        "error": "owner_token_required",
        "approval_id": approval_id,
        "message": (
            f"终端 {tid} 的 owner_token 不匹配或缺失。"
            "若您是 agent，请使用 spawn 返回的 owner_token；"
            "否则请调用 command_guard_request_approval 获取批准后重试。"
        ),
    }


@terminal_router.post("/spawn", operation_id="exec_terminal_spawn")
async def terminal_spawn(req: TerminalSpawnRequest):
    """启动一个后台终端会话，立即返回终端 ID。

    命令在后台异步执行，通过 /terminals/{tid} 轮询状态和输出。
    用于长时间运行的命令（模型下载、pip安装等），避免阻塞 HTTP 请求。

    支持两种命令模式：
    - cmd + shell：传统模式，通过 shell 解析 cmd 字符串（cmd.exe/powershell/bash/python）
    - cmd_list：直接传命令列表，绕过 shell 解析（避免 cmd.exe 引号陷阱，exec_python 内部用）
    """
    tid = f"term_{uuid.uuid4().hex[:12]}"
    try:
        work_dir = _resolve_work_dir(req.cwd)
        if req.cmd_list:
            # 直接命令列表模式（exec_python 内部用，绕过 shell 引号陷阱）
            full_cmd = list(req.cmd_list)
            shell_family = "raw"
            shell_version = "raw"
            shell_info = {
                "requested_shell": "raw",
                "shell_executable": full_cmd[0] if full_cmd else "",
                "shell_family": "raw",
            }
        else:
            full_cmd, shell_info = _resolve_shell(req.shell, req.cmd)
            shell_family = shell_info.get("shell_family", shell_info["requested_shell"])
            shell_version = await asyncio.to_thread(
                _shell_version, shell_info["shell_executable"], shell_family,
            )
    except Exception as exc:
        return {"success": False, "error": str(exc), "tid": tid}

    # Guard：cmd_list 模式用 cmd_list[0] 作为可执行文件名 + 全部 join 作为检查内容
    guard_cmd_str = " ".join(req.cmd_list) if req.cmd_list else req.cmd
    guard_shell = "raw" if req.cmd_list else req.shell
    guard = await check_command(guard_cmd_str, guard_shell, str(work_dir), req.approval_token)
    if guard.blocked:
        return {
            "success": False,
            "blocked": True,
            "error": "command_blocked_by_user_rule",
            "tid": tid,
            "approval_id": guard.approval_id,
            "guard_reason": guard.reason,
            "agent_instruction": AGENT_BLOCK_MESSAGE,
        }

    # 4-8: 上限检查与占位原子化——create_subprocess_exec 的 await 是唯一交错窗口，
    # 用预留计数同步增减覆盖该窗口（事件循环单线程，检查+自增之间无 await，天然原子），
    # 并发 spawn 不再能同时通过同一检查突破 _MAX_RUNNING_TERMINALS
    global _spawn_inflight
    running_count = (
        sum(1 for terminal in _terminals.values() if terminal["status"] == "running")
        + _spawn_inflight
    )
    if running_count >= _MAX_RUNNING_TERMINALS:
        return {
            "success": False,
            "error": f"运行中终端已达上限（{_MAX_RUNNING_TERMINALS}），请等待或终止旧会话",
            "running": running_count,
        }
    _spawn_inflight += 1
    try:
        spawn_options = {}
        if sys.platform == "win32":
            spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn_options["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=_utf8_process_env(str(work_dir)),
            **spawn_options,
        )
    except Exception as e:
        return {"success": False, "error": str(e), "tid": tid}
    finally:
        # spawn 返回/失败即解除占位：此后到 _terminals[tid] = session 之间无 await，
        # 登记在本轮同步代码内完成，不会再与并发 spawn 交错
        _spawn_inflight -= 1

    # 生成 owner_token：agent 自建终端后续 kill/delete/input 凭此 token 免审
    # 简短（8 字符 hex），便于 agent 传递；token 不匹配时走标准审批流程
    owner_token = secrets.token_hex(4)
    session = {
        "tid": tid,
        "cmd": req.cmd,
        "shell": req.shell,
        "shell_executable": shell_info["shell_executable"],
        "shell_family": shell_family,
        "shell_version": shell_version,
        "cwd": str(work_dir),
        "label": req.label or req.cmd[:60],
        "status": "running",
        "pid": proc.pid,
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "stdout_chars": 0,
        "stderr_chars": 0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "output_version": 0,
        "timeout": req.timeout,
        "owner_token": owner_token,  # 后续 kill/delete/input 凭此 token 免审
        "_proc": proc,  # 保留进程引用，用于 stdin 写入
        "_stdout_path": _terminal_log_path(tid, "stdout"),
        "_stderr_path": _terminal_log_path(tid, "stderr"),
        "_consumer_events": set(),  # SSE 流消费者独立唤醒事件集合（见 _wake_output_waiters）
        "last_activity_at": time.time(),  # T00: TTL 清理用（spec D11 第三层）
        "temp_file": None,  # T01: exec_python 改造后写入临时 .py 路径，TTL 清理时删除
    }
    session["_stdout_path"].touch()
    session["_stderr_path"].touch()
    _terminals[tid] = session
    _cleanup_old_terminals()

    # 启动后台收集任务
    session["_collector_task"] = asyncio.create_task(_collect_terminal_output(tid, proc))

    logger.info(f"终端会话启动: {tid} (pid={proc.pid}) $ {req.cmd[:80]}")
    return {
        "success": True, "tid": tid, "pid": proc.pid, "label": session["label"],
        "shell_executable": session["shell_executable"],
        "shell_family": session["shell_family"], "shell_version": session["shell_version"],
        "cwd": session["cwd"],
        "owner_token": owner_token,  # 后续 kill/delete/input 传此 token 免审
    }


async def _collect_terminal_output(tid: str, proc: asyncio.subprocess.Process):
    """后台任务：持续收集终端输出，直到进程结束"""
    session = _terminals.get(tid)
    if not session:
        return

    stdout_task = asyncio.create_task(_pump_terminal_stream(session, proc.stdout, "stdout"))
    stderr_task = asyncio.create_task(_pump_terminal_stream(session, proc.stderr, "stderr"))
    try:
        timeout = session.get("timeout", 0)
        if timeout > 0:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                session["status"] = "timeout"
                _append_terminal_text(session, "stderr", f"\n[终端超时，已终止（{timeout}秒）]")
                await _terminate_terminal_process(session)
                await proc.wait()
        else:
            await proc.wait()
        await asyncio.gather(stdout_task, stderr_task)
        session["exit_code"] = proc.returncode or 0
        if session["status"] == "running":
            session["status"] = "done"
        session["finished_at"] = time.time()
        elapsed = session["finished_at"] - session["started_at"]
        logger.info(f"终端会话结束: {tid} (exit={session['exit_code']}, {elapsed:.1f}s)")

    except Exception as e:
        session["status"] = "error"
        _append_terminal_text(session, "stderr", f"\n[收集输出异常: {e}]")
        session["finished_at"] = time.time()
        logger.error(f"终端会话异常: {tid}: {e}")
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        # 唤醒 SSE 等待者，确保它们能收到结束信号
        _wake_output_waiters(session)
        _cleanup_old_terminals()


# ========== 终端输入接口 ==========

class TerminalActionRequest(BaseSchema):
    """终端操作请求（kill/delete 凭 owner_token 免审）"""
    owner_token: str = ""


class TerminalInputRequest(BaseSchema):
    """向运行中的终端发送输入"""
    text: str = ""  # 要发送的文本
    ctrl_c: bool = False  # 发送 Ctrl+C 中断信号
    owner_token: str = ""  # 凭此 token 免审（agent 自建终端）


@terminal_router.post("/{tid}/input", operation_id="exec_terminal_input")
async def terminal_input(tid: str, req: TerminalInputRequest, request: Request):
    """向运行中的终端发送输入（支持交互式命令）

    可用于：
    - 回应交互式提示（如 pip install 的 y/n 确认）
    - 向 REPL 发送命令（python -i, cmd）
    - Ctrl+C 中断当前操作

    权限：GUI 请求免审（已有自己的确认弹窗）；agent 请求需 owner_token。
    """
    t = _terminals.get(tid)
    if not t:
        return {"success": False, "error": f"终端不存在: {tid}"}
    denied = _check_terminal_permission(tid, req.owner_token, request, "input")
    if denied:
        return denied
    if t["status"] != "running":
        return {"success": False, "error": f"终端已结束（状态: {t['status']}）"}
    proc = t.get("_proc")
    if not proc or not proc.stdin:
        return {"success": False, "error": "终端不支持输入（stdin 未开启）"}
    try:
        if req.ctrl_c:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
        else:
            data = req.text + "\n"
            proc.stdin.write(data.encode("utf-8"))
            await proc.stdin.drain()
        return {"success": True, "message": "输入已发送"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ========== SSE 实时输出流 ==========

from fastapi.responses import StreamingResponse  # noqa: E402  # SSE 段局部导入


@terminal_router.get("/{tid}/stream", operation_id="exec_terminal_stream")
async def terminal_stream(tid: str, stdout_offset: int | None = None, stderr_offset: int | None = None):
    """SSE 实时输出流 — 浏览器 EventSource 连接后自动接收新输出

    用法：
        const es = new EventSource('/terminals/{tid}/stream');
        es.onmessage = (e) => { console.log(JSON.parse(e.data)); };
    """
    t = _terminals.get(tid)
    if not t:
        return {"error": f"终端不存在: {tid}"}

    # 每消费者独立 Event（2026-09-13 code review 4-5：共享 Event 时先醒的消费者
    # clear 会丢掉其他消费者的唤醒）。注册先于首次读，注册后的写入必唤醒。
    consumer_event = asyncio.Event()
    consumers = t.get("_consumer_events")
    if consumers is not None:
        consumers.add(consumer_event)

    async def event_generator():
        current_stdout_offset = stdout_offset
        current_stderr_offset = stderr_offset
        if current_stdout_offset is None:
            current_stdout_offset = max(0, t["stdout_bytes"] - _TERMINAL_MEMORY_TAIL_CHARS)
        if current_stderr_offset is None:
            current_stderr_offset = max(0, t["stderr_bytes"] - _TERMINAL_MEMORY_TAIL_CHARS)
        while True:
            current_t = _terminals.get(tid)
            if not current_t:
                yield f"data: {json.dumps({'type': 'end', 'reason': 'terminal_deleted'})}\n\n"
                break

            stdout_chunk = _read_terminal_range(current_t, "stdout", current_stdout_offset, _TERMINAL_MEMORY_TAIL_CHARS)
            stderr_chunk = _read_terminal_range(current_t, "stderr", current_stderr_offset, _TERMINAL_MEMORY_TAIL_CHARS)
            current_stdout_offset = stdout_chunk["next_offset"]
            current_stderr_offset = stderr_chunk["next_offset"]

            if stdout_chunk["content"]:
                yield f"data: {json.dumps({'type': 'stdout', 'data': stdout_chunk['content'], 'offset': stdout_chunk['offset'], 'next_offset': current_stdout_offset})}\n\n"
            if stderr_chunk["content"]:
                yield f"data: {json.dumps({'type': 'stderr', 'data': stderr_chunk['content'], 'offset': stderr_chunk['offset'], 'next_offset': current_stderr_offset})}\n\n"

            if current_t["status"] != "running":
                yield f"data: {json.dumps({'type': 'end', 'status': current_t['status'], 'exit_code': current_t['exit_code']})}\n\n"
                break

            # 事件驱动：等待新输出或 5 秒超时兜底心跳（自己的 Event，clear 安全）
            try:
                await asyncio.wait_for(consumer_event.wait(), timeout=5.0)
                consumer_event.clear()
            except TimeoutError:
                pass  # 超时也无妨，继续 yield 心跳

    # 流结束（含客户端断开触发的 generator close）后注销消费者事件，防集合滞留
    from starlette.background import BackgroundTask

    def _unregister_consumer() -> None:
        if consumers is not None:
            consumers.discard(consumer_event)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(_unregister_consumer),
    )




@terminal_router.get("", operation_id="exec_terminals_list")
async def terminals_list():
    """列出所有终端会话（运行中和已结束）"""
    result = []
    for tid, t in sorted(_terminals.items(), key=lambda x: x[1].get("started_at", 0), reverse=True):
        result.append({
            "tid": tid,
            "label": t["label"],
            "cmd": t["cmd"][:120],
            "shell": t["shell"],
            "shell_executable": t["shell_executable"],
            "shell_family": t["shell_family"],
            "shell_version": t["shell_version"],
            "cwd": t["cwd"],
            "status": t["status"],
            "pid": t["pid"],
            "exit_code": t["exit_code"],
            "started_at": t["started_at"],
            "finished_at": t["finished_at"],
            "elapsed": (t["finished_at"] or time.time()) - t["started_at"],
            "stdout_chars": t["stdout_chars"],
            "stderr_chars": t["stderr_chars"],
            "stdout_bytes": t["stdout_bytes"],
            "stderr_bytes": t["stderr_bytes"],
            "output_version": t["output_version"],
        })
    running = sum(1 for t in result if t["status"] == "running")
    return {"count": len(result), "running": running, "terminals": result}


@terminal_router.get("/{tid}", operation_id="exec_terminal_detail")
async def terminal_detail(tid: str, tail: int = 0):
    """获取终端会话详情（stdout/stderr）。

    Args:
        tid: 终端 ID
        tail: 只返回最后 N 字符；0 使用安全默认值 8000。
              完整日志请使用 /terminal/{tid}/output 游标读取。
    """
    t = _terminals.get(tid)
    if not t:
        return {"error": f"终端不存在: {tid}", "available": list(_terminals.keys())[-5:]}

    safe_tail = max(0, min(tail, _TERMINAL_MEMORY_TAIL_CHARS))
    stdout = _read_terminal_tail(t, "stdout", safe_tail)
    stderr = _read_terminal_tail(t, "stderr", safe_tail)

    return {
        "tid": tid,
        "label": t["label"],
        "cmd": t["cmd"],
        "shell": t["shell"],
        "shell_executable": t["shell_executable"],
        "shell_family": t["shell_family"],
        "shell_version": t["shell_version"],
        "cwd": t["cwd"],
        "status": t["status"],
        "pid": t["pid"],
        "exit_code": t["exit_code"],
        "started_at": t["started_at"],
        "finished_at": t["finished_at"],
        "elapsed": (t["finished_at"] or time.time()) - t["started_at"],
        "stdout": stdout,
        "stderr": stderr,
        "stdout_total_chars": t["stdout_chars"],
        "stderr_total_chars": t["stderr_chars"],
        "stdout_total_bytes": t["stdout_bytes"],
        "stderr_total_bytes": t["stderr_bytes"],
        "preview_chars": safe_tail or _TERMINAL_DETAIL_DEFAULT_CHARS,
        "stdout_preview_truncated": t["stdout_chars"] > len(stdout),
        "stderr_preview_truncated": t["stderr_chars"] > len(stderr),
        "output_version": t["output_version"],
    }


@terminal_router.get("/{tid}/output", operation_id="exec_terminal_output")
async def terminal_output(tid: str, channel: str = "stdout", offset: int = 0, limit: int = 65536):
    """按字节游标增量读取完整终端日志，单次最多 256 KiB。"""
    t = _terminals.get(tid)
    if not t:
        return {"error": f"终端不存在: {tid}"}
    if channel not in ("stdout", "stderr"):
        return {"error": "channel 必须是 stdout 或 stderr"}
    return {
        "tid": tid,
        "status": t["status"],
        "output_version": t["output_version"],
        **_read_terminal_range(t, channel, offset, limit),
    }


@terminal_router.post("/{tid}/kill", operation_id="exec_terminal_kill")
async def terminal_kill(tid: str, req: TerminalActionRequest | None = None, request: Request = None):  # type: ignore[assignment]
    """终止正在运行的终端会话

    权限：GUI 请求免审（已有自己的确认弹窗）；agent 请求需 owner_token。
    """
    if req is None:
        req = TerminalActionRequest()
    t = _terminals.get(tid)
    if not t:
        return {"success": False, "error": f"终端不存在: {tid}"}
    denied = _check_terminal_permission(tid, req.owner_token, request, "kill")
    if denied:
        return denied
    if t["status"] != "running":
        return {"success": False, "error": f"终端已结束（状态: {t['status']}）"}

    try:
        await _terminate_terminal_process(t)
        t["status"] = "killed"
        t["finished_at"] = time.time()
        _append_terminal_text(t, "stderr", "\n[用户手动终止]")
        logger.info(f"终端会话被终止: {tid} (pid={t['pid']})")
        return {"success": True, "message": f"终端 {tid} 已终止"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@terminal_router.delete("/{tid}", operation_id="exec_terminal_delete")
async def terminal_delete(tid: str, req: TerminalActionRequest | None = None, request: Request = None):  # type: ignore[assignment]
    """删除已结束的终端会话记录

    权限：GUI 请求免审（已有自己的确认弹窗）；agent 请求需 owner_token。
    """
    if req is None:
        req = TerminalActionRequest()
    t = _terminals.get(tid)
    if not t:
        return {"success": False, "error": f"终端不存在: {tid}"}
    denied = _check_terminal_permission(tid, req.owner_token, request, "delete")
    if denied:
        return denied
    if t["status"] == "running":
        return {"success": False, "error": "终端正在运行，请先终止"}
    _delete_terminal_files(t)
    del _terminals[tid]
    return {"success": True, "message": f"终端 {tid} 已删除"}


@output_router.get("", operation_id="exec_output_list")
async def list_outputs():
    """列出所有缓冲的输出ID及摘要"""
    result = []
    for eid, buf in _output_buffers.items():
        result.append({
            "exec_id": eid,
            "timestamp": buf.get("timestamp", ""),
            "stdout_chars": len(buf.get("stdout", "")),
            "stderr_chars": len(buf.get("stderr", "")),
            "code_preview": buf.get("code", "")[:100],
        })
    return {"count": len(result), "outputs": result}


def _record_history(code: str, success: bool, elapsed_ms: int, error: str | None = None):
    """记录执行历史"""
    _exec_history.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "code_preview": code[:100],
        "success": success,
        "elapsed_ms": elapsed_ms,
        "error": error,
    })
    if len(_exec_history) > _MAX_HISTORY:
        _exec_history.pop(0)


# ============================================================================
# T00: exec_inspect / exec_kill / exec_send_input / wait 工具
# spec D4/D9/D11 + Anti-Cheat 11
# ============================================================================

def _build_inspect_snapshot(t: dict) -> dict:
    """构造 exec_inspect 快照响应（spec D4）。

    返回结构：tid / status / pid / exit_code / elapsed / stdout_so_far /
    stderr_so_far / stdout_chars / stderr_chars。
    stdout/stderr_so_far 取尾部 _TERMINAL_DETAIL_DEFAULT_CHARS 字符预览。
    """
    return {
        "success": True,
        "tid": t["tid"],
        "status": t["status"],
        "pid": t["pid"],
        "exit_code": t.get("exit_code"),
        "elapsed": round((t.get("finished_at") or time.time()) - t["started_at"], 2),
        "stdout_so_far": _read_terminal_tail(t, "stdout", _TERMINAL_DETAIL_DEFAULT_CHARS),
        "stderr_so_far": _read_terminal_tail(t, "stderr", _TERMINAL_DETAIL_DEFAULT_CHARS),
        "stdout_chars": t["stdout_chars"],
        "stderr_chars": t["stderr_chars"],
    }


@router.post("/inspect", operation_id="exec_inspect")
async def exec_inspect(req: ExecInspectRequest):
    """查询终端状态（spec D4：新输出/子进程结束/N秒到任一触发立即返回）。

    timeout=0（默认）：立即返回当前快照。
    timeout>0：阻塞等待，任一触发立即返回：
      - 子进程结束（status 变为 done/killed/timeout/failed）
      - stdout/stderr 有新输出（chars 数变化）
      - N 秒到期

    已结束的 terminal 调用时立即返回，不阻塞。
    """
    t = _terminals.get(req.tid)
    if not t:
        return {"success": False, "error": f"终端不存在: {req.tid}"}

    t["last_activity_at"] = time.time()

    # 立即返回快照
    if req.timeout <= 0:
        return _build_inspect_snapshot(t)

    # terminal 已结束：立即返回（不阻塞）
    if t["status"] != "running":
        return _build_inspect_snapshot(t)

    # 阻塞等待：200ms 轮询，任一触发立即返回
    initial_stdout_chars = t["stdout_chars"]
    initial_stderr_chars = t["stderr_chars"]
    deadline = time.time() + req.timeout

    while True:
        await asyncio.sleep(0.2)
        t = _terminals.get(req.tid)
        if not t:
            return {"success": False, "error": f"终端已移除: {req.tid}"}

        # 触发条件 1：子进程结束
        if t["status"] != "running":
            t["last_activity_at"] = time.time()
            return _build_inspect_snapshot(t)

        # 触发条件 2：有新输出
        if t["stdout_chars"] != initial_stdout_chars or t["stderr_chars"] != initial_stderr_chars:
            t["last_activity_at"] = time.time()
            return _build_inspect_snapshot(t)

        # 触发条件 3：timeout 到期
        if time.time() >= deadline:
            t["last_activity_at"] = time.time()
            return _build_inspect_snapshot(t)


@router.post("/kill", operation_id="exec_kill")
async def exec_kill(req: ExecKillRequest):
    """终止终端子进程（SIGKILL 等价，spec D9）。

    幂等：terminal 已结束时返回当前 status，不重复 kill。
    """
    t = _terminals.get(req.tid)
    if not t:
        return {"success": False, "error": f"终端不存在: {req.tid}"}

    # 幂等：terminal 已结束
    if t["status"] != "running":
        t["last_activity_at"] = time.time()
        return {"success": True, "tid": req.tid, "status": t["status"], "message": "终端已结束"}

    try:
        await _terminate_terminal_process(t)
        t["status"] = "killed"
        t["finished_at"] = time.time()
        t["last_activity_at"] = time.time()
        _append_terminal_text(t, "stderr", "\n[agent exec_kill 终止]")
        logger.info("exec_kill 终止终端: %s (pid=%s)", req.tid, t["pid"])
        return {"success": True, "tid": req.tid, "status": "killed"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/send_input", operation_id="exec_send_input")
async def exec_send_input(req: ExecSendInputRequest):
    """向终端 stdin 发送文本（spec D9）。

    自动追加换行。子进程 stdin 已关闭时返回 ok=false。
    """
    t = _terminals.get(req.tid)
    if not t:
        return {"success": False, "error": f"终端不存在: {req.tid}"}

    if t["status"] != "running":
        return {"success": False, "error": f"终端已结束（状态: {t['status']}）"}

    proc = t.get("_proc")
    if not proc or not proc.stdin:
        return {"success": False, "error": "终端不支持输入（stdin 未开启或已关闭）"}

    try:
        data = req.text + "\n"
        proc.stdin.write(data.encode("utf-8"))
        await proc.stdin.drain()
        t["last_activity_at"] = time.time()
        return {"success": True, "tid": req.tid, "bytes_written": len(data)}
    except Exception as e:
        # stdin 已关闭（BrokenPipeError 等）
        return {"success": False, "tid": req.tid, "error": f"stdin 写入失败: {e}"}


@router.post("/wait", operation_id="wait")
async def exec_wait(req: WaitRequest):
    """纯 sleep（spec Anti-Cheat 11：不查 terminal 状态）。

    通用工具，不限于 terminal 场景。
    LLM 想纯等待时用 wait(N) + exec_inspect(tid, timeout=0) 两步流程；
    想频繁看进展时用 exec_inspect(tid, timeout=N)（新输出即返回）。
    """
    if req.seconds <= 0:
        return {"success": False, "error": "seconds 必须 > 0"}
    if req.seconds > 3600:
        return {"success": False, "error": "seconds 上限 3600"}

    await asyncio.sleep(req.seconds)
    return {"success": True, "slept": req.seconds}


# ============================================================================
# T00: terminal TTL 清理（spec D11 第三层保障）
# ============================================================================

_TERMINAL_TTL_SECONDS = 30 * 60  # finished/killed 30 分钟后移除审计记录
_TERMINAL_TTL_SCAN_INTERVAL = 300  # 5 分钟扫描一次
_terminal_ttl_task: asyncio.Task | None = None


async def _terminal_ttl_cleanup_loop():
    """后台任务：每 5 分钟扫描 _terminals，移除已结束且超时的记录。

    spec D11 第三层：running 状态的 terminal 不被 TTL 移除
    （仅由 exec_kill 或子进程自然结束清理）。
    移除时关联的 .py 临时文件 + stdout/stderr 日志文件一起删。
    """
    while True:
        await asyncio.sleep(_TERMINAL_TTL_SCAN_INTERVAL)
        try:
            now = time.time()
            expired_tids = []
            for tid, t in _terminals.items():
                if t["status"] == "running":
                    continue  # running 不移除
                last_activity = t.get("last_activity_at") or t.get("finished_at") or t["started_at"]
                if (now - last_activity) > _TERMINAL_TTL_SECONDS:
                    expired_tids.append(tid)

            for tid in expired_tids:
                t = _terminals.pop(tid, None)
                if not t:
                    continue
                # 清理 .py 临时文件（T01 exec_python 改造后会写 temp_file）
                temp_file = t.get("temp_file")
                if temp_file:
                    try:
                        Path(temp_file).unlink(missing_ok=True)
                    except Exception:
                        pass
                # 清理 stdout/stderr 日志文件
                for channel in ("stdout", "stderr"):
                    path = t.get(f"_{channel}_path")
                    if path:
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass
                logger.info("TTL 清理终端: %s (status=%s)", tid, t["status"])
        except Exception as e:
            logger.warning("TTL 清理异常: %s", e)


def cleanup_orphan_temp_files() -> dict:
    """启动时清理孤儿临时文件（修复 bug：后端重启后 temp/exec_*.py 和 term_*.log 累积）。

    TTL 清理依赖内存中的 _terminals dict，但后端重启后内存丢失，磁盘上的孤儿文件
    永远无法被清理。本函数在启动时扫描磁盘，删除所有不在当前 _terminals dict 中的
    exec_*.py 和 terminals/term_*.log 文件。

    Returns:
        统计字典：{deleted_exec_files, deleted_term_logs, skipped, errors}
    """
    stats = {"deleted_exec_files": 0, "deleted_term_logs": 0, "skipped": 0, "errors": 0}

    # 收集当前 _terminals dict 中跟踪的所有文件路径
    tracked_temp_files = set()
    tracked_term_logs = set()
    for session in _terminals.values():
        tf = session.get("temp_file")
        if tf:
            tracked_temp_files.add(Path(tf).resolve())
        for channel in ("stdout", "stderr"):
            p = session.get(f"_{channel}_path")
            if p:
                tracked_term_logs.add(Path(p).resolve())

    # 扫描 temp/exec_*.py
    try:
        for path in TEMP_DIR.glob("exec_*.py"):
            try:
                if path.resolve() not in tracked_temp_files:
                    path.unlink(missing_ok=True)
                    stats["deleted_exec_files"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:
                stats["errors"] += 1
    except Exception:
        stats["errors"] += 1

    # 扫描 temp/terminals/term_*.log
    try:
        for path in TERMINAL_DIR.glob("term_*.log"):
            try:
                if path.resolve() not in tracked_term_logs:
                    path.unlink(missing_ok=True)
                    stats["deleted_term_logs"] += 1
                else:
                    stats["skipped"] += 1
            except Exception:
                stats["errors"] += 1
    except Exception:
        stats["errors"] += 1

    if stats["deleted_exec_files"] or stats["deleted_term_logs"]:
        logger.info(
            "启动清理孤儿临时文件: exec_*.py=%d, term_*.log=%d, skipped=%d, errors=%d",
            stats["deleted_exec_files"], stats["deleted_term_logs"],
            stats["skipped"], stats["errors"],
        )
    return stats


def start_terminal_ttl_cleanup() -> None:
    """启动 TTL 清理后台任务（幂等，重复调用不创建多个）。

    在 server.main 启动时调用一次。启动时先同步清理孤儿文件（后端重启后内存丢失，
    磁盘上的 temp/exec_*.py 和 term_*.log 无法被 TTL 清理）。
    """
    # 启动时先清理孤儿文件（修复主要根因）
    try:
        cleanup_orphan_temp_files()
    except Exception as e:
        logger.warning("启动清理孤儿临时文件失败: %s", e)

    global _terminal_ttl_task
    if _terminal_ttl_task is None or _terminal_ttl_task.done():
        _terminal_ttl_task = asyncio.create_task(_terminal_ttl_cleanup_loop())
        logger.info("terminal TTL 清理任务已启动（间隔 %ds，TTL %ds）",
                    _TERMINAL_TTL_SCAN_INTERVAL, _TERMINAL_TTL_SECONDS)
