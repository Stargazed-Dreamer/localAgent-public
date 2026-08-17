"""arknights_gacha 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/arknights_gacha/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
"""

GUIDE_REGISTRY_ENTRIES: dict = {
    "recurring.arknights_gacha": {
        "skill": "arknights_gacha",
        "name": "明日方舟寻访",
        "skill_file": "workspace/arknights_gacha/SKILL.md",
        # 移除跨 skill 重复的单独"抽卡"/"寻访"（与其他抽卡 skill 冲突）
        # 保留"明日方舟"（完整游戏名，不会与其他 skill 冲突）和组合词
        "keywords": ["明日方舟抽卡", "方舟寻访", "更新方舟记录", "arknights抽卡", "方舟抽卡统计", "导入小黑盒数据", "明日方舟"],
        "description": "官网/小黑盒数据采集",
        "memory_key": "arknights_gacha",
        "first_action": "读取 memory_get('arknights_gacha')；确认采集方式（官网/小黑盒）",
        "workflow_summary": "1.官网采集(90天)或小黑盒导入(全量) 2.增量合并 3.更新卡池注册表",
        "mcp_tools_priority": [
            "memory_get('arknights_gacha')",
            "exec_python (workspace/arknights_gacha/arknights_gacha.py / workspace/arknights_gacha/arknights_import.py)",
        ],
        "key_pitfalls": [
            "【浏览器操作】先 browser_match_site(domain='bwiki.rs') / browser_match_site(domain='xiaoheihe.cn') 查站点经验——xiaoheihe.cn 已记录（强制，避免重复踩坑）",
            "v4 用 page.route 把 size=10 改为 size=50，减少翻页次数",
            "v4 用户手动登录，不再自动填写",
            "API稀有度2-5对应★3-★6",
            "不直接调API（浏览器内fetch不带认证）",
        ],
        "level": "standard",
    },
}
