from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wuwa_multiset_merge_restores_identical_occurrences():
    wuwa = _load_module("wuwa_gacha_test", "workspace/wuwa_gacha/wuwa_gacha.py")
    record = {
        "cardPoolType": "1",
        "resourceId": 1001,
        "qualityLevel": 3,
        "resourceType": "武器",
        "name": "测试武器",
        "count": 1,
        "time": "2026-07-31 12:00:00",
    }

    existing = [dict(record)]
    counts = Counter(wuwa._record_key(item) for item in existing)
    merged, added = wuwa.merge_records(existing, [dict(record), dict(record)], counts)

    assert added == 1
    assert len(merged) == 2

    merged_again, added_again = wuwa.merge_records(
        merged,
        [dict(record), dict(record)],
        Counter(wuwa._record_key(item) for item in merged),
    )
    assert added_again == 0
    assert merged_again == merged


def test_arknights_import_keeps_same_second_same_name_at_different_positions():
    importer = _load_module(
        "arknights_import_test",
        "workspace/arknights_gacha/arknights_import.py",
    )
    base = {
        "timestamp": 1785456000,
        "time": "2026-07-31 00:00:00",
        "pool": "测试寻访",
        "name": "测试干员",
        "rarity": 3,
        "rarity_label": "★4",
        "is_new": False,
        "source": "xhh",
    }

    merged, added = importer.merge_records(
        [{**base, "pos": 0}],
        [{**base, "pos": 0}, {**base, "pos": 1}],
    )

    assert added == 1
    assert [record["pos"] for record in merged] == [0, 1]


def test_arknights_official_output_directory_is_project_workspace():
    official = _load_module(
        "arknights_gacha_test",
        "workspace/arknights_gacha/arknights_gacha.py",
    )
    assert official.OUTPUT_DIR == PROJECT_ROOT / "workspace" / "arknights_gacha"


def test_arknights_incremental_collapses_cross_source_timestamp_precision_duplicates(tmp_path):
    official = _load_module(
        "arknights_gacha_dedupe_test",
        "workspace/arknights_gacha/arknights_gacha.py",
    )
    official.OUTPUT_DIR = tmp_path
    legacy = {
        "timestamp": 1785456000,
        "time": "2026-07-31 00:00:00",
        "pool": "测试寻访",
        "name": "测试干员",
        "rarity": 3,
        "rarity_label": "★4",
        "is_new": False,
        "pos": 0,
    }
    current = {
        **legacy,
        "timestamp": 1785456000.123,
        "source": "official",
        "poolId": "pool-id",
        "charId": "char-id",
    }
    (tmp_path / "gacha_records.json").write_text(
        __import__("json").dumps([legacy, current], ensure_ascii=False),
        encoding="utf-8",
    )

    records = official.incremental_update([current])

    assert records == [current]


def test_endfield_incremental_key_is_scoped_to_pool(tmp_path):
    endfield = _load_module(
        "endfield_gacha_test",
        "workspace/endfield_gacha/endfield_gacha.py",
    )
    endfield.OUTPUT_DIR = tmp_path
    shared = {
        "type": "char",
        "timestamp": 1,
        "time": "1970-01-01 00:00:01",
        "poolId": "pool",
        "pool": "pool",
        "charId": "char",
        "name": "name",
        "rarity": 4,
        "rarity_label": "★4",
        "is_new": False,
        "is_free": False,
        "seqId": "123",
    }

    records = endfield.incremental_update(
        [
            {**shared, "poolType": "pool-a"},
            {**shared, "poolType": "pool-b"},
        ],
        "char",
    )

    assert len(records) == 2


def test_endfield_summary_does_not_persist_u8_token():
    endfield = _load_module(
        "endfield_gacha_summary_test",
        "workspace/endfield_gacha/endfield_gacha.py",
    )
    summary = endfield.generate_summary(
        [],
        [],
        {"uid": "uid", "provider": "hypergryph", "u8_token": "secret"},
    )

    assert summary["user"] == {"uid": "uid", "provider": "hypergryph"}


def test_endfield_log_parser_accepts_current_parameter_names(tmp_path):
    endfield = _load_module(
        "endfield_gacha_log_test",
        "workspace/endfield_gacha/endfield_gacha.py",
    )
    log_path = tmp_path / "HGWebview.log"
    log_path.write_text(
        "https://ef-webview.hypergryph.com/page/gacha_char"
        "?u8_token=test-token&server=1&lang=zh-cn",
        encoding="utf-8",
    )
    endfield.LOG_PATHS["hypergryph"] = log_path

    result = endfield.read_token_from_log("hypergryph")

    assert result is not None
    assert result["u8_token"] == "test-token"
    assert result["server_id"] == "1"
