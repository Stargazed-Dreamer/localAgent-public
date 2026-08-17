"""folder_info.py — 文件夹信息聚合（纯逻辑，无 Qt 依赖）

用于 DetailsDialog 显示文件夹详情：
- file_count: 内部文件总数（递归）
- ext_distribution: 扩展名分布 {ext_lower: count}，按 count 降序
- max_depth: 最大嵌套深度（根目录=0，子目录=1，依此类推）
- total_size: 内部文件总字节数

设计为纯逻辑模块便于单元测试（参考 type_descriptor.py 的设计决策）。
"""

import os
from collections import Counter


def describe_folder(path: str, max_files_limit: int = 5000) -> dict:
    """聚合文件夹信息

    Args:
        path: 文件夹路径
        max_files_limit: 累计文件数硬上限，防止扫描超大目录卡死 GUI（默认 5000）

    Returns:
        dict: {
            "file_count": int,
            "ext_distribution": list[tuple[str, int]],  # [("jpg", 12), ("pdf", 3), ...] 降序
            "max_depth": int,
            "total_size": int,
            "truncated": bool,  # 是否因达上限截断
            "error": str | None,  # 扫描异常时填入
        }
    """
    result = {
        "file_count": 0,
        "ext_distribution": [],
        "max_depth": 0,
        "total_size": 0,
        "truncated": False,
        "error": None,
    }
    if not path or not os.path.isdir(path):
        result["error"] = "路径不是文件夹或不存在"
        return result

    ext_counter = Counter()
    try:
        for root, dirs, filenames in os.walk(path):
            # 计算当前深度：相对根路径的层数
            rel = os.path.relpath(root, path)
            if rel == ".":
                depth = 0
            else:
                depth = rel.count(os.sep) + 1
            if depth > result["max_depth"]:
                result["max_depth"] = depth
            for fname in filenames:
                if result["file_count"] >= max_files_limit:
                    result["truncated"] = True
                    break
                result["file_count"] += 1
                # 扩展名分布（小写，无点）
                _, ext = os.path.splitext(fname)
                ext_lower = ext.lower().lstrip(".")
                # 空扩展名单独统计为 ""
                ext_counter[ext_lower] += 1
                # 累计大小
                try:
                    fpath = os.path.join(root, fname)
                    result["total_size"] += os.path.getsize(fpath)
                except OSError:
                    pass
            if result["truncated"]:
                break
    except OSError as e:
        result["error"] = str(e)

    # 扩展名分布转 list[tuple] 降序（count 降序，同 count 按扩展名字母序）
    result["ext_distribution"] = sorted(
        ext_counter.items(), key=lambda x: (-x[1], x[0])
    )
    return result


def format_ext_distribution(distribution: list, top_n: int = 10) -> str:
    """格式化扩展名分布为友好字符串

    Args:
        distribution: describe_folder 返回的 ext_distribution 列表
        top_n: 最多显示前 N 个扩展名（默认 10）

    Returns:
        "jpg: 12, pdf: 3, txt: 1" 或 "（无文件）"
    """
    if not distribution:
        return "（无文件）"
    top = distribution[:top_n]
    parts = []
    for ext, count in top:
        # 空扩展名显示为 "无扩展名"
        label = ext if ext else "无扩展名"
        parts.append(f"{label}: {count}")
    text = ", ".join(parts)
    if len(distribution) > top_n:
        text += f" 等 {len(distribution)} 种"
    return text
