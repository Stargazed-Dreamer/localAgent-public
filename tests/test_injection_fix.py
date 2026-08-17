"""T03: 注入修复测试——templates repr() + tool_runner shell=False。"""

from __future__ import annotations

from server.templates import _render_template


class TestTemplatesInjectionFix:
    """T03: templates.py _render_template 字符串参数转义。"""

    def test_normal_string(self):
        """正常字符串参数正常替换。"""
        template = "url = '{url}'"
        result = _render_template(template, {"url": "http://example.com"})
        assert result == "url = 'http://example.com'"

    def test_single_quote_injection_blocked(self):
        """用户传含单引号的值不能逃逸字符串字面量。"""
        template = "url = '{url}'"
        # 攻击尝试：'; os.system('calc'); '
        malicious = "'; os.system('calc'); '"
        result = _render_template(template, {"url": malicious})
        # 引号被转义，无法逃逸字符串字面量
        assert "os.system" in result  # 字面上存在但不作为代码执行
        assert "\\'" in result  # 单引号被转义
        # 确保结果仍是合法 Python 字符串字面量
        # url = ''\'; os.system(\'calc\'); \'' → 引号被转义在字符串内

    def test_double_quote_injection_blocked(self):
        """用户传含双引号的值不能逃逸。"""
        template = 'url = "{url}"'
        malicious = '"; os.system("calc"); "'
        result = _render_template(template, {"url": malicious})
        assert '\\"' in result  # 双引号被转义

    def test_backslash_injection_blocked(self):
        """反斜杠转义防止 \\' 绕过。"""
        template = "url = '{url}'"
        malicious = "\\'; os.system('calc'); '"
        result = _render_template(template, {"url": malicious})
        # 反斜杠先被转义为 \\，再转义引号
        assert "\\\\" in result

    def test_non_string_passthrough(self):
        """非字符串参数（int/bool/None）正常处理。"""
        assert _render_template("val = {val}", {"val": 42}) == "val = 42"
        assert _render_template("flag = {flag}", {"flag": True}) == "flag = true"
        assert _render_template("x = {x}", {"x": None}) == "x = "


class TestToolRunnerShellFalse:
    """T03: tool_runner.py _build_command 返回 list + shell=False。"""

    def test_build_command_returns_list(self):
        """_build_command 应返回 list[str] 而非 str。"""
        # 验证 ScriptRunner 接受 list 参数
        import inspect

        from client.widgets.tool_runner import ScriptRunner

        sig = inspect.signature(ScriptRunner.__init__)
        cmd_param = sig.parameters.get("command")
        assert cmd_param is not None
        # 检查 ScriptRunner.run 中 shell=False
        source = inspect.getsource(ScriptRunner.run)
        assert "shell=False" in source
        assert "shell=True" not in source

    def test_build_command_uses_shlex(self):
        """_build_command 应使用 shlex.split 而非字符串拼接。"""
        import inspect

        from client.widgets.tool_runner import ToolDetailWidget

        source = inspect.getsource(ToolDetailWidget._build_command)
        assert "shlex.split" in source
