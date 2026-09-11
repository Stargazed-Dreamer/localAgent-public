"""session_store.py — 文件分类工具的会话存储（ticket E：B2 会话机制）

每个会话 = 一个源文件夹的分类进度，落盘为 sessions/<session_id>.json（每会话一文件）。

落盘时机（已确认决策，严格限定三种）：
1. 用户手动触发（Ctrl+S / "保存会话"按钮）
2. 关闭会话（删除确认流程本身不落盘——删除即移除文件）
3. 应用窗口关闭（closeEvent）时静默保存所有活动会话

session_id 由 source_dir 派生（sanitize 目录名 + abspath 短哈希），
同一源目录恒映射同一会话文件，避免重复会话。
"""
import hashlib
import json
import os
import re
from datetime import datetime

# 会话目录：工具目录下的 sessions/
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")


class SessionState:
    """单个会话的完整状态（内存对象，落盘为 JSON）"""

    def __init__(self, session_id: str = "", source_dir: str = ""):
        self.session_id = session_id
        self.source_dir = source_dir          # 会话根源目录
        self.categories = []                  # 分类配置快照 [{"name","path","extensions"}]
        self.current_categories = {}          # filename -> category_name（手动分类）
        self.predictions = {}                 # filename -> {"category","confidence",...}
        self.skipped = set()                  # 跳过的文件名
        self.reviewed = set()                 # 已审文件名（仅 R2 通过产生）
        self.column_order = []                # ticket I/Q12：每会话独立列序
        self.sort_directions = {}             # logical 列号(str) -> bool 升序
        self.sort_active = False              # 排序是否已激活
        self.updated_at = ""                  # 最近保存时间

    @property
    def title(self) -> str:
        """标签页标题：源目录名；无源目录时为新会话"""
        if self.source_dir:
            return os.path.basename(os.path.normpath(self.source_dir)) or self.source_dir
        return "新会话"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "source_dir": self.source_dir,
            "categories": self.categories,
            "current_categories": self.current_categories,
            "predictions": self.predictions,
            "skipped": sorted(self.skipped),
            "reviewed": sorted(self.reviewed),
            "column_order": self.column_order,
            "sort_directions": self.sort_directions,
            "sort_active": self.sort_active,
            "updated_at": datetime.now().isoformat(),
        }

    @staticmethod
    def from_dict(data: dict) -> "SessionState":
        s = SessionState(
            session_id=data.get("session_id", ""),
            source_dir=data.get("source_dir", ""),
        )
        s.categories = data.get("categories", [])
        s.current_categories = data.get("current_categories", {})
        s.predictions = data.get("predictions", {})
        s.skipped = set(data.get("skipped", []))
        s.reviewed = set(data.get("reviewed", []))
        s.column_order = data.get("column_order", [])
        s.sort_directions = data.get("sort_directions", {})
        s.sort_active = bool(data.get("sort_active", False))
        s.updated_at = data.get("updated_at", "")
        return s


def derive_session_id(source_dir: str) -> str:
    """由源目录派生会话 ID：sanitize(目录名) + abspath 短哈希

    同一源目录（不区分大小写/末尾分隔符）恒得到同一 ID。
    """
    abspath = os.path.normcase(os.path.abspath(source_dir))
    digest = hashlib.md5(abspath.encode("utf-8")).hexdigest()[:8]
    # normcase 后再取目录名：Windows 下大小写不同的同一路径得到同一 ID
    base = os.path.basename(os.path.normpath(os.path.normcase(source_dir))) or "root"
    # 仅保留安全字符，避免路径问题
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", base).strip("_") or "root"
    return f"{safe}_{digest}"


def session_file_path(session_id: str) -> str:
    """会话文件的绝对路径"""
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


def save_session(state: SessionState) -> str:
    """保存会话到 sessions/<id>.json，返回文件路径"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    if not state.session_id and state.source_dir:
        state.session_id = derive_session_id(state.source_dir)
    path = session_file_path(state.session_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load_session(path: str) -> SessionState:
    """从文件加载会话"""
    with open(path, encoding="utf-8") as f:
        return SessionState.from_dict(json.load(f))


def list_sessions() -> list:
    """列出所有会话文件，按 updated_at 倒序（最近保存的在前）

    Returns:
        [(path, SessionState)] 列表；损坏文件跳过
    """
    if not os.path.isdir(SESSIONS_DIR):
        return []
    result = []
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            result.append((path, load_session(path)))
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    result.sort(key=lambda p: p[1].updated_at, reverse=True)
    return result


def delete_session(session_id: str) -> bool:
    """删除会话文件（仅删除保存的进度与状态，不影响已移动的文件）"""
    path = session_file_path(session_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False
