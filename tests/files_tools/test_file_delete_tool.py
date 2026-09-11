"""file_delete 工具测试 — T05 回收站 + dry_run + 审批

覆盖：
- dry_run 默认 True：返回将删清单不真删
- dry_run=False：走 SHFileOperationW 回收站（mock 验证调用）
- 反向验证：dry_run=True 时文件仍存在；dry_run=False 时文件不直接 unlink
- 安全网：autouse fixture mock SHFileOperationW，防止任何测试真删文件

测试原则（spec Anti-Cheat）：
- 不 mock 被测对象（FileDeleteTool.execute 不 mock，只 mock _delete_to_recycle_bin）
- 反向验证：dry_run=True 文件仍存在；dry_run=False 走回收站而非 Path.unlink()
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from client.core.agent.builtin_tools import file_delete as fd_module
from client.core.agent.builtin_tools.file_delete import FileDeleteTool
from client.core.agent.types import ToolCall


@pytest.fixture(autouse=True)
def _safety_mock_recycle_bin(monkeypatch):
    """安全网（autouse）：默认 mock _delete_to_recycle_bin，防止任何测试真删文件。

    本 fixture 是机制级防护——即便某个测试忘了传 dry_run=True 或忘了 mock，
    也不会真调 SHFileOperationW 把文件删到回收站。

    需要验证回收站调用的测试，自行 monkeypatch 覆盖（后注册的覆盖本 fixture）。
    """
    def _fake_delete(file_path: str) -> tuple[bool, str | None]:
        # 默认成功，但不真删（测试自行验证调用参数）
        return True, None
    monkeypatch.setattr(fd_module, "_delete_to_recycle_bin", _fake_delete)


@pytest.fixture(autouse=True)
def _mock_is_path_safe(monkeypatch, tmp_path):
    """让 is_path_safe 接受 tmp_path 下的文件（默认只允许 PROJECT_ROOT）。

    这样测试可以用 tmp_path 创建临时文件，不被 is_path_safe 拒绝。
    """
    def _fake_is_safe(file_path: str, allowed_roots=None):
        # 只校验文件在 tmp_path 下，跳过其他 4 重校验（测试用文件无遍历风险）
        try:
            p = Path(file_path).resolve()
            p.relative_to(tmp_path.resolve())
            return True, "ok"
        except (ValueError, OSError):
            return False, "outside tmp_path"
    monkeypatch.setattr(fd_module, "is_path_safe", _fake_is_safe)


def _make_tool_call(file_paths, dry_run=None) -> ToolCall:
    """构造测试用 ToolCall"""
    args = {"file_paths": file_paths}
    if dry_run is not None:
        args["dry_run"] = dry_run
    return ToolCall(id="test_call", name="file_delete", args=args)


def _run_async(coro):
    """同步运行 async 测试"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ========== T05: dry_run 默认 True ==========

class TestDryRunDefault:
    def test_dry_run_default_returns_preview_not_delete(self, tmp_path):
        """T05：不传 dry_run 时默认 True，返回清单不真删（反向验证：文件仍存在）"""
        # 准备：创建临时文件
        f1 = tmp_path / "file1.txt"
        f1.write_text("content1", encoding="utf-8")
        f2 = tmp_path / "file2.txt"
        f2.write_text("content2", encoding="utf-8")

        # 执行：不传 dry_run（默认 True）
        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(f1), str(f2)])))

        # 验证：返回 DRY RUN 清单
        assert result.variant.value == "success" or result.variant.value == "SUCCESS" or "DRY RUN" in result.content
        assert "Would delete 2 file(s)" in result.content
        assert str(f1) in result.content
        assert str(f2) in result.content
        assert "No files were deleted" in result.content
        # 反向验证：文件仍存在（未真删）
        assert f1.exists(), "dry_run=True 不应真删文件1"
        assert f2.exists(), "dry_run=True 不应真删文件2"

    def test_dry_run_explicit_true_returns_preview(self, tmp_path):
        """T05：显式传 dry_run=True，返回清单不删"""
        f1 = tmp_path / "explicit.txt"
        f1.write_text("data", encoding="utf-8")

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(f1)], dry_run=True)))

        assert "DRY RUN" in result.content
        assert "Would delete 1 file(s)" in result.content
        assert f1.exists(), "dry_run=True 不应真删"

    def test_dry_run_default_does_not_call_recycle_bin(self, tmp_path, monkeypatch):
        """T05：dry_run=True 时 _delete_to_recycle_bin 不被调用"""
        calls = []
        def _spy_delete(file_path):
            calls.append(file_path)
            return True, None
        monkeypatch.setattr(fd_module, "_delete_to_recycle_bin", _spy_delete)

        f1 = tmp_path / "no_call.txt"
        f1.write_text("x", encoding="utf-8")

        tool = FileDeleteTool()
        _run_async(tool.execute(_make_tool_call([str(f1)], dry_run=True)))

        assert len(calls) == 0, "dry_run=True 不应调用回收站 API"


# ========== T05: 实际删除走回收站 ==========

class TestActualDeleteViaRecycleBin:
    def test_dry_run_false_calls_recycle_bin_not_unlink(self, tmp_path, monkeypatch):
        """T05：dry_run=False 时调 _delete_to_recycle_bin，不调 Path.unlink()"""
        recycle_calls = []
        unlink_calls = []

        def _spy_delete(file_path):
            recycle_calls.append(file_path)
            return True, None
        monkeypatch.setattr(fd_module, "_delete_to_recycle_bin", _spy_delete)

        # 拦截 Path.unlink 验证不被调用
        def _spy_unlink(self, *args, **kwargs):
            unlink_calls.append(str(self))
            # 不真删，返回 None 模拟成功
            return None
        monkeypatch.setattr(Path, "unlink", _spy_unlink)

        f1 = tmp_path / "to_delete.txt"
        f1.write_text("data", encoding="utf-8")

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(f1)], dry_run=False)))

        assert "Deleted 1 file(s) to recycle bin" in result.content
        assert len(recycle_calls) == 1, "dry_run=False 应调用回收站 API"
        assert recycle_calls[0] == str(f1)
        # 反向验证：不调 Path.unlink（旧实现会调）
        assert len(unlink_calls) == 0, "不应调用 Path.unlink（应走回收站 API）"

    def test_dry_run_false_multiple_files_all_via_recycle_bin(self, tmp_path, monkeypatch):
        """T05：多文件删除全部走回收站"""
        recycle_calls = []
        def _spy_delete(file_path):
            recycle_calls.append(file_path)
            return True, None
        monkeypatch.setattr(fd_module, "_delete_to_recycle_bin", _spy_delete)

        files = []
        for i in range(3):
            f = tmp_path / f"multi_{i}.txt"
            f.write_text(f"content{i}", encoding="utf-8")
            files.append(str(f))

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call(files, dry_run=False)))

        assert "Deleted 3 file(s) to recycle bin" in result.content
        assert len(recycle_calls) == 3
        for fp in files:
            assert fp in recycle_calls

    def test_recycle_bin_failure_returns_error(self, tmp_path, monkeypatch):
        """T05：回收站 API 失败时返回 error，文件不被 unlink"""
        def _fail_delete(file_path):
            return False, "SHFileOperationW error code: 2"
        monkeypatch.setattr(fd_module, "_delete_to_recycle_bin", _fail_delete)

        unlink_calls = []
        def _spy_unlink(self, *args, **kwargs):
            unlink_calls.append(str(self))
            return None
        monkeypatch.setattr(Path, "unlink", _spy_unlink)

        f1 = tmp_path / "fail.txt"
        f1.write_text("data", encoding="utf-8")

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(f1)], dry_run=False)))

        # 应返回 error variant（无成功删除 + 有错误）
        assert "Errors:" in result.content
        assert "SHFileOperationW error code: 2" in result.content
        # 反向验证：失败时也不调 unlink（不绕过回收站）
        assert len(unlink_calls) == 0, "回收站失败时不应 fallback 到 unlink"


# ========== T05: 反向验证（spec Anti-Cheat 要求） ==========

class TestReverseVerification:
    def test_dry_run_true_file_still_exists_after_call(self, tmp_path):
        """反向验证：dry_run=True 调用后文件仍存在（未进回收站/未 unlink）"""
        f1 = tmp_path / "reverse.txt"
        f1.write_text("important data", encoding="utf-8")
        original_content = f1.read_text(encoding="utf-8")

        tool = FileDeleteTool()
        _run_async(tool.execute(_make_tool_call([str(f1)], dry_run=True)))

        # 文件存在 + 内容不变
        assert f1.exists(), "dry_run=True 后文件应仍存在"
        assert f1.read_text(encoding="utf-8") == original_content, "文件内容不应改变"

    def test_dry_run_false_file_removed_via_recycle_bin(self, tmp_path, monkeypatch):
        """反向验证：dry_run=False 时文件被删（mock 回收站成功），且通过回收站 API 而非 unlink"""
        f1 = tmp_path / "real_delete.txt"
        f1.write_text("data", encoding="utf-8")

        # mock 回收站成功删除（模拟 SHFileOperationW 行为：文件被移到回收站）
        def _mock_recycle_delete(file_path):
            # 模拟回收站行为：文件从原位置消失（移到回收站）
            Path(file_path).unlink()
            return True, None
        monkeypatch.setattr(fd_module, "_delete_to_recycle_bin", _mock_recycle_delete)

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(f1)], dry_run=False)))

        assert "Deleted 1 file(s) to recycle bin" in result.content
        # 文件从原位置消失（被移到回收站）
        assert not f1.exists(), "dry_run=False 后文件应被移到回收站（原位置消失）"


# ========== T05: 边界情况 ==========

class TestEdgeCases:
    def test_empty_file_paths_returns_error(self):
        """T05：空 file_paths 返回 invalid_argument"""
        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([])))

        assert "file_paths is required" in result.content

    def test_not_found_file_in_errors(self, tmp_path):
        """T05：不存在的文件记入 errors"""
        not_exist = tmp_path / "not_exist.txt"

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(not_exist)], dry_run=True)))

        assert "not_found" in result.content

    def test_directory_rejected(self, tmp_path):
        """T05：目录被拒绝（应用 rm 命令而非 file_delete）"""
        d1 = tmp_path / "subdir"
        d1.mkdir()

        tool = FileDeleteTool()
        result = _run_async(tool.execute(_make_tool_call([str(d1)], dry_run=False)))

        assert "is_directory" in result.content

    def test_string_path_normalized_to_list(self, tmp_path):
        """T05：传字符串路径自动转 list"""
        f1 = tmp_path / "string_path.txt"
        f1.write_text("data", encoding="utf-8")

        tool = FileDeleteTool()
        # 直接传字符串而非 list
        result = _run_async(tool.execute(ToolCall(
            id="test", name="file_delete",
            args={"file_paths": str(f1), "dry_run": True},
        )))

        assert "Would delete 1 file(s)" in result.content
        assert f1.exists(), "dry_run=True 不应真删"
