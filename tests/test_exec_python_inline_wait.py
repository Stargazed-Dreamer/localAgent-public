"""exec_python 内联等待改造（v16）测试。

覆盖 spec Proof 1-4, 6, 9：
- AC1 短任务一步返回 done（stdout 含结果，无需 inspect）
- AC2 长任务返回 running（inline_wait_secs 超时，含 terminal_id）
- AC3 禁用回退 v15（inline_wait_secs=0 → 立即返回 running，无 stdout）
- AC4 输出截断生效（stdout 超 8000 字符被截断）
- AC5 exit_code 透传（sys.exit(3) → exit_code==3）
- AC6 配置生效（inline_wait_secs=3 → 实测等 ~3s）

通过 monkeypatch server.config.get_server_config 控制 inline_wait_secs，
不依赖 config.toml 真实值（测试隔离）。
"""

import time


def _set_inline_wait(monkeypatch, secs: int):
    """覆盖 get_server_config 返回指定 inline_wait_secs（其他字段保持默认）。"""
    def fake_server_config():
        return {
            "host": "127.0.0.1",
            "port": 8766,
            "exec_python_inline_wait_secs": secs,
        }
    monkeypatch.setattr("server.config.get_server_config", fake_server_config)


class TestExecPythonInlineWait:
    """v16 exec_python 内联等待测试。"""

    def test_short_task_returns_done_with_stdout(self, client, monkeypatch):
        """AC1: 短任务（<10s）应一步返回 status=done 且 stdout 含结果，无需 inspect。"""
        _set_inline_wait(monkeypatch, 10)
        resp = client.post("/exec/python", json={"code": "print(1+1)"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert "2" in data["stdout"]
        assert data["exit_code"] == 0

    def test_long_task_returns_running_with_terminal_id(self, client, monkeypatch):
        """AC2: 长任务（>inline_wait_secs）应返回 status=running 且含 terminal_id。"""
        _set_inline_wait(monkeypatch, 2)
        resp = client.post("/exec/python", json={
            "code": "import time; time.sleep(15)",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "running"
        assert data["terminal_id"] != ""
        # running 路径不带 stdout（v15 兼容）
        assert data["stdout"] == ""

    def test_disabled_falls_back_to_v15(self, client, monkeypatch):
        """AC3: inline_wait_secs=0 时回退 v15 立即返回（status=running，无 stdout）。"""
        _set_inline_wait(monkeypatch, 0)
        resp = client.post("/exec/python", json={"code": "print('immediate')"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "running"
        assert data["terminal_id"] != ""
        assert data["stdout"] == ""
        assert data["exit_code"] is None

    def test_output_truncated_when_exceeds_threshold(self, client, monkeypatch):
        """AC4: done 路径 stdout 超 8000 字符应被截断，含截断标记。"""
        _set_inline_wait(monkeypatch, 30)
        resp = client.post("/exec/python", json={
            "code": "for i in range(10000): print(f'line {i}: ' + 'x' * 50)",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert data["stdout_truncated"] is True
        assert data["stdout_total_chars"] > 8000
        assert "已截断" in data["stdout"]

    def test_exit_code_propagated(self, client, monkeypatch):
        """AC5: sys.exit(N) → 响应 exit_code==N。"""
        _set_inline_wait(monkeypatch, 10)
        resp = client.post("/exec/python", json={
            "code": "import sys; sys.exit(3)",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert data["exit_code"] == 3

    def test_config_inline_wait_secs_takes_effect(self, client, monkeypatch):
        """AC6: inline_wait_secs=3 → 短任务实测等 ~3s 内返回 done。

        用一个 sleep(1) 的短任务验证：3s 配置下能等到 done（1<3），
        同时验证 elapsed 反映实际执行时间。
        """
        _set_inline_wait(monkeypatch, 3)
        start = time.time()
        resp = client.post("/exec/python", json={
            "code": "import time; time.sleep(1); print('done after 1s')",
        })
        elapsed = time.time() - start
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert "done after 1s" in data["stdout"]
        # 实际等待应该在 1-3s 之间（子进程 1s + 少量开销）
        assert elapsed >= 0.9
        assert elapsed < 3.5

    def test_error_output_captured_in_done_path(self, client, monkeypatch):
        """补充：异常代码 done 路径应捕获 stderr + 非零 exit_code。"""
        _set_inline_wait(monkeypatch, 10)
        resp = client.post("/exec/python", json={
            "code": "raise ValueError('test error from inline')",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "done"
        assert "test error from inline" in data["stderr"]
        assert data["exit_code"] != 0

    def test_cleanup_terminal_after_done(self, client, monkeypatch):
        """补充：done 路径完成后 terminal 仍存在于 _terminals（供 exec_output 查全量）。

        验证 done 后可用 terminal_id 查 detail（spec 不要求清理 done terminal，
        TTL 清理机制兜底）。
        """
        _set_inline_wait(monkeypatch, 10)
        resp = client.post("/exec/python", json={"code": "print('cleanup test')"})
        data = resp.json()
        assert data["status"] == "done"
        tid = data["terminal_id"]
        # detail 应能查到
        detail = client.get(f"/terminals/{tid}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "done"
