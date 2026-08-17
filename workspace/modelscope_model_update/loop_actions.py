"""modelscope_model_update 组件 — ModelScope 模型库更新。

本模块由主代码库的 _load_optional_guide_entries() 动态发现并加载。
主代码库不直接 import 本模块，删除 workspace/modelscope_model_update/ 目录后
主代码库仍正常工作（自动跳过注册）。

导出接口：
    - GUIDE_REGISTRY_ENTRIES: dict, agent guide 条目
"""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

GUIDE_REGISTRY_ENTRIES = {
    "adhoc.modelscope_model_update": {
        "skill": "modelscope_model_update",
        "name": "ModelScope 模型库更新",
        "skill_file": "workspace/modelscope_model_update/SKILL.md",
        "keywords": ["更新modelscope", "更新魔搭模型", "modelscope模型", "前沿模型清单", "项目缺什么模型", "扫描魔搭", "魔搭社区", "魔搭"],
        "description": "扫描ModelScope前沿模型+榜单评估+更新keys.json",
        "memory_key": "modelscope_model_update",
        "first_action": "读取 memory_get('modelscope_model_update')；读取 data/llm/keys.json 按 scope 聚合当前模型；AskUserQuestion 问用户本次更新哪些方向（LLM/VL/aigc_image/aigc_video 可写入；TTS/ASR 仅记录）",
        "workflow_summary": "1.读keys.json 2.问方向 3.CDP扫描ModelScope 4.验证魔搭社区API源 5.榜单评估 6.推荐报告(TTS/ASR标注仅记录) 7.用户确认(仅4个可写入方向) 8.写keys.json",
        "mcp_tools_priority": [
            "memory_get('modelscope_model_update')",
            "exec_python (读取 data/llm/keys.json)",
            "browser_list_tabs / browser_open",
            "exec_python (Playwright connect_over_cdp)",
            "WebFetch (artificialanalysis.ai / superclueai.com)",
            "RunCommand (启动 clash_verge，仅 arena.ai 需要，必须先问用户)",
            "AskUserQuestion (用户确认采纳清单)",
            "exec_python (写入 keys.json + memory_set)",
        ],
        "key_pitfalls": [
            "【浏览器操作】先查 .agents/skills/browser_lessons/ 看 modelscope.cn 是否有 sites 文件（强制，避免重复踩坑）",
            "API 来源必须含'魔搭社区'标签，仅显示 OpenAI/Anthropic 等外部源的一律跳过",
            "arena.ai 需梯子，启动 clash_verge 前必须 AskUserQuestion 询问用户许可",
            "阶段 1-7 只读 keys.json，绝不擅自修改；阶段 8 写入前必须用户 AskUserQuestion 确认",
            "图片生成走异步任务（POST /v1/images/generations + GET /v1/tasks/{task_id}），与 LLM 同步调用不同",
            "TTS/ASR 方向仅扫描记录在推荐报告中，不进入阶段 7 确认、不写入 keys.json（项目当前无 use_case，未来用到再说）",
            "ModelScope 网站选择器为 2026-07 探索结果（acss-17aobl4 / acss-b90rmo / acss-d9h0gd），改版失效时重新探索",
        ],
        "prerequisites": ["调试浏览器已启动（CDP 9222）", "data/llm/keys.json 存在"],
        "level": "full",
    },
}
