"""disk_manager 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/disk_manager/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
"""

GUIDE_REGISTRY_ENTRIES: dict = {
    "adhoc.disk_cleanup": {
        "skill": "disk_manager",
        "name": "磁盘管理",
        "skill_file": "workspace/disk_manager/SKILL.md",
        # 移除单独"磁盘"（太泛，用户说"磁盘"可能指很多场景）
        # 已有"清理硬盘"/"磁盘清理"/"空间不足"覆盖磁盘管理场景
        "keywords": ["清理硬盘", "磁盘清理", "空间不足", "备份系统", "环境备份", "游戏存档备份", "SpaceSniffer", "sns快照"],
        "description": "空间扫描+快照导入+清理建议+全面备份",
        "memory_key": "disk_manager",
        "first_action": "读取 memory_get('disk_manager')；询问用户要清理还是备份。清理：有 SpaceSniffer .sns 快照则先 parse_sns.py 导入（等效全盘 scan、零磁盘 IO），否则走 scan→drill→find→dry-run→delete 闭环",
        "workflow_summary": "清理：0.有 .sns 快照先 parse_sns.py 导入（--cache 复用后续查询） 1.scan 紧凑tree 2.drill/find 下钻定位 3.dry-run 出回执 4.用户勾选 5.delete 走回收站 | 备份：L1文件+L2环境+L3恢复",
        "mcp_tools_priority": [
            "memory_get('disk_manager')",
            "exec_python (parse_sns.py parse <快照.sns> --json — 有 SpaceSniffer 快照时优先，等效全盘 scan)",
            "exec_python (scan_disk.py scan/tree/files/find/caches --json，快照导入后全部 --cache 复用)",
            "exec_python (cleanup.py --dry-run --json → --delete --items <ids> --json)",
        ],
        "key_pitfalls": [
            "铁律：所有删除/移动操作必须用户审核确认，绝不擅自操作",
            "清理前先 --dry-run 出逐项回执，用 AskUserQuestion 让用户勾选，再 --delete --items <ids>",
            "scan_disk.py 子命令：scan/tree/files/find/caches/dups/resume，旧 --overview/--deep/--large/drill 已移除",
            ".sns 快照导入失败（截断/栈不平衡报错）说明快照损坏，不要改用部分数据",
            "删除走 ctypes 回收站（可恢复），--delete 必须基于 dry-run 回执的 item id",
        ],
        "prerequisites": ["后端运行中"],
        "level": "full",
    },
}
