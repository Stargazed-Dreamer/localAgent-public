"""E9 · artifact 文件权限 0o600（D-spec：artifact 文件权限安全）

TDD 红测试：当前 l0_artifact_store.py 创建 artifact 文件时不设置权限，
默认 umask 可能给出 0o644（world-readable）。本测试断言：
- artifact 文件权限 = 0o600（owner read/write only）
- session 目录权限 = 0o700（owner only）

跨平台策略：
- Mock-based 测试（Win + Linux）：断言 os.chmod 被调用且 mode 正确
- Stat-based 测试（仅 Linux）：断言真实 stat mode 正确（Windows stat 不能反映 Unix 权限）
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.core.agent.l0_artifact_store import L0ArtifactStore  # noqa: E402

# ============================================================================
# Mock-based 测试（跨平台，Win + Linux 都跑）
# ============================================================================


class TestE9ArtifactPermissionMock:
    """Mock-based 验证 os.chmod 被调用且 mode 正确。"""

    def test_artifact_file_chmod_to_600(self, tmp_path):
        """artifact 文件创建后调 os.chmod(path, 0o600)。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "x" * (8 * 1024 + 100)  # > 8KB 触发落盘

        with patch("os.chmod") as mock_chmod:
            new_content, path = store.maybe_persist(
                session_id="sess_e9_1",
                tool_call_id="call_e9_1",
                content=large_content,
            )

        assert path is not None
        # 找到调用 mode=0o600 的 chmod 调用
        modes_called = [call.args[1] for call in mock_chmod.call_args_list]
        modes_oct = [oct(m) for m in modes_called]
        assert 0o600 in modes_called, f"Expected 0o600 in chmod modes, got {modes_oct}"

    def test_session_dir_chmod_to_700(self, tmp_path):
        """session 目录创建后调 os.chmod(dir, 0o700)。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "x" * (8 * 1024 + 100)

        with patch("os.chmod") as mock_chmod:
            store.maybe_persist(
                session_id="sess_e9_2",
                tool_call_id="call_e9_2",
                content=large_content,
            )

        modes_called = [call.args[1] for call in mock_chmod.call_args_list]
        modes_oct = [oct(m) for m in modes_called]
        assert 0o700 in modes_called, f"Expected 0o700 in chmod modes, got {modes_oct}"

    def test_chmod_called_after_write(self, tmp_path):
        """chmod 必须在 write_text 之后调用（否则文件被覆盖时 mode 可能被重置）。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "x" * (8 * 1024 + 100)

        # 记录 write_text 和 chmod 的调用顺序
        call_order: list[str] = []
        original_write_text = Path.write_text

        def spy_write_text(self_path, *args, **kwargs):
            call_order.append(f"write_text:{self_path}")
            return original_write_text(self_path, *args, **kwargs)

        with patch("os.chmod") as mock_chmod:
            mock_chmod.side_effect = lambda p, m: call_order.append(f"chmod:{p}:{oct(m)}")
            with patch.object(Path, "write_text", spy_write_text):
                store.maybe_persist(
                    session_id="sess_e9_3",
                    tool_call_id="call_e9_3",
                    content=large_content,
                )

        # 找到 write_text 和 chmod 的相对顺序
        write_idx = next((i for i, s in enumerate(call_order) if s.startswith("write_text:")), None)
        chmod_600_idx = next(
            (i for i, s in enumerate(call_order) if "chmod:" in s and "0o600" in s),
            None,
        )
        assert write_idx is not None, "write_text was not called"
        assert chmod_600_idx is not None, "os.chmod with 0o600 was not called"
        assert chmod_600_idx > write_idx, (
            f"chmod 0o600 must be called AFTER write_text, "
            f"got write_idx={write_idx}, chmod_idx={chmod_600_idx}, order={call_order}"
        )

    def test_no_chmod_when_below_threshold(self, tmp_path):
        """content <= 8KB 时不落盘，也不应调 chmod。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        small_content = "small"

        with patch("os.chmod") as mock_chmod:
            new_content, path = store.maybe_persist(
                session_id="sess_e9_4",
                tool_call_id="call_e9_4",
                content=small_content,
            )

        assert path is None
        assert new_content == small_content
        mock_chmod.assert_not_called()

    def test_chmod_called_on_existing_dir_reuse(self, tmp_path):
        """session 目录已存在时（多次落盘同 session），仍应对新文件调 chmod 0o600。

        注：对已存在的目录不再 chmod 0o700（幂等），但对新文件必须 chmod。
        """
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content_1 = "a" * (8 * 1024 + 100)
        large_content_2 = "b" * (8 * 1024 + 200)

        # 第一次：创建目录 + 文件
        with patch("os.chmod") as mock_chmod_1:
            store.maybe_persist(
                session_id="sess_e9_5",
                tool_call_id="call_e9_5a",
                content=large_content_1,
            )
        first_call_count = mock_chmod_1.call_count
        assert first_call_count >= 2, f"First persist should chmod dir+file, got {first_call_count}"

        # 第二次：目录已存在，但新文件仍需 chmod
        with patch("os.chmod") as mock_chmod_2:
            store.maybe_persist(
                session_id="sess_e9_5",  # 同 session
                tool_call_id="call_e9_5b",  # 不同 tool_call
                content=large_content_2,
            )
        # 第二次必须 chmod 文件 0o600
        modes_called = [call.args[1] for call in mock_chmod_2.call_args_list]
        modes_oct = [oct(m) for m in modes_called]
        assert 0o600 in modes_called, (
            f"Second persist on same session must still chmod new file 0o600, "
            f"got modes={modes_oct}"
        )


# ============================================================================
# Stat-based 测试（仅 Linux，Windows stat 不反映 Unix 权限）
# ============================================================================


@pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions only verifiable on Linux")
class TestE9ArtifactPermissionStat:
    """Linux 实际 stat mode 验证。"""

    def test_actual_file_mode_600(self, tmp_path):
        """artifact 文件实际 stat mode == 0o600。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "x" * (8 * 1024 + 100)

        _, path = store.maybe_persist(
            session_id="sess_e9_stat_1",
            tool_call_id="call_e9_stat_1",
            content=large_content,
        )

        assert path is not None
        st = os.stat(path)
        actual_mode = stat.S_IMODE(st.st_mode)
        assert actual_mode == 0o600, f"Expected 0o600, got {oct(actual_mode)}"

    def test_actual_dir_mode_700(self, tmp_path):
        """session 目录实际 stat mode == 0o700。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "x" * (8 * 1024 + 100)

        _, path = store.maybe_persist(
            session_id="sess_e9_stat_2",
            tool_call_id="call_e9_stat_2",
            content=large_content,
        )

        session_dir = Path(path).parent
        st = os.stat(session_dir)
        actual_mode = stat.S_IMODE(st.st_mode)
        assert actual_mode == 0o700, f"Expected 0o700, got {oct(actual_mode)}"


# ============================================================================
# 回归测试：现有行为不破坏
# ============================================================================


class TestE9Regression:
    """回归：现有 maybe_persist 行为不破坏。"""

    def test_below_threshold_returns_original_content(self, tmp_path):
        """小 content 原样返回，不落盘。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        new_content, path = store.maybe_persist(
            session_id="sess_regress",
            tool_call_id="call_regress",
            content="small content",
        )
        assert path is None
        assert new_content == "small content"

    def test_above_threshold_returns_preview_plus_path(self, tmp_path):
        """大 content 返回 preview + path 提示。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "abcdefghij" * 1000  # 10KB
        new_content, path = store.maybe_persist(
            session_id="sess_regress",
            tool_call_id="call_regress",
            content=large_content,
        )
        assert path is not None
        assert "[artifact_saved_at:" in new_content
        assert Path(path).exists()

    def test_load_returns_persisted_content(self, tmp_path):
        """load 读取已落盘内容。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path))
        large_content = "xyz" * 4000  # 12KB
        _, path = store.maybe_persist(
            session_id="sess_regress",
            tool_call_id="call_regress",
            content=large_content,
        )
        loaded = store.load(path)
        assert loaded == large_content

    def test_failopen_on_write_error(self, tmp_path):
        """write 失败时 fail-open 返回原 content。"""
        store = L0ArtifactStore(artifacts_dir=str(tmp_path / "nonexistent" / "deep"))
        # artifacts_dir 本身可创建，但若 session_dir 创建失败会 fail-open
        large_content = "x" * (8 * 1024 + 100)
        # 把 artifacts_dir 设到一个不可写路径模拟失败（用文件当目录）
        invalid_dir = tmp_path / "blocker_file"
        invalid_dir.write_text("i am a file, not a dir")
        store._artifacts_dir = str(invalid_dir)  # 强制设到文件路径触发 mkdir 失败

        new_content, path = store.maybe_persist(
            session_id="sess_regress",
            tool_call_id="call_regress",
            content=large_content,
        )
        # fail-open：返回原 content，path=None
        assert path is None
        assert new_content == large_content
