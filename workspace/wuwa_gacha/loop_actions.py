"""wuwa_gacha 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/wuwa_gacha/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
"""

GUIDE_REGISTRY_ENTRIES: dict = {
    "recurring.wuwa_gacha": {
        "skill": "wuwa_gacha",
        "name": "鸣潮抽卡记录",
        "skill_file": "workspace/wuwa_gacha/SKILL.md",
        # 移除跨 skill 重复的单独"鸣潮"/"抽卡"（与其他抽卡 skill 冲突）
        # 保留组合词"鸣潮抽卡"/"鸣潮唤取记录"等
        "keywords": ["鸣潮抽卡", "更新鸣潮卡池", "鸣潮抽卡记录", "鸣潮唤取记录"],
        "description": "日志解密/内存扫描+API采集",
        "memory_key": "wuwa_gacha",
        "first_action": "读取 memory_get('wuwa_gacha')；确认游戏已打开唤取记录页面",
        "workflow_summary": "1.日志解密提取URL（优先） 2.失败回退内存扫描 3.调API采集 4.增量合并",
        "mcp_tools_priority": [
            "memory_get('wuwa_gacha')",
            "exec_python (workspace/wuwa_gacha/wuwa_gacha.py)",
        ],
        "key_pitfalls": [
            "【浏览器操作】先 browser_match_site(domain='kurogames.com') 查站点经验（强制，避免重复踩坑）",
            "API必须用POST方法，参数名是languageCode不是languageType",
            "日志解密：Scheme A（奇数位XOR 0xA5/偶数位XOR 0xEF）/ Scheme B（XOR 0x55）",
            "URL有效期约1小时，过期需重新打开唤取记录",
            "内存扫描需管理员权限",
        ],
        "level": "standard",
    },
}
