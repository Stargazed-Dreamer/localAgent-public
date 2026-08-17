"""test_folder_info.py — folder_info 纯逻辑单元测试

用 importlib.util 动态加载 tools/file_classifier/folder_info.py
（与 test_state_manager.py / test_type_descriptor.py 一致）。
"""

import importlib.util
import os

# 动态加载 folder_info 模块
_FOLDER_INFO_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "tools", "file_classifier", "folder_info.py",
)
spec = importlib.util.spec_from_file_location("folder_info", _FOLDER_INFO_PATH)
folder_info = importlib.util.module_from_spec(spec)
spec.loader.exec_module(folder_info)


# ── describe_folder 基础场景 ──

def test_describe_folder_empty_dir_returns_zero_counts(tmp_path):
    result = folder_info.describe_folder(str(tmp_path))
    assert result["file_count"] == 0
    assert result["ext_distribution"] == []
    assert result["max_depth"] == 0
    assert result["total_size"] == 0
    assert result["truncated"] is False
    assert result["error"] is None


def test_describe_folder_with_flat_files(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x" * 100)
    (tmp_path / "b.jpg").write_bytes(b"y" * 50)
    (tmp_path / "c.pdf").write_text("hello")
    result = folder_info.describe_folder(str(tmp_path))
    assert result["file_count"] == 3
    assert result["max_depth"] == 0
    assert result["total_size"] == 100 + 50 + 5  # 3 files
    # jpg x2, pdf x1，按 count 降序
    assert result["ext_distribution"] == [("jpg", 2), ("pdf", 1)]
    assert result["truncated"] is False


def test_describe_folder_with_nested_dirs_updates_max_depth(tmp_path):
    (tmp_path / "sub1").mkdir()
    (tmp_path / "sub1" / "a.txt").write_text("a")
    (tmp_path / "sub1" / "sub2").mkdir()
    (tmp_path / "sub1" / "sub2" / "b.txt").write_text("b")
    result = folder_info.describe_folder(str(tmp_path))
    assert result["file_count"] == 2
    assert result["max_depth"] == 2  # sub1/sub2 = depth 2
    assert result["ext_distribution"] == [("txt", 2)]


def test_describe_folder_extension_case_insensitive(tmp_path):
    (tmp_path / "A.JPG").write_text("a")
    (tmp_path / "b.jpg").write_text("b")
    (tmp_path / "c.Jpg").write_text("c")
    result = folder_info.describe_folder(str(tmp_path))
    # 所有 jpg 变体归一为 "jpg"
    assert result["ext_distribution"] == [("jpg", 3)]


def test_describe_folder_no_extension_counted_as_empty(tmp_path):
    (tmp_path / "README").write_text("r")
    (tmp_path / "Makefile").write_text("m")
    result = folder_info.describe_folder(str(tmp_path))
    assert result["file_count"] == 2
    # 空扩展名归为 ""
    assert result["ext_distribution"] == [("", 2)]


def test_describe_folder_total_size_aggregates_all_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"\x00" * 1000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\x00" * 500)
    result = folder_info.describe_folder(str(tmp_path))
    assert result["total_size"] == 1500


def test_describe_folder_nonexistent_path_returns_error(tmp_path):
    fake = str(tmp_path / "does_not_exist")
    result = folder_info.describe_folder(fake)
    assert result["error"] is not None
    assert result["file_count"] == 0
    assert "不存在" in result["error"] or "不是" in result["error"]


def test_describe_folder_file_path_returns_error(tmp_path):
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x")
    result = folder_info.describe_folder(str(f))
    assert result["error"] is not None
    assert result["file_count"] == 0


def test_describe_folder_empty_string_returns_error():
    result = folder_info.describe_folder("")
    assert result["error"] is not None
    assert result["file_count"] == 0


def test_describe_folder_truncated_when_exceeds_limit(tmp_path):
    # 创建超过 limit 的文件
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    result = folder_info.describe_folder(str(tmp_path), max_files_limit=3)
    assert result["file_count"] == 3
    assert result["truncated"] is True


def test_describe_folder_not_truncated_under_limit(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("x")
    result = folder_info.describe_folder(str(tmp_path), max_files_limit=5)
    assert result["file_count"] == 3
    assert result["truncated"] is False


def test_describe_folder_ext_distribution_sorted_by_count_desc(tmp_path):
    # pdf 出现 3 次，jpg 出现 1 次
    for i in range(3):
        (tmp_path / f"p{i}.pdf").write_text("x")
    (tmp_path / "j.jpg").write_text("y")
    result = folder_info.describe_folder(str(tmp_path))
    # pdf 在前
    assert result["ext_distribution"][0] == ("pdf", 3)
    assert result["ext_distribution"][1] == ("jpg", 1)


def test_describe_folder_ext_distribution_tie_sorted_alphabetically(tmp_path):
    # 同 count 时按扩展名字母序
    (tmp_path / "z.zzz").write_text("x")
    (tmp_path / "a.aaa").write_text("x")
    (tmp_path / "m.mmm").write_text("x")
    result = folder_info.describe_folder(str(tmp_path))
    exts = [e for e, _ in result["ext_distribution"]]
    assert exts == ["aaa", "mmm", "zzz"]


def test_describe_folder_symlink_to_dir_does_not_crash(tmp_path):
    # 软链接文件夹（os.walk 默认不跟随 symlink）
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.txt").write_text("a")
    link = tmp_path / "link"
    try:
        os.symlink(str(tmp_path / "real"), str(link), target_is_directory=True)
    except OSError:
        # Windows 无权限创建软链接则跳过
        return
    result = folder_info.describe_folder(str(tmp_path))
    # os.walk 默认不跟随 symlink，只扫描真实文件夹
    assert result["error"] is None


# ── format_ext_distribution ──

def test_format_ext_distribution_empty_returns_no_files():
    assert folder_info.format_ext_distribution([]) == "（无文件）"


def test_format_ext_distribution_basic():
    dist = [("jpg", 12), ("pdf", 3), ("txt", 1)]
    text = folder_info.format_ext_distribution(dist)
    assert "jpg: 12" in text
    assert "pdf: 3" in text
    assert "txt: 1" in text


def test_format_ext_distribution_top_n_limit():
    dist = [(f"e{i}", 1) for i in range(15)]
    text = folder_info.format_ext_distribution(dist, top_n=10)
    assert "e0: 1" in text
    assert "e9: 1" in text
    assert "等 15 种" in text
    # e10+ 不应出现
    assert "e10: 1" not in text


def test_format_ext_distribution_empty_ext_label():
    dist = [("", 5), ("jpg", 2)]
    text = folder_info.format_ext_distribution(dist)
    assert "无扩展名: 5" in text
    assert "jpg: 2" in text


def test_format_ext_distribution_top_n_default_10():
    dist = [(f"e{i}", 1) for i in range(11)]
    text = folder_info.format_ext_distribution(dist)
    # 默认 top_n=10，第 11 个不显示
    assert "e10: 1" not in text
    assert "等 11 种" in text
