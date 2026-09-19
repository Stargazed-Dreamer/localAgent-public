"""文件类型分类：扩展名 → 类型类别 → 配色。

复刻 SpaceSniffer 的「按文件类型着色」：方块图里每种类型一个颜色，配一个图例。
类别集合是自觉收窄的 12 类 —— 再细分颜色就不够用了（暗底上超过一打颜色会糊成
一片，反而看不出分布）。分类覆盖不到的扩展名一律落到 `other`。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from lib.ui import tokens

OTHER = "other"

# --- 类别中文名（图例、详情面板、类型统计都用它） ---------------------------
CATEGORY_LABELS: Dict[str, str] = {
    "video": "视频",
    "audio": "音频",
    "image": "图片",
    "document": "文档",
    "archive": "压缩包",
    "executable": "可执行",
    "system": "系统文件",
    "code": "代码",
    "font": "字体",
    "database": "数据库",
    "disk_image": "磁盘镜像",
    "other": "其他",
}

# 图例展示顺序：按「用户关心程度」而非字母序
CATEGORY_ORDER: List[str] = [
    "video",
    "audio",
    "image",
    "document",
    "archive",
    "executable",
    "disk_image",
    "database",
    "code",
    "font",
    "system",
    "other",
]

_EXT_GROUPS: Dict[str, Tuple[str, ...]] = {
    "video": (
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
        "rmvb", "rm", "3gp", "vob", "m2ts", "ogv", "divx", "asf", "f4v", "mts",
    ),
    "audio": (
        "mp3", "flac", "wav", "aac", "ogg", "m4a", "wma", "ape", "opus", "aiff",
        "alac", "mid", "midi", "amr", "cda", "mka",
    ),
    "image": (
        "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "svg", "ico",
        "heic", "heif", "raw", "cr2", "nef", "arw", "dng", "psd", "ai", "xcf",
        "avif", "jfif", "tga", "exr", "dds",
    ),
    "document": (
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf",
        "odt", "ods", "odp", "csv", "tsv", "epub", "mobi", "azw3", "djvu", "wps",
        "et", "dps", "pages", "numbers", "key", "one", "msg", "eml", "chm",
    ),
    "archive": (
        "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "lz", "lzma", "cab",
        "tgz", "tbz2", "arj", "lzh", "ace", "zipx", "001", "z",
    ),
    "executable": (
        "exe", "msi", "dll", "com", "scr", "bat", "cmd", "ps1", "vbs", "apk",
        "appx", "msix", "jar", "deb", "rpm", "appimage", "dmg", "pkg", "lnk",
        "cpl", "ocx", "drv", "efi", "gadget",
    ),
    "system": (
        "sys", "ini", "inf", "reg", "dat", "log", "tmp", "cat", "mun",
        "manifest", "pol", "etl", "evtx", "pf", "blf", "job", "nls", "mui", "msc",
        "theme", "deskthemepack", "thumbcache", "bak", "old", "swp", "dmp",
    ),
    "code": (
        "py", "pyc", "pyi", "js", "mjs", "cjs", "ts", "tsx", "jsx", "c", "h",
        "cc", "cpp", "hpp", "cxx", "hxx", "cs", "java", "class", "go", "rs", "rb",
        "php", "pl", "lua", "sh", "zsh", "swift", "kt", "kts", "scala", "m", "mm",
        "r", "jl", "dart", "vue", "svelte", "html", "htm", "css", "scss", "sass",
        "less", "json", "xml", "yaml", "yml", "toml", "sql", "ipynb", "gradle",
        "cmake", "asm", "v", "sv", "proto", "graphql", "lock", "cfg", "conf",
    ),
    "font": ("ttf", "otf", "woff", "woff2", "eot", "fon", "fnt", "pfb", "pfm", "ttc"),
    "database": (
        "db", "db3", "sqlite", "sqlite3", "mdb", "accdb", "dbf", "mdf", "ldf",
        "idx", "pak", "realm", "wal", "shm", "edb", "nsf", "frm", "ibd", "myi",
        "myd", "rdb", "leveldb", "sst",
    ),
    "disk_image": (
        "iso", "img", "vhd", "vhdx", "vmdk", "vdi", "qcow2", "bin", "cue", "nrg",
        "mds", "wim", "esd", "gho", "tib",
    ),
}


def _build_index() -> Dict[str, str]:
    """扩展名 → 类别。构建期展开成扁平字典 ——

    过滤与类型统计要对几十万条目逐个判类型，这里是热路径，必须 O(1) 查表。
    """
    index: Dict[str, str] = {}
    for category, extensions in _EXT_GROUPS.items():
        for ext in extensions:
            index[ext] = category
    return index


EXT_TO_CATEGORY: Dict[str, str] = _build_index()


def category_of(ext: str) -> str:
    """扩展名 → 类型类别。空扩展名归入 `other`。

    这里**主动归一化**（去前导点 + 转小写）而不是依赖调用方传对：
    `Entry.ext` 本来就是小写，所以这一步在热路径上近乎零成本（CPython 的
    `str.lower()` 对已小写的 ASCII 串直接返回原对象），但能挡掉「直接拿用户
    输入或 CSV 里的 `.JPG` 来查表 → 悄悄落到 other」这类难查的错。
    """
    if not ext:
        return OTHER
    return EXT_TO_CATEGORY.get(ext.lstrip(".").lower(), OTHER)


def color_of(ext: str) -> str:
    """扩展名 → 该类型对应的颜色（hex）。大小写与是否带前导点都不敏感。"""
    return tokens.FILE_TYPE_COLORS[category_of(ext)]


def color_of_category(category: str) -> str:
    return tokens.FILE_TYPE_COLORS.get(category, tokens.FILE_TYPE_OTHER)


def label_of_category(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)


def legend() -> List[Tuple[str, str, str]]:
    """类型图例数据：[(category, 中文名, 颜色)]，按 `CATEGORY_ORDER`。"""
    return [
        (cat, CATEGORY_LABELS[cat], tokens.FILE_TYPE_COLORS[cat])
        for cat in CATEGORY_ORDER
    ]


def sub_extensions(category: str, limit: int = 12) -> List[str]:
    """某类别下的代表扩展名（详情面板「该类型包含哪些后缀」提示用）。

    按 `_EXT_GROUPS` 里的**声明顺序**取，不排序 —— 声明时就是按常见程度写的
    （image 里 jpg/png 在前），按字母序会变成 ai/arw/avif 打头，那就不叫「代表」了。
    """
    return list(_EXT_GROUPS.get(category, ()))[:limit]
