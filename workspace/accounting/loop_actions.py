"""accounting 组件 — 可选 agent_guide 条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/accounting/ 目录后主代码库仍正常工作。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）
"""

GUIDE_REGISTRY_ENTRIES: dict = {
    "recurring.accounting": {
        "skill": "accounting",
        "name": "记账",
        "skill_file": "workspace/accounting/SKILL.md",
        "keywords": ["记账", "帮我记账", "识别账单", "整理账单", "账单", "微信支付账单", "支付宝账单"],
        "description": "账单识别→GUI审核→导出Obsidian",
        "memory_key": "accounting",
        "first_action": "读取 memory_get('accounting') 了解上次进度；提示用户打开客户端 GUI（python -m client.main）→ 💰 记账 tab；检查 workspace/accounting/账单/ 是否有新文件",
        "workflow_summary": "1.导入账单(GUI 📥) 2.审核分类(GUI ✏️) 3.导出Obsidian md(GUI 📤) 4.git commit(可选)",
        "mcp_tools_priority": [
            "memory_get('accounting')",
            "exec_python (检查 workspace/accounting/账单/ 目录是否有新文件)",
            "exec_python (检查 data/accounting.db 是否存在、最近导入批次)",
        ],
        "key_pitfalls": [
            "GUI 面板不走后端 HTTP，直读 SQLite（data/accounting.db）+ 直读 config.toml",
            "退款与原支出对冲（如 5电费-2.66电费）",
            "映射表/审核配置已迁移到 SQLite 表（store.py 管理，旧 .json 首次启动自动迁移）",
            "旧 Web 服务（accounting_server.py 端口 8780）保留为 legacy，不推荐",
        ],
        "prerequisites": ["客户端 GUI 运行中（python -m client.main）", "workspace/accounting/账单/ 有账单文件"],
        "level": "full",
    },
}
