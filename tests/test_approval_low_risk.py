"""测试 D7（LLM 重试 + AST 低风险降级）和 D8（路径白名单）

策略：
- _is_low_risk()：直接调用，覆盖各种代码模式（纯读/写/删除/网络/子进程/eval/import 黑名单）
- D7-B 降级放行：monkeypatch _llm_review 返回 unavailable，验证 exec_python 低风险被放行
- D7-B 不降级场景：exec_python 高风险 + exec_cmd 不可降级
- D8 路径白名单：构造 temp//workspace/ 路径写操作，验证静态规则豁免
- D7-A LLM 重试：monkeypatch call_llm_simple 模拟首次空→重试成功/重试也空
"""

from pathlib import Path

from server import approval_review

# ========== D7-B: _is_low_risk() 单元测试 ==========

class TestIsLowRisk:
    """测试 _is_low_risk() 的 5 个判定条件"""

    def test_pure_read_passes(self):
        """纯读操作 → 低风险"""
        code = """
import os
files = os.listdir('.')
for f in files:
    print(f)
"""
        assert approval_review._is_low_risk(code) is True

    def test_open_read_mode_passes(self):
        """open(r) → 低风险"""
        code = "data = open('test.txt', 'r').read()"
        assert approval_review._is_low_risk(code) is True

    def test_open_no_mode_passes(self):
        """open() 无 mode 参数 → 低风险（默认 'r'）"""
        code = "f = open('test.txt')"
        assert approval_review._is_low_risk(code) is True

    def test_open_write_mode_fails(self):
        """open('w') → 非低风险"""
        code = "open('test.txt', 'w').write('data')"
        assert approval_review._is_low_risk(code) is False

    def test_open_append_mode_fails(self):
        """open('a') → 非低风险"""
        code = "open('test.txt', 'a').write('data')"
        assert approval_review._is_low_risk(code) is False

    def test_subprocess_import_fails(self):
        """import subprocess → 非低风险"""
        code = "import subprocess\nsubprocess.run(['ls'])"
        assert approval_review._is_low_risk(code) is False

    def test_subprocess_from_import_fails(self):
        """from subprocess import run → 非低风险"""
        code = "from subprocess import run\nrun(['ls'])"
        assert approval_review._is_low_risk(code) is False

    def test_socket_import_fails(self):
        """import socket → 非低风险"""
        code = "import socket\ns = socket.socket()"
        assert approval_review._is_low_risk(code) is False

    def test_pickle_import_fails(self):
        """import pickle → 非低风险"""
        code = "import pickle\npickle.loads(b'\\x80')"
        assert approval_review._is_low_risk(code) is False

    def test_os_system_fails(self):
        """os.system() → 非低风险"""
        code = "import os\nos.system('rm -rf /')"
        assert approval_review._is_low_risk(code) is False

    def test_os_remove_fails(self):
        """os.remove() → 非低风险"""
        code = "import os\nos.remove('test.txt')"
        assert approval_review._is_low_risk(code) is False

    def test_shutil_rmtree_fails(self):
        """shutil.rmtree() → 非低风险"""
        code = "import shutil\nshutil.rmtree('/important')"
        assert approval_review._is_low_risk(code) is False

    def test_eval_fails(self):
        """eval() → 非低风险"""
        code = "result = eval('1+1')"
        assert approval_review._is_low_risk(code) is False

    def test_exec_fails(self):
        """exec() → 非低风险"""
        code = "exec('print(1)')"
        assert approval_review._is_low_risk(code) is False

    def test_dunder_import_fails(self):
        """__import__() → 非低风险"""
        code = "mod = __import__('os')"
        assert approval_review._is_low_risk(code) is False

    def test_path_unlink_fails(self):
        """Path.unlink() → 非低风险"""
        code = "from pathlib import Path\nPath('test.txt').unlink()"
        assert approval_review._is_low_risk(code) is False

    def test_path_rename_fails(self):
        """Path.rename() → 非低风险"""
        code = "from pathlib import Path\nPath('a.txt').rename('b.txt')"
        assert approval_review._is_low_risk(code) is False

    def test_syntax_error_fails(self):
        """语法错误 → 非低风险"""
        code = "def broken(:"
        assert approval_review._is_low_risk(code) is False

    def test_too_many_lines_fails(self):
        """超过 50 行 → 非低风险"""
        lines = [f"x_{i} = {i}" for i in range(51)]
        code = "\n".join(lines)
        assert approval_review._is_low_risk(code) is False

    def test_exactly_50_lines_passes(self):
        """恰好 50 行 → 低风险"""
        lines = [f"x_{i} = {i}" for i in range(50)]
        code = "\n".join(lines)
        assert approval_review._is_low_risk(code) is True

    def test_empty_code_fails(self):
        """空代码 → 非低风险"""
        assert approval_review._is_low_risk("") is False
        assert approval_review._is_low_risk("   ") is False

    def test_simple_read_with_json(self):
        """复杂纯读操作 → 低风险"""
        code = """
import json
with open('config.json', 'r') as f:
    data = json.load(f)
print(data['key'])
"""
        assert approval_review._is_low_risk(code) is True

    def test_open_with_keyword_mode_r(self):
        """open(mode='r') keyword arg → 低风险"""
        code = "f = open('test.txt', mode='r')"
        assert approval_review._is_low_risk(code) is True

    def test_open_with_keyword_mode_w(self):
        """open(mode='w') keyword arg → 非低风险"""
        code = "f = open('test.txt', mode='w')"
        assert approval_review._is_low_risk(code) is False

    def test_ctypes_import_fails(self):
        """import ctypes → 非低风险"""
        code = "import ctypes"
        assert approval_review._is_low_risk(code) is False

    def test_requests_import_fails(self):
        """import requests → 非低风险"""
        code = "import requests"
        assert approval_review._is_low_risk(code) is False


# ========== D7-B: 降级放行集成测试 ==========

class TestLowRiskDegradation:
    """测试 try_auto_approve 中 LLM 不可用时的降级放行逻辑"""

    def _make_request(self, operation_id="exec_python", code="x = 1 + 1"):
        """构造审批请求数据"""
        return {
            "operation_id": operation_id,
            "request_data": {"code": code},
            "method": "POST",
            "path": "/exec/python",
        }

    def _patch_llm_unavailable(self, monkeypatch):
        """让 _llm_review 返回 unavailable"""
        monkeypatch.setattr(
            approval_review, "_llm_review",
            lambda op, req, m, p: {"decision": "unavailable", "reason": "LLM 池未初始化"}
        )

    def _patch_llm_approve(self, monkeypatch):
        """让 _llm_review 返回 approve"""
        monkeypatch.setattr(
            approval_review, "_llm_review",
            lambda op, req, m, p: {"decision": "approve", "reason": "纯读操作，安全"}
        )

    def _patch_config(self, monkeypatch):
        """mock 配置，确保通过 reviewable 检查"""
        monkeypatch.setattr(
            approval_review, "get_command_guard_config",
            lambda: {"llm_review_enabled": True, "llm_review_endpoints": list(approval_review._REVIEWABLE_OPS)}
        )
        monkeypatch.setattr(
            approval_review, "get_cleanup_config",
            lambda: {
                "approvals_log_max_bytes": 10 * 1024 * 1024,
                "approvals_log_backup_count": 3,
                "approval_audit_log_max_bytes": 10 * 1024 * 1024,
                "approval_audit_log_backup_count": 3,
            }
        )

    def test_llm_unavailable_low_risk_degraded_approve(self, monkeypatch):
        """LLM 不可用 + exec_python + 低风险 → 降级放行"""
        self._patch_config(monkeypatch)
        self._patch_llm_unavailable(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        result = approval_review.try_auto_approve(
            "exec_python", {"code": "x = 1\nprint(x)"}, "POST", "/exec/python"
        )
        assert result["auto_approved"] is True
        assert result["layer2_llm"] == "low_risk_degraded"
        assert "低风险降级放行" in result["reason"]

    def test_llm_unavailable_high_risk_not_degraded(self, monkeypatch):
        """LLM 不可用 + exec_python + 高风险（含 eval）→ 不降级，fail_closed

        用 eval() 作为高风险代码：它通过静态规则（不在 _DANGER_PATTERNS），
        但 _is_low_risk 判定为非低风险（在 _DANGEROUS_CALL_NAMES 中）。
        """
        self._patch_config(monkeypatch)
        self._patch_llm_unavailable(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        result = approval_review.try_auto_approve(
            "exec_python",
            {"code": "result = eval('1+1')"},
            "POST", "/exec/python"
        )
        assert result["auto_approved"] is False
        assert result["layer2_llm"] == "unavailable"
        assert "LLM" in result["reason"]

    def test_llm_unavailable_exec_cmd_not_degraded(self, monkeypatch):
        """LLM 不可用 + exec_cmd → 不降级（shell 无法 AST 分析）"""
        self._patch_config(monkeypatch)
        self._patch_llm_unavailable(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        result = approval_review.try_auto_approve(
            "exec_cmd", {"cmd": "ls -la"}, "POST", "/exec/cmd"
        )
        assert result["auto_approved"] is False
        assert result["layer2_llm"] == "unavailable"

    def test_llm_available_no_degradation(self, monkeypatch):
        """LLM 可用 + approve → 正常放行（不走降级路径）"""
        self._patch_config(monkeypatch)
        self._patch_llm_approve(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        result = approval_review.try_auto_approve(
            "exec_python", {"code": "print('hello')"}, "POST", "/exec/python"
        )
        assert result["auto_approved"] is True
        assert result["layer2_llm"] == "approve"

    def test_llm_unavailable_open_write_not_degraded(self):
        """LLM 不可用 + exec_python + open(w) → 不降级（有写操作）"""
        # 注意：open(w) 会被静态规则拦截，所以这里测的是静态通过后的场景
        # 实际上 open(w) 到不了 LLM 审查（被 Layer 1 拦截）
        # 所以这里直接测 _is_low_risk
        assert approval_review._is_low_risk("open('test.txt', 'w').write('data')") is False


# ========== D8: 路径白名单测试 ==========

class TestPathWhitelist:
    """测试 D8：temp/ 和 workspace/ 路径写操作豁免静态拦截"""

    def test_temp_path_write_bypassed(self):
        """temp/ 路径写操作 → 静态规则豁免"""
        # 用项目内 temp/ 绝对路径
        project_root = Path(__file__).resolve().parents[1]
        temp_file = project_root / "temp" / "test_output.txt"
        code = f"open(r'{temp_file}', 'w').write('test data')"
        result, reason = approval_review._static_review(code)
        assert result == "pass"
        assert reason is None

    def test_workspace_path_write_bypassed(self):
        """workspace/ 路径写操作 → 静态规则豁免"""
        project_root = Path(__file__).resolve().parents[1]
        ws_file = project_root / "workspace" / "test_output.txt"
        code = f"open(r'{ws_file}', 'w').write('test data')"
        result, reason = approval_review._static_review(code)
        assert result == "pass"
        assert reason is None

    def test_non_whitelisted_path_write_blocked(self):
        """非白名单路径写操作 → 静态规则拦截"""
        code = "open('C:/Users/testuser/Desktop/test.txt', 'w').write('data')"
        result, reason = approval_review._static_review(code)
        assert result == "block"
        assert "文件写" in reason

    def test_mixed_paths_write_blocked(self):
        """混合路径（白名单+非白名单）→ 静态规则拦截"""
        project_root = Path(__file__).resolve().parents[1]
        temp_file = project_root / "temp" / "ok.txt"
        code = (
            f"open(r'{temp_file}', 'w').write('ok')\n"
            f"open('C:/Users/testuser/Desktop/bad.txt', 'w').write('bad')"
        )
        result, reason = approval_review._static_review(code)
        assert result == "block"
        assert "文件写" in reason

    def test_only_whitelisted_paths_bypass(self):
        """全部白名单路径写操作 → 豁免"""
        project_root = Path(__file__).resolve().parents[1]
        temp_file = project_root / "temp" / "a.txt"
        ws_file = project_root / "workspace" / "b.txt"
        code = (
            f"open(r'{temp_file}', 'w').write('a')\n"
            f"open(r'{ws_file}', 'w').write('b')"
        )
        result, reason = approval_review._static_review(code)
        assert result == "pass"

    def test_subprocess_still_blocked_even_with_whitelist(self):
        """即使有白名单路径，subprocess 仍被拦截"""
        project_root = Path(__file__).resolve().parents[1]
        temp_file = project_root / "temp" / "a.txt"
        code = (
            f"open(r'{temp_file}', 'w').write('a')\n"
            f"import subprocess\nsubprocess.run(['ls'])"
        )
        result, reason = approval_review._static_review(code)
        assert result == "block"
        assert "subprocess" in reason


# ========== D7-A: LLM 重试测试 ==========

class TestLLMRetry:
    """测试 D7-A：LLM 返回空时重试 1 次"""

    def test_llm_empty_then_retry_success(self, monkeypatch):
        """LLM 首次返回空 → 重试成功 → 正常解析"""
        self._patch_config(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        call_count = [0]
        call_results = [
            None,  # 首次空
            "DECISION: APPROVE\nREASON: 纯读操作",  # 重试成功
        ]

        def _fake_call_llm_simple(prompt, system_prompt, **kwargs):
            idx = min(call_count[0], len(call_results) - 1)
            call_count[0] += 1
            return call_results[idx]

        # patch server.llm_pool 模块级别（_llm_review 内部 from ... import 会取到）
        import server.llm_pool
        monkeypatch.setattr(server.llm_pool, "call_llm_simple", _fake_call_llm_simple)
        monkeypatch.setattr(server.llm_pool, "is_initialized", lambda: True)
        # 禁用 sleep 避免测试慢
        monkeypatch.setattr("time.sleep", lambda x: None)

        result = approval_review._llm_review(
            "exec_python", {"code": "x = 1"}, "POST", "/exec/python"
        )
        assert result["decision"] == "approve"
        assert call_count[0] == 2  # 调用 2 次

    def test_llm_empty_retry_also_empty(self, monkeypatch):
        """LLM 首次空 + 重试也空 → unavailable"""
        self._patch_config(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        call_count = [0]

        def _fake_call_llm_simple(prompt, system_prompt, **kwargs):
            call_count[0] += 1
            return None  # 每次都返回空

        import server.llm_pool
        monkeypatch.setattr(server.llm_pool, "call_llm_simple", _fake_call_llm_simple)
        monkeypatch.setattr(server.llm_pool, "is_initialized", lambda: True)
        monkeypatch.setattr("time.sleep", lambda x: None)

        result = approval_review._llm_review(
            "exec_python", {"code": "x = 1"}, "POST", "/exec/python"
        )
        assert result["decision"] == "unavailable"
        assert "重试后仍空" in result["reason"]
        assert call_count[0] == 2  # 调用 2 次

    def test_llm_non_empty_no_retry(self, monkeypatch):
        """LLM 首次非空 → 不重试"""
        self._patch_config(monkeypatch)
        monkeypatch.setattr(approval_review, "_llm_cooldown", False)

        call_count = [0]

        def _fake_call_llm_simple(prompt, system_prompt, **kwargs):
            call_count[0] += 1
            return "DECISION: DENY\nREASON: 危险操作"

        import server.llm_pool
        monkeypatch.setattr(server.llm_pool, "call_llm_simple", _fake_call_llm_simple)
        monkeypatch.setattr(server.llm_pool, "is_initialized", lambda: True)
        monkeypatch.setattr("time.sleep", lambda x: None)

        result = approval_review._llm_review(
            "exec_python", {"code": "x = 1"}, "POST", "/exec/python"
        )
        assert result["decision"] == "deny"
        assert call_count[0] == 1  # 只调用 1 次

    def _patch_config(self, monkeypatch):
        """mock 配置"""
        monkeypatch.setattr(
            approval_review, "get_command_guard_config",
            lambda: {"llm_review_enabled": True, "llm_review_endpoints": list(approval_review._REVIEWABLE_OPS)}
        )
        monkeypatch.setattr(
            approval_review, "get_cleanup_config",
            lambda: {
                "approvals_log_max_bytes": 10 * 1024 * 1024,
                "approvals_log_backup_count": 3,
                "approval_audit_log_max_bytes": 10 * 1024 * 1024,
                "approval_audit_log_backup_count": 3,
            }
        )
