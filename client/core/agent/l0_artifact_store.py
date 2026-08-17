"""v6-lite T08: L0 大工具结果落盘（v6-lite §3 W5）

设计依据：
- v6-lite §3 W5：L0 单条工具结果 >8 KiB 落盘 `data/client/artifacts/`，模型只看 preview+path
- v6-lite §4.5：error-as-output-variant——工具错误是结构化输出
- v6-06 §6（参考）：大结果不进 LLM 上下文，落盘后给 path + preview

职责：
1. 接收 ToolResult.content，若 > THRESHOLD_BYTES 则落盘到 artifacts 目录
2. 返回新的 content：preview（前 N 字符）+ artifact_path 提示
3. 路径规则：`data/client/artifacts/<session_id>/<tool_call_id>.txt`
4. 幂等：同 tool_call_id 多次保存覆盖
5. 不抛异常：落盘失败时返回原 content（fail-open，模型看完整内容）
6. E9：文件权限 0o600 / 目录权限 0o700（owner-only，防其他用户读取敏感工具结果）

不依赖 Qt（纯 Python，可在 CLI 脚本和 GUI 中复用）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("localagent.agent.l0_artifact_store")

# v6-lite §3 W5 阈值：8 KiB
THRESHOLD_BYTES = 8 * 1024

# preview 长度（字符数，约 1-2 KiB）
PREVIEW_CHARS = 800

# E9：artifact 文件权限 owner-only（read/write for owner, nothing for group/other）
# Linux/macOS：生效；Windows：os.chmod 仅切 read-only flag，权限由 ACL 控制（不影响）
ARTIFACT_FILE_MODE = 0o600

# E9：session 目录权限 owner-only（rwx for owner, nothing for group/other）
ARTIFACT_DIR_MODE = 0o700

# 项目根目录（agent.db 在 data/client/，artifacts 同级）
_DEFAULT_ARTIFACTS_DIR = str(
    Path(__file__).resolve().parents[3] / "data" / "client" / "artifacts"
)


class L0ArtifactStore:
    """L0 大工具结果落盘。

    用法：
        store = L0ArtifactStore()  # 用默认目录 data/client/artifacts/
        new_content = store.maybe_persist(
            session_id="sess-1",
            tool_call_id="call-abc",
            content=large_result_text,
        )
        # 若 content > 8KB，new_content 是 preview + artifact_path 提示
        # 否则 new_content == content 原样返回
    """

    def __init__(self, artifacts_dir: str | None = None):
        self._artifacts_dir = artifacts_dir or _DEFAULT_ARTIFACTS_DIR

    def maybe_persist(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        content: str,
    ) -> tuple[str, str | None]:
        """若 content 超阈值则落盘，返回 (new_content, artifact_path)。

        - content <= THRESHOLD_BYTES：返回 (content, None)，不落盘
        - content > THRESHOLD_BYTES：
            - 落盘到 `<artifacts_dir>/<session_id>/<tool_call_id>.txt`
            - 返回 (preview + path 提示, artifact_path)
        - 落盘失败：返回 (content, None)（fail-open，模型看完整内容）

        artifact_path 是绝对路径，便于后续读取/调试。
        """
        if not isinstance(content, str) or len(content.encode("utf-8")) <= THRESHOLD_BYTES:
            return content, None

        # 构造路径：<artifacts_dir>/<session_id>/<tool_call_id>.txt
        safe_session = _sanitize_path_component(session_id) or "unknown_session"
        safe_tc_id = _sanitize_path_component(tool_call_id) or "unknown_tc"
        session_dir = Path(self._artifacts_dir) / safe_session
        artifact_path = session_dir / f"{safe_tc_id}.txt"

        try:
            # E9：目录权限 0o700（owner-only）。仅对新创建的目录 chmod（幂等）。
            # mkdir(exist_ok=True) 不能区分新建 vs 已存在，用 exists() 判断避免重复 chmod。
            dir_newly_created = not session_dir.exists()
            session_dir.mkdir(parents=True, exist_ok=True)
            if dir_newly_created:
                try:
                    os.chmod(session_dir, ARTIFACT_DIR_MODE)
                except OSError as chmod_err:
                    # chmod 失败不阻断（Windows 可能受限），warning 即可
                    logger.warning(
                        "L0 artifact dir chmod failed (non-fatal): %s", chmod_err,
                    )

            artifact_path.write_text(content, encoding="utf-8")

            # E9：文件权限 0o600（owner-only）。必须在 write_text 之后调用，
            # 否则 write_text 创建文件时受 umask 影响，且某些场景下会重置 mode。
            try:
                os.chmod(artifact_path, ARTIFACT_FILE_MODE)
            except OSError as chmod_err:
                logger.warning(
                    "L0 artifact file chmod failed (non-fatal): %s", chmod_err,
                )

            logger.info(
                "L0 artifact saved: %s (%d bytes, session=%s, tool_call=%s)",
                artifact_path, len(content.encode("utf-8")), session_id, tool_call_id,
            )
        except Exception as e:
            logger.warning(
                "L0 artifact save failed (fail-open, returning full content): %s", e,
            )
            return content, None

        # 构造 preview + path 提示
        preview = content[:PREVIEW_CHARS]
        if len(content) > PREVIEW_CHARS:
            preview += f"\n\n... (truncated, total {len(content)} chars) ..."
        new_content = (
            f"{preview}\n\n"
            f"[artifact_saved_at: {artifact_path}]"
        )
        return new_content, str(artifact_path)

    def load(self, artifact_path: str) -> str | None:
        """读取落盘的完整内容（调试/审计用）。失败返回 None。"""
        try:
            return Path(artifact_path).read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("L0 artifact load failed (%s): %s", artifact_path, e)
            return None


def _sanitize_path_component(name: str) -> str:
    """清理路径组件，防止目录穿越。

    保留字母/数字/下划线/连字符，其余替换为 _。
    """
    if not name:
        return ""
    safe = []
    for ch in name:
        if ch.isalnum() or ch in ("_", "-", "."):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe)[:64]  # 限长 64，防超长文件名
