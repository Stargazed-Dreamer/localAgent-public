"""yihuan_gacha 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/yihuan_gacha/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
"""

GUIDE_REGISTRY_ENTRIES: dict = {
    "recurring.yihuan_gacha": {
        "skill": "yihuan_gacha",
        "name": "异环抽卡记录",
        "skill_file": "workspace/yihuan_gacha/SKILL.md",
        # 移除跨 skill 重复的单独"抽卡"/"异环"（与其他抽卡 skill + yihuan_simulator 冲突）
        # "抽卡记录"太泛（所有游戏都有），改为"异环抽卡记录"
        # 保留"汇总抽卡"/"读取棋盘"（异环特有机制）
        "keywords": ["异环抽卡记录", "异环抽卡", "汇总抽卡", "读取棋盘"],
        "description": "截图OCR采集+清洗归档",
        "memory_key": "yihuan_gacha",
        "first_action": "读取 memory_get('yihuan_gacha')；确认异环游戏窗口可截图",
        "workflow_summary": "1.截图异环窗口+OCR 2.翻页采集 3.清洗标准化 4.审核 5.归档",
        "mcp_tools_priority": [
            "memory_get('yihuan_gacha')",
            "capture_screen (window模式)",
            "ocr_path",
            "exec_python (workspace/yihuan_gacha/yihuan_gacha.py / workspace/yihuan_gacha/yihuan_clean.py)",
        ],
        "key_pitfalls": [
            "原始数据永不覆盖",
            "不做去重（十连抽同一秒多条相同记录是合法数据）",
            "清洗不是一劳永逸，新道具/角色需更新映射表",
        ],
        "level": "standard",
    },
}
