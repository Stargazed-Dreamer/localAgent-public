"""入站 key 库（独立 JSON 文件，线程安全）

key 结构（spec "Implementation Decisions"）:
    {
        "key_id":       "inb_a1",              # 内部标识
        "secret":       "sk-la-<20hex>",       # 面板展示/复制用
        "name":         "cline-work",          # 连接名
        "project":      "cline",               # 透传给 pool 的 project 标签
        "enabled":      true,                  # 停用后 401
        "aliases":      {"gpt-4o": "deepseek-v4-chat"},
        "alias_fallback_tiers": {"gpt-4o": [2, 4]},  # 可选：别名的兜底 Tier 范围 [lo, hi]（1-5）。
                                                # 调用时作为 pool 的 tier 硬过滤透传——
                                                # 目标模型不可用时停留在该范围内兜底。
                                                # 无此字段/无该别名 = 不兜底（决策#2 默认）。
                                                # 刻意不进 _REQUIRED_FIELDS：存量 key 文件照旧可读。
        "created_at":   "2026-09-01T12:00:00",
        "last_used_at": "2026-09-01T23:18:42",  # 面板「最后使用」列
    }

存储: data/inbound_keys.json（data/* 已被 .gitignore 忽略，secret 不进 git）。

并发: 变更走 RLock + 立即落盘（管理操作低频）；last_used_at 只改内存 + dirty 标记，
由 flush() 周期落盘，避免每次请求都写盘。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KEY_PREFIX = "sk-la-"
SECRET_HEX_LEN = 20


def _resolve_against_project_root(p: Path | str) -> Path:
    """相对路径基于项目根目录解析（不依赖 cwd）；绝对路径原样返回（5-7）。

    与 server/llm_pool/key_store.py 同名模式一致；本模块在 server/inbound_gateway/
    下，3 级 parent 到项目根。cwd 不在项目根时相对路径解析会导致入站 key 库
    "消失"（fail-open 空库 → 全部 401）。
    """
    p = Path(p)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parent.parent.parent / p


DEFAULT_KEY_FILE = _resolve_against_project_root(Path("data") / "inbound_keys.json")

_REQUIRED_FIELDS = ("key_id", "secret", "name", "project", "enabled", "aliases")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_tier_ranges(tiers: dict[str, Any] | None) -> dict[str, list[int]]:
    """把每个别名的兜底值归一为 [lo, hi] 范围（1-5）。

    接受单档（int → [n, n]）或两元素 [lo, hi]；越界/顺序颠倒自动钳制排序，
    无法解释的值静默丢弃（面板侧已有下拉约束，此处兜底）。
    """
    out: dict[str, list[int]] = {}
    for alias, v in (tiers or {}).items():
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
            out[alias] = [v, v]
            continue
        if isinstance(v, (list, tuple)) and len(v) == 2:
            nums = [x for x in v if isinstance(x, int) and not isinstance(x, bool)]
            if len(nums) == 2:
                lo = min(max(nums[0], 1), 5)
                hi = min(max(nums[1], 1), 5)
                out[alias] = [min(lo, hi), max(lo, hi)]
    return out


class KeyNotFoundError(KeyError):
    """按 key_id 或 secret 查不到。"""


class KeyStore:
    """入站 key 库：线程安全的 JSON 读写 + 签发 + 校验。"""

    def __init__(self, path: Path | str = DEFAULT_KEY_FILE):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._keys: list[dict[str, Any]] = []
        self._last_used_dirty = False
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not self._path.exists():
            self._keys = []
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            keys = data.get("keys", []) if isinstance(data, dict) else data
            self._keys = [k for k in keys if isinstance(k, dict) and all(f in k for f in _REQUIRED_FIELDS)]
        except (json.JSONDecodeError, OSError) as e:
            logger.error("入站 key 库读取失败（fail-open，按空库启动）: %s", e)
            self._keys = []

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"keys": self._keys}
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # 原子替换，避免写一半损坏
        self._last_used_dirty = False

    # ---------- 查询 ----------

    def list_keys(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(k) for k in self._keys]

    def get_by_id(self, key_id: str) -> dict[str, Any]:
        with self._lock:
            for k in self._keys:
                if k["key_id"] == key_id:
                    return dict(k)
        raise KeyNotFoundError(key_id)

    def verify(self, secret: str) -> dict[str, Any] | None:
        """按 secret 校验。存在且 enabled → 返回 key 副本；否则 None（调用方统一 401，不区分两种失败）。"""
        if not secret or not secret.startswith(KEY_PREFIX):
            return None
        with self._lock:
            for k in self._keys:
                if k["secret"] == secret:
                    if not k["enabled"]:
                        return None
                    k["last_used_at"] = _now_iso()
                    self._last_used_dirty = True
                    return dict(k)
        return None

    def touch_by_id(self, key_id: str) -> None:
        """调用结束后更新 last_used_at（verify 时已更，这里兜底流式场景）。"""
        with self._lock:
            for k in self._keys:
                if k["key_id"] == key_id:
                    k["last_used_at"] = _now_iso()
                    self._last_used_dirty = True
                    return

    def flush(self) -> None:
        """仅当 last_used_at 有脏数据时落盘（供后台队列周期调用）。"""
        with self._lock:
            if self._last_used_dirty:
                self._save_locked()

    # ---------- 管理（面板用） ----------

    def create(self, name: str, project: str, aliases: dict[str, str] | None = None,
               enabled: bool = True,
               alias_fallback_tiers: dict[str, Any] | None = None) -> dict[str, Any]:
        if not name or not name.strip():
            raise ValueError("连接名称不能为空")
        if not project or not project.strip():
            raise ValueError("project 标签不能为空")
        key = {
            "key_id": f"inb_{secrets.token_hex(2)}",
            "secret": KEY_PREFIX + secrets.token_hex(SECRET_HEX_LEN),
            "name": name.strip(),
            "project": project.strip(),
            "enabled": bool(enabled),
            "aliases": dict(aliases or {}),
            "alias_fallback_tiers": _clean_tier_ranges(alias_fallback_tiers),
            "created_at": _now_iso(),
            "last_used_at": None,
        }
        with self._lock:
            self._keys.append(key)
            self._save_locked()
        return dict(key)

    def update(self, key_id: str, *, name: str | None = None, project: str | None = None,
               aliases: dict[str, str] | None = None, enabled: bool | None = None,
               alias_fallback_tiers: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            for k in self._keys:
                if k["key_id"] == key_id:
                    if name is not None:
                        if not name.strip():
                            raise ValueError("连接名称不能为空")
                        k["name"] = name.strip()
                    if project is not None:
                        if not project.strip():
                            raise ValueError("project 标签不能为空")
                        k["project"] = project.strip()
                    if aliases is not None:
                        k["aliases"] = dict(aliases)
                    if alias_fallback_tiers is not None:
                        k["alias_fallback_tiers"] = _clean_tier_ranges(alias_fallback_tiers)
                    if enabled is not None:
                        k["enabled"] = bool(enabled)
                    self._save_locked()
                    return dict(k)
        raise KeyNotFoundError(key_id)

    def delete(self, key_id: str) -> None:
        with self._lock:
            before = len(self._keys)
            self._keys = [k for k in self._keys if k["key_id"] != key_id]
            if len(self._keys) == before:
                raise KeyNotFoundError(key_id)
            self._save_locked()

    # ---------- 模型解析（决策#2：别名 → 池内精确匹配 → 404；兜底改为按别名显式可选） ----------

    def resolve_model(self, key_id: str, requested: str, pool_models: set[str]) -> str | None:
        """解析请求模型名。命中返回池内模型名；都不中返回 None（调用方 404）。"""
        with self._lock:
            aliases: dict[str, str] = {}
            for k in self._keys:
                if k["key_id"] == key_id:
                    aliases = k["aliases"]
                    break
        target = aliases.get(requested)
        if target and target in pool_models:
            return target
        if requested in pool_models:
            return requested
        return None

    def get_fallback_tier_range(self, key_id: str, requested: str) -> tuple[int, int] | None:
        """该 key 下请求名（别名）配置的兜底 Tier 范围 (lo, hi)；未配置返回 None（不兜底）。

        调用侧把非 None 值作为 pool 的 tier 硬过滤透传：目标模型不可用时，
        池在该范围内选可用模型兜底（model 仍是软偏好）。
        """
        with self._lock:
            for k in self._keys:
                if k["key_id"] == key_id:
                    tiers = k.get("alias_fallback_tiers") or {}
                    v = tiers.get(requested)
                    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
                        return (v, v)  # 旧格式单档兜底
                    if isinstance(v, (list, tuple)) and len(v) == 2:
                        nums = [x for x in v if isinstance(x, int) and not isinstance(x, bool)]
                        if len(nums) == 2 and all(1 <= x <= 5 for x in nums):
                            return (min(nums), max(nums))
                    return None
        return None

    def all_alias_names(self) -> list[str]:
        """全部 key 的别名请求名并集（/v1/models 聚合用）。"""
        with self._lock:
            names: set[str] = set()
            for k in self._keys:
                names.update(k["aliases"].keys())
            return sorted(names)


_store: KeyStore | None = None
_store_lock = threading.Lock()


def get_key_store() -> KeyStore:
    """进程级单例（测试里用 key_store.reset_key_store() 或直接构造新实例替换）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = KeyStore()
        return _store


def reset_key_store(path: Path | str | None = None) -> KeyStore:
    """重置单例（测试用）。传 path 则用新路径。"""
    global _store
    with _store_lock:
        _store = KeyStore(path) if path is not None else KeyStore()
        return _store
