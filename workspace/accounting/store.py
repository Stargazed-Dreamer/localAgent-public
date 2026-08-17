"""记账存储层 — SQLite CRUD（交易表 + 映射表 + 配置表）

替代旧的 bill_review_data.json / name_mapping.json / review_config.json 三文件分散存储。
DB 路径由 config.toml [accounting] db_path 配置，默认 data/accounting.db。

表结构：
  transactions     — 每条交易一行（原始字段 + 分类字段 + 确认状态）
  mapping_fixed    — 固定映射（map_key → name + category）
  mapping_variable — 可变映射（需确认）
  skip_patterns    — 跳过模式
  refund_keywords  — 退款关键词
  config_sectors   — 板块→大类列表
  config_descriptions — 大类→说明文本列表
  import_batches   — 导入批次记录
  mapping_events   — 每次导入的映射命中/未命中记录
  schema_version   — schema 版本管理
"""

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.parent.parent
MAPPING_JSON = Path(__file__).parent / "name_mapping.json"
CONFIG_JSON = Path(__file__).parent / "review_config.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    trade_time TEXT NOT NULL,
    trade_type TEXT,
    counterparty TEXT,
    product TEXT,
    direction TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT,
    map_key TEXT,
    sector TEXT,
    category TEXT,
    description TEXT,
    mark TEXT,
    confirm_reason TEXT,
    is_skip INTEGER DEFAULT 0,
    is_refund INTEGER DEFAULT 0,
    confirm_status TEXT DEFAULT 'auto',
    month_key TEXT,
    import_batch TEXT,
    imported_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(source, trade_time, amount, counterparty)
);
CREATE INDEX IF NOT EXISTS idx_tx_time ON transactions(trade_time);
CREATE INDEX IF NOT EXISTS idx_tx_month ON transactions(month_key);
CREATE INDEX IF NOT EXISTS idx_tx_confirm ON transactions(confirm_status);
CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);

CREATE TABLE IF NOT EXISTS mapping_fixed (
    map_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS mapping_variable (
    map_key TEXT PRIMARY KEY,
    hint TEXT,
    default_category TEXT,
    default_name TEXT,
    need_confirm INTEGER DEFAULT 1,
    rules_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS skip_patterns (
    pattern TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS refund_keywords (
    keyword TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS config_sectors (
    sector TEXT PRIMARY KEY,
    categories_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_descriptions (
    category TEXT PRIMARY KEY,
    descriptions_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    item_count INTEGER,
    date_range_start TEXT,
    date_range_end TEXT
);

CREATE TABLE IF NOT EXISTS mapping_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT,
    map_key TEXT NOT NULL,
    matched_key TEXT,
    mapping_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    source TEXT,
    trade_time TEXT,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mapping_events_key ON mapping_events(matched_key);
CREATE INDEX IF NOT EXISTS idx_mapping_events_map_key ON mapping_events(map_key);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

# 交易表可更新字段（分类相关，排除 id/原始字段/时间戳）
_TX_UPDATE_FIELDS = {
    "sector", "category", "description", "mark", "confirm_reason",
    "is_skip", "is_refund", "confirm_status", "map_key",
}

CURRENT_SCHEMA_VERSION = 1


class AccountingStore:
    """记账 SQLite 存储层

    用法：
        store = AccountingStore("data/accounting.db")
        store.initialize()
        store.upsert_transactions(records)
        store.update_transaction("xxx", {"category": "吃", "description": "KFC"})
        pending = store.get_pending()
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()

    def initialize(self) -> None:
        """建表 + 幂等迁移 + 首次运行从 JSON 导入初始数据"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION,),
        )
        self._conn.commit()

        # 首次运行：从 JSON 文件迁移映射表和配置
        self._migrate_from_json_if_empty()
        self._normalize_legacy_categories()
        self._backfill_mapping_events_if_empty()

    def _migrate_from_json_if_empty(self) -> None:
        """首次运行时从 name_mapping.json / review_config.json 导入初始数据"""
        cur = self._conn.execute("SELECT COUNT(*) FROM mapping_fixed")
        if cur.fetchone()[0] > 0:
            return  # 已有数据，跳过

        # 迁移映射表
        if MAPPING_JSON.exists():
            try:
                mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))
                for map_key, info in mapping.get("fixed", {}).items():
                    self._conn.execute(
                        "INSERT OR IGNORE INTO mapping_fixed (map_key, name, category) VALUES (?, ?, ?)",
                        (map_key, info.get("name", ""), self._normalize_category(info.get("category", ""))),
                    )
                for map_key, info in mapping.get("variable", {}).items():
                    rules = info.get("rules")
                    self._conn.execute(
                        """INSERT OR IGNORE INTO mapping_variable
                           (map_key, hint, default_category, default_name, need_confirm, rules_json)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            map_key,
                            info.get("hint", ""),
                            self._normalize_category(info.get("default_category", "")),
                            info.get("default_name", ""),
                            1 if info.get("need_confirm", True) else 0,
                            json.dumps(rules, ensure_ascii=False) if rules else None,
                        ),
                    )
                for pattern in mapping.get("skip_patterns", []):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO skip_patterns (pattern) VALUES (?)",
                        (pattern,),
                    )
                for keyword in mapping.get("refund_keywords", []):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO refund_keywords (keyword) VALUES (?)",
                        (keyword,),
                    )
                logger.info(f"从 name_mapping.json 迁移映射表完成")
            except Exception as e:
                logger.warning(f"迁移 name_mapping.json 失败: {e}")

        # 迁移配置
        if CONFIG_JSON.exists():
            try:
                config = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                for sector, categories in config.get("sectors", {}).items():
                    self._conn.execute(
                        "INSERT OR REPLACE INTO config_sectors (sector, categories_json) VALUES (?, ?)",
                        (sector, json.dumps(categories, ensure_ascii=False)),
                    )
                for category, descriptions in config.get("descriptions", {}).items():
                    self._conn.execute(
                        """INSERT OR REPLACE INTO config_descriptions
                           (category, descriptions_json) VALUES (?, ?)""",
                        (category, json.dumps(descriptions, ensure_ascii=False)),
                    )
                logger.info(f"从 review_config.json 迁移配置完成")
            except Exception as e:
                logger.warning(f"迁移 review_config.json 失败: {e}")

        self._conn.commit()

    @staticmethod
    def _normalize_category(category: str) -> str:
        """将旧版退款/待对冲伪大类归并回普通大类。

        退款和 AA 回流是同一大类中的正负流水，不应再通过独立类别参与统计。
        """
        value = (category or "").strip()
        for suffix in ("待对冲", "退款"):
            if value.endswith(suffix) and len(value) > len(suffix):
                return value[: -len(suffix)]
        return value

    def _normalize_legacy_categories(self) -> None:
        """启动时把已有交易和映射中的旧伪大类归并为普通大类。"""
        rows = self._conn.execute(
            "SELECT id, category FROM transactions WHERE category LIKE '%退款' OR category LIKE '%待对冲'"
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "UPDATE transactions SET category = ? WHERE id = ?",
                (self._normalize_category(row["category"]), row["id"]),
            )
        fixed = self._conn.execute(
            "SELECT map_key, category FROM mapping_fixed WHERE category LIKE '%退款' OR category LIKE '%待对冲'"
        ).fetchall()
        for row in fixed:
            self._conn.execute(
                "UPDATE mapping_fixed SET category = ? WHERE map_key = ?",
                (self._normalize_category(row["category"]), row["map_key"]),
            )
        variable = self._conn.execute(
            "SELECT map_key, default_category FROM mapping_variable WHERE default_category LIKE '%退款' OR default_category LIKE '%待对冲'"
        ).fetchall()
        for row in variable:
            self._conn.execute(
                "UPDATE mapping_variable SET default_category = ? WHERE map_key = ?",
                (self._normalize_category(row["default_category"]), row["map_key"]),
            )
        self._conn.commit()

    def _backfill_mapping_events_if_empty(self) -> None:
        """为升级前已导入的交易补写一次映射轨迹。"""
        if self._conn.execute("SELECT 1 FROM mapping_events LIMIT 1").fetchone():
            return
        rows = self._conn.execute(
            "SELECT id, map_key, mark, source, trade_time, confirm_reason FROM transactions"
        ).fetchall()
        if not rows:
            return
        mapping = self.get_mapping()
        events = []
        for row in rows:
            map_key = row["map_key"] or ""
            matched_key = ""
            mapping_type = "unmapped"
            if map_key in mapping["fixed"]:
                matched_key, mapping_type = map_key, "fixed"
            elif map_key in mapping["variable"]:
                matched_key, mapping_type = map_key, "variable"
            else:
                for pattern in mapping["skip_patterns"]:
                    if pattern in map_key:
                        matched_key, mapping_type = pattern, "skip"
                        break
            events.append({
                "transaction_id": row["id"],
                "map_key": map_key,
                "matched_key": matched_key,
                "mapping_type": mapping_type if matched_key else "unmapped",
                "outcome": row["mark"] or "?",
                "source": row["source"] or "",
                "trade_time": row["trade_time"] or "",
                "details": row["confirm_reason"] or "",
            })
        self.record_mapping_events(events)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.initialize()
        return self._conn

    # ─── 交易 CRUD ─────────────────────────────────────────────

    @staticmethod
    def _make_tx_id(source: str, time: str, amount: float, counterparty: str) -> str:
        """生成交易 ID：source + time + amount + counterparty 的 SHA1 前 16 位"""
        raw = f"{source}|{time}|{amount}|{counterparty}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def upsert_transactions(self, records: list[dict]) -> tuple[int, int]:
        """批量插入交易（INSERT OR IGNORE，重复跳过）

        records 中每条需含：source, time, trade_type, counterparty, product,
        direction, amount, status, map_key, sector, category, description,
        mark, confirm_reason, is_skip, is_refund, month_key, import_batch
        返回 (inserted, skipped)
        """
        inserted = 0
        skipped = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            for rec in records:
                tx_id = self._make_tx_id(
                    rec["source"], rec["time"], rec["amount"], rec.get("counterparty", "")
                )
                try:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO transactions
                           (id, source, trade_time, trade_type, counterparty, product,
                            direction, amount, status, map_key, sector, category,
                            description, mark, confirm_reason, is_skip, is_refund,
                            confirm_status, month_key, import_batch, imported_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tx_id,
                            rec["source"],
                            rec["time"],
                            rec.get("trade_type", ""),
                            rec.get("counterparty", ""),
                            rec.get("product", ""),
                            rec["direction"],
                            rec["amount"],
                            rec.get("status", ""),
                            rec.get("map_key", ""),
                            rec.get("sector", ""),
                            self._normalize_category(rec.get("category", "")),
                            rec.get("description", ""),
                            rec.get("mark", "?"),
                            rec.get("confirm_reason", ""),
                            1 if rec.get("is_skip") else 0,
                            1 if rec.get("is_refund") else 0,
                            "auto",
                            rec.get("month_key", ""),
                            rec.get("import_batch", ""),
                            now,
                        ),
                    )
                    if self.conn.total_changes > 0:
                        cur = self.conn.execute(
                            "SELECT changes()"
                        )
                        if cur.fetchone()[0] > 0:
                            inserted += 1
                        else:
                            skipped += 1
                    else:
                        skipped += 1
                except sqlite3.IntegrityError:
                    skipped += 1
            self.conn.commit()
        return inserted, skipped

    def update_transaction(self, tx_id: str, updates: dict) -> None:
        """更新交易分类字段（sector/category/description/mark/confirm_status 等）"""
        if not updates:
            return
        set_clauses = []
        params = []
        for field, value in updates.items():
            if field not in _TX_UPDATE_FIELDS:
                continue
            set_clauses.append(f"{field} = ?")
            params.append(self._normalize_category(value) if field == "category" else value)
        if not set_clauses:
            return
        set_clauses.append("updated_at = datetime('now','localtime')")
        params.append(tx_id)
        with self._write_lock:
            self.conn.execute(
                f"UPDATE transactions SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
            self.conn.commit()

    def get_transaction(self, tx_id: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def query(
        self,
        month: Optional[str] = None,
        category: Optional[str] = None,
        confirm_status: Optional[str] = None,
        source: Optional[str] = None,
        mark: Optional[str] = None,
        include_skip: bool = True,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """任意条件查询交易"""
        conditions = []
        params = []
        if month:
            conditions.append("month_key = ?")
            params.append(month)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if confirm_status:
            conditions.append("confirm_status = ?")
            params.append(confirm_status)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if mark:
            conditions.append("mark = ?")
            params.append(mark)
        if not include_skip:
            conditions.append("is_skip = 0")
        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM transactions WHERE {where} ORDER BY trade_time DESC"
        if limit:
            sql += f" LIMIT {limit}"
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def get_pending(self) -> list[dict]:
        """待审核交易（未确认且未跳过），按时间正序"""
        cur = self.conn.execute(
            """SELECT * FROM transactions
               WHERE confirm_status != 'confirmed' AND is_skip = 0
               ORDER BY trade_time ASC"""
        )
        return [dict(r) for r in cur.fetchall()]

    def get_months(self) -> list[str]:
        """所有有交易的月份列表（降序）"""
        cur = self.conn.execute(
            "SELECT DISTINCT month_key FROM transactions WHERE month_key != '' ORDER BY month_key DESC"
        )
        return [r[0] for r in cur.fetchall()]

    def get_month_summary(self, month_key: str) -> dict:
        """按 category 聚合月度统计

        返回 {category: {count, total_expense, total_income, items: [...]}}
        """
        cur = self.conn.execute(
            """SELECT * FROM transactions
               WHERE month_key = ? AND is_skip = 0
               ORDER BY trade_time ASC""",
            (month_key,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        summary: dict = {}
        for row in rows:
            cat = self._normalize_category(row["category"] or "未知")
            if cat not in summary:
                summary[cat] = {"count": 0, "total_expense": 0.0, "total_income": 0.0, "items": []}
            summary[cat]["count"] += 1
            if row["direction"] == "收入":
                summary[cat]["total_income"] += row["amount"]
            else:
                summary[cat]["total_expense"] += row["amount"]
            summary[cat]["items"].append(row)
            summary[cat]["net"] = summary[cat]["total_income"] - summary[cat]["total_expense"]
        return summary

    def get_month_confirmed(self, month_key: str) -> list[dict]:
        """获取某月已确认交易（导出用），按时间正序"""
        cur = self.conn.execute(
            """SELECT * FROM transactions
               WHERE month_key = ? AND confirm_status = 'confirmed' AND is_skip = 0
               ORDER BY trade_time ASC""",
            (month_key,),
        )
        return [dict(r) for r in cur.fetchall()]

    def confirm_transactions(self, tx_ids: list[str]) -> int:
        """批量确认交易"""
        if not tx_ids:
            return 0
        placeholders = ",".join("?" * len(tx_ids))
        with self._write_lock:
            cur = self.conn.execute(
                f"""UPDATE transactions
                    SET confirm_status = 'confirmed', updated_at = datetime('now','localtime')
                    WHERE id IN ({placeholders})""",
                tx_ids,
            )
            self.conn.commit()
            return cur.rowcount

    def confirm_all_pending(self) -> int:
        """确认所有待审核交易"""
        with self._write_lock:
            cur = self.conn.execute(
                """UPDATE transactions
                   SET confirm_status = 'confirmed', updated_at = datetime('now','localtime')
                   WHERE confirm_status != 'confirmed' AND is_skip = 0"""
            )
            self.conn.commit()
            return cur.rowcount

    # ─── 映射表 ─────────────────────────────────────────────────

    def get_mapping(self) -> dict:
        """返回旧格式映射表（兼容 bill_converter）"""
        fixed = {}
        cur = self.conn.execute("SELECT map_key, name, category FROM mapping_fixed")
        for row in cur:
            fixed[row["map_key"]] = {
                "name": row["name"],
                "category": self._normalize_category(row["category"]),
            }

        variable = {}
        cur = self.conn.execute(
            "SELECT map_key, hint, default_category, default_name, need_confirm, rules_json FROM mapping_variable"
        )
        for row in cur:
            info = {
                "hint": row["hint"] or "",
                "default_category": self._normalize_category(row["default_category"] or ""),
                "default_name": row["default_name"] or "",
                "need_confirm": bool(row["need_confirm"]),
            }
            if row["rules_json"]:
                try:
                    info["rules"] = json.loads(row["rules_json"])
                except json.JSONDecodeError:
                    pass
            variable[row["map_key"]] = info

        cur = self.conn.execute("SELECT pattern FROM skip_patterns")
        skip_patterns = [r[0] for r in cur.fetchall()]

        cur = self.conn.execute("SELECT keyword FROM refund_keywords")
        refund_keywords = [r[0] for r in cur.fetchall()]

        return {
            "fixed": fixed,
            "variable": variable,
            "skip_patterns": skip_patterns,
            "refund_keywords": refund_keywords,
        }

    def update_mapping_fixed(self, map_key: str, name: str, category: str) -> None:
        """新增/更新固定映射"""
        with self._write_lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO mapping_fixed (map_key, name, category, updated_at)
                   VALUES (?, ?, ?, datetime('now','localtime'))""",
                (map_key, name, self._normalize_category(category)),
            )
            self.conn.commit()

    def get_mapping_fixed_dict(self) -> dict:
        """返回 {map_key: {name, category}} 供 parser 分类用"""
        cur = self.conn.execute("SELECT map_key, name, category FROM mapping_fixed")
        return {
            r["map_key"]: {"name": r["name"], "category": self._normalize_category(r["category"])}
            for r in cur
        }

    def delete_mapping_fixed(self, map_key: str) -> None:
        """删除固定映射"""
        with self._write_lock:
            self.conn.execute("DELETE FROM mapping_fixed WHERE map_key = ?", (map_key,))
            self.conn.commit()

    def update_mapping_variable(self, map_key: str, hint: str = "",
                                default_name: str = "", default_category: str = "",
                                need_confirm: bool = True) -> None:
        """新增/更新可变映射"""
        with self._write_lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO mapping_variable
                   (map_key, hint, default_name, default_category, need_confirm, updated_at)
                   VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))""",
                (map_key, hint, default_name, self._normalize_category(default_category), int(need_confirm)),
            )
            self.conn.commit()

    def delete_mapping_variable(self, map_key: str) -> None:
        """删除可变映射"""
        with self._write_lock:
            self.conn.execute("DELETE FROM mapping_variable WHERE map_key = ?", (map_key,))
            self.conn.commit()

    def add_skip_pattern(self, pattern: str) -> None:
        """添加跳过模式（去重）"""
        existing = [r[0] for r in self.conn.execute("SELECT pattern FROM skip_patterns").fetchall()]
        if pattern in existing:
            return
        with self._write_lock:
            self.conn.execute("INSERT INTO skip_patterns (pattern) VALUES (?)", (pattern,))
            self.conn.commit()

    def delete_skip_pattern(self, pattern: str) -> None:
        """删除跳过模式"""
        with self._write_lock:
            self.conn.execute("DELETE FROM skip_patterns WHERE pattern = ?", (pattern,))
            self.conn.commit()

    def add_refund_keyword(self, keyword: str) -> None:
        """添加退款关键词（去重）"""
        existing = [r[0] for r in self.conn.execute("SELECT keyword FROM refund_keywords").fetchall()]
        if keyword in existing:
            return
        with self._write_lock:
            self.conn.execute("INSERT INTO refund_keywords (keyword) VALUES (?)", (keyword,))
            self.conn.commit()

    def delete_refund_keyword(self, keyword: str) -> None:
        """删除退款关键词"""
        with self._write_lock:
            self.conn.execute("DELETE FROM refund_keywords WHERE keyword = ?", (keyword,))
            self.conn.commit()

    def get_available_months(self) -> list[str]:
        """返回有交易数据的月份列表（YYYY.MM 格式，供日历灰化用）"""
        cur = self.conn.execute(
            "SELECT DISTINCT substr(trade_time, 1, 7) as ym FROM transactions ORDER BY ym"
        )
        return [r["ym"].replace("-", ".") for r in cur]

    # ─── 映射命中诊断 ──────────────────────────────────────────

    def record_mapping_events(self, events: list[dict]) -> int:
        """记录一次导入中每条交易的映射结果，供后续诊断无法命中的映射。"""
        if not events:
            return 0
        with self._write_lock:
            self.conn.executemany(
                """INSERT INTO mapping_events
                   (transaction_id, map_key, matched_key, mapping_type, outcome,
                    source, trade_time, details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(
                    e.get("transaction_id", ""), e.get("map_key", ""),
                    e.get("matched_key", ""), e.get("mapping_type", "unmapped"),
                    e.get("outcome", "?"), e.get("source", ""),
                    e.get("trade_time", ""), e.get("details", ""),
                ) for e in events],
            )
            self.conn.commit()
        return len(events)

    def get_mapping_hit_counts(self) -> dict[str, int]:
        """返回 {映射键: 触发次数}，只统计实际命中的映射。"""
        cur = self.conn.execute(
            """SELECT matched_key, COUNT(*) AS hits
               FROM mapping_events
               WHERE matched_key IS NOT NULL AND matched_key != ''
               GROUP BY matched_key"""
        )
        return {r["matched_key"]: int(r["hits"]) for r in cur}

    def get_mapping_events(self, map_key: str | None = None, limit: int = 200) -> list[dict]:
        """读取映射触发记录，默认返回最近记录。"""
        if map_key:
            cur = self.conn.execute(
                "SELECT * FROM mapping_events WHERE matched_key = ? OR map_key = ? ORDER BY id DESC LIMIT ?",
                (map_key, map_key, limit),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM mapping_events ORDER BY id DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in cur.fetchall()]

    # ─── 配置 ───────────────────────────────────────────────────

    def get_config(self) -> dict:
        """返回旧格式配置（兼容 accounting_server）"""
        cur = self.conn.execute("SELECT sector, categories_json FROM config_sectors")
        sectors = {r["sector"]: json.loads(r["categories_json"]) for r in cur}

        cur = self.conn.execute("SELECT category, descriptions_json FROM config_descriptions")
        descriptions = {r["category"]: json.loads(r["descriptions_json"]) for r in cur}

        return {"sectors": sectors, "descriptions": descriptions}

    def get_categories_for_sector(self, sector: str) -> list[str]:
        cur = self.conn.execute(
            "SELECT categories_json FROM config_sectors WHERE sector = ?", (sector,)
        )
        row = cur.fetchone()
        return json.loads(row["categories_json"]) if row else []

    def ensure_category(self, sector: str, category: str) -> bool:
        """确保大类存在于指定板块；返回是否新增。"""
        sector = (sector or "出").strip() or "出"
        category = self._normalize_category(category)
        if not category:
            return False
        categories = self.get_categories_for_sector(sector)
        if category in categories:
            return False
        categories.append(category)
        with self._write_lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO config_sectors (sector, categories_json) VALUES (?, ?)",
                (sector, json.dumps(categories, ensure_ascii=False)),
            )
            self.conn.commit()
        return True

    def rename_category(self, sector: str, old_category: str, new_category: str) -> bool:
        """重命名大类，并同步配置、映射和交易分类。"""
        old_category = self._normalize_category(old_category)
        new_category = self._normalize_category(new_category)
        if not old_category or not new_category or old_category == new_category:
            return False
        categories = self.get_categories_for_sector(sector)
        if old_category not in categories:
            return False
        if new_category in categories:
            raise ValueError(f"大类已存在: {new_category}")
        categories[categories.index(old_category)] = new_category
        old_desc = self.get_descriptions_for_category(old_category)
        new_desc = self.get_descriptions_for_category(new_category)
        merged_desc = sorted(set(old_desc + new_desc))
        with self._write_lock:
            self.conn.execute(
                "UPDATE config_sectors SET categories_json = ? WHERE sector = ?",
                (json.dumps(categories, ensure_ascii=False), sector),
            )
            if merged_desc:
                self.conn.execute(
                    "INSERT OR REPLACE INTO config_descriptions (category, descriptions_json) VALUES (?, ?)",
                    (new_category, json.dumps(merged_desc, ensure_ascii=False)),
                )
            self.conn.execute("DELETE FROM config_descriptions WHERE category = ?", (old_category,))
            self.conn.execute("UPDATE transactions SET category = ? WHERE category = ?", (new_category, old_category))
            self.conn.execute("UPDATE mapping_fixed SET category = ? WHERE category = ?", (new_category, old_category))
            self.conn.execute("UPDATE mapping_variable SET default_category = ? WHERE default_category = ?", (new_category, old_category))
            self.conn.commit()
        return True

    def delete_category(self, sector: str, category: str) -> bool:
        """删除大类配置及依赖映射；历史交易保留原分类。"""
        category = self._normalize_category(category)
        categories = self.get_categories_for_sector(sector)
        if category not in categories:
            return False
        categories.remove(category)
        with self._write_lock:
            self.conn.execute(
                "UPDATE config_sectors SET categories_json = ? WHERE sector = ?",
                (json.dumps(categories, ensure_ascii=False), sector),
            )
            self.conn.execute("DELETE FROM mapping_fixed WHERE category = ?", (category,))
            self.conn.execute(
                "DELETE FROM mapping_variable WHERE default_category = ?", (category,)
            )
            self.conn.commit()
        return True

    def get_category_usage(self, category: str) -> dict[str, int]:
        """返回大类被历史交易和映射引用的数量，供删除前说明影响。"""
        category = self._normalize_category(category)
        counts = {}
        for key, table, column in (
            ("transactions", "transactions", "category"),
            ("fixed_mappings", "mapping_fixed", "category"),
            ("variable_mappings", "mapping_variable", "default_category"),
        ):
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (category,)
            ).fetchone()
            counts[key] = int(row[0])
        return counts

    def get_descriptions_for_category(self, category: str) -> list[str]:
        cur = self.conn.execute(
            "SELECT descriptions_json FROM config_descriptions WHERE category = ?", (category,)
        )
        row = cur.fetchone()
        return json.loads(row["descriptions_json"]) if row else []

    def add_description(self, category: str, description: str) -> None:
        """添加说明文本到某大类（去重，已存在则跳过）"""
        existing = self.get_descriptions_for_category(category)
        if description in existing:
            return
        existing.append(description)
        existing.sort()
        with self._write_lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO config_descriptions (category, descriptions_json)
                   VALUES (?, ?)""",
                (category, json.dumps(existing, ensure_ascii=False)),
            )
            self.conn.commit()

    # ─── 导入批次 ───────────────────────────────────────────────

    def record_import_batch(
        self, batch_id: str, source_file: str, item_count: int,
        date_range_start: str, date_range_end: str,
    ) -> None:
        with self._write_lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO import_batches
                   (id, source_file, imported_at, item_count, date_range_start, date_range_end)
                   VALUES (?, ?, datetime('now','localtime'), ?, ?, ?)""",
                (batch_id, source_file, item_count, date_range_start, date_range_end),
            )
            self.conn.commit()

    # ─── 统计 ───────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """整体统计（GUI 汇总页用）"""
        cur = self.conn.execute("SELECT COUNT(*) FROM transactions")
        total = cur.fetchone()[0]
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE confirm_status = 'confirmed'"
        )
        confirmed = cur.fetchone()[0]
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE confirm_status != 'confirmed' AND is_skip = 0"
        )
        pending = cur.fetchone()[0]
        cur = self.conn.execute("SELECT COUNT(*) FROM transactions WHERE is_skip = 1")
        skipped = cur.fetchone()[0]
        cur = self.conn.execute("SELECT COUNT(*) FROM mapping_fixed")
        mapping_count = cur.fetchone()[0]
        cur = self.conn.execute("SELECT COUNT(*) FROM mapping_events")
        mapping_events = cur.fetchone()[0]
        return {
            "total": total,
            "confirmed": confirmed,
            "pending": pending,
            "skipped": skipped,
            "mapping_count": mapping_count,
            "mapping_events": mapping_events,
        }
