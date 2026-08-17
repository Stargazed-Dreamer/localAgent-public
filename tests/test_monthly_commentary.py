"""测试月度市场简评功能

覆盖：
- store.py: 建表幂等 / save + get_active / 新存作废旧 / 过期清理 / list
- analyzer.py: build_prompt 注入 monthly_commentary 段 + 免责声明
- analyzer.py: _load_monthly_commentary_for_prompt 仅 evening/adhoc 注入

隔离：每个测试用临时 db_path，不污染真实 stock_advisor.db
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workspace.stock_advisor import store
from workspace.stock_advisor.analyzer import (
    _load_monthly_commentary_for_prompt,
    build_prompt,
)

# ==================== fixtures ====================

@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """临时 SQLite 路径，每个测试独立"""
    db = tmp_path / "test_monthly.db"
    store.init_schema(db_path=db)
    return db


def _save_one(
    db: Path,
    month_label: str = "2026年7月简评",
    valid_from: str = "2026-08-01 00:00:00",
    valid_until: str = "2026-08-31 23:59:59",
    market_review: str = "7月市场下跌。",
    is_active: int = 1,
) -> int:
    """直接写一条记录（绕过 save_monthly_commentary 的自动作废旧逻辑，用于测试）"""
    import time
    with store.get_conn(db) as conn:
        cur = conn.execute(
            """INSERT INTO monthly_commentary(
                month_label, valid_from, valid_until,
                market_review, macro_research, industry_trend,
                micro_structure, strategy_section, source,
                is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                month_label, valid_from, valid_until,
                market_review, "宏观", "中观", "微观", "策略",
                "测试", is_active, int(time.time()),
            ),
        )
        return cur.lastrowid


# ==================== store.py 测试 ====================

class TestStoreSchema:
    def test_init_schema_idempotent(self, tmp_db: Path):
        """init_schema 应幂等：多次调用不报错，表存在"""
        store.init_schema(db_path=tmp_db)  # 第二次
        with sqlite3.connect(str(tmp_db)) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "monthly_commentary" in tables

    def test_index_exists(self, tmp_db: Path):
        with sqlite3.connect(str(tmp_db)) as conn:
            idx = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
        assert "idx_monthly_commentary_valid" in idx


class TestSaveAndGet:
    def test_save_returns_id(self, tmp_db: Path):
        cid = store.save_monthly_commentary(
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            market_review="7月下跌",
            db_path=tmp_db,
        )
        assert cid > 0

    def test_get_active_within_window(self, tmp_db: Path):
        """生效区间内可查"""
        store.save_monthly_commentary(
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            market_review="7月下跌",
            db_path=tmp_db,
        )
        row = store.get_active_monthly_commentary(now="2026-08-15 12:00:00", db_path=tmp_db)
        assert row is not None
        assert row["month_label"] == "2026年7月简评"
        assert row["market_review"] == "7月下跌"
        assert row["is_active"] == 1

    def test_get_active_before_window_returns_none(self, tmp_db: Path):
        """生效起点之前返回 None"""
        store.save_monthly_commentary(
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            db_path=tmp_db,
        )
        row = store.get_active_monthly_commentary(now="2026-07-15 12:00:00", db_path=tmp_db)
        assert row is None

    def test_get_active_after_window_returns_none(self, tmp_db: Path):
        """失效之后返回 None（月底失效）"""
        store.save_monthly_commentary(
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            db_path=tmp_db,
        )
        row = store.get_active_monthly_commentary(now="2026-09-01 00:00:00", db_path=tmp_db)
        assert row is None


class TestNewSaveDeactivatesOld:
    def test_save_new_deactivates_previous_active(self, tmp_db: Path):
        """新存作废旧：插入新记录前，把此前 is_active=1 的旧记录置 0"""
        old_id = store.save_monthly_commentary(
            month_label="2026年6月简评",
            valid_from="2026-07-01 00:00:00",
            valid_until="2026-07-31 23:59:59",
            market_review="6月数据",
            db_path=tmp_db,
        )
        # 即便旧的已过期，save 新的仍应把它从 is_active=1 改为 0
        new_id = store.save_monthly_commentary(
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            market_review="7月数据",
            db_path=tmp_db,
        )
        with sqlite3.connect(str(tmp_db)) as conn:
            old_active = conn.execute(
                "SELECT is_active FROM monthly_commentary WHERE id=?", (old_id,)
            ).fetchone()[0]
            new_active = conn.execute(
                "SELECT is_active FROM monthly_commentary WHERE id=?", (new_id,)
            ).fetchone()[0]
        assert old_active == 0, "旧记录应被作废"
        assert new_active == 1, "新记录应为 active"

        # get_active 只返回新记录
        row = store.get_active_monthly_commentary(now="2026-08-15 12:00:00", db_path=tmp_db)
        assert row is not None
        assert row["id"] == new_id


class TestCleanupExpired:
    def test_cleanup_marks_expired_inactive(self, tmp_db: Path):
        """cleanup_expired_monthly_commentary 把 valid_until < now 的标记为 is_active=0"""
        # 写一条已过期的 active 记录
        _save_one(
            tmp_db,
            month_label="2026年6月简评",
            valid_from="2026-06-01 00:00:00",
            valid_until="2026-06-30 23:59:59",
            is_active=1,
        )
        n = store.cleanup_expired_monthly_commentary(now="2026-07-15 12:00:00", db_path=tmp_db)
        assert n == 1
        # 再调一次，应返回 0（已无 active 过期记录）
        n2 = store.cleanup_expired_monthly_commentary(now="2026-07-15 12:00:00", db_path=tmp_db)
        assert n2 == 0

    def test_cleanup_keeps_valid_active(self, tmp_db: Path):
        """未过期的 active 记录不被清理"""
        _save_one(
            tmp_db,
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            is_active=1,
        )
        n = store.cleanup_expired_monthly_commentary(now="2026-08-15 12:00:00", db_path=tmp_db)
        assert n == 0


class TestList:
    def test_list_orders_by_created_desc(self, tmp_db: Path):
        store.save_monthly_commentary(
            month_label="第一版",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            db_path=tmp_db,
        )
        store.save_monthly_commentary(
            month_label="第二版",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            db_path=tmp_db,
        )
        rows = store.list_monthly_commentary(limit=10, db_path=tmp_db)
        assert len(rows) == 2
        assert rows[0]["month_label"] == "第二版"  # 最新在前


# ==================== analyzer.py 注入测试 ====================

class TestPromptInjection:
    def _patch_db(self, monkeypatch, tmp_db: Path):
        """让 store 模块的所有函数都走 tmp_db

        通过 patch _db_path 单例，避免 patch get_active_monthly_commentary 导致递归。
        """
        store._db_path = tmp_db
        # 同时清空 _db_path 缓存，确保下次 _resolve_db_path 返回 tmp_db
        monkeypatch.setattr(store, "_db_path", tmp_db)

    def test_load_monthly_commentary_evening_returns_content(self, tmp_db: Path, monkeypatch):
        """evening phase 应返回简评内容"""
        _save_one(
            tmp_db,
            month_label="2026年7月简评",
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
            market_review="7月下跌",
        )
        self._patch_db(monkeypatch, tmp_db)

        content = _load_monthly_commentary_for_prompt("evening")
        assert "2026年7月简评" in content
        assert "7月下跌" in content
        assert "2026-08-31 23:59:59" in content  # 失效时间

    def test_load_monthly_commentary_opening_returns_empty(self, tmp_db: Path, monkeypatch):
        """opening phase 不注入，返回空串"""
        _save_one(
            tmp_db,
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
        )
        self._patch_db(monkeypatch, tmp_db)

        content = _load_monthly_commentary_for_prompt("opening")
        assert content == ""

    def test_load_monthly_commentary_closing_returns_empty(self, tmp_db: Path, monkeypatch):
        """closing phase 不注入，返回空串"""
        _save_one(
            tmp_db,
            valid_from="2026-08-01 00:00:00",
            valid_until="2026-08-31 23:59:59",
        )
        self._patch_db(monkeypatch, tmp_db)

        content = _load_monthly_commentary_for_prompt("closing")
        assert content == ""

    def test_load_monthly_commentary_no_active_returns_empty(self, tmp_db: Path, monkeypatch):
        """无生效简评时返回空串（不抛异常）"""
        # tmp_db 已建表但无记录
        self._patch_db(monkeypatch, tmp_db)

        content = _load_monthly_commentary_for_prompt("evening")
        assert content == ""

    def test_load_monthly_commentary_exception_returns_empty(self, monkeypatch):
        """store 抛异常时返回空串（失败容忍，不阻塞主流程）"""
        def _raise(now=None, db_path=None):
            raise RuntimeError("db locked")
        monkeypatch.setattr(store, "get_active_monthly_commentary", _raise)

        content = _load_monthly_commentary_for_prompt("evening")
        assert content == ""


class TestBuildPromptSection:
    def _make_minimal_args(self) -> dict:
        return {
            "phase": "evening",
            "market_data": {"indices": [], "stocks": []},
            "watchlist": {"stocks": []},
            "user_profile": {"risk_tolerance": "moderate"},
        }

    def test_prompt_contains_monthly_commentary_when_provided(self):
        """build_prompt 注入 monthly_commentary 段"""
        args = self._make_minimal_args()
        args["monthly_commentary"] = (
            "- 简评月份：2026年7月简评\n- 失效时间：2026-08-31 23:59:59\n\n"
            "#### 市场简评\n\n7月下跌 13.14%"
        )
        prompt = build_prompt(**args)
        assert "月度市场简评参考" in prompt
        assert "2026年7月简评" in prompt
        assert "7月下跌 13.14%" in prompt

    def test_prompt_contains_disclaimer_when_injected(self):
        """注入简评时必须包含免责声明（用户原话"所有位置都要说明这只是参考"）"""
        args = self._make_minimal_args()
        args["monthly_commentary"] = "测试简评内容"
        prompt = build_prompt(**args)
        assert "仅供参考，非绝对正确" in prompt
        assert "以实时数据为准" in prompt

    def test_prompt_no_monthly_section_when_empty(self):
        """monthly_commentary 为空时，prompt 不含月度简评段"""
        args = self._make_minimal_args()
        args["monthly_commentary"] = ""
        prompt = build_prompt(**args)
        assert "月度市场简评参考" not in prompt

    def test_prompt_section_position_after_accuracy_feedback(self):
        """月度简评段位置：在 accuracy_feedback 之后、wisdom 之前"""
        args = self._make_minimal_args()
        args["monthly_commentary"] = "MONTHLY_MARKER"
        args["accuracy_feedback"] = "ACCURACY_MARKER"
        args["wisdom_content"] = "WISDOM_MARKER"
        prompt = build_prompt(**args)
        pos_accuracy = prompt.find("ACCURACY_MARKER")
        pos_monthly = prompt.find("MONTHLY_MARKER")
        pos_wisdom = prompt.find("WISDOM_MARKER")
        assert pos_accuracy < pos_monthly < pos_wisdom, (
            f"位置错误：accuracy={pos_accuracy} monthly={pos_monthly} wisdom={pos_wisdom}"
        )
