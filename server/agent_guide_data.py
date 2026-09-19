"""Agent Guide 数据模块 — 纯配置数据，无副作用。

由 server/agent_guide.py 单向 import。包含：
- GUIDE_REGISTRY: 内置 skill 的路由元数据（task_type → entry）
- _DEV_ROLES: dev 桶 skill 的 role 分层映射（entry/method/support/tool）
- _DEV_ENTRY_POINTS: dev 桶入口决策树（按目标清晰度选入口）
- GENERAL_GUIDE: GeneralGuide 响应的通用指导内容

本文件是 agent_guide.py Step 1 拆分产物，将 ~1540 行配置数据从 2008 行的主文件中分离，
使主文件只保留业务逻辑+路由（~470 行）。API 零变化：GUIDE_REGISTRY 等符号在 agent_guide.py
中重新导出，现有 `from server.agent_guide import GUIDE_REGISTRY` 的测试无需修改。

注意：workspace 可选组件的 GUIDE_REGISTRY 条目由 agent_guide.py 的 _load_optional_guide_entries()
在运行时通过 GUIDE_REGISTRY.update() 原地合并，本文件只含内置静态条目。
"""

# ========== GUIDE_REGISTRY：内置 skill 的路由元数据（运行时合并可选组件条目） ==========

GUIDE_REGISTRY: dict[str, dict] = {
    # ────────────── recurring（周期循环任务）──────────────
    # ────────────── 组件化模块（recurring.accounting / community_review / niuke_review /
    #                yihuan_gacha / wuwa_gacha / arknights_gacha / endfield_gacha /
    #                bilibili_gacha / official_gacha）已迁移到 workspace/<module>/loop_actions.py，
    #                通过 _load_optional_guide_entries() 自动加载，删除 workspace/<module>/
    #                后条目自动注销。详见各模块 manifest.toml。

    # ────────────── recurring.daily_summary（今日工作总结，依赖 Loop 系统采集数据）──────────────
    "recurring.daily_summary": {
        "skill": "daily_summary",
        "name": "今日工作总结",
        "skill_file": ".agents/skills/daily_summary.md",
        "keywords": ["今日总结", "工作总结", "今天干了什么", "给我今日工作总结", "日报", "今日工作总结", "时间线", "活动总结", "今日活动"],
        "description": "读取本日 hourly 汇总 → 生成当日日志",
        "first_action": "确定'今天'的日期：当前时间≥05:00 用当天日期，<05:00 用前一天日期（如 07-14 04:00 请求→今天=07-13）。立即读取 data/activity/hourly/ 下该日期所有 .md 文件汇总生成，不要等'今天结束'——用户要日报时不会期待更多记录",
        "workflow_summary": "1.确定日期(5am分界) 2.读取 hourly md 3.汇总为日总结 4.写入 private_vault/activity/daily/YYYYMMDD.md 5.提示用户审阅",
        "mcp_tools_priority": [
            "agent_guide(recurring.daily_summary)",
            "exec_python (读 data/activity/hourly/*.md)",
            "exec_python (写 private_vault/activity/daily/YYYYMMDD.md)",
        ],
        "key_pitfalls": [
            "'今天'=上一个05:00到当前时刻，不是等下一个05:00。用户要日报时立即生成，不要因'今天没结束'推迟或询问",
            "日期划分按05:00：当前<05:00时日报文件名用前一天日期(如07-14 04:00→20260713.md)",
            "数据源优先级：先读 hourly md（本源）→ git log 补缺失时段（--since 用 05:00）→ memory 仅参考。不要先查 git log",
            "不要在日报中加入 agent 的 task_closure 工作报告——日报是用户活动记录，不是 agent 收尾报告",
            "hourly 文件可能不全（后端未全程运行），需在日总结中标注缺失时段",
            "日总结文件已存在时不覆盖，追加 _v2 后缀",
            "若 data/activity/hourly/ 目录不存在或今日无文件，说明 Loop activity_tracker 未运行，应提示用户检查 [loops.activity_tracker] 配置",
        ],
        "level": "standard",
    },

    # ────────────── recurring.life_design（斯坦福人生设计课，每季度对话型任务）──────────────
    "recurring.life_design": {
        "skill": "life_design",
        "name": "人生设计",
        "skill_file": ".agents/skills/daily/life_design/SKILL.md",
        "keywords": [
            "人生设计", "斯坦福人生设计课", "人生蓝图", "奥德赛计划", "重启人生",
            "人生规划", "life design", "设计我的人生", "人生设计师", "重力问题",
        ],
        "description": "斯坦福人生设计课方法论：agent 读 prompt 与用户多轮深度对话，产出《个人人生设计蓝图》（8000-12000字）",
        "memory_key": "life_design",
        "first_action": "【被动展示型任务】此任务到期后只在 client DueTodos 面板展示，agent 不主动开启、正常对话中不理会；用户主动说'做人生设计'时才执行。执行时：读 workspace/life_design/prompt.txt 获取完整角色设定和对话规则（必读，不要凭记忆复述）；按 prompt 末尾'开始'节的语气向用户问好并进入第一阶段第一题；每轮只问一个主问题，苏格拉底式追问；每轮对话用 Edit 追加到 private_vault/life_design/dialogues/YYYYMMDD_dialogue.md；素材足够后产出《个人人生设计蓝图》到 private_vault/life_design/blueprints/YYYYMMDD_blueprint.md；完成后 todos_mark_done",
        "workflow_summary": "1.读prompt 2.按prompt开场 3.多轮对话(6-9主问题,每轮一个) 4.存档对话到private_vault 5.产出蓝图到private_vault 6.todos_mark_done",
        "mcp_tools_priority": [
            "Read (workspace/life_design/prompt.txt)",
            "Edit (private_vault/life_design/dialogues/YYYYMMDD_dialogue.md)",
            "Write (private_vault/life_design/blueprints/YYYYMMDD_blueprint.md)",
            "memory_get (life_design，了解上次设计要点)",
            "memory_set (life_design，写入跨会话复用的结构化洞察)",
            "todos_mark_done (recurring.life_design)",
        ],
        "key_pitfalls": [
            "被动展示型：到期后 agent 不主动开启对话，只在 client DueTodos 面板展示。agent 在正常对话中不主动提及此任务，等用户主动说'做人生设计'才执行",
            "每轮只聚焦一个主问题，禁止一次性抛出所有问题——这是 prompt 的硬规则",
            "苏格拉底式追问：多问当时几岁/具体哪件事/那一刻什么感觉，但追问有度，结束回主线",
            "温暖而犀利：共情+接纳，但捕捉到逻辑漏洞/自我设限/重力问题死磕要敏锐点出",
            "主动分清重力问题（无法改变的现实）和可设计的真问题，这是本 skill 区别于普通生涯咨询的关键动作",
            "不评判用户选择，不替用户做决定",
            "主问题控制在 6-9 个，对话节奏由 agent 掌控",
            "蓝图 8000-12000 字，覆盖：你在这里/真问题/指南针/能量图/三个奥德赛计划/原型行动清单/失败免疫",
            "对话存档和蓝图产出都放 private_vault/life_design/，不进 release，是 obsidian vault 的一部分",
            "反向推演（五年不变周二）可选且需征求同意，用户情绪脆弱时直接跳过",
        ],
        "prerequisites": ["用户愿意进行深度人生对话", "private_vault/ 目录存在", "workspace/life_design/prompt.txt 存在"],
        "level": "standard",
    },

    # ────────────── adhoc（一次性任务）──────────────
    # ────────────── 组件化模块（adhoc.disk_cleanup）已迁移到 workspace/disk_manager/loop_actions.py，
    #                通过 _load_optional_guide_entries() 自动加载，删除 workspace/disk_manager/
    #                后条目自动注销。详见 manifest.toml。

    # ────────────── 组件化模块（exam_prep/mindforge/apikey_test/modelscope_model_update）
    #                仍在 workspace/<module>/loop_actions.py，通过 _load_optional_guide_entries() 自动加载。
    #                deep_research 和 office_docs 已移回 .agents/skills/（纯 skill，非组件）。

    "adhoc.gkd_signin_automation": {
        "skill": "gkd-signin-automation",
        "name": "GKD签到规则自动化",
        "skill_file": ".agents/skills/gkd_signin_automation/SKILL.md",
        "keywords": [
            "GKD签到", "GKD规则", "自动签到", "签到规则", "快照解析", "节点匹配",
            "规则失效", "实机回归", "GKD快照", "selector drift", "gkd sign-in",
        ],
        "description": "GKD Android 签到流程确认、ADB快照采集、节点解析、规则生成、订阅导入和实机回归",
        "memory_key": None,
        "first_action": "读取 .agents/skills/gkd_signin_automation/SKILL.md；由用户确认目标 App 和完整签到路径；检查 adb/GKD 无障碍状态后先做 A-D 可行性分级",
        "workflow_summary": "1.用户确认App与流程 2.可行性分级 3.GKD快照采集+ADB传输 4.节点解析 5.候选规则+静态回放 6.订阅导入 7.日志和业务状态实机回归 8.App更新漂移诊断",
        "mcp_tools_priority": [
            "agent_guide(adhoc.gkd_signin_automation)",
            "exec_cmd (ADB只读检查、reverse和文件传输；删除仍走命令保护)",
            "capture_screen / adb screencap (每次手机动作前后留证)",
            "scripts/collect.py (批量采集)",
            "scripts/inspect_snapshot.py (节点和可点击祖先解析)",
            "scripts/validate_rule.py (规则静态检查和快照回放)",
        ],
        "key_pitfalls": [
            "每日要签哪些 App 和是否执行最终签到由用户决定",
            "完整 id 与同末段 vid 通常是同一节点，写成两个 rule 会双击",
            "签到前后仅 drawable 变化且无障碍属性相同时，GKD 无法判断自然日已签到",
            "无节点且位置随机的关闭图标不适合纯 GKD，转 OCR/视觉 Computer Use 或人工",
            "GKD 订阅列表下拉刷新才会更新 URL 订阅；卡片时钟图标是触发记录",
        ],
        "prerequisites": ["Android USB调试", "adb", "GKD无障碍服务"],
        "level": "standard",
    },

    "adhoc.deep_research": {
        "skill": "deep_research",
        "name": "深度研究",
        "skill_file": ".agents/skills/deep_research/SKILL.md",
        "keywords": [
            "深度研究", "研究一下", "做个研究", "调研一下", "竞品分析",
            "行业研究", "研究一个新行业", "从零研究", "横纵分析", "行业分析",
            "商业模式拆解", "帮我看看这个东西怎么样", "这个产品怎么回事", "这个公司怎么回事",
            "这个行业怎么回事", "帮我摸清楚", "帮我搞懂", "deep research",
        ],
        "description": "横纵分析法+行业研究实战范式，产品/公司/行业/概念/技术/人物深度研究报告（Markdown+PDF）",
        "memory_key": None,
        "first_action": "确认研究对象+类型（行业/公司/产品/概念/技术/人物）+输出格式（md only/md+pdf）；商业对象（行业/公司/商业产品）必读 references/industry_research_toolkit.md 做商业模式拆解；用 Task 工具并行起子 Agent 联网搜集纵向+横向信息；长报告（>1万字）用 LLM 池分段生成",
        "workflow_summary": "1.明确研究对象 2.并行联网搜集(纵向/横向子Agent) 3.纵向分析(起源→演进→当下) 4.横向分析(竞品对比) 4.5.商业对象:商业模式拆解+单位毛利归因 5.横纵交汇洞察 6.生成md报告(可选PDF复用office_pdf)",
        "mcp_tools_priority": [
            "Task (起子Agent并行联网搜集，prompt含WebSearch/WebFetch/arxiv指引)",
            "exec_python (curl arxiv API: export.arxiv.org/api/query)",
            "exec_python (调 office_pdf skill 用 reportlab 现场生成 PDF)",
            "Read (读 references/industry_research_toolkit.md 商业对象必读)",
            "Read (读 references/pdf_report_style.md 仅当需封面页样式时)",
            "/llm/pool/call (长报告分段生成，use_case=deep_research_write)",
        ],
        "key_pitfalls": [
            "必须联网：不能仅靠已有知识，研究报告价值在深度和完整度",
            "商业对象（行业/公司/商业产品）必做商业模式拆解+单位经济模型表，这是行业研究范式的核心增量，纯横纵分析容易浮于叙事",
            "诚实标注暂缺：信息搜不到时标注「该信息暂缺」，绝不编造",
            "一手来源优先：避免多媒体引用同一错误造成循环印证假象；发债主体财务数据比单个公司透明",
            "不重复造PDF脚本：复用office_pdf skill 的方法论，需封面页样式时参考references/pdf_report_style.md现场实现",
            "长报告分段生成：>1万字用LLM池分段（纵向/横向/交汇各一段），避免单次上下文溢出",
            "研究不能只看行业内部，要考虑替代品竞争（如高铁对航空的替代）",
            "用接地气的词替换术语：把'商业模式'说成'靠什么赚钱'更有洞察力",
        ],
        "prerequisites": ["联网能力（WebSearch/WebFetch）", "可选: uv pip install reportlab pypdf pdfplumber（生成PDF时）"],
        "level": "standard",
    },

    "adhoc.readme_author": {
        "skill": "readme_author",
        "name": "README 编写",
        "skill_file": ".agents/skills/readme_author/SKILL.md",
        "keywords": [
            "写 README", "改 README", "README 公开版", "公开版 README", "整理 README",
            "README 太弱", "README 怎么写", "awesome-readme", "公开项目 readme",
            "项目门面", "public readme", "readme 修订",
        ],
        "description": "编写或修订面向公开分享的项目 README，覆盖从零起草和增量修订两种模式，含公开项目特有陷阱（过度承诺、部署门槛伪装、竞品对比翻车）",
        "memory_key": None,
        "first_action": "先 Read 现有 README（如有）+ 项目核心文档（AGENTS.md/CHANGELOG/docs/architecture.md）；用 AskUserQuestion 收集 5 项上下文（项目定位/作者态度/目标读者/部署故事/差异化）；按 7 段结构起草或按增量修订流程改写；写完按反模式清单 + 交付前 checklist 自检",
        "workflow_summary": "0.收集5项上下文 1.tone校准(默认诚实自用型) 2.竞品对比规则(承认竞品更强+说清差异化) 3.快速开始诚实原则(门槛+密钥+troubleshooting) 4.增量修订(先读完整README再改)或从零起草(7段结构) 5.反模式清单自检 6.交付前checklist(30秒测试+诚实度+竞品+部署)",
        "mcp_tools_priority": [
            "Read (读现有 README + 项目核心文档 AGENTS.md/CHANGELOG/docs/architecture.md)",
            "AskUserQuestion (5 项上下文收集：定位/态度/读者/部署/差异化)",
            "WebFetch (查 awesome-readme 等参考范例)",
            "WebSearch (竞品调研，如对竞品不熟先做调研)",
            "Edit/Write (修订 README_public.md 或从零写 README.md)",
            "Grep (检查私有路径泄露：F:\\ / 个人邮箱 / 内部 IP)",
        ],
        "key_pitfalls": [
            "tone 错配：个人项目用官方产品型语气最违和，默认推荐诚实自用型（主要自己用，公开是因为 X，你愿意折腾就试试）",
            "竞品对比翻车：不拉踩竞品、不假装唯一、不回避对比；主动 frame 差异化维度（聚合 / 接入协议 / 跨平台上下文持久化等）",
            "隐藏部署门槛：写 easy to install 但实际要 5 个密钥+GPU+管理员权限；底线是'努力一下能跑起来'，做不到就明说",
            "feature dump：把每个 feature 都列出来，读者无法判断哪些是核心；应按读者画像分组+突出 1-2 个真正差异化点",
            "形容词堆砌：一句话定位里超过 2 个形容词几乎都是空话；每个承诺后面要能补'具体指什么'",
            "改前不读完整 README：边读边改会破坏段间衔接；必须先读完整篇再画映射表",
            "项目特有：改完后必须 Grep 检查没有泄露私有路径（F:\\ / 个人邮箱 / 内部 IP / chrome_debug 等 gitignored 目录）",
            "项目特有：链接的 docs/ 文件必须实际存在；版本号 badge 与 server/core/version.py 一致",
        ],
        "prerequisites": ["项目已有 README 或项目核心文档可供读取"],
        "level": "standard",
    },

    "adhoc.bat_writing": {
        "skill": "bat_writing",
        "name": "批处理编写与修复",
        "skill_file": ".agents/skills/bat_writing/SKILL.md",
        "keywords": [
            "写bat", "写 bat", "bat脚本", "bat 脚本", "bat 乱码", "bat 跑不了",
            "批处理", "批处理乱码", "批处理报错", "批处理编码", ".bat", ".cmd",
            "启动脚本", "不是内部或外部命令", "找不到批处理标签",
            "batch script", "cmd脚本", "cmd 脚本",
        ],
        "description": "编写/修复中文 Windows 批处理：GBK+CRLF+无BOM 铁律、写完必须 --check 自检、坏了用 scripts/fix_bat_encoding.py 修复",
        "memory_key": None,
        "first_action": "读 .agents/skills/bat_writing/SKILL.md；确认目标机代码页（本机 ACP=OEMCP=936 → GBK）；按 references/guide.md 模板编写；写完必须跑 uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件> --check，全 OK 才算完成，不过则 --inplace 修复后复检；交付前实际运行一次",
        "workflow_summary": "1.读SKILL.md确认铁律 2.确认目标机代码页 3.按模板编写(标签用英文/%~dp0拼路径/中途不chcp) 4.--check自检(BOM/GBK/CRLF/末尾换行) 5.不过则--inplace修复后复检 6.cmd实跑验证",
        "mcp_tools_priority": [
            "Read (.agents/skills/bat_writing/SKILL.md，写 bat 前必读)",
            "Write/Edit (bat 内容)",
            "exec_cmd (uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件> --check 自检)",
            "exec_cmd (同脚本 --inplace 修复)",
        ],
        "key_pitfalls": [
            "三铁律：GBK(CP936)+CRLF+无BOM，任意一条破坏脚本就以'编码问题'的方式炸",
            "LF-only 是最常见死法：cmd 对 LF-only 文件按字符数定位行偏移，GBK 双字节导致逐行错位，报错全是半截汉字；纯 ASCII 的 LF-only 往往能跑，所以坑几乎只在中文脚本上爆",
            "不信任任何写入工具的行尾/编码：Edit/Write/编辑器都可能产出 LF 或 UTF-8，GBK 与 UTF-8 在乱码爆发前肉眼无法分辨，写完必须 --check",
            "禁止脚本中途切 chcp：运行中切代码页让 cmd 缓存的字节↔字符映射失效；含中文时禁用 UTF-8+chcp 65001 方案（实测 echo 行会被误解析成命令）",
            "goto/call 标签用英文；提权重启后工作目录变 System32，路径一律 %~dp0 拼接",
            "文件末尾留换行（最后一行无换行符会被吞）；交付前实际运行一次，不是只在 IDE 里看过源码",
            "--check 全过还报错 → 是脚本逻辑问题，不是编码问题，按普通 bug 排查",
        ],
        "prerequisites": ["目标机为 Windows（本机 ACP=OEMCP=936 已实测）"],
        "level": "standard",
    },

    "adhoc.office_docx": {
        "skill": "office_docx",
        "name": "Word 文档生成",
        "skill_file": ".agents/skills/office_docs/SKILL_docx.md",
        "keywords": [
            "生成 Word", "生成 docx", "md 转 docx", "生成复习提纲", "生成报告", "生成简历",
            "正式文档", "长文本", "学术论文", "academic paper",
            "可填表单", "合同模板", "SOW 模板", "HR 入职表", "医疗问卷",
        ],
        "description": "Markdown/JSON/文本→docx（纯Python，python-docx+oxml；含学术论文(APA/Chicago/IEEE/MLA引用格式)/Word 表单(SDT/content control/MERGEFIELD/mail merge/documentProtection)/Report 三场景）",
        "memory_key": None,
        "first_action": "确认输入源（Markdown/JSON/文本路径）和输出路径（默认 workspace/office_docs/{描述}_{日期}.docx）；按场景读对应 references/：基础→docx_basic.md，学术论文→docx_academic_paper.md，Word 表单/SDT→docx_word_form.md，Report 配方→docx_report_recipes.md；用 exec_python 调 python-docx+oxml 现场实现",
        "workflow_summary": "1.确认输入源+输出路径 2.读对应 references 3.增量构建（结构→内容→格式）4.验证 docx.Document(path) 重开+token leak grep+Visual audit 5.报告路径+大小+关键内容",
        "mcp_tools_priority": [
            "exec_python (用 python-docx + oxml 现场实现，复杂样式写临时脚本到 temp/)",
            "Read (读对应 references/ 详细参考)",
        ],
        "key_pitfalls": [
            "纯 Python 实现：不用 JS 库（docx-js）不用 LibreOffice",
            "中文字体必须用 oxml 设 w:eastAsia（python-docx 高层 API 不暴露此属性）",
            "不要用空段落造间距：用 spaceBefore/spaceAfter",
            "不要把 {{customer_name}} 当 body 文本写：用 MERGEFIELD field",
            "不要两种 page break 机制叠加：pageBreakBefore=true OR pagebreak，不要同时用",
            "Table caption 在表上方，Figure caption 在图下方（4 种 Citation 风格一致）",
            "生成后必须 docx.Document(path) 重开验证，不能'生成就完事'",
            "输出路径默认 workspace/office_docs/，文件名规范 {描述}_{日期}.docx",
            "主色用绿色 #2E7D32（项目偏好），不用紫色",
        ],
        "prerequisites": ["uv pip install python-docx"],
        "level": "standard",
    },

    "adhoc.office_xlsx": {
        "skill": "office_xlsx",
        "name": "Excel 表格生成",
        "skill_file": ".agents/skills/office_docs/SKILL_xlsx.md",
        "keywords": [
            "生成 Excel", "生成 xlsx", "md 转 xlsx", "JSON 转 xlsx", "CSV 转 xlsx",
            "账单汇总", "数据报表", "多 sheet 分析", "带公式",
            "财务模型", "数据可视化", "透视表", "数据验证",
        ],
        "description": "JSON/CSV/Markdown→xlsx（纯Python，openpyxl；含财务模型(DCF/LBO/WACC/NPV/IRR/XIRR/sensitivity table/unit economics/CAC-LTV/cap table forecast)/数据仪表盘(KPI/analytics/executive dashboard/CSV to dashboard)+条件格式(conditional formatting/named range/PivotTable/sparkline)两场景+CFO 4-color+Three-zone）",
        "memory_key": None,
        "first_action": "确认输入源（JSON/CSV/Markdown 表格路径）和输出路径（默认 workspace/office_docs/{描述}_{日期}.xlsx）；按场景读对应 references/：基础→xlsx_basic.md，条件格式→xlsx_conditional_formatting.md，财务模型→xlsx_financial_model.md，仪表盘→xlsx_data_dashboard.md；用 exec_python 调 openpyxl 现场实现",
        "workflow_summary": "1.确认输入源+输出路径 2.读对应 references 3.增量构建（结构→公式→格式）4.验证 load_workbook 重开+Excel error sweep+Visual audit 5.报告路径+大小+sheet 数+关键 KPI",
        "mcp_tools_priority": [
            "exec_python (用 openpyxl 现场实现，复杂样式写临时脚本到 temp/)",
            "Read (读对应 references/ 详细参考)",
        ],
        "key_pitfalls": [
            "纯 Python 实现：不用 JS 库不用 LibreOffice",
            "公式优先于 hardcode：用 =SUM(B2:B9) 而非预计算结果（hardcode 让表格失去动态性）",
            "不 date as text：日期用 datetime 对象或 YYYY-MM-DD 格式",
            "不 scientific notation：用 numFmt 0.00 或 ¥#,##0.00 避免",
            "不窄列中文：column width 显式设置，中文字符比英文宽",
            "不中文函数名：用英文 SUM/VLOOKUP，不要 求和",
            "不 merged cell write loss：合并单元格后只能写左上角",
            "不 non-Chinese fonts：默认字体可能不支持中文，显式设置 Microsoft YaHei",
            "不 year 显示 2,026：年份用 numFmt='@' 或字符串类型",
            "财务模型必须遵循 CFO 4-color code（blue/black/green/red）+ Three-zone architecture（Inputs/Calc/Outputs）",
            "生成后必须 load_workbook(path) 重开验证",
            "输出路径默认 workspace/office_docs/，文件名规范 {描述}_{日期}.xlsx",
            "主色用绿色 #2E7D32，不用紫色",
        ],
        "prerequisites": ["uv pip install openpyxl"],
        "level": "standard",
    },

    "adhoc.office_pptx": {
        "skill": "office_pptx",
        "name": "PowerPoint 演示文稿生成",
        "skill_file": ".agents/skills/office_docs/SKILL_pptx.md",
        "keywords": [
            "生成 PPT", "生成 pptx", "md 转 pptx", "汇报演示", "复习 PPT", "项目总结",
            "融资路演", "字体配对", "调色板", "3D 模型",
        ],
        "description": "Markdown/JSON→pptx（纯Python，python-pptx；含融资路演(pitch deck/Series A-C/seed round/SAFE/bridge/Use-of-Funds/traction/unit econ/TAM-SAM-SOM/CAC-LTV/Morph transitions)/通用配方(6 anti-AI-slop 设计原则：visual floor/12-column grid/字体配对/调色板/3D 模型 .glb)两场景）",
        "memory_key": None,
        "first_action": "确认输入源（Markdown/JSON/文本路径）和输出路径（默认 workspace/office_docs/{描述}_{日期}.pptx）；按场景读对应 references/：基础→pptx_basic.md，设计原则→pptx_design_principles.md，融资路演→pptx_pitch_deck.md，通用配方→pptx_recipes.md；用 exec_python 调 python-pptx 现场实现",
        "workflow_summary": "1.确认输入源+输出路径 2.读对应 references 3.增量构建（结构→内容→格式，shape 必须显式定位+命名）4.验证 Presentation(path) 重开+overflow/contrast/structure+Visual audit 5.报告路径+大小+slide 数",
        "mcp_tools_priority": [
            "exec_python (用 python-pptx + oxml 现场实现，复杂动画/transition/Morph 用 oxml 注入)",
            "Read (读对应 references/ 详细参考)",
        ],
        "key_pitfalls": [
            "纯 Python 实现：不用 JS 库（pptxgenjs）不用 LibreOffice",
            "shape 必须显式定位 x/y/width/height（无 layout engine）+ 创建时即命名（positional [N] 在 reorder 后会漂移）",
            "每张 slide 必须有 speaker notes（H7 hard rule）",
            "每张 slide 至少一个视觉元素（bullet-only slide 等同 Word doc）",
            "字号下限：title ≥36pt bold，body ≥18pt（Visual Floor）",
            "12-column grid：edge margin ≥1.27cm，inter-block gap ≥0.76cm，≥20% negative space",
            "6 anti-AI-slop 设计原则：每页有视觉元素/Dominance over equality 60-70%+10%+10%+<5%/Compose don't web-center/Vary layout/No emoji as iconography/Visual motif commitment",
            "不 hockey-stick y-axis：line chart y 轴必须从 0 起（axismin=0）",
            "不 team slide = portfolio：每卡需 prior-company 或 prior-achievement line",
            "不 TAM without methodology：claimed number 必须有 top-down 或 bottom-up source footnote",
            "不 Use-of-Funds as 3/5-bucket：4-bucket (Eng/GTM/G&A/Reserve) 是 convention",
            "不连续两 slide 用同一 pattern",
            "生成后必须 pptx.Presentation(path) 重开验证",
            "输出路径默认 workspace/office_docs/，文件名规范 {描述}_{日期}.pptx",
            "Forest & Moss 调色板（2C5F2D primary），不用紫色",
        ],
        "prerequisites": ["uv pip install python-pptx"],
        "level": "standard",
    },

    "adhoc.office_pdf": {
        "skill": "office_pdf",
        "name": "PDF 文档生成",
        "skill_file": ".agents/skills/office_docs/SKILL_pdf.md",
        "keywords": [
            "生成 PDF", "md 转 pdf", "归档材料", "printable 报告",
            "合并 PDF", "拆分 PDF", "PDF 加密", "PDF 水印", "PDF 旋转", "PDF 表单填充",
            "提取 PDF 文本", "提取 PDF 表格",
        ],
        "description": "Markdown/文本→pdf + PDF 操作（合并/拆分/加密/水印/提取）（纯Python，reportlab+pypdf+pdfplumber）",
        "memory_key": None,
        "first_action": "确认输入源（Markdown/文本/现有 PDF 路径）和输出路径（默认 workspace/office_docs/{描述}_{日期}.pdf）；读 references/pdf_basic.md（含创建+操作+提取全部元素）；用 exec_python 调 reportlab/pypdf/pdfplumber 现场实现",
        "workflow_summary": "1.确认输入源+输出路径 2.读 pdf_basic.md 3.增量构建（中文字体注册→样式→Story→Build）4.验证 pypdf.PdfReader 重开+token leak+Visual audit（无中文黑方块/无 Unicode 上下标）5.报告路径+大小+页数",
        "mcp_tools_priority": [
            "exec_python (用 reportlab + pypdf + pdfplumber 现场实现)",
            "Read (读 references/pdf_basic.md 详细参考)",
        ],
        "key_pitfalls": [
            "纯 Python 实现：不用 JS 库（pdfkit/wkhtmltopdf）不用 LibreOffice",
            "中文字体必须用 pdfmetrics.registerFont(TTFont('MSYH', 'C:/Windows/Fonts/msyh.ttc')) 注册 + registerFontFamily 让 <b> 自动用 bold，否则渲染为黑方块",
            "不 Unicode 上下标字符：₀₁₂ ⁰¹² 渲染成黑方块，用 <sub>/<super> 标签或 reportlab 原生 API",
            "不长表格跨页断裂：用 TableStyle + splitByRow=True + repeatRows=1",
            "不图片溢出：用 Image 的 maxWidth/maxHeight 或 kind='proportional' 缩放",
            "不 Paragraph XML 转义：< > & 必须用 &lt; &gt; &amp;",
            "不 Spacer 单位错误：用 inch 或 cm，不要裸数字",
            "不 header/footer 样式泄漏：canvas.saveState() + canvas.restoreState() 配对",
            "生成后必须 pypdf.PdfReader(path) 重开验证",
            "输出路径默认 workspace/office_docs/，文件名规范 {描述}_{日期}.pdf",
            "主色用绿色 #2E7D32，不用紫色",
        ],
        "prerequisites": ["uv pip install reportlab pypdf pdfplumber"],
        "level": "standard",
    },

    "adhoc.ocr": {
        "skill": "ocr",
        "name": "OCR识别",
        "skill_file": ".agents/skills/ocr.md",
        "keywords": ["识别图片", "OCR", "读取图片", "识别文字"],
        "description": "本地 PaddleOCR 文字识别/可靠 bbox 定位 + 远程 VL 文档描述",
        "memory_key": None,
        "first_action": "已有图片用 ocr_file/ocr_path；屏幕文字定位用 screen_ocr(window, engine=ocr)，VL 默认只做描述",
        "mcp_tools_priority": ["ocr_file", "ocr_path", "localagent_advanced_tool(screen_ocr)"],
        "key_pitfalls": [
            "截图 OCR 已关闭 UVDoc，bbox 可定位；禁止恢复旧 kx/ky 仿射参数",
            "浏览器优先 DOM locator；桌面文字用 OCR bbox；纯图标才用 vision_locate",
            "screen_snapshot 只返回文字摘要，不返回 bbox",
        ],
        "level": "minimal",
    },

    # ────────────── system（系统元任务）──────────────
    "system.task_reminder": {
        "skill": "task_reminder",
        "name": "任务提醒",
        "skill_file": ".agents/skills/task_reminder.md",
        "keywords": ["有什么任务", "该做什么", "提醒我", "任务清单", "待办清单", "今天做啥", "这周做啥", "有什么没做", "任务提醒",
                     # 机会型跑批（用户 2026-09-20 明确的口径：没有推送，只能被问时现场核对）
                     "额度很多", "额度有余", "额度多余", "额外额度", "空闲额度", "长期自动化", "顺便跑", "能跑什么"],
        "description": "检查周期任务到期+WIP待办汇总；用户表示额度有余/想跑长期自动化时按周期台账自查可跑项",
        "memory_key": None,
        "first_action": "调用 todos_due 获取到期任务；调用 wip_list(summary=true) 获取 WIP 摘要。需要单个 WIP 详情时用 wip_get(task_id='wip_xxx')（直连 MCP 工具，免审批），禁止用 exec_python 发 HTTP 调本地 API\n\n用户说\"额度很多/想跑长期自动化/有什么可以顺便跑的\"时：读 docs/periodic-task-inventory.md 的「空闲额度自查流程」台账，逐项实测每项的上次活动信号（文件 mtime / GET /loop/tasks 的 last_run_at / GET /inbox）与阈值比较，一次性报告超期项并等用户点单，禁止不核对就断言都新鲜、禁止未点单就开跑",
        "workflow_summary": "1.todos_due 取到期任务 2.wip_list 取WIP 3.汇总报告 4.完成后 todos_mark_done",
        "mcp_tools_priority": [
            "todos_due",
            "wip_list",
            "wip_get (直连，查详情)",
            "todos_mark_done",
            "todos_check_trigger (triggered 类型手动检查触发条件)",
            "exec_python (from datetime import date; print(date.today()))",
        ],
        "key_pitfalls": [
            "last_done_at 为 null 视为到期",
            "条件任务（如找工作期间）不确定时询问用户",
            "完成后必须调 todos_mark_done 更新 last_done_at + next_due_at",
            "phased_recurring 超出 end_date 后自动 archived，不再到期",
            "triggered 类型不进 todos_due，由 loop 轮询触发后推送 /user/message",
        ],
        "level": "standard",
    },
    "system.wip_tracking": {
        "skill": "wip_tracker",
        "name": "未完成任务追踪",
        "skill_file": ".agents/skills/wip_tracker.md",
        "keywords": ["留档", "存档", "标记未完成", "WIP", "断头工作", "继续之前的工作", "有哪些未完成", "工作进度"],
        "description": "中断任务留档+恢复",
        "memory_key": None,
        "first_action": "调用 wip_list 获取所有 WIP 任务。需要详情用 wip_get(task_id='wip_xxx')（直连 MCP 工具，免审批），禁止用 exec_python 发 HTTP",
        "workflow_summary": "留档：wip_create 创建 | 查询：wip_list | 恢复：wip_get 读详情 | 更新：wip_update",
        "mcp_tools_priority": [
            "wip_list",
            "wip_get (直连，查详情)",
            "localagent_advanced_tool(wip_create/wip_update/wip_delete)",
        ],
        "key_pitfalls": [
            "留档：JSON 字段用结构化格式（next_steps 数组、current_state 对象）",
            "去重：用户重复提需求时先 wip_list 搜索识别已有探索",
            "状态：active/blocked/paused/completed",
        ],
        "level": "standard",
    },
    "system.doc_sync": {
        "skill": "neat-freak",
        "name": "文档洁癖",
        "skill_file": ".agents/skills/neat-freak.md",
        # 移除"更新记忆"（与 recurring.memory_generation 重复，后者是专门的记忆管理 skill）
        "keywords": ["同步一下", "整理文档", "整理一下", "梳理一下", "/sync", "/neat"],
        "description": "会话结束后文档一致性审查",
        "memory_key": None,
        "first_action": "审查 AGENTS.md / Skill文件 / _index.md / 测试 / 配置 / 监控面板的一致性",
        "workflow_summary": "对照功能变更检查清单逐项审查，确保文档跟得上代码变化",
        "mcp_tools_priority": ["Read / Edit / Grep / Glob"],
        "key_pitfalls": [
            "会话结束或用户说整理一下时触发",
            "审查范围：AGENTS.md / _index.md / config.example.toml / tools_manifest / 监控面板",
        ],
        "level": "standard",
    },
    "system.task_closure": {
        "skill": "task_closure",
        "name": "任务收尾",
        "skill_file": ".agents/skills/task_closure.md",
        "keywords": ["收尾", "任务结束", "做完了", "总结一下", "这个任务完成了", "收尾guide", "task closure", "结束了"],
        "description": "任务结束时的统一收尾入口（WIP+经验+文档自查+报告）",
        "memory_key": None,
        "first_action": "按 task_closure_workflow 逐步执行：1.评估任务状态(含当前任务授权主动收回) 2.WIP处理 3.经验提炼(含可消费性自检) 4.查重写入 5.文档自查 6.收尾报告",
        "workflow_summary": "1.评估状态+当前任务授权收回 2.WIP处理 3.经验提炼+可消费性自检 4.查重写入 5.文档自查 6.收尾报告",
        "mcp_tools_priority": [
            "screen_release_control (step_1 当前任务授权主动收回，若授权仍 active)",
            "wip_list / wip_update / wip_create (step_2 WIP处理)",
            "memory_list / memory_get / memory_set (step_3-4 经验提炼+写入)",
            "Read / Edit / Grep (step_5 文档自查)",
        ],
        "key_pitfalls": [
            "可消费性自检：写入记忆前必须填 consumption_contexts + trigger_keywords，否则跳过",
            "文档自查不询问用户：agent 自行对照 project_rules.md 检查清单，完整 neat-freak 由用户触发",
            "无实质产出的纯对话不强制收尾，避免无意义记忆",
            "任务中断时跳过经验提取（还没做完），只做 WIP 留档+文档自查",
            "当前任务授权兜底：任务结束（完成/中断）时若授权仍 active，必须调 screen_release_control 主动收回；即使遗漏，NORMAL 模式 30min 无操作也会自动降级到 NO_PERMISSION，WATCHDOG 模式到期降级到 NORMAL",
        ],
        "prerequisites": ["任务完成/中断/会话结束前"],
        "level": "full",
        "task_closure_workflow": {
            "step_1_assess": {
                "name": "评估任务状态 + 当前任务授权主动收回",
                "instruction": (
                    "判断当前任务状态，决定后续步骤集：\n"
                    "- 任务完成（目标达成，有产出）→ step_2(wip关闭) → step_3-4(经验) → step_5(文档自查) → step_6\n"
                    "- 任务中断（未完成需中断）→ step_2(wip留档) → step_5(文档自查) → step_6（跳过经验提取）\n"
                    "- 无实质产出（纯对话/查询）→ 直接 step_6（提示跳过）\n"
                    "判断标准：有代码/文档/数据产出？用户说'做完了'/'先这样'？只是回答问题？\n\n"
                    "当前任务授权兜底收回：\n"
                    "任务结束（完成或中断）时，若当前任务授权仍 active，\n"
                    "必须调 screen_release_control（POST /screen/control/release）主动收回。\n"
                    "收回后覆盖层隐藏、授权状态清除、授权 worker 停止。\n"
                    "即使 agent 遗漏，NORMAL 模式 30min 无操作也会自动降级到 NO_PERMISSION，\n"
                    "WATCHDOG 模式到期降级到 NORMAL（不直接释放）。"
                ),
            },
            "step_2_wip": {
                "name": "WIP 处理",
                "instruction": (
                    "任务完成：wip_list 检查对应 WIP → 有则 wip_update(status=completed, progress=100) → 无则跳过\n"
                    "任务中断：wip_list 检查 → 有则 wip_update(progress/next_steps/current_state) → 无则 wip_create\n"
                    "wip_create 必填：title/goal/progress/next_steps(可执行数组)/current_state(dict)/related_skills/related_files"
                ),
            },
            "step_3_extract": {
                "name": "经验提炼 + 可消费性自检",
                "instruction": (
                    "回顾当前任务，识别候选记忆（preference/project/reference）。\n"
                    "不存储：代码结构(可grep)/git历史/已修复bug/临时调试/AGENTS.md已记录/config.toml已配置。\n\n"
                    "可消费性自检（核心约束）：每条候选必须填：\n"
                    "- consumption_contexts: 哪些 task_type 应读取此记忆（通用用['*']，具体用['recurring.accounting']）\n"
                    "- trigger_keywords: 任务描述出现这些词时应读取（如['退款','curl']）\n"
                    "无法明确消费场景的候选 → 跳过不写入（存了没人用=垃圾）\n"
                    "例外：纯 project 状态记忆填对应 task_type 后可写入"
                ),
            },
            "step_4_dedup_write": {
                "name": "查重 + 写入",
                "instruction": (
                    "对每条通过自检的候选：\n"
                    "1. memory_list 查现有 key\n"
                    "2. key 已存在 → memory_get 读取 → 比较（更新/重复/新增信息）\n"
                    "3. memory_set(key, {data: <structured_value>, merge: true}) 写入\n\n"
                    "structured_value JSON schema:\n"
                    "{\n"
                    '  "type": "preference|project|reference",\n'
                    '  "name": "简短名称",\n'
                    '  "description": "一句话描述",\n'
                    '  "content": "实际内容",\n'
                    '  "why": "为什么重要",\n'
                    '  "how_to_apply": "如何应用",\n'
                    '  "consumption_contexts": ["recurring.accounting"],\n'
                    '  "trigger_keywords": ["退款"],\n'
                    '  "created_at": "YYYY-MM-DD",\n'
                    '  "source_task": "任务名"\n'
                    "}\n"
                    "key 命名：preferences.{name} / project.{name} / 沿用 skill 名"
                ),
            },
            "step_5_doc_sync": {
                "name": "文档自查（不询问用户）",
                "instruction": (
                    "agent 自行对照 .agents/rules/project_rules.md 检查清单，检查本轮变更：\n"
                    "- 新增路由是否在 server/main.py 注册？\n"
                    "- Pydantic 模型完整？/health 反映新接口？\n"
                    "- 硬编码路径/端口/密钥？→ 移入 config.toml\n"
                    "- _index.md / AGENTS.md / CHANGELOG.md 需要同步？\n"
                    "- config.example.toml / tools_manifest.json 需要更新？\n\n"
                    "项目结构漂移检查（响应中的 structure_diff 字段）：\n"
                    "- unknown_paths 非空 → 本轮新增了目录/文件未在 data/project_structure.json baseline 中\n"
                    "  · 若是本轮有意新增 → 用 exec_python 调 server.project_structure.sync_baseline(add_descriptions={path: desc}) 补全\n"
                    "  · 若是无关产物（缓存/临时）→ 报告用户确认是否跳过\n"
                    "- missing_paths 非空 → baseline 中记录的路径磁盘上不存在（可能被删/改名）\n"
                    "  · sync_baseline() 一并移除（幂等，改前先看它打印的 added/removed 清单）\n"
                    "- ⚠️ update_baseline_descriptions() 只写 top_level：二级目录漂移（unknown_paths 里 kind=sub）用它补无效且静默无报错，必须走 sync_baseline()\n"
                    "- summary='无漂移' → 跳过此项\n"
                    "发现问题 → 直接修复（小改动）或报告用户（大改动）。\n"
                    "完整 neat-freak 审查（跨文件一致性/监控面板/测试）由用户主动触发。"
                ),
            },
            "step_6_closure_report": {
                "name": "收尾报告",
                "instruction": (
                    "向用户报告：\n"
                    "- 任务状态（完成/中断/无实质产出）\n"
                    "- WIP 处理（更新了/创建了/无WIP）\n"
                    "- 经验提炼（写入N条记忆含消费场景 + 跳过M条候选及原因）\n"
                    "- 文档自查（无需同步/已同步/发现问题）\n"
                    "- 下一步建议（如有）"
                ),
            },
        },
        "do_not_store": [
            "代码结构（可 grep 代码库获取）",
            "git 历史（可 git log 获取）",
            "已修复的 bug（修复后不再需要）",
            "临时调试信息",
            "AGENTS.md / _index.md 中已记录的内容",
            "config.toml 中已配置的值",
        ],
    },

    "system.temp_cleanup": {
        "skill": "temp_cleanup",
        "name": "temp文件夹清理",
        "skill_file": ".agents/skills/temp_cleanup.md",
        "keywords": ["整理temp", "清理temp", "temp清理", "临时文件整理", "temp太乱", "清理临时文件", "temp cleanup", "temp整理"],
        "description": "temp目录定期清理工作流（按性质三问判定→静态清单→用户确认→回收站删除→移动→wip处理）",
        "memory_key": "temp_cleanup",
        "first_action": "读取 .agents/skills/temp_cleanup.md 获取三问决策树和四条交叉校验；扫描 temp/ 顶层，每个拟删文件先实际读 ≥30 行再判定，按性质而非按目录名查表",
        "workflow_summary": "1.扫描+逐文件读内容+三问判定 2.生成静态路径快照清单（含内容证据）询问用户 3.执行前二次diff 4.回收站删除 5.移动 6.wip待办处理 7.收尾报告",
        "mcp_tools_priority": [
            "exec_python (扫描 temp/ 目录并按大小排序顶层条目)",
            "Grep (交叉校验：路径/文件名是否被 AGENTS.md、docs/、tests/、client/、server/ 引用)",
            "Read (逐文件读内容后再判定——拟删文件至少读 30 行，大文件读头 30 + 尾 30；另用于检查 .agents/wip/ open 任务)",
            "Shell via uv (删除走回收站: uv run python tools/disk/recycle.py --list 快照文件 --on-error continue)",
            "Shell via git (只读校验 worktree 注册状态与 commit 祖先关系)",
            "Shell with PowerShell (Move-Item 移动文件)",
            "wip_create (清理中发现遗漏任务或归属错位工具时)",
        ],
        "key_pitfalls": [
            "删除必走回收站且只用 tools/disk/recycle.py：本机 PowerShell Add-Type 被安全策略拦截，Microsoft.VisualBasic.FileIO 方案照做必失败；禁用 Remove-Item/rm/shutil.rmtree",
            "判「可删」前必须真的读过这个文件：每个拟删文件实际读 ≥30 行（大文件头 30 + 尾 30），清单证据栏必须有内容证据；禁止仅凭文件名/扩展名/体积下结论。2026-08-30 按名字把 temp/audit/final_report.md 与 pyright_summary.md 当「可复现产物」删掉，实际是被文档引用 5 处、且代码版本已变更无法重放的一次性历史记录",
            "「可重建」必须验证重放路径现在真能跑通：生成脚本还在 + 上游数据还在 + 对应代码版本没变；跑不通就是一去不回的历史记录，不能按 Q1 删",
            "清单必须是静态路径快照：扫描时刻展开成具体路径文件，禁止「根目录所有 *.py」这类执行时求值的活规则——2026-08-30 实测清理进行中 temp 被并发会话写入，根目录 .py 从 94 变 95",
            "执行前二次 diff：把清单生成后新增的路径剔除或补入，静态快照防误伤、二次 diff 防漏清",
            "清单先行且不覆盖保护类：用户说「全部删」也不能删保护清单里的东西，有冲突必须明确指出让用户确认",
            "被文档引用的文件一律移出清理范围：AGENTS.md/docs//tests/ 里 grep 命中的路径可能只有 temp 一份（drift_detector 案例），先另议归属迁移，不要顺手删了再改文档",
            "活跃性窗口 2 小时：mtime 近 2 小时内 / 有 wip 关联 / 被源码引用 → 不删；运行中终端的会话日志同样不删",
            "temp 里可藏已注册的 git worktree：必须「回收站删目录 → git worktree prune」；git worktree remove --force 属永久删除，违反硬约束",
            "源码副本先确认原始位置真的存在：700+ 个 .py 的副本按「.py=临时脚本」筛会被整份端掉",
            "备份分级：回收站本身即备份，已确认入站时不再叠一层；仅对逼近回收站容量的大批量、非 NTFS/网络盘、可能覆盖同名的结构性移动强制先备份",
            "业务数据不删：个人创作/笔记/财务等唯一副本只能移动到 workspace",
            "临时解压/查看现场是 temp 的正当用途，任务还在进行就保留；temp 无固定结构，不要强加 refs//scripts/ 之类目录",
            "内容比对（MD5 相等）是可选优化项而非删除前置条件：一次性脚本与 tools/ 通用工具几乎永远不会字节相同，把 MD5 相等当门槛会导致永远删不掉或放宽标准误删。⚠️ 这只豁免「逐字节比对」，不豁免「读内容」——两者别混为一谈",
        ],
        "prerequisites": ["tools/disk/recycle.py 可用", "temp/ 目录存在"],
        "level": "full",
        "classification_table": {
            "pre_read_content": "判定前置：每个拟删文件实际读 ≥30 行（大文件头 30 + 尾 30），清单必须附内容证据；没读过 = 没有判定资格，不得进删除清单",
            "Q1_rebuildable": "可重建（日志、缓存、中间产物、测试输出、probe 记录、已提交批次的脚本）→ 回收站；须验证重放路径现在还能跑通",
            "Q2_unique_copy": "唯一副本（个人笔记/复盘/财务/设计蓝图、未提交产出、源码 clone、快照压缩包）→ ★保护，绝不删",
            "Q3_owned": "有归属（属于某 workspace 任务 / 别的项目 / 仓库注册对象）→ 移回归属地，不删不留在 temp；无归属则标待定交用户",
            "project_source_of_truth": "temp/sdd/<slug>/ 与 temp/planning_archive/ → 绝对保护（上下文压缩后的恢复锚点，归档由用户决策）",
            "cross_check_active": "活跃性：mtime 近 2 小时 / wip 关联 / 运行中终端日志 → 保留",
            "cross_check_referenced": "引用性：被 AGENTS.md、docs/、tests/、client/、server/ grep 命中 → 保留并另议归属迁移",
            "cross_check_worktree": "仓库注册对象：.git 是 gitdir 指针 → worktree list + merge-base --is-ancestor + status clean → 回收站删目录后 git worktree prune",
            "cross_check_other_project": "跨项目性：内容属别的项目 → 直接移回，不再讨论可重建性",
        },
    },

    # ────────────── dev（开发任务）──────────────
    "dev.skill_creation": {
        "skill": "skill-creator",
        "name": "Skill创建器",
        "skill_file": ".agents/skills/skill-creator.md",
        "keywords": ["创建skill", "新建skill", "写个skill", "优化skill", "skill-creator", "创建技能"],
        "description": "引导创建/修改skill",
        "memory_key": None,
        "first_action": "读取 .agents/skills/skill-creator.md 获取编写规范和模板",
        "workflow_summary": "意图捕获→Skill文件编写→测试验证→迭代优化→更新_index.md和GUIDE_REGISTRY",
        "mcp_tools_priority": ["Read / Write / Edit"],
        "key_pitfalls": [
            "创建后必须更新 _index.md 和 GUIDE_REGISTRY",
            "遵循 frontmatter 格式（name + description含触发词）",
            "参考4种常见Skill类型模板",
        ],
        "level": "standard",
    },
    "dev.html_debug": {
        "skill": "html-dev-debug",
        "name": "HTML工具开发调试",
        "skill_file": ".agents/skills/html-dev-debug.md",
        "keywords": ["写HTML页面", "创建工具页面", "HTML调试", "可视化页面", "Dashboard", "前端页面"],
        "description": "浏览器+截图+OCR闭环验证",
        "memory_key": None,
        "first_action": "确认输出位置（如 workspace/ 或 temp/）；编写/修改 HTML 代码；多步浏览器操作先 browser_session_create 复用热态 session（自动注入站点经验）",
        "workflow_summary": "1.写代码 2.browser_session_create 复用热态（P2-1 自动注入 site_lessons） 3.browser_navigate(goto) 打开文件 4.browser_snapshot 看结构 5.browser_action 操作元素 6.popup/filechooser/dialog 场景优先 browser_wait_and_action（原子 wait+trigger） 7.browser_evaluate 读 DOM 状态 8.browser_console_logs 抓 JS 报错 9.browser_screenshot 10.OCR 11.分析问题 12.修改→重新验证 13.browser_session_close 收尾（关闭 tab 释放资源，避免 Chrome tab 累积）",
        "mcp_tools_priority": [
            "browser_session_create (多步操作首步，热态 <250ms，自动注入站点经验)",
            "browser_navigate (goto/back/forward/reload，已有 session 时导航到新文件)",
            "browser_snapshot (看可访问性 DOM，定位元素)",
            "browser_action (统一 click/fill/press/select/check/hover/scroll)",
            "browser_wait_and_action (原子 wait+trigger：popup/filechooser/dialog 场景同请求完成)",
            "browser_evaluate (读 DOM 状态，只读)",
            "browser_console_logs (抓 JS 报错，since_cursor 增量读)",
            "browser_screenshot (视口/全页/元素)",
            "browser_selector_stats (查已知稳定 selector 规避失败选择器)",
            "browser_find_url (找历史访问的内部页面，auto_open=true 一键开 session)",
            "browser_match_site (显式查站点经验库)",
            "browser_session_close (收尾必步：关闭 tab 释放资源，避免 Chrome tab 累积)",
            "capture_screen / ocr_path (兜底)",
        ],
        "key_pitfalls": [
            "模板字面量引号冲突、嵌套反引号",
            "静态文件路径 404",
            "Canvas 参数为负",
            "浏览器缓存（browser_navigate(action='reload') 强制刷新）",
            "每轮只修1-2个问题",
            "session 失效时回退到无 session 模式（url_pattern 子进程）",
            "browser_action 严格模式：多个匹配报 MULTIPLE_MATCHES，需用 nth 或细化 target",
            "site_lessons 命中时优先按 lessons 提示的 selector/反爬规则操作",
            "失败 selector 会在 selector_stats 中累积 fail_count，先查 stats 规避",
            "同 URL 多标签场景必须传 tab_id（从 /browser/tabs 响应的 target_id 拿）精确指定",
            "browser_evaluate 拦截写操作（location/cookie/fetch/XHR/eval），写操作改走 browser_action",
            "SPA back/forward 可能 wait_timeout=true 但 navigation_completed=true（不是失败）",
            "console_logs 增量读：传 since_cursor=上次响应的 next_cursor 避免重复",
            "snapshot 的 node_id 可直接传 browser_action(target={'node_id':N})，无需转 css；缓存 60s 或 URL 变化后失效返回 STALE_NODE",
            "dialog 被 Playwright 自动 dismiss，wait_for(dialog) 仅作意图声明；accept/prompt 输入必须用 browser_wait_and_action",
            "popup/filechooser 跨 HTTP 请求不可靠，必须用 browser_wait_and_action 原子 API",
            "SPA 频繁 mutation 场景：browser_action 传 target.verify_dom_freshness=true 校验 dom_hash 一致性",
        ],
        "level": "standard",
    },
    "dev.computer_use": {
        "skill": "computer_use",
        "name": "Guarded Computer Use",
        "skill_file": ".agents/skills/computer_use/SKILL.md",
        # 移除"翻页"（太泛，与 browser_automation 冲突）
        "keywords": ["操作电脑", "屏幕操作", "点击", "截图", "帮我操作", "自动操作"],
        "description": "UIA 语义层（P0-1 CoInitialize + P1-4 硬编码 ControlType） + OCR/DOM 优先定位 + 图像描述兜底 + 键鼠操控（P0 焦点安全 + P0-2 Unicode 代理对 + P0-3 状态语义 + P0-4 format=path + P1-1 dry_run + P1-2 canonical_window 刷新 + P1-3 hwnd 统一 + P1-A OCR 可观测性 + P1-B 窗口生命周期 + P2-1 desktop_transaction）",
        "memory_key": None,
        "first_action": "在第一次副作用 Computer Use 操作前先调 screen_request_control(task_description=具体任务, source='agent')，由用户决定仅允许本次还是勾选当前任务授权；获准后再用 screen_app_list/screen_window_resolve 拿 window_token→focus_window→capture_screen→按 UIA > 浏览器 DOM > OCR bbox > vision_locate 定位→危险操作先 dry_run 且始终逐次确认→多步原子操作用 screen_desktop_transaction→任务结束调 screen_release_control。三档权限模式：NO_PERMISSION(无授权)→NORMAL(普通执行,10min 告警+20min 降级)→WATCHDOG(看门狗,默认10h到期降级到 NORMAL,不因空闲撤销)",
        "workflow_summary": "1.screen_request_control 请求用户介导的当前任务授权（normal/watchdog 由 agent 在请求时指定，用户只能允许/拒绝不能改模式） 2.screen_app_list/window_resolve 拿 window_token 3.focus_window 激活目标 4.capture_screen 拿 snapshot_id 5.定位按 UIA > browser DOM > OCR bbox > vision_locate 6.语义动作用 screen_semantic_action 7.坐标动作用 execute_action/batch_actions 8.检查 transport/delivery/postcondition 9.危险操作 dry_run 后仍逐次确认 10.多步原子操作用 screen_desktop_transaction 11.WINDOW_TOKEN_INVALID 后重新 resolve 12.任务结束 screen_release_control；NORMAL 模式 10min 无操作进告警(黄色),30min 自动降级到 NO_PERMISSION；WATCHDOG 模式默认 10h 到期降级到 NORMAL(不直接释放)",
        "mcp_tools_priority": [
            "screen_request_control (第一次副作用操作前请求；task_description 写清任务，source=agent)",
            "list_windows / screen_app_list / screen_window_resolve (P1-B 窗口生命周期 + token 颁发)",
            "screen_app_launch / screen_app_wait (P1-B 应用启动+等待窗口)",
            "focus_window (键盘动作前必须先激活)",
            "capture_screen (拿 snapshot_id 绑定坐标)",
            "screen_accessibility_snapshot (P0-5 UIA 语义层 + P0-1 CoInitialize + P1-4 硬编码 ControlType，优先于 OCR/视觉)",
            "screen_semantic_action (P0-5 invoke/select/toggle/set_value/expand/collapse/scroll)",
            "localagent_advanced_tool(screen_ocr) / ocr_path (P1-A 可观测：/ocr/status 看 cold_start)",
            "browser_action(action=click, target={css/text/...}) (浏览器 DOM 优先)",
            "understand_image / vision_locate (按需兜底)",
            "execute_action / batch_actions (传 snapshot_id+expected+dry_run，P0-2 Unicode 代理对 + P0-3 状态语义 + P1-1 dry_run)",
            "screen_desktop_transaction (P2-1 桌面事务 operation_id：多步原子 + 必填 expected + rollback_policy=auto 可回滚)",
            "screen_window_minimize / screen_window_restore / screen_window_raise / screen_window_close (P1-B 窗口操作+post_state)",
            "preview_action (P0-4 默认 format=path，避免 base64 污染上下文)",
            "screen_release_control (任务完成/中断时主动收回；NORMAL 模式 30min 无操作自动降级到 NO_PERMISSION 仅作兜底；WATCHDOG 模式到期降级到 NORMAL 不直接释放)",
        ],
        "key_pitfalls": [
            "先查 .agents/skills/computer_use/apps/<process_name>.md 软件经验（screen_match_app 一键查，含 UIA 友好度/快捷键/菜单路径/已知坑），避免重复踩坑；list_windows/screen_app_list 响应含 app_lessons_hint 时说明已有经验",
            "安全铁律：每步截图思考再点击，不能用脚本直接点",
            "涉及删除/支付/关机等危险操作必须用户确认",
            "优先指定窗口模式，避免误操作其他程序",
            "紧急停止：Ctrl+` 反引号键，触发后10秒内禁止操作",
            "完全不能执行就放弃，不要硬做/瞎点",
            "当前任务授权（三档权限模式）：第一次副作用操作前调 screen_request_control；agent 在请求时指定 mode=normal 或 mode=watchdog，用户只能允许/拒绝不能改模式。NO_PERMISSION(无授权)只读操作放行,副作用端点 403；NORMAL(普通执行)10min 无操作进告警(黄色)+20min 自动降级到 NO_PERMISSION,副作用成功后 extend idle 计时；WATCHDOG(看门狗)默认 10h 到期降级到 NORMAL(不直接释放),不因空闲撤销,可选允许关机。危险、不可逆、require_confirm=true 和安全阻断永不绕过,watchdog 模式下 CONFIRM 级别操作仍弹窗=超时 deny。收到 success=false/status=cancelled 后不要重试施压；GUI 异常或 30 秒超时均 fail-closed",
            "受控托管模式（API 兼容名 mode=watchdog）：用户要求持续监控/看门场景（如睡前盯着任务跑完）时，调 screen_request_control(mode='watchdog', task_description='...', max_duration_hours=10)。watchdog 模式不因空闲撤销，默认 10h 硬上限到期降级到 NORMAL（config takeover_watchdog_default_hours/watchdog_max_hours 可调，1-999h 范围）。用户 AFK 时必须 fail-closed：无法通过已授权、可观测、安全路径解决的问题直接停止，不得绕过确认、放宽安全策略、切换未授权通道、修改配置或无限重试。不确定是否安全就放弃。takeover_confirm 禁用（用户不在场）。CONFIRM 级别操作仍弹窗=超时 deny；DANGER_KEYWORDS_BLOCK 仍拦截。关机不走 computer use，走 auto_shutdown 模块（独立 CLI 子进程）",
            "OCR bbox 已修复：普通文字控件直接定位；VL 昂贵且慢，默认只描述图像，坐标定位仅作无文字兜底",
            "verify_prompt 只用于高风险/失败重试/复杂视觉状态；普通文字操作优先 OCR/DOM 验证；batch_actions 不支持",
            # 评估文档 P0 焦点安全
            "FOCUS_LEAK_PREVENTED 不可绕过：前台是 ChatGPT/Codex/Trae/Cursor 等受保护进程时，即使 allow_unfocused_input=true 也零按键发送——必须先 focus_window 切换前台",
            "FOCUS_NOT_VERIFIED：键盘动作未指定 hwnd/window_title 且 allow_unfocused_input=false 时零按键发送；agent 必须显式传 hwnd 或显式承担风险",
            "STALE_COORDINATES：窗口移动/缩放后旧坐标失效，返回 blocked+not_sent；不要重试用旧坐标，必须重新 capture_screen 拿新 snapshot_id",
            "executed_unverified ≠ success：未声明 expected 时状态是 executed_unverified（已发送未验证），不要当成功上报；高风险动作必须传 expected={type:ocr_contains,text:...} 走 verified",
            "batch_actions 焦点漂移停止：stop_on_focus_drift=true（默认）时任一步 delivery_status=leaked 立即停止；如已预期焦点切换需显式传 false",
            "UWP/WinUI 同族 HWND：'设置'/'计算器' 等存在外框+核心子窗口多层 HWND，传任一同族 hwnd 都能正确匹配（canonical_window 已规范化）",
            # 第二轮评估 P0-2 Unicode 代理对
            "Unicode 输入：emoji（U+1F600）和 CJK Ext B（U+20000）等补充平面字符已按 UTF-16 拆分高低代理对分两次 SendInput；BMP 字符（U+0000–U+FFFF）单次 SendInput；不要假定 type 能直接送 emoji——已修复但复杂输入建议测试验证",
            # 第二轮评估 P0-3 状态语义分层
            "postcondition_failed ≠ blocked：动作已成功投递（transport=sent）但 OCR 后验失败时返回 postcondition_failed；动作未执行（transport=not_sent）才返回 blocked；不要把 postcondition_failed 当动作失败重试，要先查 delivery_status 判断是否焦点漂移",
            "transport=sent + delivery=delivered + postcondition=not_checked 不再被包装成 error：动作本身成功，只是没做后验——agent 据此判断是否需要补 expected 做后验",
            # 第二轮评估 P0-4 preview_action format=path
            "preview_action 默认 format=path：返回 path 字段（JPEG temp 文件路径），不污染 LLM 上下文；仅显式 format=inline 时返回 base64+mcp_image_block=true",
            # 第二轮评估 P1-1 dry_run
            "dry_run=true 通过所有前置校验但不投递输入事件（transport=not_sent）：危险操作前先 dry_run 验证焦点/坐标/危险关键词；受保护窗口焦点漂移测试必须用 dry_run，禁止真实输入到 ChatGPT/Codex",
            # 第二轮评估 P2-1 desktop_transaction
            "desktop_transaction vs batch_actions：事务必填 expected（事务级后验）+ rollback_policy（none/auto）+ rollback_actions；batch_actions 无事务语义；多步原子操作（如填表→提交→验证）用 desktop_transaction，纯顺序操作用 batch_actions",
            "desktop_transaction 事务状态：committed（全步骤成功+expected 通过）| postcondition_failed（全步骤成功但 expected 未满足）| aborted（某步失败中断）| rolled_back（失败+自动回滚成功）| rollback_failed（失败+回滚也失败）",
            # 评估文档 P0-5 UIA 语义层
            "UIA 语义优先：能用 screen_accessibility_snapshot + screen_semantic_action 就别用坐标点击——invoke/select/toggle 比 SendInput 稳定且无焦点依赖",
            "STALE_ELEMENT：element_id 绑定 snapshot_id，UI 树变化后失效；不要重试旧 element_id，重新 screen_accessibility_snapshot 拿新 element_id",
            "UIA_NOT_AVAILABLE：uiautomation 库不可用（非 Windows 或依赖缺失）时回退 OCR/视觉定位，不要假定 UIA 一定可用",
            "element_id ≠ RuntimeId：element_id 是 {canonical_hwnd}:{snapshot_id[:8]}:{element_index} 的快照内编号，语义动作通过 RuntimeId BFS 重新定位元素（不依赖 element_index）",
            # 第二轮评估 P0-1 CoInitialize
            "UIA CoInitialize：所有 UIA 入口已在线程级初始化 COM（pythoncom.CoInitialize 回退 ctypes CoInitializeEx），FastAPI 线程池模式下不再返回 WinError -2147221008；若仍失败返回 UIA_NOT_AVAILABLE fallback",
            # 第二轮评估 P1-4 ControlType 硬编码映射
            "UIA ControlType 硬编码映射：_CONTROL_TYPE_HARDCODED（41 项，50000=Button/50004=Edit/50032=Window 等，跨版本稳定）；未知 ID 返回 Unknown_{id} 前缀；agent 可稳定识别 Edit/Button/Document 等核心控件类型",
            # 评估文档 P1-A OCR 可观测性
            "OCR cold_start：首次调用需 ~10s 加载 PaddleOCR；若 config ocr.preload_on_startup=true 则后端启动时后台预热（不阻塞 startup）",
            "OCR /ocr/status 看四态：model_ready/cold_start/loading/failed；推理慢时查 load_elapsed_ms/last_inference_ms/inference_count 定位是冷启动还是单次推理慢",
            "OCR failed 状态：模型加载失败时 last_error 有错误信息；不要重试 OCR 调用，先查 last_error 修复配置",
            # 第三轮评估 P3-1：OCR fallback 语义
            "OCR 是 UIA 不可用时的 fallback，不是首选定位方式：UIA 友好应用（记事本/Win32 表单/系统设置）必须先 screen_accessibility_snapshot 拿 element_id，OCR 仅用于 UIA_NOT_AVAILABLE 或无语义控件的场景（自绘 UI/游戏/图片按钮）；UIA 语义动作（screen_semantic_action）比 OCR bbox + 坐标点击更稳定，无焦点漂移风险，且第三轮 P1-2 支持 expected={type:uia_value_equals,text} 内建后验无需再 snapshot",
            # 评估文档 P1-B 窗口生命周期
            "WINDOW_TOKEN_INVALID：window_token 含 canonical_hwnd + pid + process_create_time；hwnd 销毁/pid 复用/进程重启都会失效，必须重新 screen_window_resolve 拿新 token",
            "window_token vs hwnd 衔接：action 系列端点（execute_action/screen_ocr/focus_window/capture_screen 等）用 hwnd:int；只有 4 个窗口生命周期端点（minimize/restore/raise/close）才用 window_token:dict 做可选校验。screen_window_resolve 响应同时返回 canonical_hwnd（作 hwnd 用）和 window_token（作生命周期校验用）",
            "WINDOW_AMBIGUOUS：screen_window_resolve 多匹配时返回所有候选，不要随机选；用更具体 criteria（automation_id/pid）二次 resolve 缩小范围",
            "screen_window_close 默认 WM_CLOSE（优雅关闭，应用可弹保存对话框）；force=true 用 DestroyWindow（强杀，可能丢数据）；优先 WM_CLOSE",
            "screen_window_close/restore/minimize/raise 返回 post_state（exists/is_visible/is_minimized/is_foreground/title/bbox）——必须查 post_state 验证操作生效，不要假定成功",
            "screen_app_launch 用 DETACHED_PROCESS 独立子进程，shell=False 防命令注入；命令失败返回 success=false + error，不要重试同命令",
        ],
        "prerequisites": ["后端运行中", "管理员权限（键鼠操控需要）"],
        "level": "full",
    },
    "adhoc.browser_automation": {
        "skill": "browser_lessons",
        "name": "浏览器自动化",
        "skill_file": ".agents/skills/browser_lessons/SKILL.md",
        "keywords": ["浏览器操作", "登录网站", "网页操作", "下载报表", "填表", "浏览器自动化",
            "browser use", "网页点击", "网页填表", "网页提交",
            "操作网页", "自动登录", "网页截图", "浏览器任务",
        ],
        "description": "通用浏览器编排：snapshot→action→wait_for 闭环，支持任意网站的自动化操作",
        "memory_key": None,
        "first_action": "确认目标 URL 和操作意图；browser_session_create(url=...) 创建持久 session（自动注入站点经验）；browser_snapshot 看页面结构；browser_action 操作元素",
        "workflow_summary": "1.browser_session_create(url) 创建热态 session（自动注入站点经验） 2.browser_snapshot(session_id, interesting_only=true) 获取 ARIA tree 3.从 nodes 中找目标元素的 node_id 或 role+name 4.browser_action(session_id, target, action) 执行 click/fill/press/select 5.browser_wait_for(session_id, condition) 等待结果 6.验证：再 snapshot 确认状态变化 7.循环 2-6 直到目标达成 8.popup/filechooser/dialog 用 browser_wait_and_action 原子操作 9.browser_session_close 收尾（关闭 tab 释放资源，避免 Chrome tab 累积）",
        "mcp_tools_priority": [
            "browser_session_create (首步，热态 <250ms；自动注入 site_lessons)",
            "browser_snapshot (ARIA tree 定位元素，获取 node_id)",
            "browser_action (click/fill/press/select/check/hover/scroll_into_view)",
            "browser_wait_for (等待 navigation/url/text/selector/load_state/download)",
            "browser_wait_and_action (popup/filechooser/dialog 原子操作，支持 trigger_node_id)",
            "browser_evaluate (读 DOM 状态，只读)",
            "browser_navigate (back/forward/reload/goto)",
            "browser_screenshot (视口/全页/元素)",
            "browser_console_logs (抓 JS 报错)",
            "browser_match_site (查站点经验库)",
            "browser_session_close (收尾必步：关闭 tab 释放资源，避免 Chrome tab 累积)",
            "capture_screen / ocr_path (兜底)",
        ],
        "key_pitfalls": [
            "先查 browser_lessons/sites/<domain>.md 站点经验（browser_match_site 一键查），避免重复踩坑",
            "必须用调试浏览器 + connect_over_cdp(9222)，禁止无头模式",
            "STALE_NODE：snapshot 缓存 60 秒或 URL 变化后失效，需重新 snapshot 拿新 node_id",
            "MULTIPLE_MATCHES：多个匹配时报错，需用 nth 指定第几个或细化 target",
            "popup/filechooser/dialog 场景必须用 browser_wait_and_action 原子 API（同请求内 wait+trigger）",
            "dialog 被 Playwright 自动 dismiss，wait_for(dialog) 仅作意图声明",
            "console_logs 增量：传 since_cursor=上次 next_cursor 避免重复读",
            "session 失效时回退到无 session 模式（url_pattern 子进程）",
            "任务完成后必须调 browser_session_close 关闭 tab（close_page=true 默认），否则 Chrome tab 累积导致资源泄漏；idle 900s 超时只是兜底",
        ],
        "level": "standard",
    },
    "adhoc.auto_shutdown": {
        "skill": "auto_shutdown",
        "name": "自动关机（挂机监控）",
        "skill_file": ".agents/skills/auto_shutdown.md",
        "keywords": [
            "自动关机", "挂机关机", "等完成关机", "定时关机", "条件关机",
            "auto_shutdown", "监控关机", "后台关机", "睡前关机", "游戏挂机",
        ],
        "description": "agent 在 Trae 会话里自己跑 loop（sleep + 自定义检测），满足条件时调 POST /auto_shutdown/trigger 触发关机（120s 倒计时），用户可喊取消调 /auto_shutdown/cancel",
        "memory_key": None,
        "first_action": "确认关机触发条件（游戏完成 / 时间到 / 文件出现 / 自定义脚本）；agent 自己写检测 loop（time.sleep + 检测），条件满足时调 POST /auto_shutdown/trigger（task_id/reason/screenshot）；用户喊取消时调 /auto_shutdown/cancel。**调 trigger 前先关闭有窗口的程序（尤其模拟器/全屏应用），否则关机会被阻碍**",
        "workflow_summary": "1.确认触发条件 + task_id 2.agent 写检测 loop（sleep + 检测函数）3.条件满足 → 调 POST /auto_shutdown/trigger（端点内部自动截屏 + 推 inbox + 调 shutdown /s /t 120）4.120s 内用户反悔 → 调 POST /auto_shutdown/cancel（端点调 shutdown /a）5.关机完成 / 用户中止",
        "mcp_tools_priority": [
            "localagent_advanced_tool(tool='auto_shutdown_trigger', params={task_id, reason, screenshot, dry_run})",
            "localagent_advanced_tool(tool='auto_shutdown_cancel', params={dry_run})",
            "inbox_list (查关机通知历史)",
            "capture_screen / screen_ocr (检测条件时用，agent 自行决定)",
            "exec_python (跑检测脚本，agent 自行编写)",
        ],
        "key_pitfalls": [
            "agent 自己写检测脚本，不依赖旧 monitor.py（已删除）",
            "dry_run=true 用于测试，生产用 dry_run=false",
            "120s 倒计时内可调 cancel 中止（shutdown /a）",
            "调 trigger 前 agent 应先关闭有窗口的程序（尤其模拟器/全屏应用），否则关机会被阻碍",
            "关机命令绕过 command_guard（用户睡前无法审批，已明确同意关机）",
            "端点幂等：重复 trigger 递增 trigger_count，重复 cancel 不报错",
            "inbox/截屏失败不阻塞关机（端点内部 try/except 兜底）",
        ],
        "level": "standard",
    },

    # ────────────── adhoc.headless_session（后端自主 agent 会话，可一次性可周期性）──────────────
    "adhoc.headless_session": {
        "skill": "headless_session",
        "name": "后端自主 agent 会话（headless session）",
        "skill_file": None,  # 工作流定义，无独立 skill 文件；详见 agent_guide_data.py 本条目
        "keywords": [
            "headless session", "后端 agent", "后台 agent", "定时 agent 任务",
            "无头会话", "headless_agent", "judge agent", "判定 agent",
            "后端自主会话", "定时触发 agent", "周期 agent 任务",
        ],
        "description": (
            "后端独立管理完整 agent 交互任务（定时触发 → 静默执行 → 客户端 chat 面板查看历史/实时运行），"
            "不依赖 IDE 在线。主 agent 跑完后由 judge agent 判定 success/failed/uncertain，"
            "uncertain 一律算 failed。资源限制宽松（max_iterations=100 / "
            "wall_clock_budget_secs=1800s），支持 [loops.headless_session] 配置覆盖。"
            "B2 后只记账 token 不控制 cost 预算。"
        ),
        "memory_key": None,
        "first_action": (
            "用户问'后端 agent 怎么配'/'定时跑个 agent 任务'时：\n"
            "1. 引导用户编辑 config.toml 的 [loops.headless_session] 段（参考 config.example.toml 模板）\n"
            "2. 关键字段：trigger_text（触发任务文本）/ skill（绑定 skill 取 first_action）/"
            "one_shot（一次性 true / 周期 false）/ cron（周期表达式）\n"
            "3. 资源限制可选覆盖：max_iterations / wall_clock_budget_secs / "
            "judge_max_iterations / judge_wall_clock_budget_secs\n"
            "4. 跑完后查 client chat 面板（[后端] / [判定] badge 区分主会话和 judge 会话）\n"
            "5. 需中断时调 POST /headless/sessions/{session_id}/interrupt"
        ),
        "workflow_summary": (
            "1.config.toml 配 [loops.headless_session] 2.后端重启自动加载 3.cron/interval 触发 → "
            "HeadlessSessionAction.execute() 4.run_main_agent(主会话 mode=headless) "
            "5.run_judge_agent(judge 会话 mode=headless_judge, 5 条降级规则全 → uncertain) "
            "6.verdict=success → 整体 success / verdict!=success（含 uncertain）→ 整体 failed + fail_count +1 "
            "7.写 memory_set + wip_create + one_shot 时 pause_task"
        ),
        "mcp_tools_priority": [
            "Read (config.toml [loops.headless_session] 段)",
            "Edit (config.toml 修改触发条件/资源限制)",
            "Read (config.example.toml 模板参考)",
            "localagent_advanced_tool(tool='loop_list_tasks') (查后端 loop 任务状态)",
            "exec_python (调 server.activity_tracker.headless_runner.run_main_agent 验证配置生效)",
        ],
        "key_pitfalls": [
            "judge verdict=uncertain 一律算 failed（spec 用户硬要求），不要为了让任务通过把 uncertain 当 success",
            "资源限制默认值宽松（max_iterations=100 / wall_clock_budget_secs=1800s），"
            "正常任务能跑完；不要为了让测试通过而调小（spec Anti-Cheat 明令禁止）。"
            "B2 后只记账 token 不控制 cost 预算",
            "主 agent 和 judge agent 用独立的 RunnerConfig（main 用 max_iterations / judge 用 judge_max_iterations），"
            "不要混用",
            "中断主会话 → judge 不启动 → 整体 failed；中断 judge 会话 → 降级 uncertain → 整体 failed",
            "headless session 写同一个 agent.db（WAL 模式支持并发写），不要改成独立 DB（spec 已验证 WAL 可用）",
            "trigger_text 留空 + skill 指定时，HeadlessSessionAction 会调 agent_guide(task_type=skill) 取 first_action 作为 trigger_text",
            "one_shot=true 的任务执行后自动 pause（不重复触发）；周期任务 one_shot=false 按 cron 周期触发",
            "client 进程不在线时后端仍能跑（headless session 完全后端自主，不依赖 IDE）",
        ],
        "prerequisites": [
            "后端运行中（端口 8766 可达）",
            "config.toml 含 [loops.headless_session] 段（参考 config.example.toml）",
            "LLM pool 可用（[model] 配置 + keys.json 有效）",
        ],
        "level": "standard",
    },
    "dev.client_dev": {
        "skill": "client_dev",
        "name": "PySide6客户端开发",
        "skill_file": ".agents/skills/client_dev.md",
        # 移除重复的"客户端开发"（与"开发客户端"语义重复）
        "keywords": ["开发客户端", "GUI开发", "新增面板", "面板卡顿", "PySide6", "客户端性能", "面板开发", "widget优化"],
        "description": "PySide6面板开发+性能规范+异步加载模式",
        "memory_key": None,
        "first_action": "读取 .agents/skills/client_dev.md 获取面板架构和性能规范",
        "workflow_summary": "1.读skill文件了解架构 2.修改/新增面板 3.遵循性能规范(on_show轻量/异步加载/N+1修复) 4.启动客户端验证",
        "mcp_tools_priority": [
            "Read / Edit / Write",
            "exec_python (启动客户端验证)",
        ],
        "key_pitfalls": [
            "on_show() 不能做重活：同步DB查询/大量widget重建会导致面板切换卡顿",
            "N+1查询：循环中每行查DB改为一次性预取",
            "QThread引用必须存为实例属性，否则被GC回收",
            "SQLite跨线程需要check_same_thread=False（AccountingStore已配置）",
        ],
        "level": "standard",
    },
    "dev.anti_hallucination": {
        "skill": "anti_hallucination",
        "name": "反幻觉工作流",
        "skill_file": ".agents/skills/anti_hallucination/SKILL.md",
        "keywords": [
            # "修 bug" 系（用户明确要修 bug 时触发）
            "修 bug", "修这个 bug", "修个小 bug",
            # "理解现有代码"场景（用户想读懂代码）
            "这段代码怎么工作", "这段代码什么意思", "帮我理解代码",
            # "为什么"系（用户明确表达要理解原因）
            "为什么不对", "为什么不显示", "为什么报错",
            # "小修补"场景（用户强调小范围，区别于 goal_engineering 的"新功能/中等以上复杂度"）
            "小修小补", "改这个小地方", "改一行代码",
            # "防幻觉/debug"场景
            "帮我 debug", "帮我看看哪里错了", "排查报错", "定位问题",
        ],
        "description": "日常小修改的6步防幻觉安全网（索引先于动手，读到再用，禁止编造，方案先确认）",
        "memory_key": None,
        "first_action": "读取 .agents/skills/anti_hallucination/SKILL.md 获取完整工作流和4种模式模板；棘手 bug 转 references/diagnosing-bugs.md",
        "workflow_summary": "1.定位起点(grep/索引) 2.建立上下文(Read依赖+Grep调用方) 3.压缩(超限时摘要) 4.对齐确认(等用户确认方向) 5.可选log调试(Bug模式) 6.提方案(等确认后执行)",
        "mcp_tools_priority": [
            "Read / Grep / SearchCodebase (建立上下文)",
            "Glob (定位文件)",
            "exec_python (Step 5 log插桩)",
        ],
        "key_pitfalls": [
            "跳过Step 2直接动手=凭想象写代码，幻觉高发区",
            "禁止编造方法名/字段名，必须有Read/Grep证据",
            "Step 4必须让用户确认方向后才能列方案",
            "Bug模式证据不足时走Step 5加log，不要硬猜根因",
            "大任务走task_closure SDD链，本skill只适用于小修小补",
        ],
        "level": "standard",
    },

    # ────────────── dev.engineering（工程化开发，借鉴 mattpocock/skills 改造）──────────────
    "dev.goal_engineering": {
        "skill": "goal_engineering",
        "name": "目标工程编排",
        "skill_file": ".agents/skills/dev/goal_engineering/SKILL.md",
        # 移除单独"开发"（太泛，与所有 dev 桶 skill 冲突）
        # 保留组合词"开发新功能"（明确新功能场景）
        "keywords": ["做个功能", "重构", "帮我定目标", "定义目标", "让 agent 自己跑", "goal engineering", "目标工程", "开发新功能", "做个模块"],
        "description": "开发任务统一入口：询问→写方案→执行三阶段闸门流程，引用 leader 心法",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/goal_engineering/SKILL.md 获取三阶段流程；引用 dev.leader 的七问+Harness 心法；阶段1路由 dev.grilling 追问，阶段2路由 dev.to_spec 写融合 Harness 的 spec，阶段3路由 dev.to_tickets+dev.implement 执行到底",
        "workflow_summary": "1.询问(调研+追问≤5问+拍板) 2.写方案(spec融合Harness,审核闸门) 3.执行(拆tickets+implement,不再询问)",
        "mcp_tools_priority": ["Read", "AskUserQuestion (阶段闸门)", "Write (design-decisions.md/spec.md)", "agent_guide(dev.grilling/dev.to_spec/dev.to_tickets/dev.implement)"],
        "key_pitfalls": [
            "执行阶段不再询问用户，遇问题按 spec 的 Anti-Cheat 节自行决策",
            "上下文压缩后必须先读 planning_notes/<slug>/spec.md + tickets.md 续跑，禁止重拆重写",
            "小修小补不走本流程，走 dev.anti_hallucination",
            "方案审核通过是关键闸门，通过后不可逆",
        ],
        "level": "full",
    },
    "dev.leader": {
        "skill": "leader",
        "name": "目标工程方法论",
        "skill_file": ".agents/skills/dev/leader/SKILL.md",
        # method role：只留方法论标识词，不抢 entry (dev.goal_engineering) 的用户触发词。
        # 用户说"帮我定目标/定义目标/让 agent 自己跑/目标工程/goal engineering"时路由到 goal_engineering (entry)，
        # goal_engineering 的 first_action 会引用本 skill 拿七问+五种死法心法。
        # 详见 docs/agent-guide-keywords.md 原则 2。
        "keywords": [
            "Commander's Intent", "Harness", "目标七问", "五种死法",
            "leader 心法", "目标任务书结构", "leader",
        ],
        "description": "目标工程方法论源：Commander's Intent + 目标七问 + Harness 五种死法",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/leader/SKILL.md 获取七问+五种死法心法；读 references/anatomy.md 获取任务书六节结构规格。本 skill 是方法论参考，被 dev.goal_engineering 编排引用",
        "workflow_summary": "1.调研(实测代码库+联网) 2.提问≤5个(该做/不该做拍板) 3.写方案(六节结构) 4.交付 5.验收(明卷+暗卷)",
        "mcp_tools_priority": ["Read", "AskUserQuestion (≤5个问题)", "Write (planning_notes/<slug>/spec.md)", "WebSearch / WebFetch (联网调研)"],
        "key_pitfalls": [
            "Harness 比 Goal 更重要：没有 Harness 的目标，agent 永远会找到捷径",
            "防五种死法：作弊达标/幻觉命令/失忆/一条道走到黑/静默事故",
            "本项目无 /goal，产物写入 planning_notes/<slug>/spec.md 融合六节",
            "来源 KKKKhazix/khazix-skills/leader，保留原作者署名",
        ],
        "level": "standard",
    },
    "dev.cross_workspace_advisor": {
        "skill": "cross_workspace_advisor",
        "name": "工作区决策树（跨工作区/生活/临时任务）",
        "skill_file": ".agents/skills/dev/cross_workspace_advisor/SKILL.md",
        # 移除跨 skill 重复的"记账"（recurring.accounting 是真源）和太泛的"日程"
        # 保留组合词"生活记账"（明确生活场景，不与 accounting 冲突）
        "keywords": [
            "开发网站", "开发游戏", "写后端", "做 App", "跨工作区开发", "无关开发", "新项目开发",
            "开发一个", "做个独立项目", "生活任务", "临时任务", "工作区决策", "其他工作",
            "临时脚本", "生活记账", "排个班", "健身记录",
        ],
        "description": "工作区决策树：非本项目维护的请求按决策树选工作区位置（新项目文件夹/temp/workspace），建立后询问是否协助打开，完成时明确工作区位置",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/cross_workspace_advisor/SKILL.md 获取决策树。第一层判本项目维护（是→不走本skill）；第二层分支A独立项目级开发(4项全满足)→新项目文件夹+dev_toolkit+重开对话；分支B生活/其他工作→自主判断临时性(一次就结束/一天内失效=临时→temp/；反复使用/项目插件=持久→workspace/)，判断不了用AskUserQuestion询问(必须给判断标准)。分支B建文件夹后询问是否协助打开，完成时汇总明确工作区位置",
        "workflow_summary": "1.第一层判本项目维护(是→不走) 2.第二层分支A(独立项目级,4项全满足)→新项目文件夹+dev_toolkit+重开对话 3.分支B(生活/其他)→自主判断临时性 4.B临时→temp/ B持久→workspace/ 判断不了→AskUserQuestion 5.通用收尾:建后询问是否打开+完成时明确工作区位置",
        "mcp_tools_priority": [
            "AskUserQuestion (分支A确认+询问路径 / 分支B判断不了时询问临时持久 / 通用收尾询问是否打开)",
            "exec_python (分支A复制dev_toolkit / 分支B建文件夹 / 通用收尾os.startfile打开)",
            "Read (dev_toolkit 结构)",
        ],
        "key_pitfalls": [
            "第一层先判本项目维护：是本项目维护就不走本skill，避免误路由",
            "分支A严守4项条件：独立项目级开发才走新项目文件夹，不要把生活临时任务误导到新项目",
            "分支B先自主判断：能判断就不问用户，判断不了再问且必须给明确判断标准(一次就结束/一天内失效=临时)",
            "不跨区操作：绝不在此工作区写另一项目的代码",
            "必须告知工作区位置：分支B建立后询问是否打开，完成时汇总明确绝对路径，严防用户找不到",
            "temp/ 可被 system.temp_cleanup 清理；workspace/ 长期保留",
        ],
        "level": "standard",
    },
    "dev.tdd": {
        "skill": "tdd",
        "name": "测试驱动开发",
        "skill_file": ".agents/skills/dev/tdd/SKILL.md",
        "keywords": ["TDD", "测试驱动", "红绿循环", "red-green-refactor", "先写测试", "测试先行"],
        "description": "测试驱动开发（TDD）：先写失败测试→写最小实现→重构",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/tdd/SKILL.md 获取 TDD 工作流；读 dev/tdd/tests.md 和 refactoring.md 了解测试与重构细节",
        "workflow_summary": "1.RED写失败测试 2.GREEN写最小实现 3.REFACTOR重构 4.重复",
        "mcp_tools_priority": ["Read / Edit / Write", "exec_python (uv run pytest)"],
        "key_pitfalls": [
            "测试命令：uv run pytest（测试）或 uv run python -m server.main（启动验证）",
            "不要跳过 RED 阶段直接写实现",
            "每轮 RED→GREEN→REFACTOR 只处理一个测试用例",
        ],
        "level": "standard",
    },
    "dev.codebase_design": {
        "skill": "codebase-design",
        "name": "代码库设计",
        "skill_file": ".agents/skills/dev/codebase-design/SKILL.md",
        "keywords": ["代码库设计", "架构设计", "deep module", "shallow module", "seam", "design it twice", "codebase design", "深化机会"],
        "description": "代码库架构设计原则：deep modules、seams、deletion test、design-it-twice",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/codebase-design/SKILL.md 获取架构词汇和原则；读 DEEPENING.md 和 DESIGN-IT-TWICE.md 了解细节",
        "workflow_summary": "1.识别 shallow modules 2.应用 deletion test 3.design-it-twice 探索接口 4.深化为 deep modules",
        "mcp_tools_priority": ["Read / Grep / SearchCodebase"],
        "key_pitfalls": [
            "使用规范术语：module/interface/depth/seam/adapter/leverage/locality",
            "不要漂移到 component/service/API/boundary",
        ],
        "level": "standard",
    },
    "dev.domain_modeling": {
        "skill": "domain-modeling",
        "name": "领域建模",
        "skill_file": ".agents/skills/dev/domain-modeling/SKILL.md",
        "keywords": ["领域模型", "通用语言", "ubiquitous language", "术语表", "CONTEXT.md", "ADR", "架构决策记录", "domain modeling", "领域术语"],
        "description": "构建领域模型、维护 CONTEXT.md 词汇表、记录 ADR",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/domain-modeling/SKILL.md；读 ADR-FORMAT.md 和 CONTEXT-FORMAT.md 了解格式",
        "workflow_summary": "1.识别领域术语 2.写入 CONTEXT.md 3.重要决策写 ADR 4.维护术语一致性",
        "mcp_tools_priority": ["Read / Write / Edit"],
        "key_pitfalls": [
            "CONTEXT.md 是项目词汇表的真源",
            "ADR 存 docs/adr/ 目录，编号递增",
        ],
        "level": "standard",
    },
    "dev.resolving_merge_conflicts": {
        "skill": "resolving-merge-conflicts",
        "name": "解决合并冲突",
        "skill_file": ".agents/skills/dev/resolving-merge-conflicts/SKILL.md",
        "keywords": ["合并冲突", "merge conflict", "resolve conflict", "解决冲突", "rebase 冲突", "git 冲突"],
        "description": "解决 git 合并冲突的规范流程",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/resolving-merge-conflicts/SKILL.md 获取冲突解决规则",
        "workflow_summary": "1.读冲突文件 2.理解双方意图 3.合并语义而非文本 4.运行测试验证",
        "mcp_tools_priority": ["Read / Edit", "exec_python (uv run python -m server.main 验证启动)"],
        "key_pitfalls": [
            "绝不 git merge --abort",
            "不发明新行为，只合并双方既有意图",
            "记录 trade-off 在 commit message",
            "测试命令：uv run python -m server.main",
        ],
        "level": "standard",
    },
    "dev.to_spec": {
        "skill": "to-spec",
        "name": "写 Spec",
        "skill_file": ".agents/skills/dev/to-spec/SKILL.md",
        "keywords": ["写 spec", "整理 spec", "出 PRD", "把讨论记下来", "to-spec", "规格说明"],
        "description": "把当前对话综合成 spec 并存档（不做访谈）",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/to-spec/SKILL.md 获取 spec 模板和存档流程",
        "workflow_summary": "1.综合已讨论内容 2.写 spec 到 workspace/specs/<slug>.md 3.wip_create(type='spec') 关联 4.标注 seams",
        "mcp_tools_priority": ["Write", "wip_create (type='spec', extra_data={spec_path, seams, status, source})"],
        "key_pitfalls": [
            "不做访谈，只综合已经讨论的内容",
            "spec 存 workspace/specs/<feature-slug>.md",
            "用 wip_create 关联 spec 与任务",
        ],
        "level": "standard",
    },
    "dev.to_tickets": {
        "skill": "to-tickets",
        "name": "拆 Tickets",
        "skill_file": ".agents/skills/dev/to-tickets/SKILL.md",
        "keywords": ["拆 ticket", "拆任务", "to-tickets", "spec 转 ticket", "任务拆解", "tracer bullet"],
        "description": "把 spec 拆成可执行的 tickets（tracer-bullet 切片）",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/to-tickets/SKILL.md 获取 ticket 拆分原则",
        "workflow_summary": "1.读 spec 2.识别 tracer-bullet 切片 3.拆 tickets 到 workspace/tickets/<slug>/ 4.wip_create(type='ticket') 关联",
        "mcp_tools_priority": ["Read", "Write", "wip_create (type='ticket', extra_data={ticket_path, feature_slug, blocked_by, parent_spec_id, acceptance_criteria})"],
        "key_pitfalls": [
            "tracer-bullet 是端到端垂直切片，有阻塞边",
            "ticket 存 workspace/tickets/<feature-slug>/<NN>-<slug>.md",
            "用 extra_data.blocked_by 表达阻塞关系",
        ],
        "level": "standard",
    },
    "dev.wayfinder": {
        "skill": "wayfinder",
        "name": "Wayfinder 寻路",
        "skill_file": ".agents/skills/dev/wayfinder/SKILL.md",
        "keywords": ["wayfinder", "寻路", "fog of war", "战争迷雾", "探索地图", "chart the map", "work through the map"],
        "description": "处理 fog of war（未知领域）：chart the map 或 work through the map",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/wayfinder/SKILL.md 了解双模式：chart the map（建图）vs work through the map（执行）",
        "workflow_summary": "1.识别 fog of war 2.chart the map 建图(workspace/wayfinder/) 3.work through the map 领取 ticket 执行 4.wip_create(type='wayfinder_map'/'wayfinder_ticket')",
        "mcp_tools_priority": ["Read / Write", "wip_create / wip_list / wip_update"],
        "key_pitfalls": [
            "ticket 类型：research/prototype/grilling/task（HITL vs AFK）",
            "claim 机制：wip_update(extra_data={claimed_by, claimed_at})",
            "地图存 workspace/wayfinder/<map-slug>.md",
        ],
        "level": "full",
    },
    "dev.prototype": {
        "skill": "prototype",
        "name": "原型探索",
        "skill_file": ".agents/skills/dev/prototype/SKILL.md",
        # 避免泛动词 keyword："试一下/看看效果"是语气助词会被泛用
        # （用户说"试一下保存文章"会被 prototype 抢，实际意图是 web_archive）
        # 详见 docs/agent-guide-keywords.md 原则 4。
        "keywords": ["原型", "prototype", "试几种方案", "做原型", "设计变体", "UI 选项", "prototype this", "let me play with it"],
        "description": "构建一次性原型细化设计：UI 多变体切换 / 状态机终端驱动",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/prototype/SKILL.md 选择分支：UI 问题走 references/UI.md，Logic 问题走 references/LOGIC.md",
        "workflow_summary": "1.识别问题类型(UI/Logic) 2.选分支 3.构建 throwaway prototype(temp/prototype_<name>/) 4.用户驱动验证 5.winner 折进真实 code 6.清理 prototype",
        "mcp_tools_priority": ["Read / Write", "exec_python (Logic 分支终端运行)", "browser_navigate (UI 分支预览)"],
        "key_pitfalls": [
            "prototype 是 throwaway，不写 tests 不抽象，命名要明显是 prototype",
            "完成后 winner 折进真实 code，其余移到 temp/prototype_<name>/ 不进 main",
            "Logic 分支要把 logic 隔离为 portable module，TUI 是 throwaway shell",
            "UI 分支变体要结构不同（layout/hierarchy/affordance），不是只调颜色",
        ],
        "level": "standard",
    },
    "dev.implement": {
        "skill": "implement",
        "name": "实现 Ticket",
        "skill_file": ".agents/skills/dev/implement/SKILL.md",
        "keywords": ["implement", "实现 ticket", "领取 ticket", "执行 ticket", "开发 ticket"],
        "description": "领取并实现 ticket 的 6 步流程",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/implement/SKILL.md 获取 6 步实现流程",
        "workflow_summary": "1.领取 ticket(wip_update claimed_by) 2.TDD 实现(可选 dev.tdd) 3.定期检查 4.代码审查 5.提交 6.更新 ticket 状态",
        "mcp_tools_priority": ["wip_update (claim)", "Read / Edit / Write", "exec_python (测试)"],
        "key_pitfalls": [
            "领取前检查 extra_data.blocked_by 是否已解除",
            "实现完更新 ticket 状态为 done",
            "代码审查可参考 code_review skill",
        ],
        "level": "standard",
    },
    "dev.triage": {
        "skill": "triage",
        "name": "分诊 Issues",
        "skill_file": ".agents/skills/dev/triage/SKILL.md",
        "keywords": ["triage", "分诊", "issue 分诊", "bug 分类", "feature request 分类", "审查 issue"],
        "description": "通过角色驱动的状态机分诊 issues（category + state）",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/triage/SKILL.md 获取分诊角色和状态机",
        "workflow_summary": "1.收集 issue 2.推荐 category(bug/enhancement)+state 3.可选 grill-with-docs 4.应用 outcome 5.wip_update(extra_data={category, state})",
        "mcp_tools_priority": ["Read", "wip_list / wip_update (extra_data.category, extra_data.state)"],
        "key_pitfalls": [
            "AI disclaimer 必填：> *This was generated by AI during triage.*",
            "category: bug/enhancement",
            "state: needs-triage/needs-info/ready-for-agent/ready-for-human/wontfix",
        ],
        "level": "standard",
    },
    "dev.code_review": {
        "skill": "code_review",
        "name": "代码审查",
        "skill_file": ".agents/skills/code_review.md",
        "keywords": ["code review", "代码审查", "review", "审查", "PR review", "review since", "双轴审查"],
        "description": "全量/定向/diff-based 双轴代码审查",
        "memory_key": None,
        "first_action": "读取 .agents/skills/code_review.md 获取审查工作流（全量/定向/diff-based 三种模式）",
        "workflow_summary": "1.确定审查范围 2.按 Standards+Spec 双轴审查 3.分严重级输出 findings（diff-based 模式）；或按模块分批次审查（全量模式）",
        "mcp_tools_priority": ["Read / Grep / SearchCodebase", "exec_python (uv run python -m server.main 验证)"],
        "key_pitfalls": [
            "diff-based 模式：Standards 轴用 repo 已记录的 standard 优先于 Fowler 12 smells baseline",
            "diff-based 模式：两轴报告分离展示，不合并选总冠军",
            "区分硬违规（documented-standard breach）与 judgement call（baseline smell）",
        ],
        "level": "standard",
    },
    "dev.grill_me": {
        "skill": "grill-me",
        "name": "拷问我的计划",
        "skill_file": ".agents/skills/dev/grill-me/SKILL.md",
        "keywords": ["grill me", "grill-me", "拷问我", "压力测试计划", "找漏洞", "追问计划", "打磨计划"],
        "description": "对计划或设计进行持续追问式访谈，压力测试其稳健性",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/grill-me/SKILL.md；本 skill 是 dev.grilling 的入口",
        "workflow_summary": "1.路由到 dev.grilling 2.逐个问题追问 3.每个问题附推荐答案 4.达成共同理解才执行",
        "mcp_tools_priority": ["Read", "AskUserQuestion (逐个问题)"],
        "key_pitfalls": [
            "一次只问一个问题",
            "fact 能查到的不要问用户，decision 才问",
        ],
        "level": "standard",
    },
    "dev.grilling": {
        "skill": "grilling",
        "name": "追问访谈核心",
        "skill_file": ".agents/skills/dev/grilling/SKILL.md",
        # support role：只留方法论标识词，不抢 entry (dev.grill_me) 的用户触发词。
        # 用户说"拷问我/找漏洞/打磨计划/追问计划"时路由到 grill_me (entry)，
        # grill_me 的 first_action 会引用本 skill 拿核心访谈逻辑。
        # 详见 docs/agent-guide-keywords.md 原则 2。
        "keywords": ["grilling", "design tree", "grill 核心访谈", "深度追问", "反复拷问", "持续压力测试"],
        "description": "围绕计划或设计持续追问用户的核心访谈逻辑",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/grilling/SKILL.md 获取追问规则",
        "workflow_summary": "1.沿 design tree 每个分支追问 2.每个问题附推荐答案 3.一次只问一个 4.达成共同理解前不执行",
        "mcp_tools_priority": ["AskUserQuestion (逐个问题)", "Read / Grep (查 fact)"],
        "key_pitfalls": [
            "一次只问一个问题，问多个会失去方向",
            "fact 能通过探索 codebase 找到就直接查，不要问用户",
            "decisions 属于用户，逐个交给用户决定",
        ],
        "level": "standard",
    },
    "dev.grill_with_docs": {
        "skill": "grill-with-docs",
        "name": "边拷问边沉淀文档",
        "skill_file": ".agents/skills/dev/grill-with-docs/SKILL.md",
        "keywords": ["grill-with-docs", "grill with docs", "边追问边记录", "边拷问边出 ADR", "边打磨边沉淀文档"],
        "description": "对计划或设计进行持续追问式访谈，并在过程中沉淀 ADR 和项目词汇表",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/grill-with-docs/SKILL.md；本 skill 路由到 dev.grilling + dev.domain_modeling",
        "workflow_summary": "1.路由到 dev.grilling 追问 2.同时路由到 dev.domain_modeling 沉淀 ADR 和 CONTEXT.md 3.边追问边记录",
        "mcp_tools_priority": ["Read", "AskUserQuestion", "Write (ADR / CONTEXT.md)"],
        "key_pitfalls": [
            "比 dev.grill_me 更重，适合需要长期决策记录的场景",
            "ADR 存 docs/adr/，词汇表更新 CONTEXT.md",
        ],
        "level": "standard",
    },
    "dev.impeccable": {
        "skill": "impeccable",
        "name": "前端设计工艺",
        "skill_file": ".agents/skills/dev/impeccable/SKILL.md",
        # 移除"redesign"（与 dev.hallmark 重复，hallmark 的 4 动词之一更核心）
        "keywords": ["impeccable", "前端设计", "UI 设计", "craft", "shape", "audit UI", "polish", "critique", "落地页", "dashboard 设计", "组件设计", "UI 太丑", "AI 味"],
        "description": "工程化前端设计工艺 skill（融合自 pbakaus/impeccable v3.9.1 Apache-2.0）：23 命令覆盖从 craft 新 UI 到 polish 现有 UI 到 audit AI 痕迹到 distill 设计 DNA 的完整设计工艺流程。命令动词含 design/redesign/shape/critique/audit/polish/harden/optimize/adapt/animate/colorize/extract/bolder/quieter/delight/onboard/layout/typeset/live mode，当用户要改进前端界面时调用",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/impeccable/SKILL.md；根据用户动词映射到 23 命令之一（模糊动词默认 polish）；读对应 reference/<cmd>.md 规范；读 reference/brand.md 或 reference/product.md 选 register（不可跳过）",
        "workflow_summary": "1.Setup 跳过 context.mjs，agent 直接读项目根 PRODUCT.md/DESIGN.md 2.按命令规范执行 craft/audit/polish 等 3.对照 absolute bans 自检（side-stripe borders / gradient text / glassmorphism / identical card grids 等）4.mobile 必检 320/375/414/768 px 4 档",
        "mcp_tools_priority": [
            "Read（读 upstream/SKILL.md + reference/<cmd>.md + register.md）",
            "localagent_agent_chat 或 localagent_advanced_tool(llm_pool_call)（生成设计代码 / 文档）",
            "browser_navigate + browser_snapshot + browser_take_screenshot（audit 验证现有页面 / live 模式实时迭代）",
            "Write（产出设计代码 / 规范文档）",
        ],
        "key_pitfalls": [
            "本项目无 node 环境，scripts/*.mjs 仅作方法论参考，不主动执行",
            "Setup 阶段原版 context.mjs 改为 agent 直接读 PRODUCT.md/DESIGN.md",
            "register 选择必读 brand.md 或 product.md 之一，跳过会产出 generic 输出",
            "absolute bans 触发即重写整元素，不要修补",
            "颜色必用 OKLCH 不混 hex/rgb；mobile 4 档必检",
            "与 dev.hallmark 互补：impeccable craft 后可用 hallmark audit 二次检查 AI slop",
        ],
        "level": "full",
        "vendor_repo": "https://github.com/pbakaus/impeccable",
        "vendor_path": ".agents/skills/dev/impeccable/upstream/",
    },
    "dev.hallmark": {
        "skill": "hallmark",
        "name": "反 AI-slop 设计",
        "skill_file": ".agents/skills/dev/hallmark/SKILL.md",
        "keywords": ["hallmark", "反 AI 味", "AI slop", "audit 设计", "redesign", "study 设计", "extract 设计 DNA", "build 落地页", "设计主题", "避免 AI 味", "看起来手工做的", "design.md"],
        "description": "反 AI-slop 专项设计 skill（融合自 nutlope/hallmark v1.1.0 MIT）：4 动词（build/audit/redesign/study）+ 20 内置主题 + 58 slop-test 检查门 + 结构多样性规则，让 UI 看起来是手工做的不是 AI 生成的",
        "memory_key": None,
        "first_action": "读取 .agents/skills/dev/hallmark/SKILL.md；根据用户动词分流：default → Design flow；audit → 读 references/anti-patterns.md 对照检查不编辑；redesign → in-place 修改保留 route tree；study → URL 走 WebFetch 读 HTML/CSS、image 走截图分析",
        "workflow_summary": "1.选动词（build/audit/redesign/study）2.选主题（20 catalog 或 custom 分支，按 diversification rule 不重复）3.产出设计代码 4.对照 6 大 disciplines 自检（pre-emit critique / honest copy / locked tokens / no re-drawn chrome / mobile 4 档 / no italic headers）5.可选 emit design.md（study 命令）",
        "mcp_tools_priority": [
            "Read（读 upstream/SKILL.md + references/<topic>.md）",
            "localagent_agent_chat 或 localagent_advanced_tool(llm_pool_call)（生成设计代码 / DNA 提取 / audit 报告）",
            "browser_navigate + browser_snapshot + browser_take_screenshot（audit 验证现有页面）",
            "WebFetch（study URL mode 读 HTML/CSS）",
            "Write（产出设计代码 / design.md / audit punch list）",
        ],
        "key_pitfalls": [
            "audit 命令永不编辑，只返回 ranked punch list",
            "redesign 默认 in-place 修改，不重建 route tree；多组件删除需用户显式确认",
            "study URL mode 严格：emit design.md 需用户 attest 来源是自己拥有或公共参考",
            "diversification rule：同一会话内不同 brief 不重复使用相同主题，记到 workspace/hallmark/log.json",
            "mobile 4 档必检：320 / 375 / 414 / 768 px",
            "与 dev.impeccable 互补：hallmark build 后可用 impeccable polish 打磨 typography/motion 细节",
        ],
        "level": "full",
        "vendor_repo": "https://github.com/nutlope/hallmark",
        "vendor_path": ".agents/skills/dev/hallmark/upstream/",
    },

    # ────────────── daily（日常事务，借鉴 mattpocock/skills 改造）──────────────
    "daily.teach": {
        "skill": "teach",
        "name": "教学",
        "skill_file": ".agents/skills/daily/teach/SKILL.md",
        "keywords": ["teach", "教我", "我想学", "学一下", "给我上课", "讲解概念", "teach me", "learn"],
        "description": "在持久化教学工作区中教用户新技能或概念（多 session 状态化）",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/teach/SKILL.md；确定 topic-slug，检查 workspace/teaching/<topic-slug>/ 是否已存在",
        "workflow_summary": "1.确定 mission(workspace/teaching/<slug>/MISSION.md) 2.填充 RESOURCES.md 3.按 zone of proximal development 设计 lesson 4.产出 lessons/*.html + reference/*.html 5.记录 learning-records/",
        "mcp_tools_priority": ["Read / Write", "WebSearch / WebFetch (找 trusted resources)"],
        "key_pitfalls": [
            "教学工作区在 workspace/teaching/<topic-slug>/，不是当前目录",
            "lesson 是自包含 HTML，存 lessons/0001-<slug>.html",
            "reference docs 存 reference/*.html",
            "MISSION.md 是教学 grounding，必须先填",
        ],
        "level": "full",
    },
    "daily.cangjie_extraction": {
        "skill": "cangjie_extraction",
        "name": "蒸馏长内容为方法论 skill",
        "skill_file": ".agents/skills/daily/cangjie_extraction/SKILL.md",
        "keywords": ["拆书", "蒸馏", "提取方法论", "把书做成 skill", "cangjie", "RIA-TV++", "把视频做成 skill", "蒸馏一本书", "抽象出理论", "distill book"],
        "description": "把书/长视频转写/播客文字稿/课程/访谈蒸馏成原子化、可被 agent 调用的方法论 skill（RIA-TV++ 流水线）；也用于 agent 主动识别任务中可复用方法论并抽象",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/cangjie_extraction/SKILL.md；从用户处确认源文本路径 + 元信息 + 是否首次试点；检查 workspace/cangjie/<slug>/PIPELINE_STATE.md 是否存在以续跑",
        "workflow_summary": "0.Adler 整书理解 → 1.5 个 sub-agent 并行提取 → 1.5.三重验证筛选（用户轻确认）→ 2.RIA++ 构造 skill → 3.Zettelkasten 链接 → 4.压力测试 darwin 兼容 → 5.交付+产物路由（项目 skills / workspace / memory / wip 四向路由）",
        "mcp_tools_priority": [
            "Task sub-agent（阶段 1 并行提取、阶段 4 盲测）",
            "Read / Write（读写工作目录文件）",
            "localagent_agent_chat 或 localagent_advanced_tool(llm_pool_call)（各阶段生成内容）",
            "localagent_memory_*（阶段 5 路由到 memory 时）",
            "localagent_wip_create（阶段 5 路由到 wip 时）",
        ],
        "key_pitfalls": [
            "工作目录是 workspace/cangjie/<slug>/，不是原版的 books/<slug>/",
            "禁止凭记忆拆书 — 没文本就停下来问用户",
            "阶段 5 不能只装到 skills 目录就完事，必须按产物路由决策表分别路由到 workspace/memory/wip/项目 skills",
            "装入 .agents/skills/ 前先用 memory_list 和 _index.md 查重，避免与已有 skill 重复",
            "upstream 档案在 .agents/skills/daily/cangjie_extraction/upstream/（含 RIA-TV++ 完整方法论/extractors/templates），不可修改原版",
        ],
        "level": "full",
        "vendor_repo": "https://github.com/kangarooking/cangjie-skill",
        "vendor_path": ".agents/skills/daily/cangjie_extraction/upstream/",
    },

    # ────────────── daily.思维工具集（12 个通用思维 Prompt，来自文章 https://mp.weixin.qq.com/s/NAdhdFrUq9-BKelqzqpwBQ）──────────────
    # 组合哲学：发散优先，举例非穷尽，agent 自行判断组合方式，拿不准列给用户选。详见 .agents/skills/daily/README.md
    "daily.socratic_questioning": {
        "skill": "socratic_questioning",
        "name": "苏格拉底式提问",
        "skill_file": ".agents/skills/daily/socratic_questioning/SKILL.md",
        "keywords": ["苏格拉底提问", "苏格拉底式问诊", "澄清困惑", "找到真正的问题", "问清问题", "我到底想问什么", "socratic questioning"],
        "description": "用户困惑模糊、嘴上问的和心里想的不一致时，通过最多 6 个逐个追问找到真正值得回答的问题。每次只问一个，根据回答决定下一问，信息足够时立即停止",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/socratic_questioning/prompt.md 获取完整 Prompt；用 AskUserQuestion 请用户描述困惑；按 Prompt 规则逐个追问（每次 1 问，先说明判断更新，只问可能改变结论的问题）；信息足够后整理 6 项并等用户确认新问题再给建议",
        "workflow_summary": "1.读prompt 2.让用户填困惑 3.逐个追问(每次1问) 4.整理6项(原问题/真问题/事实/假设/关键变量/新问题) 5.用户确认新问题后给判断",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (每次1个问题)"],
        "key_pitfalls": [
            "每次只问一个问题，不要提前给一整套问卷",
            "先不要给建议，本 skill 目标是澄清问题不是解决问题",
            "不凑满 6 个，信息足够时立刻停止",
            "每次提问前用一句话说明上一条回答让你更新了什么判断",
            "🧩 兄弟工具(按需组合非必须): steel_man_decision(澄清后若二选一决策) / grill_me(先澄清再压力测试) / first_principles(澄清后拆本质) / expert_consultation(澄清后多视角)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.dual_layer_explanation": {
        "skill": "dual_layer_explanation",
        "name": "双层解释法",
        "skill_file": ".agents/skills/daily/dual_layer_explanation/SKILL.md",
        "keywords": ["双层解释", "双层解释法", "小白专家两版解释", "学一个概念", "听不懂的概念", "用两层解释帮我学", "dual layer explanation"],
        "description": "学陌生概念时分别从小白和专家两个角度解释一遍，避免'好像懂了'的错觉。小白版用生活化语言+具体例子，专家版用准确术语讲清机制/边界/误解",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/dual_layer_explanation/prompt.md；用 AskUserQuestion 问用户想学什么概念；按 Prompt 执行两层解释+3 项整理(对应关系/易错点/3 个检查问题)",
        "workflow_summary": "1.读prompt 2.问用户想学什么 3.小白版(生活化+例子) 4.专家版(术语+机制+边界+误解) 5.整理3项(对应关系/易错点/检查问题)",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问想学什么)"],
        "key_pitfalls": [
            "必须分两层，不要只给一层解释",
            "小白版必须有具体例子，不只是类比",
            "专家版必须讲适用边界和常见误解，不只是定义",
            "🧩 兄弟工具: reverse_decomposition(学成品先拆解) / fact_checking(学完核查说法) / teach(需多session系统化教学) / cross_domain_borrowing(跨领域类比)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.reverse_decomposition": {
        "skill": "reverse_decomposition",
        "name": "反向拆解",
        "skill_file": ".agents/skills/daily/reverse_decomposition/SKILL.md",
        "keywords": ["反向拆解", "拆解优秀作品", "拆解范例", "学习它好在哪", "拆解这个产品", "反向工程一个作品", "reverse decomposition"],
        "description": "看到优秀作品想学习它好在哪时，先说它解决了什么问题，再反向拆解为什么有效（5 项分析），最后给可复用规律+操作清单+小练习",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/reverse_decomposition/prompt.md；用 AskUserQuestion 问用户范例+想学什么；按 Prompt 先说解决了什么问题，再 5 项分析(服务谁/结构流程/关键选择/完成标准/可迁移规律)，最后给 3 项(规律/操作清单/小练习)",
        "workflow_summary": "1.读prompt 2.问范例+想学什么 3.先说解决了什么问题 4.5项分析 5.给3项(规律/操作清单/小练习)",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问范例+想学什么)"],
        "key_pitfalls": [
            "先说解决了什么问题，不要直接跳到拆解",
            "区分可迁移规律 vs 案例细节，不要把所有细节都当规律",
            "必须给操作清单和小练习，不只是分析",
            "🧩 兄弟工具: dual_layer_explanation(拆解后遇陌生概念) / fact_checking(拆解中数据核查) / dev.impeccable distill(UI设计DNA更专业) / dev.hallmark study(反AI味设计)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.fact_checking": {
        "skill": "fact_checking",
        "name": "事实核查",
        "skill_file": ".agents/skills/daily/fact_checking/SKILL.md",
        "keywords": ["事实核查", "核查说法", "验证观点", "检查推理链", "这个说法对吗", "可信度评估", "fact checking", "笛卡尔怀疑"],
        "description": "对任何说法(观点/结论/数据/方案)做笛卡尔式怀疑。先拆成事实/结论/价值判断三层，对事实部分联网核查并标记 5 档可信度，再检查推理链 5 项漏洞，最后给补强版本",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/fact_checking/prompt.md；用 AskUserQuestion 问用户要核查什么；按 Prompt 拆三层(事实/结论/价值判断)→联网核查(WebSearch/WebFetch)标 5 档→检查推理链 5 项→输出 4 项(可信事实/关键漏洞/补强版本/可相信程度)",
        "workflow_summary": "1.读prompt 2.问要核查什么 3.拆三层 4.联网核查标5档 5.检查推理链5项 6.输出4项",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问要核查什么)", "WebSearch / WebFetch (联网核查事实)"],
        "key_pitfalls": [
            "必须联网核查，不能仅靠已有知识，事实核查的价值在验证",
            "5 档可信度必须明确，不要给'可能对可能错'的废话",
            "事实/结论/价值判断必须分开，不要混在一起核查",
            "诚实标注暂缺：找不到证据时明确写'暂未核实'，绝不编造",
            "🧩 兄弟工具: dual_layer_explanation(核查后遇陌生概念) / expert_consultation(需多专家视角) / socratic_questioning(问题模糊先澄清)。代码相关事实核查走 anti_hallucination 不是本 skill。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.expert_consultation": {
        "skill": "expert_consultation",
        "name": "专家会诊",
        "skill_file": ".agents/skills/daily/expert_consultation/SKILL.md",
        "keywords": ["专家会诊", "多专家视角", "三视角分析", "互补专家团", "专家互相质疑", "expert consultation", "多视角会诊"],
        "description": "为问题选择 3 种真正互补的专业视角，各自重新定义问题+推荐路径+忽略的风险+改变判断的证据，然后互相质疑找出真正分歧，最后综合输出推荐方案+适用条件+最大风险+退出条件+第一步行动",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/expert_consultation/prompt.md；用 AskUserQuestion 问用户问题+已知事实+目标+现实约束；按 Prompt 选 3 种互补视角→各自答 4 项→互相质疑找 3 项(共同事实/真正分歧/不同假设)→综合输出 5 项(推荐方案/适用条件/最大风险/退出条件/第一步行动)",
        "workflow_summary": "1.读prompt 2.问问题+事实+目标+约束 3.选3种互补视角 4.各自答4项 5.互相质疑找3项 6.综合输出5项",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问问题+事实+目标+约束)"],
        "key_pitfalls": [
            "三视角必须真正互补，不要选三个高度相似的身份",
            "不模仿或编造真实人物观点，自己构造视角",
            "必须互相质疑，真正的信息往往在分歧里，不是各自发言就结束",
            "先不要直接给方案，先走完三视角再综合",
            "🧩 兄弟工具: first_principles(会诊后拆本质) / cross_domain_borrowing(跨领域借解) / steel_man_decision(会诊后二选一) / socratic_questioning(问题模糊先澄清)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.first_principles": {
        "skill": "first_principles",
        "name": "第一性原理",
        "skill_file": ".agents/skills/daily/first_principles/SKILL.md",
        "keywords": ["第一性原理", "拆到本质", "回到本质", "first principles", "重新推导路径", "打补丁不如重推", "路径依赖拆解"],
        "description": "把问题拆回最底层，区分已确认基本事实/习惯性假设/真正目标/现实约束，暂时放下行业惯例和现成方案，只从基本事实重新推导可行路径。适合路径依赖/打补丁式方案/复杂系统改造",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/first_principles/prompt.md；用 AskUserQuestion 问用户要解决的问题；按 Prompt 拆 4 层(基本事实/习惯假设/真正目标/现实约束)→暂时放下现成方案→从基本事实重新推导→输出 4 项(原方案补表面部分/新路径/成立前提/验证第一步)",
        "workflow_summary": "1.读prompt 2.问要解决的问题 3.拆4层 4.暂时放下现成方案 5.从基本事实重新推导 6.输出4项",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问要解决的问题)"],
        "key_pitfalls": [
            "必须区分'基本事实'和'习惯性假设'，很多'理所当然'其实是未验证的假设",
            "必须暂时放下现成方案，不要在旧方案上修补",
            "新路径必须有成立前提和验证第一步，不只是空想",
            "🧩 兄弟工具: cross_domain_borrowing(拆完本质跨领域借解) / expert_consultation(多视角验证) / steel_man_decision(拆完二选一) / minimal_experiment(推导后最小实验验证)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.cross_domain_borrowing": {
        "skill": "cross_domain_borrowing",
        "name": "跨领域借解",
        "skill_file": ".agents/skills/daily/cross_domain_borrowing/SKILL.md",
        "keywords": ["跨领域借解", "跨领域类比", "其他行业怎么解决", "跨领域借鉴", "cross domain", "跨界借解", "底层结构相似"],
        "description": "把问题剥掉行业术语抽象成底层结构，从历史案例和至少 3 个距离较远的领域寻找底层结构相似的解法，翻译成适合当前处境的解决方案+推荐低成本可逆实验",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/cross_domain_borrowing/prompt.md；用 AskUserQuestion 问用户背景+当前做法+现实约束+具体卡点；按 Prompt 剥行业术语→找 3 项(底层结构/核心矛盾/普通解法失效原因)→从历史案例+至少 3 个距离较远领域找相似问题→每案例说明 5 项→选 3 种机制翻译成方案+推荐低成本可逆实验",
        "workflow_summary": "1.读prompt 2.问背景+做法+约束+卡点 3.剥行业术语找3项 4.历史案例+3个远领域找相似 5.每案例说明5项 6.选3种机制翻译+推荐实验",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问背景+做法+约束+卡点)", "WebSearch (可选,查历史案例和其他领域解法)"],
        "key_pitfalls": [
            "至少 3 个彼此距离较远的领域，不要选 3 个相近领域",
            "必须说明'什么条件下会失效'，不是所有机制都能迁移",
            "最后必须给低成本可逆实验，不只是理论分析",
            "🧩 兄弟工具: first_principles(先拆到本质再跨领域) / expert_consultation(跨领域后多视角验证) / minimal_experiment(借解后最小实验验证) / steel_man_decision(借解后二选一)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.steel_man_decision": {
        "skill": "steel_man_decision",
        "name": "双向钢人论证",
        "skill_file": ".agents/skills/daily/steel_man_decision/SKILL.md",
        "keywords": ["钢人论证", "双向钢人", "犹豫不决", "两个选项选哪个", "难以决定选哪个", "steel man", "决策二选一", "纠结选哪个"],
        "description": "两个选项间犹豫不决时，分别构造双方最强论证(不是稻草人)，找出真正分歧和最可能改变结论的关键变量，只问一个最关键的问题，再给判断。和 grill-me 不同：grill-me 是决策前拷问计划，钢人是决策中二选一",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/steel_man_decision/prompt.md；用 AskUserQuestion 问用户问题+两个选项+目标+现实约束；按 Prompt 先别急着回答→双向钢人(重述选择/双方最强论证各 5 项/找 3 项分歧)→只问一个最可能改变结论的问题→用户回答后给判断+理由+适用条件+下一步",
        "workflow_summary": "1.读prompt 2.问问题+两选项+目标+约束 3.先别急着回答 4.双向钢人(重述/双方最强5项/找3项分歧) 5.只问一个关键问题 6.用户回答后给判断",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问决策场景+最后只问一个关键问题)"],
        "key_pitfalls": [
            "必须构造双方最强论证，不要故意弱化任何一方(钢人不是稻草人)",
            "先别急着回答，先走完钢人论证再给判断",
            "只问一个问题，最可能改变结论的那个",
            "等用户回答后再判断，不要预先下结论",
            "与 dev.grill_me 区别：grill-me 决策前拷问计划，steel_man 决策中二选一，两者可串联",
            "🧩 兄弟工具: socratic_questioning(问题模糊先澄清) / grill_me(先压力测试计划再钢人) / first_principles(先拆本质) / expert_consultation(先多视角) / minimal_experiment(钢人后仍犹豫用最小实验) / decision_protocol(重大人生抉择走完整协议)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.minimal_experiment": {
        "skill": "minimal_experiment",
        "name": "用最小实验替代空想",
        "skill_file": ".agents/skills/daily/minimal_experiment/SKILL.md",
        "keywords": ["最小实验", "最小可行实验", "用实验替代空想", "试一下再说", "低成本验证", "minimal experiment", "验证假设"],
        "description": "当纸上谈兵无法再让你更清晰时，找出最需要验证的 3 个假设，选出最可能改变结论的那一个，设计一个低成本、可逆、7 天内能完成的最小实验，明确观察指标和支持/停止的判定标准",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/minimal_experiment/prompt.md；用 AskUserQuestion 问用户的选择或想法；按 Prompt 找 3 个最需验证的假设→选最可能改变结论的→设计低成本可逆 7 天实验→写清 6 项(做什么/投入/指标/支持继续/提醒停止/新信息)→告诉用户明天就能开始的第一个动作",
        "workflow_summary": "1.读prompt 2.问选择或想法 3.找3个最需验证假设 4.选最可能改变结论的 5.设计低成本可逆7天实验 6.写清6项 7.给明天的第一个动作",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (问选择或想法)"],
        "key_pitfalls": [
            "必须低成本、可逆，不要设计需要大投入或不可逆的实验",
            "必须有明确的停止条件，不要'看看再说'",
            "必须给明天的第一个动作，不要停留在抽象实验设计",
            "🧩 兄弟工具: steel_man_decision(钢人后仍犹豫用最小实验) / first_principles(先拆本质再设计实验) / cross_domain_borrowing(借解后验证迁移) / dev.prototype(软件原型验证走它更专业) / decision_protocol(重大决策走完整协议,本 skill 是其中'执行'环节候选)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "standard",
    },
    "daily.talent_mining": {
        "skill": "talent_mining",
        "name": "挖掘隐藏天赋",
        "skill_file": ".agents/skills/daily/talent_mining/SKILL.md",
        "keywords": ["挖掘天赋", "隐藏天赋", "天赋挖掘", "我的天赋是什么", "个人天赋说明书", "talent mining", "找天赋", "人生天赋"],
        "description": "agent 扮演熟悉盖洛普优势识别/心流理论/荣格心理学的资深生涯咨询师，通过多轮深度对话(最多 10 个主问题)，在怪癖/缺点/嫉妒/无意识胜任区/能量模式里找到被压抑的天赋，最终产出万字《个人天赋使用说明书》",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/talent_mining/prompt.md(含完整 Role/对话规则/必须覆盖的主线/输出结构)；按 Prompt 末尾'开始'节语气向用户说明流程；用 AskUserQuestion 逐个追问(每次 1 问，节奏:问→答→简短反馈→再问)；必须覆盖 4 条主线(16岁前废寝忘食+顽固缺点/无意识胜任区/回血事vs抽干事/强烈嫉妒过谁)；信息足够后输出万字说明书",
        "workflow_summary": "1.读prompt 2.开场说明流程 3.逐个追问(每次1问,最多10主问题) 4.覆盖4条主线 5.产出万字说明书(天赋+经历链/阴影面/能量地图/适合环境/职业方向/30天实验)",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (每次1个问题)"],
        "key_pitfalls": [
            "每次只问一个问题，节奏:你问→用户答→你简短反馈→再问下一题",
            "反宿命论：天赋不等于固定技能，不会因年龄过期",
            "能量审计：单纯擅长但做完消耗的事要单独区分",
            "阴影即宝藏：被批评的缺点/怪癖/嫉妒可能是天赋被压抑后的背面",
            "所有判断要有经历对应，证据不足用'可能'",
            "🧩 兄弟工具: recurring.life_design(天赋挖掘后向前看人生设计) / socratic_questioning(对话中问题模糊先澄清) / decision_protocol(天赋挖掘后面临职业转型决策走完整协议)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "full",
    },
    "daily.decision_protocol": {
        "skill": "decision_protocol",
        "name": "重大决策协议(预烘焙组合)",
        "skill_file": ".agents/skills/daily/decision_protocol/SKILL.md",
        "keywords": ["重大决策", "人生抉择", "重要选择", "重大决定", "难以决定人生方向", "decision protocol", "决策协议", "重大人生决策"],
        "description": "预烘焙组合套餐：启动前对齐(目标/成功标准/资源/限制/协作对象)+最强论证(双向钢人)+执行纪律。适用于重大人生抉择(职业转型/关系抉择/价值观冲突)。简单二选一不走本 skill 走 steel_man_decision",
        "memory_key": None,
        "first_action": "读取 .agents/skills/daily/decision_protocol/prompt.md(含完整核心原则/对齐协议/最强论证/执行纪律/输出要求/提问示例)；按 Prompt 执行 4 阶段：阶段1对齐(重述目标+明确5要素,缺失给2-4选择题不连环追问)→阶段2最强论证(重述问题/双方最强论证/找分歧和关键变量/只问一个最关键问题)→阶段3执行突发(回到已对齐目标决策)→阶段4输出(可执行+有判断+有理由,禁止'各有利弊'废话)",
        "workflow_summary": "1.读prompt 2.阶段1对齐(重述目标+5要素,缺失给选择题) 3.阶段2最强论证(双方最强/找分歧/只问一个关键问题) 4.阶段3执行突发(回到已对齐目标) 5.阶段4输出(可执行+判断+理由)",
        "mcp_tools_priority": ["Read (prompt.md)", "AskUserQuestion (每次只问一个最关键的问题,附 2-4 选项+推荐)"],
        "key_pitfalls": [
            "每次只问一个最关键的问题(用 AskUserQuestion,1 个问题,附 2-4 选项+推荐)",
            "不谄媚不附和：给最可能正确的判断，不是用户想听的",
            "少问开放性问题：能查能推能枚举的不要问",
            "禁止'各有利弊'废话：必须给判断+理由+下一步",
            "本 skill 是预烘焙套餐(对齐+钢人+执行纪律)，但不是唯一组合方式，agent 也可不走它自行组合其他思维工具",
            "🧩 兄弟工具(可替代或补充本套餐的组件): socratic_questioning(替代阶段1轻量版) / grill_me(更重的对齐) / steel_man_decision(本 skill 阶段2就是钢人,简单二选一可直接走) / first_principles(先拆本质) / expert_consultation(先多视角) / minimal_experiment(协议后仍犹豫用最小实验)。组合方式自行判断，拿不准列给用户选。详见 daily/README.md 组合哲学",
        ],
        "level": "full",
    },

    # ────────────── recurring.meta（记忆生成元任务）──────────────
    "recurring.memory_generation": {
        "skill": "memory_generation",
        "name": "记忆生成",
        "skill_file": ".agents/skills/memory_generation.md",
        "keywords": ["生成记忆", "记忆生成", "记忆检查", "提取记忆", "更新记忆", "记忆维护", "保存经验", "记忆总结", "memory generation"],
        "description": "从当前会话提取结构化记忆事实",
        "memory_key": None,
        "first_action": "回顾当前会话的关键决策和事件，按下方 memory_generation_workflow 逐步执行",
        "workflow_summary": "1.回顾任务 2.分类候选 3.查重 4.写入 5.可选触发维护",
        "mcp_tools_priority": [
            "memory_list (查看现有记忆)",
            "memory_get (查重)",
            "memory_set (写入结构化记忆)",
            "localagent_advanced_tool(memory_maintain) (可选触发维护)",
        ],
        "key_pitfalls": [
            "不存储可 grep 的内容（代码结构、git 历史、已修复的 bug）",
            "结构化优于自由文本：每条记忆必须有 type 字段",
            "不确定时保留（when in doubt, keep）",
        ],
        "prerequisites": ["当前会话有可提取的任务经验"],
        "level": "full",
        "memory_generation_workflow": {
            "step_1_recall": {
                "name": "回顾任务",
                "instruction": (
                    "回顾当前会话中完成的工作。识别以下类型的候选记忆：\n"
                    "1. 用户偏好（preference）：用户明确表达的习惯、喜好、工作方式\n"
                    "2. 项目状态（project）：任务进度、配置变更、架构决策\n"
                    "3. 参考资料（reference）：外部 API 文档地址、数据格式、第三方工具用法\n"
                    "跳过：代码结构（可 grep）、git 历史、已修复的 bug、临时调试信息"
                ),
            },
            "step_2_classify": {
                "name": "分类候选",
                "instruction": (
                    "对每个候选记忆，确定类型：\n"
                    "- preference: 用户偏好（如'用户喜欢用 curl.exe 而非 PowerShell curl'）\n"
                    "- project: 项目状态（如'异环抽卡清洗脚本 v3 新增了道具映射'）\n"
                    "- reference: 参考资料（如'ModelScope VL API 限流 RPM≈5'）\n"
                    "每条记忆必须能回答：这是什么？为什么重要？如何应用？"
                ),
            },
            "step_3_dedup": {
                "name": "查重",
                "instruction": (
                    "对每个候选记忆：\n"
                    "1. memory_list 查看现有 key\n"
                    "2. 如果 key 已存在，用 memory_get(key) 读取现有内容\n"
                    "3. 比较新旧内容：如果只是更新 → 用 merge 模式覆盖\n"
                    "    如果完全重复 → 跳过\n"
                    "    如果新增信息 → 合并后写入"
                ),
            },
            "step_4_write": {
                "name": "写入结构化记忆",
                "instruction": (
                    "用 memory_set(key, {data: <structured_value>, merge: true}) 写入。\n"
                    "structured_value JSON schema:\n"
                    "{\n"
                    '  "type": "preference|project|reference",\n'
                    '  "name": "简短名称",\n'
                    '  "description": "一句话描述",\n'
                    '  "content": "实际内容/事实",\n'
                    '  "why": "为什么重要",\n'
                    '  "how_to_apply": "如何在未来应用",\n'
                    '  "created_at": "YYYY-MM-DD",\n'
                    '  "source_task": "触发本次记忆生成的任务名"\n'
                    "}\n"
                    "key 命名规范：{scope}.{name}，如 preferences.curl_usage, project.yihuan_gacha\n"
                    "已有的 key（如 accounting, wip_index）保持原名，用 merge 模式更新。"
                ),
            },
            "step_5_optional_maintain": {
                "name": "可选触发维护",
                "instruction": (
                    "如果写入了 3 条以上新记忆，可调用 "
                    "localagent_advanced_tool(memory_maintain, {force: true}) 触发一次维护，"
                    "让维护器验证新写入的记忆。\n"
                    "这不是必须的——维护器会在下次到期时自动运行。"
                ),
            },
            "validation_self_check": {
                "name": "自检（可选）",
                "instruction": (
                    "写入后自检：\n"
                    "1. 每条记忆是否有 type 字段？\n"
                    "2. 是否有可 grep 的内容？（如果有，删除并改为查代码/文档）\n"
                    "3. 是否重复了已有记忆？（memory_list 交叉检查）\n"
                    "4. 如果不确定某条记忆是否该保留 → 保留（when in doubt, keep）"
                ),
            },
        },
        "do_not_store": [
            "代码结构（可 grep 代码库获取）",
            "git 历史（可 git log 获取）",
            "已修复的 bug（修复后不再需要）",
            "临时调试信息（如某次 OCR 返回的坐标）",
            "AGENTS.md / _index.md 中已记录的内容",
            "config.toml 中已配置的值",
        ],
    },
    "system.wip_archive": {
        "skill": "wip_archive",
        "name": "WIP 归档",
        "skill_file": ".agents/skills/wip_archive.md",
        "keywords": ["归档 wip", "清理已完成 wip", "把完成的 wip 转记忆", "wip 太多", "wip 膨胀", "wip archive"],
        "description": "批量归档 completed WIP 到项目记忆 + 物理删除记录",
        "memory_key": None,
        "first_action": "按 wip_archive_workflow 逐步执行：1.wip_list(summary=true) 筛选 completed 2.逐条 wip_get 提炼经验+填 consumption_contexts/trigger_keywords 3.生成清单→用户审核→memory_set 写入 4.wip_delete 物理删除",
        "workflow_summary": "1.筛选 completed 2.提炼经验+可消费性自检 3.用户审核+写入 4.物理删除",
        "mcp_tools_priority": [
            "wip_list (step_1 筛选 completed，直连免审批)",
            "localagent_advanced_tool(wip_get) (step_2 读详情，网关 GET 免审批)",
            "memory_set (step_3 写入结构化记忆)",
            "localagent_advanced_tool(wip_delete) (step_4 物理删除，网关 DELETE)",
        ],
        "key_pitfalls": [
            "用户审核是硬约束：step_3 生成清单后必须等待用户明确批准，不擅自写入或删除",
            "可消费性自检：写入记忆前必须填 consumption_contexts + trigger_keywords，否则跳过",
            "物理删除不可恢复：归档前确认记忆已捕获关键信息（next_steps/current_state/related_files 会丢失）",
            "不归档未完成 WIP：status != 'completed' 的 WIP 永远不归档",
            "wip_delete 曾因 httpx delete() 不支持 json 参数触发 500（已修复），若再现 500 用 exec_python+requests.delete 临时绕过并报告 bug",
        ],
        "prerequisites": ["存在 status='completed' 的 WIP"],
        "level": "standard",
        "wip_archive_workflow": {
            "step_1_filter": {
                "name": "筛选 completed WIP",
                "instruction": (
                    "调用 wip_list(summary=true) 获取所有 WIP 摘要，筛选 status='completed' 的条目。\n"
                    "若没有 completed 的，告知用户无需归档并结束。\n"
                    "wip_list 是直连 MCP 工具，免审批。"
                ),
            },
            "step_2_extract": {
                "name": "逐条提炼经验",
                "instruction": (
                    "对每条 completed WIP：\n"
                    "1. localagent_advanced_tool(tool='wip_get', params={'task_id': '<id>'}) 读详情\n"
                    "2. 评估可归档性：含跨会话复用价值的项目状态/决策约束/踩坑经验 → 可归档；\n"
                    "   一次性临时任务/已被代码git记录/AGENTS.md已记录 → 不归档\n"
                    "3. 合并相似 WIP：多条 WIP 经验主题重合时合并为一条记忆\n"
                    "4. 填两个字段（强制约束）：\n"
                    "   - consumption_contexts: 哪些 task_type 应读取此记忆\n"
                    "   - trigger_keywords: 任务描述出现这些词时读取\n"
                    "   无法明确消费场景的候选 → 跳过不写入"
                ),
            },
            "step_3_review_write": {
                "name": "生成清单 → 用户审核 → 写入",
                "instruction": (
                    "生成清单表格（WIP ID/拟写入 key/类型/consumption_contexts/trigger_keywords），\n"
                    "不直接写入，先让用户审核。用户选项：整体批准/逐条修改/跳过某条。\n"
                    "用户批准后用 memory_set(key, {data: <structured>, merge: true}) 写入。\n"
                    "key 不允许点号，用下划线；不加 wip_archive_ 前缀，用语义化 key。"
                ),
            },
            "step_4_delete": {
                "name": "删除已归档 WIP",
                "instruction": (
                    "写入记忆后，物理删除 WIP 记录：\n"
                    "localagent_advanced_tool(tool='wip_delete', params={'task_id': '<id>'})\n"
                    "返回 success=true, status_code=200 即删除成功。\n"
                    "若返回 500（httpx delete bug 再现），临时绕过：\n"
                    "exec_python 调 requests.delete(f'{API}/wip/{wid}') 直接 HTTP，并立即向用户报告 bug。"
                ),
            },
        },
    },
    # "system.internal_workflow" entry removed from public package
    # (internal release workflow; release engine / policy / profiles are not distributed).

    "system.release": {
        "skill": None,
        "name": "版本发布",
        "skill_file": None,
        "keywords": [
            # 核心词
            "发版", "发个版本",
            # 衍生词：版本号操作
            "出版本号", "改版本号", "归档版本",
            # 英文/术语
            "release",
        ],
        "description": "CHANGELOG [Unreleased] → 版本号 release + 归档旧版本 + git commit",
        "memory_key": None,
        "first_action": (
            "执行版本发布 6 步流程（项目纯本地，不发 git tag / GitHub release / push remote）：\n"
            "1. 读 CHANGELOG.md 确认 [Unreleased] 段有实际变更内容（空则告知用户无需 release）\n"
            "2. 确定新版本号：读 CHANGELOG.md 找最近 release 段（如 [0.10.0] - 2026-07-15），递增 patch（0.10.0→0.10.1，常规）或 minor（0.10.0→0.11.0，重大功能），不跨 major 除非用户指定\n"
            "3. 版本号同步：RunCommand 跑 `uv run python tools/bump_version.py <新版本号>` 统一更新 server/main.py + server/core/health.py + pyproject.toml 的 VERSION 常量（防止漏改）\n"
            "4. Edit CHANGELOG.md：把 `## [Unreleased]` 改为 `## [新版本号] - YYYY-MM-DD`（今天日期），在顶部插入新的空 `## [Unreleased]` 段（含 `### Added` 下 `- (暂无)` 等占位）\n"
            "5. RunCommand 跑 `uv run python tools/migrate_changelog.py`（默认 --keep 1），把旧 release 段归档到 docs/changelog-archive.md（newest first，幂等）\n"
            "6. git add + git commit：按目录 add（.agents/ client/ server/ docs/ tests/ + 顶级文件），不要用 git add -A（避免误纳入 config.toml / data/llm/keys.json 等敏感文件）。commit 信息格式 `vX.Y.Z: 主题1 + 主题2 + ...`（从 [Unreleased] 段提炼 3-5 个主题）。注意：pre-commit hook（scripts/hooks，硬化规则检查 C1-C4）会在 commit 时自动跑，红了按提示修复后再 commit\n"
            "PowerShell 不支持 bash HEREDOC `$(cat <<'EOF'...)`，commit 信息用单个 -m 传单行标题"
        ),
        "workflow_summary": "1.查[Unreleased] 2.定版本号(patch/minor递增) 3.bump_version同步VERSION常量 4.Edit改段名+加空Unreleased 5.migrate归档 6.git add+commit",
        "mcp_tools_priority": [
            "Read (CHANGELOG.md)",
            "RunCommand (uv run python tools/bump_version.py <版本号>)",
            "Edit (CHANGELOG.md: [Unreleased]→版本号, 顶部加空[Unreleased])",
            "RunCommand (uv run python tools/migrate_changelog.py)",
            "RunCommand (git add 按目录 + git commit -m)",
        ],
        "key_pitfalls": [
            "项目纯本地：不发 git tag、不发 GitHub release、不 push remote。release 仅通过 CHANGELOG 文档化",
            "版本号递增：常规 patch+1（0.10.0→0.10.1），重大功能 minor+1（0.10.0→0.11.0），不跨 major 除非用户指定",
            "migrate_changelog.py 默认 --keep 1：CHANGELOG.md 仅保留 [Unreleased] + 最近 1 个 release，旧 release 自动归档到 docs/changelog-archive.md",
            "PowerShell 不支持 bash HEREDOC：commit 信息用单个 -m 传单行标题，或多个 -m 传多段",
            "git add 不要用 -A：按目录 add，避免误纳入敏感文件（config.toml / data/llm/keys.json 等）",
            "commit 前检查 git status 确认无敏感文件被 staged",
            "[Unreleased] 段空时不要 release",
        ],
        "prerequisites": ["工作区有未提交的变更（对应 [Unreleased] 段内容）"],
        "level": "standard",
    },
    "system.project_audit": {
        "skill": "project_audit",
        "name": "项目全面复审",
        "skill_file": ".agents/skills/project_audit.md",
        "keywords": ["项目复审", "全面复审", "项目审查", "项目体检", "project audit", "全面审查一下", "项目健康度", "复审一下项目", "项目巡检"],
        "description": "项目级多维度健康审计：让用户从 12 大类（架构/测试/文档/API/配置/可用性/安全/数据/性能/Git/环境/UX）中选择审查方向，按检查清单扫描问题并输出结构化报告",
        "memory_key": None,
        "first_action": (
            "执行项目全面复审 5 步流程：\n"
            "1. 用 AskUserQuestion(multiSelect=true) 展示 12 大类清单，让用户选择审查方向（默认推荐 A+B+C+F+J 作为最小集）\n"
            "2. ≥3 类时创建持久化文件：temp/project_audit_plan.md（计划+进度）+ temp/project_audit_findings.md（问题清单）\n"
            "3. 按所选大类并行 Read 关键文件 + 调 MCP 工具（mcp_stats/loop_list_tasks/llm_pool_status/memory_status//health）\n"
            "4. 按每类的子项检查清单扫描（详见 .agents/skills/project_audit.md 的'审查方向详细清单'章节）\n"
            "5. 输出报告：每类含健康度评分(🔴🟡🟢⚪)+问题表格；全部完成后输出汇总报告(共性问题/Top 3紧急/改进路线图)+调 task_closure 收尾"
        ),
        "workflow_summary": "1.展示12大类让用户选 2.持久化准备 3.上下文收集 4.按检查清单扫描 5.输出报告+收尾",
        "mcp_tools_priority": [
            "AskUserQuestion (step_1 让用户选方向，multiSelect=true)",
            "Read / Grep / SearchCodebase (step_3-4 扫描代码和文档)",
            "RunCommand (step_4 跑 pytest/git log/sqlite3/uv pip audit)",
            "mcp_stats / loop_list_tasks / llm_pool_status / memory_status / /health (step_3-4 运行时状态)",
            "localagent_advanced_tool (step_3 调 /openapi.json/mcp_stats 等网关端点)",
        ],
        "key_pitfalls": [
            "第一步必须用 AskUserQuestion 让用户选择方向，禁止未征询就自行全扫——12 大类全扫耗时长",
            "区别于 code_review（单文件/单模块代码审查）和 neat-freak（文档同步），本 skill 是项目级多维度健康审计",
            "≥3 类时必须创建 temp/project_audit_plan.md + temp/project_audit_findings.md，避免跨会话中断丢失进度",
            "每个问题必须有证据（Code Link 用 file:/// + #L123-L145 格式，行号范围 ≤ 100 行）+ 可执行建议（不空泛'建议优化'）",
            "禁止报告：纯描述性评论、赞美性评论、无证据猜测、UI 样式数值、代码风格",
            "复审完成后调 agent_guide(task_type='system.task_closure') 收尾，记忆写入 consumption_contexts=['system.project_audit']",
        ],
        "prerequisites": ["用户主动触发项目复审"],
        "level": "full",
    },
}


# ========== dev 桶 role 映射 ==========
# entry=任务入口/编排流程, method=方法论参考(被 entry 引用), support=辅助审查/排障, tool=工具型
# 集中映射而非逐条改 GUIDE_REGISTRY，便于维护。TaskGuide 响应附 role 字段，
# GeneralGuide 返回 dev_entry_points 决策树，_build_task_categories 的 dev scope 按 role 分组。
_DEV_ROLES: dict[str, str] = {
    # entry: 任务入口，定义目标/编排流程
    "dev.goal_engineering": "entry",
    "dev.grill_me": "entry",
    "dev.to_spec": "entry",
    "dev.to_tickets": "entry",
    "dev.implement": "entry",
    "dev.cross_workspace_advisor": "entry",
    # method: 方法论参考，被 entry 引用
    "dev.leader": "method",
    "dev.tdd": "method",
    "dev.codebase_design": "method",
    "dev.domain_modeling": "method",
    "dev.prototype": "method",
    "dev.wayfinder": "method",
    # support: 辅助审查/排障
    "dev.code_review": "support",
    "dev.triage": "support",
    "dev.resolving_merge_conflicts": "support",
    "dev.grill_with_docs": "support",
    "dev.grilling": "support",
    "dev.anti_hallucination": "support",
    # tool: 工具型 skill
    "dev.skill_creation": "tool",
    "dev.html_debug": "tool",
    "dev.computer_use": "tool",
    "dev.client_dev": "tool",
    "dev.impeccable": "tool",
    "dev.hallmark": "tool",
}

# dev 桶入口决策树：开发任务第一步根据目标清晰度选入口
_DEV_ENTRY_POINTS = {
    "_说明": "开发任务第一步：根据目标清晰度选入口。entry role 的 skill 是第一步，method/support/tool 按需引用。",
    "goal_clear_full_flow": "dev.goal_engineering（三阶段编排：询问→写方案→执行，推荐）",
    "goal_unclear": "dev.grill_me（追问澄清）→ dev.to_spec → dev.to_tickets → dev.implement",
    "goal_clear_incremental": "dev.to_spec → dev.to_tickets → dev.implement",
    "small_fix": "dev.anti_hallucination（6步防幻觉，不走三阶段）",
    "cross_workspace_dev": "dev.cross_workspace_advisor（无关开发引导建新工作区）",
}


# ========== 通用指导内容 ==========

GENERAL_GUIDE = {
    "usage": "调用 agent_guide(task='用户任务描述') 做关键词匹配，或 agent_guide(task_type='recurring.accounting') 精确获取某个任务的指导",
    "session_startup": [
        "1. 确认 MCP 可用（调任意 mcp_localagent_* 工具，如 exec_status）",
        "2. 根据用户任务调 agent_guide(task='...') 获取指导；未匹配时浏览下方全量清单选 task_type",
        "3. 【仅新会话首次任务】agent 评估本次任务可能用得上的 skill（基于 task 匹配结果 + 任务上下文 + 用户历史偏好），主动用 AskUserQuestion(multiSelect=true) 询问用户要加载哪些 skill。用户可多选、可跳过（选'其他'或直接说不需要）。本项目不分 model/user-invoked，所有 skill 都可选。用户选定后 agent 自己 Read 对应 skill 文件加载工作流。后续会话中途不再重复询问，除非用户主动要求。",
        "4. 按需读相关记忆：memory_get(<skill 的 memory_key>) 了解上次进度；通用偏好 memory_get('preferences')（轻量，按需）",
        "5. 用户问'有什么任务'/'待办'/'上次没做完的'时，再调 agent_guide(task_type='system.task_reminder') 或 wip_list(summary=true) —— 不要主动检查，避免无关查询污染上下文",
    ],
    "session_closure": [
        "1. 任务完成/中断/会话结束前，调 agent_guide(task_type='system.task_closure') 获取收尾指引",
        "2. 收尾指引含：评估任务状态 → WIP 处理 → 经验提炼(含可消费性自检) → 查重写入 → 文档自查 → 收尾报告",
        "3. 不确定是否该收尾时，也可调 task_closure 快捷查询判断标准（如'无实质产出'则跳过）",
        "4. 记忆写入时必须填 consumption_contexts + trigger_keywords，确保未来任务能消费（避免存了没人用）",
    ],
    "global_pitfalls": [
        "PowerShell 中 curl 是 Invoke-WebRequest 别名 → 用 curl.exe 或 Invoke-RestMethod",
        "禁止返回大段 base64 → 用 capture_screen(format=inline) / screen_analyze",
        "截图+OCR → 用 exec_python 一步完成（见 AGENTS.md 正确流程）",
        "坐标参数必须为整数（/screen/action 的 x/y）",
        "磁盘清理必须先拉清单给用户审核（铁律）",
        "临时脚本写 temp/，不写项目根目录",
        "DirectX 全屏游戏截图：PrintWindow 假成功返回错误内容 → 用 force_fullscreen_crop=True 跳过",
        "双屏截图：mode=fullscreen 拼接所有显示器，坐标注意虚拟屏偏移",
        "验证优先级：浏览器先查 DOM、桌面先查 OCR 文本；只有高风险/失败重试/登录态或视觉状态无法由文字判断时才传 verify_prompt 调 VL。远程 VL 昂贵且慢，不为普通文字点击逐步调用；batch_actions 不支持 verify_prompt。",
    ],
    "environment_notes": [
        "双屏配置：用户有时双屏有时单屏。全屏截图（mode=fullscreen）默认拼接所有显示器，OCR 可能返回多屏内容。需要窗口内容时用 mode=window + force_fullscreen_crop=True（DirectX 游戏尤其重要）",
        "管理员权限：键鼠操控（execute_action/batch_actions）需管理员权限，否则 Windows UIPI 阻止。操作前先 /health 检查 screen.admin=true。无权限时用 start.bat 重启（自动 UAC 提权）",
        "Computer Use 铁律：任何屏幕操控任务（截图理解/点击/输入/翻页）必须先读 .agents/skills/computer_use.md 了解窗口操作铁律、DPI 缩放、坐标系统",
        "定位优先级：浏览器 DOM > UIA > OCR bbox > vision_locate；远程 VL 默认用于描述而非普通文字坐标",
    ],
    "file_locations": {
        "临时脚本": "temp/",
        "运行时数据": "data/ (llm/keys, llm/stats, memory/)",
        "任务产出": "workspace/<task_name>/",
        "共享数据": "workspace/_shared/",
        "Skill定义": ".agents/skills/",
        "通用工具": "tools/ (debug/disk/llm/file_classifier/image_organizer/media_classifier/mindforge/browser通用工具)",
        "二级文档": "docs/ (mcp-reference, tools-guide, memory-system, llm-pool, environment-constraints)",
    },
    "mcp_priority": "优先用 MCP 工具，而非自己写脚本。只有 MCP 工具无法满足时才写自定义脚本到 temp/",
    "dev_entry_points": _DEV_ENTRY_POINTS,
}
