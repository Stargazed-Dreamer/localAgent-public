"""type_descriptor.py — 文件类型描述生成（纯逻辑，无 Qt 依赖）

根据文件名扩展名生成友好的中文类型描述，如"JPG 图片""MP4 视频""PDF 文档"。
作为 QMimeDatabase 的友好描述补充层——QMimeDatabase 返回 MIME 类型（如 "image/jpeg"），
本模块将其转换为用户可读的"JPG 图片"格式。

设计要点：
- 纯 Python，不依赖 PySide6，可独立单元测试
- 文件夹统一返回"文件夹"
- 大小写不敏感：扩展名统一转大写后匹配
- 未知扩展名 fallback 为 "{EXT} 文件"
- 无扩展名 / dotfile（如 .gitignore）返回"文件"

用法:
    from type_descriptor import describe_file_type
    label = describe_file_type("photo.jpg")  # "JPG 图片"
    label = describe_file_type("my_folder", is_dir=True)  # "文件夹"
"""

import os

# 扩展名 → 类型标签后缀的映射
# key 为小写扩展名（含点），value 为类型标签（不含扩展名前缀）
_EXT_LABEL_MAP: dict[str, str] = {
    # 图片
    ".jpg": "图片", ".jpeg": "图片", ".png": "图片", ".gif": "图片",
    ".bmp": "图片", ".webp": "图片", ".svg": "图片", ".tiff": "图片", ".ico": "图片",
    # 视频
    ".mp4": "视频", ".avi": "视频", ".mkv": "视频", ".mov": "视频",
    ".wmv": "视频", ".flv": "视频", ".webm": "视频",
    # 音频
    ".mp3": "音频", ".wav": "音频", ".flac": "音频", ".aac": "音频",
    ".ogg": "音频", ".m4a": "音频",
    # 文档
    ".txt": "文本", ".md": "文本",
    ".pdf": "文档", ".doc": "文档", ".docx": "文档",
    ".ppt": "演示", ".pptx": "演示",
    ".xls": "表格", ".xlsx": "表格",
    # 压缩包
    ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包",
    ".tar": "压缩包", ".gz": "压缩包",
    # 代码
    ".py": "代码", ".js": "代码", ".ts": "代码",
    ".html": "代码", ".css": "代码",
    # 数据
    ".json": "数据", ".xml": "数据",
    # 配置
    ".yaml": "配置", ".yml": "配置", ".toml": "配置", ".ini": "配置",
    # 可执行与系统
    ".exe": "程序", ".msi": "安装包",
    ".bat": "脚本", ".ps1": "脚本",
    ".torrent": "种子",
}


def describe_file_type(filename: str, is_dir: bool = False) -> str:
    """根据文件名生成友好的类型描述

    Args:
        filename: 文件名（仅文件名，不含路径）
        is_dir: 是否是文件夹

    Returns:
        友好的类型描述字符串，如:
        - 文件夹 → "文件夹"
        - photo.jpg → "JPG 图片"
        - movie.mp4 → "MP4 视频"
        - data.xyz → "XYZ 文件"（未知扩展名 fallback）
        - README → "文件"（无扩展名）
        - .gitignore → "文件"（dotfile 视为无扩展名）
    """
    if is_dir:
        return "文件夹"

    # 分割扩展名
    name_part, ext = os.path.splitext(filename)
    ext_lower = ext.lower()

    # 无扩展名 / dotfile（如 .gitignore，name_part 为空）→ "文件"
    if not ext_lower or not name_part:
        return "文件"

    label = _EXT_LABEL_MAP.get(ext_lower)
    if label:
        return f"{ext[1:].upper()} {label}"

    # 未知扩展名 fallback
    return f"{ext[1:].upper()} 文件"
