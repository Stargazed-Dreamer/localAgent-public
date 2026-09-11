"""记账存储层的流水与映射诊断回归测试。"""

from pathlib import Path

from workspace.accounting.bill_converter import update_md_file
from workspace.accounting.store import AccountingStore


def _store(tmp_path: Path) -> AccountingStore:
    store = AccountingStore(str(tmp_path / "accounting.db"))
    store.initialize()
    return store


def test_mapping_events_and_net_flow(tmp_path: Path):
    store = _store(tmp_path)
    store.upsert_transactions([
        {
            "source": "test",
            "time": "2026-07-01 10:00:00",
            "direction": "支出",
            "amount": 10,
            "counterparty": "water",
            "product": "",
            "trade_type": "消费",
            "category": "生活",
            "description": "water",
            "mark": "✓",
            "month_key": "2026.7",
            "map_key": "消费-water",
        },
        {
            "source": "test",
            "time": "2026-07-02 10:00:00",
            "direction": "收入",
            "amount": 4,
            "counterparty": "roommate",
            "product": "",
            "trade_type": "转账",
            "category": "生活",
            "description": "AA",
            "mark": "✓",
            "month_key": "2026.7",
            "map_key": "转账-roommate",
        },
    ])
    store.record_mapping_events([
        {"map_key": "消费-water", "matched_key": "消费-water", "mapping_type": "fixed", "outcome": "✓"},
        {"map_key": "转账-roommate", "matched_key": "", "mapping_type": "unmapped", "outcome": "?"},
    ])

    summary = store.get_month_summary("2026.7")
    assert summary["生活"]["total_expense"] == 10
    assert summary["生活"]["total_income"] == 4
    assert summary["生活"]["net"] == -6
    assert store.get_mapping_hit_counts()["消费-water"] == 1


def test_legacy_categories_are_normalized(tmp_path: Path):
    store = _store(tmp_path)
    store.upsert_transactions([
        {
            "source": "test",
            "time": "2026-07-03 10:00:00",
            "direction": "收入",
            "amount": 2,
            "counterparty": "refund",
            "product": "",
            "trade_type": "退款",
            "category": "生活退款",
            "description": "refund",
            "mark": "✓",
            "month_key": "2026.7",
            "map_key": "退款-refund",
        },
    ])
    assert store.get_transaction(store._make_tx_id("test", "2026-07-03 10:00:00", 2, "refund"))["category"] == "生活"


def test_category_crud_keeps_history_and_syncs_rename(tmp_path: Path):
    store = _store(tmp_path)
    assert store.ensure_category("出", "宠物") is True
    assert "宠物" in store.get_categories_for_sector("出")
    store.upsert_transactions([
        {
            "source": "test",
            "time": "2026-07-04 10:00:00",
            "direction": "支出",
            "amount": 8,
            "counterparty": "pet",
            "product": "",
            "trade_type": "消费",
            "category": "宠物",
            "description": "food",
            "mark": "✓",
            "month_key": "2026.7",
            "map_key": "消费-pet",
        },
    ])
    assert store.rename_category("出", "宠物", "宠物用品") is True
    assert store.get_month_summary("2026.7")["宠物用品"]["total_expense"] == 8
    store.update_mapping_fixed("消费-pet", "food", "宠物用品")
    assert store.get_category_usage("宠物用品") == {
        "transactions": 1,
        "fixed_mappings": 1,
        "variable_mappings": 0,
    }
    assert store.delete_category("出", "宠物用品") is True
    assert store.get_transaction(store._make_tx_id("test", "2026-07-04 10:00:00", 8, "pet"))
    assert "消费-pet" not in store.get_mapping()["fixed"]


def test_export_creates_configured_new_category_heading(tmp_path: Path):
    md_path = tmp_path / "2026-7.md"
    md_path.write_text("## 支出\n###### 吃：\n\n## 收入\n###### 工资：\n", encoding="utf-8")
    update_md_file(
        "2026.7",
        {"2026.7": {"宠物用品": [{"name": "food", "amount": 8, "direction": "支出", "time": "2026-07-04 10:00"}]}},
        md_path,
        category_sections={"出": ["吃", "宠物用品"], "进": ["工资"], "理财": []},
    )
    content = md_path.read_text(encoding="utf-8")
    assert "###### 宠物用品：8food" in content


def test_export_uses_stored_sector_for_deleted_mixed_flow_category(tmp_path: Path):
    md_path = tmp_path / "2026-7.md"
    md_path.write_text("## 支出\n###### 吃：\n\n## 收入\n###### 工资：\n", encoding="utf-8")
    update_md_file(
        "2026.7",
        {"2026.7": {"奖金": [
            {"name": "award", "amount": 10, "direction": "收入", "sector": "进", "time": "2026-07-05 10:00"},
            {"name": "return", "amount": 2, "direction": "支出", "sector": "进", "time": "2026-07-06 10:00"},
        ]}},
        md_path,
        category_sections={"出": ["吃"], "进": ["工资"], "理财": []},
    )
    content = md_path.read_text(encoding="utf-8")
    assert content.index("## 收入") < content.index("###### 奖金：+10award-2return")
