"""accounting 组件健康检查

由 health.py 通过 manifest [health_check] 段动态发现并调用。
删除 workspace/accounting/ 后此文件消失，health 自动跳过。
"""
from __future__ import annotations

import json
from pathlib import Path


def get_status() -> dict:
    """记账审核状态

    检查审核数据文件是否存在，不依赖 server.accounting 模块。
    accounting 服务是否运行需通过 http://127.0.0.1:8780/health 检查。
    """
    # bill_review_data.json 是私有产出，已挪到 private_vault/accounting/（obsidian vault，不进 release）
    # health.py 自己仍在 workspace/accounting/，所以从 __file__ 推算项目根再进 private_vault
    project_dir = Path(__file__).parent.parent.parent
    vault_dir = project_dir / "private_vault" / "accounting"
    review_json_path = vault_dir / "bill_review_data.json"
    review_data = None
    if review_json_path.exists():
        try:
            with open(review_json_path, encoding="utf-8") as f:
                review_data = json.load(f)
        except Exception:
            pass
    return {
        "review_data_available": review_data is not None,
        "item_count": len(review_data.get("items", [])) if review_data else 0,
    }
