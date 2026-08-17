"""T3 重点标注器（spec-l2.md 第三节 / D043）

初始化空 annotations.json。L2 只初始化不标注（标注由 L3 编辑器做）。

annotations.json 格式：
    {
        "annotations": []
    }

已存在 annotations.json 时不覆盖（保留 L3 编辑器的标注）。
"""

import json
from pathlib import Path


def init_annotations(package_path: Path | str) -> Path:
    """初始化空 annotations.json。

    如果 annotations.json 已存在则不覆盖（保留 L3 编辑器的标注）。
    如果不存在则创建空文件（{"annotations": []}）。

    Args:
        package_path: 录制包根目录路径

    Returns:
        annotations.json 文件路径
    """
    package_root = Path(package_path)
    annotations_path = package_root / "annotations.json"

    if annotations_path.exists():
        return annotations_path  # 不覆盖

    annotations_path.write_text(
        json.dumps({"annotations": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return annotations_path
