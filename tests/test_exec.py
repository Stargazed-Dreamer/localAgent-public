"""代码执行模块测试

测试 /exec 端点的状态查询、Python 代码执行（v16 内联等待）、错误处理和输出截断。

v16 注意事项（spec temp/sdd/exec-python-inline-wait/）：
- ExecRequest 无 timeout 字段（v15 spec D5 移除）
- ExecResponse.success=True 表示 spawn 成功（即使子进程 exit_code≠0）
- ExecResponse.status="done"（内联等待完成）或 "running"（超时仍在运行）
- ExecResponse.terminal_id 替代旧 exec_id
- /output/{exec_id} 端点只对 /exec/cmd（shell）有效，/exec/python 走 terminal 日志
"""

import pytest


@pytest.fixture(autouse=True)
def _cleanup_terminals():
    """autouse: 每个测试后清理 _terminals 全局 dict 残留（危险操作铁律兜底）。

    /exec/python、/exec/cmd、terminal_spawn 会在全局 _terminals dict 创建会话。
    若测试中途断言失败，terminal_kill/terminal_delete 不会执行，_collector_task
    后台任务 + 子进程残留。本 fixture 在每个测试后遍历 _terminals，对残留会话
    先 kill 再 delete，防止泄漏。

    参照 test_command_guard.py 的 autouse 兜底模式（铁律要求 autouse 默认 mock/清理危险 API）。
    _terminals 为空时 quickly return，对不碰 terminal 的测试无副作用。
    """
    yield
    from server.exec import _terminals, terminal_delete, terminal_kill

    if not _terminals:
        return

    import asyncio

    async def _cleanup() -> None:
        for tid in list(_terminals.keys()):
            try:
                await terminal_kill(tid)  # running 状态先终止进程
            except Exception:
                pass
            try:
                await terminal_delete(tid)  # 删除会话记录
            except Exception:
                pass

    try:
        asyncio.run(_cleanup())
    except Exception:
        pass


class TestExec:
    """代码执行模块测试"""

    def test_exec_status(self, client):
        """GET /exec/status 应返回状态"""
        resp = client.get("/exec/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "temp_dir" in data
        assert "history_count" in data
        assert "buffered_outputs" in data

    def test_exec_python_simple(self, client):
        """执行简单Python代码应成功（v16 内联等待 done 路径）"""
        resp = client.post("/exec/python", json={
            "code": "print('hello from test')",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert "hello from test" in data["stdout"]
        assert data["terminal_id"] != ""

    def test_exec_python_with_error(self, client):
        """执行错误代码应返回非零 exit_code 和 stderr（v16: success=True 因为 spawn 成功）"""
        resp = client.post("/exec/python", json={
            "code": "raise ValueError('test error')",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True  # spawn 成功，子进程抛异常不算 spawn 失败
        assert data["status"] == "done"
        assert data["exit_code"] == 1
        assert "test error" in data["stderr"]

    def test_exec_python_long_running_returns_running(self, client):
        """长任务超 inline_wait_secs 后应返回 status=running + terminal_id"""
        # 默认 inline_wait_secs=10s，sleep 15s 会超时
        resp = client.post("/exec/python", json={
            "code": "import time; time.sleep(15)",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "running"
        assert data["terminal_id"] != ""
        # 清理：kill terminal 避免泄漏
        if data["terminal_id"]:
            try:
                client.post("/exec/kill", json={"terminal_id": data["terminal_id"]})
            except Exception:
                pass

    def test_exec_python_math(self, client):
        """执行数学计算应返回正确结果"""
        resp = client.post("/exec/python", json={
            "code": "result = 2 + 3\nprint(f'result={result}')",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert "result=5" in data["stdout"]

    def test_exec_status_after_runs(self, client):
        """执行代码后历史记录应增加"""
        resp_before = client.get("/exec/status").json()
        count_before = resp_before["history_count"]

        client.post("/exec/python", json={
            "code": "print('history test')",
        })

        resp_after = client.get("/exec/status").json()
        assert resp_after["history_count"] >= count_before

    def test_exec_python_workspace_venv_and_cwd(self, client):
        from pathlib import Path

        project_root = Path(__file__).parent.parent.resolve()
        resp = client.post("/exec/python", json={
            "code": "import os, sys; import server.exec; print(os.getcwd()); print(sys.executable); print('中文可用')",
            "cwd": str(project_root),
            "environment": "workspace_venv",
        })
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert data["cwd"] == str(project_root)
        assert Path(data["python_executable"]).resolve() == (project_root / ".venv" / "Scripts" / "python.exe").resolve()
        assert data["environment"] == "workspace_venv"
        assert "中文可用" in data["stdout"]

    def test_exec_python_missing_workspace_venv_is_explicit(self, client, tmp_path):
        resp = client.post("/exec/python", json={
            "code": "print('never runs')",
            "cwd": str(tmp_path),
            "environment": "workspace_venv",
        })
        data = resp.json()
        assert data["success"] is False
        assert "未找到项目虚拟环境" in data["error"]


class TestExecTruncation:
    """输出截断测试（v16 done 路径）"""

    def test_short_output_not_truncated(self, client):
        """短输出不应被截断"""
        resp = client.post("/exec/python", json={
            "code": "print('short output')",
        })
        data = resp.json()
        assert data["status"] == "done"
        assert data["stdout_truncated"] is False
        assert data["stdout_total_chars"] < 8000

    def test_long_output_truncated(self, client):
        """长输出应被截断"""
        resp = client.post("/exec/python", json={
            "code": "for i in range(10000): print(f'line {i}: ' + 'x' * 50)",
        })
        data = resp.json()
        assert data["status"] == "done"
        assert data["stdout_truncated"] is True
        assert data["stdout_total_chars"] > 8000
        assert "已截断" in data["stdout"]
        assert data["terminal_id"] != ""


class TestExecOutputView:
    """/output/{exec_id} 端点测试。

    注意：v16 /exec/python 走 terminal 机制不填充 _output_buffers，
    这里用 /exec/cmd（shell 端点）产生输出，/output/ 端点仍对 shell cmd 有效。
    """

    def test_output_range(self, client):
        """按区间查看输出"""
        # 用 /exec/cmd 产生输出（shell 端点填充 _output_buffers）
        exec_resp = client.post("/exec/cmd", json={
            "cmd": "for /L %i in (1,1,100) do @echo line_%i",
            "shell": "cmd",
            "timeout": 10,
        })
        exec_id = exec_resp.json()["exec_id"]

        # 查看前100字符
        resp = client.post(f"/output/{exec_id}", json={
            "action": "range",
            "channel": "stdout",
            "start": 0,
            "end": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_chars"] > 0
        assert len(data["content"]) <= 100

    def test_output_search(self, client):
        """搜索输出内容"""
        exec_resp = client.post("/exec/cmd", json={
            "cmd": "for /L %i in (1,1,100) do @echo line_%i",
            "shell": "cmd",
            "timeout": 10,
        })
        exec_id = exec_resp.json()["exec_id"]

        resp = client.post(f"/output/{exec_id}", json={
            "action": "search",
            "query": "line_50",
            "channel": "stdout",
            "context_chars": 50,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_matches"] >= 1
        assert len(data["matches"]) >= 1
        assert "line_50" in data["matches"][0]["context"]

    def test_output_list(self, client):
        """列出缓冲的输出"""
        client.post("/exec/cmd", json={
            "cmd": "echo buffer test",
            "shell": "cmd",
            "timeout": 10,
        })
        resp = client.get("/output")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

    def test_output_range_nonexistent_id(self, client):
        """不存在的 exec_id 应返回错误"""
        resp = client.post("/output/nonexistent_id", json={
            "action": "range",
            "channel": "stdout",
            "start": 0,
            "end": 100,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    def test_output_search_nonexistent_id(self, client):
        """不存在的 exec_id 搜索应返回错误"""
        resp = client.post("/output/nonexistent_id", json={
            "action": "search",
            "query": "test",
            "channel": "stdout",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data


class TestTerminalSessions:
    """后台终端应能安全承载长时间和高输出任务。"""

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.run(coro)

    def test_terminal_large_stdout_stderr_is_paged(self):
        async def scenario():
            from server.exec import (
                _TERMINAL_MEMORY_TAIL_CHARS,
                TerminalSpawnRequest,
                _terminals,
                terminal_delete,
                terminal_detail,
                terminal_output,
                terminal_spawn,
            )

            command = (
                "import sys; "
                "[print('out-%05d-' % i + 'x' * 80) for i in range(2500)]; "
                "[print('err-%05d-' % i + 'y' * 80, file=sys.stderr) for i in range(2500)]"
            )
            spawned = await terminal_spawn(TerminalSpawnRequest(
                cmd=command, shell="python", label="large-output-test", timeout=10,
            ))
            assert spawned["success"] is True
            await _terminals[spawned["tid"]]["_collector_task"]

            detail = await terminal_detail(spawned["tid"], tail=2000)
            assert detail["status"] == "done"
            assert detail["stdout_total_chars"] > 200000
            assert detail["stderr_total_chars"] > 200000
            assert len(detail["stdout"]) <= 2000
            assert len(detail["stderr"]) <= 2000
            assert detail["stdout_preview_truncated"] is True
            assert detail["stderr_preview_truncated"] is True

            first = await terminal_output(spawned["tid"], "stdout", 0, 4096)
            assert first["content"].startswith("out-00000-")
            assert first["has_more"] is True
            second = await terminal_output(spawned["tid"], "stdout", first["next_offset"], 4096)
            assert second["offset"] == first["next_offset"]
            assert second["content"]

            session = _terminals[spawned["tid"]]
            assert len(session["stdout"]) <= _TERMINAL_MEMORY_TAIL_CHARS
            assert len(session["stderr"]) <= _TERMINAL_MEMORY_TAIL_CHARS
            await terminal_delete(spawned["tid"])

        self._run(scenario())


class TestApplyPatch:
    """UTF-8补丁入口应支持中文、dry-run和文件清单。"""

    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.run(coro)

    def test_apply_patch_utf8_and_dry_run(self, tmp_path):
        async def scenario():
            from server.exec import ApplyPatchRequest, exec_apply_patch

            patch = """*** Begin Patch
*** Add File: 中文目录/示例.txt
+第一行
+中文内容
*** End Patch"""
            dry_run = await exec_apply_patch(ApplyPatchRequest(
                patch=patch, cwd=str(tmp_path), dry_run=True,
            ))
            assert dry_run["success"] is True
            assert dry_run["dry_run"] is True
            assert dry_run["changed_files"] == ["中文目录/示例.txt"]
            assert not (tmp_path / "中文目录" / "示例.txt").exists()
            assert not (tmp_path / "中文目录").exists()

            applied = await exec_apply_patch(ApplyPatchRequest(patch=patch, cwd=str(tmp_path)))
            assert applied["success"] is True
            assert applied["changed_files"] == ["中文目录/示例.txt"]
            assert (tmp_path / "中文目录" / "示例.txt").read_text(encoding="utf-8") == "第一行\n中文内容\n"

        self._run(scenario())

    def test_apply_patch_rejects_invalid_boundaries(self, tmp_path):
        async def scenario():
            from server.exec import ApplyPatchRequest, exec_apply_patch

            result = await exec_apply_patch(ApplyPatchRequest(
                patch="*** Add File: bad.txt\n+bad", cwd=str(tmp_path),
            ))
            assert result["success"] is False
            assert "Begin Patch" in result["error"]

        self._run(scenario())

    def test_apply_patch_failure_restores_all_files(self, tmp_path):
        async def scenario():
            from server.exec import ApplyPatchRequest, exec_apply_patch

            existing = tmp_path / "existing.txt"
            existing.write_text("original\n", encoding="utf-8")
            patch = """*** Begin Patch
*** Update File: existing.txt
@@
-original
+changed
*** Update File: missing.txt
@@
-missing
+never
*** End Patch"""
            result = await exec_apply_patch(ApplyPatchRequest(patch=patch, cwd=str(tmp_path)))
            assert result["success"] is False
            assert result["rolled_back"] is True
            assert existing.read_text(encoding="utf-8") == "original\n"
            assert not (tmp_path / "missing.txt").exists()

        self._run(scenario())


class TestShellResolution:
    def test_powershell_prefers_available_modern_shell(self):
        from pathlib import Path

        from server.exec import _resolve_shell

        command, info = _resolve_shell("powershell", "$PSVersionTable.PSVersion.ToString()")
        assert command[0] == info["shell_executable"]
        assert Path(command[0]).stem.lower() in ("pwsh", "powershell")
        assert info["shell_family"] in ("pwsh", "windows_powershell")

    def test_shell_response_includes_version(self):
        import asyncio

        from server.exec import CmdRequest, exec_cmd

        result = asyncio.run(exec_cmd(CmdRequest(
            cmd="$PSVersionTable.PSVersion.ToString()", shell="powershell",
        )))
        assert result.success is True
        assert result.shell_executable
        assert result.shell_family in ("pwsh", "windows_powershell")
        assert result.shell_version and result.shell_version != "unknown"


class TestTerminalSessionCleanup:
    @staticmethod
    def _run(coro):
        import asyncio
        return asyncio.run(coro)

    def test_terminal_detail_defaults_to_safe_preview(self):
        async def scenario():
            from server.exec import TerminalSpawnRequest, _terminals, terminal_delete, terminal_detail, terminal_spawn

            spawned = await terminal_spawn(TerminalSpawnRequest(cmd="print('z' * 20000)", shell="python"))
            await _terminals[spawned["tid"]]["_collector_task"]
            detail = await terminal_detail(spawned["tid"])
            assert detail["stdout_total_chars"] >= 20000
            assert len(detail["stdout"]) <= 8000
            assert detail["stdout_preview_truncated"] is True
            await terminal_delete(spawned["tid"])

        self._run(scenario())

    def test_terminal_delete_removes_log_files(self):
        async def scenario():
            from server.exec import TerminalSpawnRequest, _terminals, terminal_delete, terminal_spawn

            spawned = await terminal_spawn(TerminalSpawnRequest(cmd="print('cleanup')", shell="python"))
            await _terminals[spawned["tid"]]["_collector_task"]
            paths = [
                _terminals[spawned["tid"]]["_stdout_path"],
                _terminals[spawned["tid"]]["_stderr_path"],
            ]
            assert all(path.exists() for path in paths)
            deleted = await terminal_delete(spawned["tid"])
            assert deleted["success"] is True
            assert all(not path.exists() for path in paths)

        self._run(scenario())
