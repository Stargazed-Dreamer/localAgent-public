"""state_manager 单元测试（Seam 2）

测试"分类即已处理"状态持久化逻辑。参考 tests/test_release_policy.py 的纯逻辑测试模式。

state_manager 位于 tools/file_classifier/（非标准包），用 importlib.util 加载，
与 loop_actions.py 加载 predictor.py 的方式一致。
"""

import importlib.util
from pathlib import Path

import pytest

# 动态加载 state_manager 模块
_STATE_MANAGER_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "file_classifier"
    / "state_manager.py"
)
_spec = importlib.util.spec_from_file_location("state_manager", _STATE_MANAGER_PATH)
state_manager = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state_manager)
StateManager = state_manager.StateManager


@pytest.fixture
def manager(tmp_path):
    """每个测试用独立的 state.json，避免污染真实状态"""
    state_path = tmp_path / "state.json"
    return StateManager(str(state_path))


# ==================== Slice 1: 分类即已处理 ====================

def test_mark_classified_marks_as_processed(manager):
    """标记分类后，is_processed 返回 True"""
    manager.mark_classified("/path/to/file.jpg", "图片")

    assert manager.is_processed("/path/to/file.jpg") is True


def test_unmarked_file_not_processed(manager):
    """未标记的文件，is_processed 返回 False"""
    assert manager.is_processed("/path/to/unknown.jpg") is False


def test_get_classification_returns_metadata(manager):
    """get_classification 返回含 category 和 timestamp 的元数据"""
    manager.mark_classified("/path/to/file.jpg", "图片")

    result = manager.get_classification("/path/to/file.jpg")

    assert result is not None
    assert result["category"] == "图片"
    assert "timestamp" in result


def test_get_classification_returns_none_for_unknown(manager):
    """未知路径的 get_classification 返回 None"""
    assert manager.get_classification("/path/to/unknown.jpg") is None


# ==================== Slice 2: 重置分类 ====================

def test_reset_classification_clears_state(manager):
    """重置分类后，is_processed 返回 False"""
    manager.mark_classified("/path/to/file.jpg", "图片")
    assert manager.is_processed("/path/to/file.jpg") is True

    manager.reset_classification("/path/to/file.jpg")

    assert manager.is_processed("/path/to/file.jpg") is False


def test_reset_unknown_path_no_error(manager):
    """重置不存在的路径不报错"""
    # 不应抛出异常
    manager.reset_classification("/path/to/never_classified.jpg")


# ==================== Slice 3: 暂存分类 ====================

def test_staging_category_detected_by_empty_path():
    """path 为空的分类是暂存分类"""
    staging_category = {"name": "暂存", "path": "", "extensions": []}

    assert StateManager.is_staging_category(staging_category) is True


def test_non_staging_category_has_path():
    """path 非空的分类不是暂存分类"""
    normal_category = {"name": "图片", "path": "E:/<data_drive>:\Pictures", "extensions": [".jpg"]}

    assert StateManager.is_staging_category(normal_category) is False


def test_staging_category_marked_as_processed(manager):
    """分类到暂存的文件标记已处理（但不移动，移动逻辑在 MoveFilesThread）"""
    manager.mark_classified("/path/to/long_term_file.txt", "暂存")

    assert manager.is_processed("/path/to/long_term_file.txt") is True


# ==================== Slice 4: 列宽记忆 ====================

def test_column_widths_round_trip(manager):
    """列宽保存和读取一致"""
    widths = {"col_0": 40, "col_1": 350, "col_2": 100, "col_3": 150}

    manager.save_column_widths(widths)

    assert manager.get_column_widths() == widths


def test_column_widths_empty_by_default(manager):
    """新状态文件的列宽默认为空字典"""
    assert manager.get_column_widths() == {}


# ==================== Slice 5: 上次源目录记忆 ====================

def test_last_source_dir_round_trip(manager):
    """上次源目录保存和读取一致"""
    manager.save_last_source_dir("E:/<data_drive>:\<system_data_root>/<data_drive>:/Downloads")

    assert manager.get_last_source_dir() == "E:/<data_drive>:\<system_data_root>/<data_drive>:/Downloads"


def test_last_source_dir_empty_by_default(manager):
    """新状态文件的上次源目录默认为空字符串"""
    assert manager.get_last_source_dir() == ""


# ==================== Slice 6: 分类配置路径记忆 ====================

def test_loaded_categories_config_round_trip(manager):
    """分类配置路径保存和读取一致"""
    config_path = "tools/file_classifier/categories.json"
    manager.save_loaded_categories_config(config_path)

    assert manager.get_loaded_categories_config() == config_path


def test_loaded_categories_config_empty_by_default(manager):
    """新状态文件的分类配置路径默认为空字符串"""
    assert manager.get_loaded_categories_config() == ""


# ==================== Slice 7: 持久化与共享读取 ====================

def test_persistence_across_instances(manager, tmp_path):
    """新实例读取旧状态（GUI/后端共享同一 state.json）"""
    manager.mark_classified("/path/to/file.jpg", "图片")
    manager.save_last_source_dir("E:/<data_drive>:/Downloads")
    manager.save_column_widths({"col_0": 40})

    # 模拟后端启动新实例读取同一 state.json
    backend_manager = StateManager(str(tmp_path / "state.json"))

    assert backend_manager.is_processed("/path/to/file.jpg") is True
    assert backend_manager.get_classification("/path/to/file.jpg")["category"] == "图片"
    assert backend_manager.get_last_source_dir() == "E:/<data_drive>:/Downloads"
    assert backend_manager.get_column_widths() == {"col_0": 40}


def test_state_file_created_on_first_save(manager, tmp_path):
    """首次保存后 state.json 文件存在"""
    state_path = tmp_path / "state.json"
    assert not state_path.exists()

    manager.mark_classified("/path/to/file.jpg", "图片")

    assert state_path.exists()


def test_state_file_is_valid_json(manager, tmp_path):
    """state.json 是合法 JSON 文件"""
    import json

    manager.mark_classified("/path/to/file.jpg", "图片")
    manager.save_column_widths({"col_0": 40})

    state_path = tmp_path / "state.json"
    with open(state_path, encoding="utf-8") as f:
        data = json.load(f)

    assert "classified_files" in data
    assert "column_widths" in data
    assert "last_source_dir" in data
    assert "loaded_categories_config" in data
    assert "/path/to/file.jpg" in data["classified_files"]
