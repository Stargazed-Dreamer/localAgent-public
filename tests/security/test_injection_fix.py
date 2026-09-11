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
    """T03: tool_runner.py _build_command 返回 list + shell=False。

    行为化改写（原为 inspect.getsource 字符串 grep，重构即误报）：
    - 捕获 Popen 实参验证 shell=False + list 传参
    - 真实调用 _build_command 验证 shlex.split(posix=False) 保住 Windows 反斜杠路径
    """

    def test_popen_receives_list_and_shell_false(self, monkeypatch):
        """ScriptRunner.run 走 Popen(list, shell=False)（行为捕获）。"""
        from pathlib import Path

        from client.widgets import tool_runner
        from client.widgets.tool_runner import ScriptRunner

        captured: dict = {}

        class FakeProc:
            def __init__(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                self.stdout = iter([])  # 空 stdout，run() 读完即返回

            def poll(self):
                return 0

        monkeypatch.setattr(tool_runner.subprocess, "Popen", FakeProc)

        runner = ScriptRunner(command=["python", "-c", "print(1)"], cwd=Path("."))
        runner.run()  # 直接调用方法体（不启线程），Popen 已被捕获

        args, kwargs = captured["args"], captured["kwargs"]
        assert args == (["python", "-c", "print(1)"],), f"Popen 应收到 list 参数，实际: {args!r}"
        assert kwargs.get("shell") is False, "Popen 必须 shell=False（防注入）"

    def test_build_command_splits_windows_path(self):
        """_build_command 用 shlex.split(posix=False)：反斜杠路径不被当转义吞掉。"""
        from client.widgets.tool_runner import ToolDetailWidget

        w = ToolDetailWidget.__new__(ToolDetailWidget)  # 绕过 Qt __init__，只测纯逻辑方法
        w.tool = {"command": r".venv\Scripts\python.exe -c print(1)"}
        w.option_widgets = {}

        cmd = w._build_command()

        assert isinstance(cmd, list)
        assert cmd[0] == r".venv\Scripts\python.exe", (
            f"posix=False 应保住反斜杠路径，实际: {cmd!r}"
        )
