"""project_structure 项目结构扫描与漂移检测测试

覆盖：
- scan_project_structure: 扫描一二级目录、跳过规则
- load_baseline / save_baseline: JSON 读写
- diff_structure: 漂移检测（未知/消失路径）
- ensure_baseline: 首次生成
- update_baseline_descriptions: 更新描述
- get_status: 状态查询
"""

import pytest

from server import project_structure
from server.project_structure import (
    diff_structure,
    ensure_baseline,
    get_status,
    load_baseline,
    save_baseline,
    scan_project_structure,
    update_baseline_descriptions,
)


@pytest.fixture
def mock_root(tmp_path):
    """创建一个模拟项目根目录"""
    # 顶层目录
    (tmp_path / "server").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / ".venv").mkdir()  # 应被跳过
    (tmp_path / ".git").mkdir()  # 运行时元数据，应被跳过
    (tmp_path / ".ruff_cache").mkdir()  # 缓存，应被跳过
    (tmp_path / "data").mkdir()
    (tmp_path / "temp").mkdir()
    # 顶层文件
    (tmp_path / "README.md").write_text("# test")
    (tmp_path / "config.toml").write_text("[server]")
    (tmp_path / "main.py").write_text("# entry")
    (tmp_path / "random.bin").write_bytes(b"\x00")  # 非重要文件，应跳过
    # 二级目录
    (tmp_path / "server" / "core").mkdir()
    (tmp_path / "server" / "screen").mkdir()
    (tmp_path / "server" / "__pycache__").mkdir()  # 应被跳过
    (tmp_path / "tests" / "unit").mkdir()
    (tmp_path / "data" / "runtime").mkdir()
    (tmp_path / "temp" / "sessions").mkdir()
    # .venv 下有内容但应被跳过
    (tmp_path / ".venv" / "Scripts").mkdir()
    return tmp_path


# ==================== scan_project_structure ====================

class TestScanProjectStructure:
    def test_scans_top_level_dirs(self, mock_root):
        """扫描顶层目录"""
        result = scan_project_structure(mock_root)
        assert "server/" in result["top_level"]
        assert "tests/" in result["top_level"]
        assert "tools/" in result["top_level"]
        assert "data/" in result["top_level"]

    def test_scans_top_level_important_files(self, mock_root):
        """扫描顶层重要文件"""
        result = scan_project_structure(mock_root)
        assert "README.md" in result["top_level"]
        assert "config.toml" in result["top_level"]
        assert "main.py" in result["top_level"]
        assert result["top_level"]["README.md"]["type"] == "file"

    def test_skips_venv(self, mock_root):
        """跳过 .venv"""
        result = scan_project_structure(mock_root)
        assert ".venv/" not in result["top_level"]

    def test_skips_git_and_ruff_cache(self, mock_root):
        """Git 元数据和 Ruff 缓存不纳入结构映射"""
        result = scan_project_structure(mock_root)
        assert ".git/" not in result["top_level"]
        assert ".ruff_cache/" not in result["top_level"]

    def test_skips_non_important_files(self, mock_root):
        """跳过非重要扩展名文件"""
        result = scan_project_structure(mock_root)
        assert "random.bin" not in result["top_level"]

    def test_scans_subdirs(self, mock_root):
        """扫描二级目录"""
        result = scan_project_structure(mock_root)
        assert "server/" in result["subdirs"]
        assert "core/" in result["subdirs"]["server/"]
        assert "screen/" in result["subdirs"]["server/"]
        assert "tests/" in result["subdirs"]
        assert "unit/" in result["subdirs"]["tests/"]

    def test_skips_pycache_in_subdirs(self, mock_root):
        """二级扫描跳过 __pycache__"""
        result = scan_project_structure(mock_root)
        assert "__pycache__/" not in result["subdirs"].get("server/", [])

    def test_skips_venv_subdir_scan(self, mock_root):
        """.venv 不做二级扫描"""
        result = scan_project_structure(mock_root)
        assert ".venv/" not in result["subdirs"]

    def test_skips_runtime_subdir_scan(self, mock_root):
        """data/temp 的运行时子目录不纳入二级结构映射"""
        result = scan_project_structure(mock_root)
        assert "data/" not in result["subdirs"]
        assert "temp/" not in result["subdirs"]

    def test_top_level_entry_has_path(self, mock_root):
        """每个顶层条目有 path 字段"""
        result = scan_project_structure(mock_root)
        assert "path" in result["top_level"]["server/"]
        assert "path" in result["top_level"]["README.md"]

    def test_default_root_is_project(self):
        """不传 root 时用项目根目录"""
        result = scan_project_structure()
        assert "server/" in result["top_level"]
        assert "tests/" in result["top_level"]


# ==================== load_baseline / save_baseline ====================

class TestLoadSaveBaseline:
    def test_save_and_load(self, tmp_path):
        """保存后能正确加载"""
        baseline = {
            "version": "1.0",
            "last_updated": "2026-01-01",
            "top_level": {"server/": {"type": "dir", "description": "后端"}},
            "subdirs": {"server/": ["core/"]},
        }
        f = tmp_path / "baseline.json"
        save_baseline(baseline, f)
        assert f.exists()
        loaded = load_baseline(f)
        assert loaded == baseline

    def test_load_nonexistent_returns_none(self, tmp_path):
        """加载不存在的文件返回 None"""
        assert load_baseline(tmp_path / "nonexistent.json") is None

    def test_load_invalid_json_returns_none(self, tmp_path):
        """加载无效 JSON 返回 None"""
        f = tmp_path / "bad.json"
        f.write_text("not json", encoding="utf-8")
        assert load_baseline(f) is None

    def test_save_creates_parent_dir(self, tmp_path):
        """保存时自动创建父目录"""
        f = tmp_path / "nested" / "deep" / "baseline.json"
        baseline = {"version": "1.0", "top_level": {}, "subdirs": {}}
        save_baseline(baseline, f)
        assert f.exists()


# ==================== diff_structure ====================

class TestDiffStructure:
    def test_no_drift(self):
        """无变化"""
        baseline = {"top_level": {"server/": {}, "tests/": {}}, "subdirs": {}}
        current = {"top_level": {"server/": {}, "tests/": {}}, "subdirs": {}}
        diff = diff_structure(baseline, current)
        assert diff["unknown_paths"] == []
        assert diff["missing_paths"] == []
        assert diff["summary"] == "无漂移"

    def test_unknown_top_level(self):
        """新增顶层路径"""
        baseline = {"top_level": {"server/": {}}, "subdirs": {}}
        current = {"top_level": {"server/": {}, "new_dir/": {}}, "subdirs": {}}
        diff = diff_structure(baseline, current)
        assert len(diff["unknown_paths"]) == 1
        assert diff["unknown_paths"][0]["path"] == "new_dir/"
        assert diff["unknown_paths"][0]["kind"] == "top"
        assert "1 个未知" in diff["summary"]

    def test_missing_top_level(self):
        """消失的顶层路径"""
        baseline = {"top_level": {"server/": {}, "old_dir/": {}}, "subdirs": {}}
        current = {"top_level": {"server/": {}}, "subdirs": {}}
        diff = diff_structure(baseline, current)
        assert len(diff["missing_paths"]) == 1
        assert diff["missing_paths"][0]["path"] == "old_dir/"
        assert "1 个消失" in diff["summary"]

    def test_unknown_subdir(self):
        """新增二级路径"""
        baseline = {"top_level": {}, "subdirs": {"server/": ["core/"]}}
        current = {"top_level": {}, "subdirs": {"server/": ["core/", "new_module/"]}}
        diff = diff_structure(baseline, current)
        assert len(diff["unknown_paths"]) == 1
        assert diff["unknown_paths"][0]["path"] == "new_module/"
        assert diff["unknown_paths"][0]["kind"] == "sub"
        assert diff["unknown_paths"][0]["parent"] == "server/"

    def test_missing_subdir(self):
        """消失的二级路径"""
        baseline = {"top_level": {}, "subdirs": {"server/": ["core/", "old_module/"]}}
        current = {"top_level": {}, "subdirs": {"server/": ["core/"]}}
        diff = diff_structure(baseline, current)
        assert len(diff["missing_paths"]) == 1
        assert diff["missing_paths"][0]["path"] == "old_module/"

    def test_both_unknown_and_missing(self):
        """同时有新增和消失"""
        baseline = {"top_level": {"old/": {}}, "subdirs": {}}
        current = {"top_level": {"new/": {}}, "subdirs": {}}
        diff = diff_structure(baseline, current)
        assert len(diff["unknown_paths"]) == 1
        assert len(diff["missing_paths"]) == 1
        assert "未知" in diff["summary"]
        assert "消失" in diff["summary"]


# ==================== ensure_baseline ====================

class TestEnsureBaseline:
    def test_creates_baseline_when_missing(self, mock_root, monkeypatch, tmp_path):
        """baseline 不存在时自动生成"""
        baseline_file = tmp_path / "project_structure.json"
        # 默认参数在定义时绑定，需直接 mock 函数
        monkeypatch.setattr(project_structure, "load_baseline", lambda path=None: None)
        monkeypatch.setattr(project_structure, "scan_project_structure",
                            lambda root=None: scan_project_structure(mock_root))
        monkeypatch.setattr(project_structure, "save_baseline",
                            lambda bl, path=None: save_baseline(bl, baseline_file))

        baseline = ensure_baseline()
        assert baseline_file.exists()
        assert baseline["version"] == "1.0"
        assert "top_level" in baseline
        assert "首次自动生成" in baseline["note"]

    def test_returns_existing_baseline(self, tmp_path, monkeypatch):
        """baseline 已存在时直接返回"""
        existing = {"version": "1.0", "top_level": {"x/": {}}, "subdirs": {}}
        monkeypatch.setattr(project_structure, "load_baseline", lambda path=None: existing)

        baseline = ensure_baseline()
        assert baseline["top_level"] == {"x/": {}}


# ==================== update_baseline_descriptions ====================

class TestUpdateBaselineDescriptions:
    def test_updates_existing_path(self, tmp_path, monkeypatch):
        """更新已有路径的 description"""
        baseline_file = tmp_path / "baseline.json"
        save_baseline({
            "version": "1.0",
            "top_level": {"server/": {"type": "dir", "description": ""}},
            "subdirs": {},
        }, baseline_file)
        monkeypatch.setattr(project_structure, "BASELINE_FILE", baseline_file)

        result = update_baseline_descriptions({"server/": "后端服务"}, baseline_file)
        assert result["top_level"]["server/"]["description"] == "后端服务"
        assert "last_updated" in result

    def test_adds_new_path(self, tmp_path, monkeypatch):
        """新路径自动添加到 baseline"""
        baseline_file = tmp_path / "baseline.json"
        save_baseline({
            "version": "1.0",
            "top_level": {},
            "subdirs": {},
        }, baseline_file)

        result = update_baseline_descriptions({"new_dir/": "新目录"}, baseline_file)
        assert "new_dir/" in result["top_level"]
        assert result["top_level"]["new_dir/"]["description"] == "新目录"
        assert result["top_level"]["new_dir/"]["type"] == "dir"

    def test_adds_new_file_path(self, tmp_path):
        """文件类型路径（不以 / 结尾）"""
        baseline_file = tmp_path / "baseline.json"
        save_baseline({"version": "1.0", "top_level": {}, "subdirs": {}}, baseline_file)

        result = update_baseline_descriptions({"new_file.py": "入口文件"}, baseline_file)
        assert result["top_level"]["new_file.py"]["type"] == "file"


# ==================== get_status ====================

class TestGetStatus:
    def test_status_no_baseline(self, monkeypatch):
        """无 baseline 时返回 baseline_exists=False"""
        monkeypatch.setattr(project_structure, "load_baseline", lambda path=None: None)
        status = get_status()
        assert status["available"] is True
        assert status["baseline_exists"] is False
        assert status["baseline_entries"] == 0

    def test_status_with_baseline(self, monkeypatch):
        """有 baseline 时返回条目数"""
        existing = {
            "version": "1.0",
            "last_updated": "2026-01-01",
            "top_level": {"a/": {}, "b/": {}, "c.py": {}},
            "subdirs": {},
        }
        monkeypatch.setattr(project_structure, "load_baseline", lambda path=None: existing)

        status = get_status()
        assert status["available"] is True
        assert status["baseline_exists"] is True
        assert status["baseline_entries"] == 3
        assert status["baseline_updated"] == "2026-01-01"
