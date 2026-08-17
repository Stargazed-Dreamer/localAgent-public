"""录制与 agent 辅助系统组件 — agent_guide 路由条目。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。主代码库不直接
import 本模块，删除 workspace/recorder/ 目录后 agent_guide 路由自动注销。

导出接口（主代码库通过约定名称发现）：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目（task_type → 决策摘要）

注：录制系统无 loop 任务（L0 采集由用户手动启动 `python -m workspace.recorder.tools.main`），
故无 LOOP_TASK_DEFS 导出。
"""

# ==================== agent_guide 路由条目 ====================
# 从 server/agent_guide.py 第 1467-1525 行迁移而来（recording.discover + recording.consume）

GUIDE_REGISTRY_ENTRIES = {
    "recording.discover": {
        "skill": "recording",
        "name": "发现录制包",
        "skill_file": "workspace/recorder/SKILL.md",
        "keywords": [
            "录制", "看一下录制", "最近的录制", "列出录制", "发现录制",
            "recording", "recordings list", "录制包",
        ],
        "description": "列出所有录制包摘要（recording_id/时长/finalized/块数/章节），用户说'看一下最近的录制'时先调此路由",
        "memory_key": None,
        "first_action": (
            "直接 from workspace.recorder.consumer import list_recordings; list_recordings()\n"
            "返回每个录制包的摘要（recording_id/created_at/duration_seconds/finalized/block_count/chapter_count/has_transcript）\n"
            "录制包根目录是 workspace/recorder/recordings/{recording_id}/，agent 也可直接读本地文件"
        ),
        "workflow_summary": "1.调 list_recordings() 2.展示 recording_id + 时长 + finalized 给用户选 3.用户选定后路由到 recording.consume",
        "mcp_tools_priority": [],
        "key_pitfalls": [
            "录制包根目录是 workspace/recorder/recordings/{recording_id}/，不要硬编码绝对路径",
            "list 默认按 created_at 倒序（新的在前），用户说'最近的'通常取第一个",
            "未 finalized 的录制包表示用户还未在 L3 编辑器标记就绪，消费时提示用户",
        ],
        "level": "standard",
    },
    "recording.consume": {
        "skill": "recording",
        "name": "消费录制包",
        "skill_file": "workspace/recorder/SKILL.md",
        "keywords": [
            "消费录制", "分析录制", "看录制", "处理录制", "录制总结",
            "录制 转脚本", "录制 蒸馏", "录制 triage", "录制 bug",
            "consume recording", "recording consume",
        ],
        "description": "消费录制包：读 timeline + 按需调 VL，agent 自主决定后续路由（不预设 routes_to）",
        "memory_key": None,
        "first_action": (
            "1. from workspace.recorder.consumer import get_merged_view; get_merged_view(package_path) 读 merge 后的最终视图\n"
            "2. agent 自主决定后续场景（处理 bug/蒸馏方法论/生成自动化脚本/总结等），不预设路由\n"
            "3. 多轮 VL 协议（D057）：Round1 get_merged_view 建立认知 → Round2 select_vl_candidates + build_vl_question + understand_image → Round3 收敛\n"
            "4. 每次 VL 调用后 log_consumption(package_path, 'vl_call', {block_id, question, frame}) 记录统计（D054 无硬限制 + 统计预警）"
        ),
        "workflow_summary": (
            "1.读 merged view 2.agent 自主决定后续 3.多轮 VL 协议（get_merged_view → select_vl_candidates → understand_image） 4.log_consumption 记录"
        ),
        "mcp_tools_priority": [
            "localagent_understand_image (VL 看图，必带 question 参数)",
        ],
        "key_pitfalls": [
            "不预存 VL 结果到录制包（D005），VL 按需调用，结果只在会话内存中",
            "consumption.log 只记录 VL 调用元信息（block_id/question/frame/timestamp），不记录 VL 返回内容",
            "VL 调用次数推荐上限 15-30 次/次消费（D054 统计预警，不 enforce）",
            "录制包不压缩本地直读（D009），不要尝试 zip 解压",
            "未 finalized 的录制包表示用户还未标记就绪，消费前提示用户先在 L3 编辑器标记",
            "get_merged_view 已自动过滤 is_trimmed 块；如需看被裁剪的块读原始 timeline.json",
            "supplements 路径在 get_block_detail 中已解析为绝对路径，直接用 understand_image 调 VL",
        ],
        "level": "full",
    },
}
