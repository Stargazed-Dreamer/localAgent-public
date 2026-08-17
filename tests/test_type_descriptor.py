"""type_descriptor 单元测试（Seam 2）

测试文件类型描述纯逻辑。参考 tests/test_release_policy.py 和 test_state_manager.py
的纯逻辑测试模式。

type_descriptor 位于 tools/file_classifier/（非标准包），用 importlib.util 加载，
与 test_state_manager.py 加载 state_manager.py 的方式一致。
"""

import importlib.util
from pathlib import Path

import pytest

# 动态加载 type_descriptor 模块
_TYPE_DESCRIPTOR_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "file_classifier"
    / "type_descriptor.py"
)
_spec = importlib.util.spec_from_file_location("type_descriptor", _TYPE_DESCRIPTOR_PATH)
type_descriptor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(type_descriptor)
describe_file_type = type_descriptor.describe_file_type


# ==================== Slice 1: 文件夹 ====================

def test_directory_returns_folder_description():
    """文件夹返回'文件夹'描述"""
    assert describe_file_type("任意名称", is_dir=True) == "文件夹"


def test_directory_ignores_filename():
    """文件夹描述不依赖文件名"""
    assert describe_file_type("my_photos", is_dir=True) == "文件夹"
    assert describe_file_type("ESJZone-novel-mirror-main", is_dir=True) == "文件夹"


# ==================== Slice 2: 图片类型 ====================

@pytest.mark.parametrize("ext", [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".tiff", ".ico"])
def test_image_extensions_return_image_description(ext):
    """常见图片扩展名返回'图片'描述"""
    assert describe_file_type(f"photo{ext}") == f"{ext[1:].upper()} 图片"


def test_jpg_uppercase_extension():
    """大写扩展名也能识别"""
    assert describe_file_type("photo.JPG") == "JPG 图片"


# ==================== Slice 3: 视频类型 ====================

@pytest.mark.parametrize("ext", [".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm"])
def test_video_extensions_return_video_description(ext):
    """常见视频扩展名返回'视频'描述"""
    assert describe_file_type(f"movie{ext}") == f"{ext[1:].upper()} 视频"


# ==================== Slice 4: 音频类型 ====================

@pytest.mark.parametrize("ext", [".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"])
def test_audio_extensions_return_audio_description(ext):
    """常见音频扩展名返回'音频'描述"""
    assert describe_file_type(f"song{ext}") == f"{ext[1:].upper()} 音频"


# ==================== Slice 5: 文档类型 ====================

@pytest.mark.parametrize("ext,expected_label", [
    (".txt", "TXT 文本"),
    (".md", "MD 文本"),
    (".pdf", "PDF 文档"),
    (".doc", "DOC 文档"),
    (".docx", "DOCX 文档"),
    (".ppt", "PPT 演示"),
    (".pptx", "PPTX 演示"),
    (".xls", "XLS 表格"),
    (".xlsx", "XLSX 表格"),
])
def test_document_extensions_return_document_description(ext, expected_label):
    """常见文档扩展名返回对应描述"""
    assert describe_file_type(f"document{ext}") == expected_label


# ==================== Slice 6: 压缩包类型 ====================

@pytest.mark.parametrize("ext", [".zip", ".rar", ".7z", ".tar", ".gz"])
def test_archive_extensions_return_archive_description(ext):
    """常见压缩包扩展名返回'压缩包'描述"""
    assert describe_file_type(f"archive{ext}") == f"{ext[1:].upper()} 压缩包"


# ==================== Slice 7: 代码与配置类型 ====================

@pytest.mark.parametrize("ext,expected_label", [
    (".py", "PY 代码"),
    (".js", "JS 代码"),
    (".ts", "TS 代码"),
    (".html", "HTML 代码"),
    (".css", "CSS 代码"),
    (".json", "JSON 数据"),
    (".xml", "XML 数据"),
    (".yaml", "YAML 配置"),
    (".yml", "YML 配置"),
    (".toml", "TOML 配置"),
    (".ini", "INI 配置"),
])
def test_code_and_config_extensions_return_description(ext, expected_label):
    """代码与配置文件扩展名返回对应描述"""
    assert describe_file_type(f"file{ext}") == expected_label


# ==================== Slice 8: 可执行与系统文件 ====================

@pytest.mark.parametrize("ext,expected_label", [
    (".exe", "EXE 程序"),
    (".msi", "MSI 安装包"),
    (".bat", "BAT 脚本"),
    (".ps1", "PS1 脚本"),
    (".torrent", "TORRENT 种子"),
])
def test_executable_extensions_return_description(ext, expected_label):
    """可执行与系统文件扩展名返回对应描述"""
    assert describe_file_type(f"file{ext}") == expected_label


# ==================== Slice 9: 未知扩展名与无扩展名 ====================

def test_unknown_extension_returns_generic_with_ext():
    """未知扩展名返回 '{EXT} 文件'"""
    assert describe_file_type("data.xyz") == "XYZ 文件"


def test_no_extension_returns_file():
    """无扩展名返回'文件'"""
    assert describe_file_type("README") == "文件"


def test_empty_filename_returns_file():
    """空文件名返回'文件'"""
    assert describe_file_type("") == "文件"


def test_only_extension_returns_file():
    """只有扩展名（如 .gitignore）返回'文件'"""
    # .gitignore 这种 dotfile 在 os.path.splitext 下 ext=".gitignore", name=""
    # 但语义上应该当作无扩展名文件
    result = describe_file_type(".gitignore")
    assert result == "文件"


# ==================== Slice 10: 大小写不敏感 ====================

def test_mixed_case_extension():
    """混合大小写扩展名规范化为大写"""
    assert describe_file_type("photo.JpG") == "JPG 图片"
    assert describe_file_type("movie.MP4") == "MP4 视频"


# ==================== Slice 11: 复杂文件名 ====================

def test_filename_with_multiple_dots():
    """文件名含多个点，取最后一个扩展名"""
    assert describe_file_type("archive.tar.gz") == "GZ 压缩包"


def test_filename_with_chinese():
    """中文文件名正常工作"""
    assert describe_file_type("达妮娅.mp4") == "MP4 视频"
    assert describe_file_type("画师推荐.jpg") == "JPG 图片"


def test_filename_with_spaces():
    """文件名含空格正常工作"""
    assert describe_file_type("my photo.png") == "PNG 图片"
