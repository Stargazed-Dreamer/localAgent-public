"""endfield_gacha 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/endfield_gacha/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
"""

GUIDE_REGISTRY_ENTRIES: dict = {
    "recurring.endfield_gacha": {
        "skill": "endfield_gacha",
        "name": "终末地寻访",
        "skill_file": "workspace/endfield_gacha/SKILL.md",
        # 移除跨 skill 重复的单独"抽卡"/"寻访"/"终末地"（4 个抽卡 skill 都有"抽卡"会同时命中）
        # 保留组合词"终末地抽卡"/"终末地寻访"等，确保用户说"终末地抽卡"时只命中本 skill
        "keywords": ["终末地抽卡", "终末地寻访", "endfield抽卡", "终末地抽卡记录", "终末地寻访记录", "更新终末地记录"],
        "description": "游戏日志/API采集",
        "memory_key": "endfield_gacha",
        "first_action": "读取 memory_get('endfield_gacha')；优先读游戏日志 HGWebview.log",
        "workflow_summary": "1.读日志提取u8_token(优先) 2.失败回退浏览器登录 3.调API采集 4.增量合并",
        "mcp_tools_priority": [
            "memory_get('endfield_gacha')",
            "exec_python (workspace/endfield_gacha/endfield_gacha.py)",
        ],
        "key_pitfalls": [
            "【浏览器操作】回退浏览器登录时先 browser_match_site(domain='hypergryph.com') / browser_match_site(domain='gryphline.com') 查站点经验（强制，避免重复踩坑）",
            "优先读游戏日志获取 u8_token（无需浏览器）",
            "不需要模拟翻页，拿到 token 后直接调 API",
            "支持国服(hypergryph)和国际服(gryphline)",
            "稀有度4-6对应★4-★6",
        ],
        "level": "standard",
    },
}
