---
name: modelscope_model_update
description: >
  扫描 ModelScope 前沿模型、验证"魔搭社区"API 来源、用权威榜单评估性能、
  生成推荐报告，用户确认后写入 keys.json。覆盖 LLM/VL/aigc_image/aigc_video
  （可写入）+ TTS/ASR（仅记录）共 6 个方向。
  触发词：更新modelscope、更新魔搭模型、modelscope模型、前沿模型清单、
  项目缺什么模型、扫描魔搭、魔搭社区。当用户提到 ModelScope、魔搭、
  前沿模型清单、补齐项目模型方向时触发。
task_type: adhoc.modelscope_model_update
---

# ModelScope 模型库更新 Skill

## 概述

定期或不定期扫描 ModelScope 网站上支持 API 调用的前沿模型，验证 API 提供方含"魔搭社区"（排除仅支持 OpenAI/Anthropic 等外部源的模型），用三大权威榜单（artificialanalysis.ai / arena.ai / superclueai.com）评估性能，生成推荐报告，用户 AskUserQuestion 确认后写入 `data/llm/keys.json`。

**ModelScope 入口**：`https://modelscope.cn/models?filter=inference_type&page=1&tabKey=task`（已预选只展示支持 API 调用的模型）

**方向覆盖**（6 类）：
- LLM / VL / aigc_image / aigc_video → **可写入 keys.json**（项目已有 use_case）
- TTS / ASR → **仅记录在推荐报告中**（项目当前无 use_case，未来用到再说）

## 触发词

- "更新 modelscope"
- "更新魔搭模型"
- "modelscope 模型"
- "前沿模型清单"
- "项目缺什么模型"
- "扫描魔搭"
- "魔搭社区"

## 工作流程

### 阶段 1：读取项目当前模型覆盖
- 读 `data/llm/keys.json`，按 `scope` 字段聚合已有模型：
  - `llm` → 文本生成方向
  - `vl` → 视觉多模态方向
  - `aigc_image` → 图片生成方向
  - `aigc_video` → 视频生成方向
  - 语音方向当前为空
- 读 `memory_get('modelscope_model_update')` 了解上次更新进度、已知缺漏

### 阶段 2：识别缺漏方向并询问用户
- 对照 6 方向清单：
  - LLM：至少 3 个前沿模型（覆盖通用/推理/快速三类）→ **可写入 keys.json**
  - VL：至少 2 个（覆盖纯视觉理解 + 多模态对话）→ **可写入 keys.json**
  - aigc_image：至少 2 个（覆盖写实 + 风格化）→ **可写入 keys.json**
  - aigc_video：至少 1 个 → **可写入 keys.json**
  - TTS：至少 1 个（项目当前无 use_case）→ **仅记录在报告中**
  - ASR：至少 1 个（项目当前无 use_case）→ **仅记录在报告中**
- **AskUserQuestion 问用户本次要更新哪些方向**，避免全扫浪费时间

### 阶段 3：ModelScope 网站扫描（CDP）
- 启动调试浏览器：`python tools/browser/start_debug_browser.py`（CDP 端口 9222）
- **必读**：先查 `.agents/skills/browser_lessons/` 看 `modelscope.cn` 是否有 sites 文件，有则读已知坑
- 访问入口 URL（已预选 API 调用过滤）
- 按用户选定方向点击左侧任务过滤项（详见 `references/modelscope_site_guide.md`）：
  - LLM → "文本生成"
  - VL → "视觉多模态理解"
  - aigc_image → "文本生成图片"
  - aigc_video → "文本生成视频"
  - TTS → "语音合成"
  - ASR → "语音识别"
- 翻页扫描模型卡片列表，提取每个模型的：模型 ID、简介前 100 字、下载量/点赞数
- 每个方向至少收集 5-10 个候选

### 阶段 4：API 来源验证（关键步骤）
- 对每个候选模型，逐个点开模型详情页 `https://modelscope.cn/models/{model_id}`
- 等待右侧 API 面板（class `acss-17aobl4`，位置 left=896, w=384）渲染
- **判断规则**：
  - 面板标题为"推理 API-Inference" **且** 提供方列表含"魔搭社区" → **可用**
  - 面板只显示"OpenAI"/"Anthropic"等其他来源 → **跳过**（用户明确排除）
  - 没有右侧 API 面板 → **跳过**（不支持 API 调用）
- 记录可用模型的 API 调用方式（LLM=OpenAI 兼容、Image=异步任务等）

### 阶段 5：性能评估（权威榜单）
- 三个榜单的访问策略（详见 `references/leaderboards_guide.md`）：
  - **artificialanalysis.ai**：WebFetch 公开页，查 LLM 的 quality/speed/price 三维
  - **arena.ai**：需梯子。`RunCommand` 启动你的梯子软件（如 Clash Verge），主页找"启动代理"开关。启动后用 CDP 浏览查 LLM 的 Elo 排名
  - **superclueai.com**：WebFetch 公开页，查中文综合榜单
- 查不到榜单数据的模型：参考 ModelScope 站内下载量/点赞数 + 模型作者权威性（腾讯混元、阿里 Qwen、智谱 GLM 等大厂默认可靠）
- **重要**：启动梯子前必须 AskUserQuestion 询问用户许可

### 阶段 6：生成推荐报告
- 输出到 `workspace/modelscope_model_update/推荐报告_{日期}.md`
- 每个方向一个小节，**LLM/VL/aigc_image/aigc_video 段落标注"可写入 keys.json"，TTS/ASR 段落标注"仅记录，未来用到再说"**

### 阶段 7：用户确认
- **AskUserQuestion 让用户选择 LLM/VL/aigc_image/aigc_video 4 个方向**各要采纳的模型（multiSelect）
- TTS/ASR 方向**不进入确认环节**，仅留在报告中供未来参考
- 用户确认后进入阶段 8

### 阶段 8：写入 keys.json（仅用户确认后，仅针对 4 个可写入方向）
- 读取 `data/llm/keys.json`
- 在 `modelscope_modelscope_dsv4_pro_2ca90cd3` 条目（或新增条目）的 `models` 数组追加新模型：
  ```json
  {
    "name": "<model_id>",
    "scope": ["<llm|vl|aigc_image|aigc_video>"],
    "limits": {},
    "tier": <按性能评估 1-5>,
    "enabled": true
  }
  ```
- **不写入 TTS/ASR 模型**
- `version` + 1，`updated_at` 更新为今天
- 写记忆 `memory_set('modelscope_model_update', {last_update, added_models, skipped_directions, tts_asr_reported_only})`

## 关键约束（必须遵守）

1. **API 来源必验**：必须确认右侧面板提供方含"魔搭社区"，否则跳过。绝不写入仅支持外部源的模型
2. **仅推荐不擅改**：阶段 1-7 只读不写 keys.json，必须用户 AskUserQuestion 确认后才能进入阶段 8
3. **TTS/ASR 仅记录**：语音方向项目当前无 use_case，扫描结果只写进推荐报告，不进入阶段 7 确认、不写入 keys.json，未来用到再说
4. **梯子需许可**：启动 clash_verge 前 AskUserQuestion 询问用户
5. **方向先问**：阶段 2 必须先问用户本次更新哪些方向，避免全扫浪费时间
6. **不擅自装库**：用现有 requests/playwright/openai，不装新依赖
7. **浏览器踩坑**：CDP 操作前查 `browser_lessons`，新发现的坑写入 `sites/modelscope-cn.md`

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 调试浏览器 | `tools/browser/start_debug_browser.py` | CDP 端口 9222 |
| Playwright | `connect_over_cdp("http://127.0.0.1:9222")` | 浏览 ModelScope |
| WebFetch | MCP 工具 | 抓 artificialanalysis.ai / superclueai.com |
| RunCommand | 启动 clash_verge | 仅 arena.ai 需要 |
| exec_python | 读写 keys.json | 用户确认后写入 |
| memory_get/set | 进度记忆 | 跨会话保留 |
| AskUserQuestion | 用户确认方向、采纳清单、梯子许可 | 阶段 2/5/7 |

## 输出

- `workspace/modelscope_model_update/推荐报告_{日期}.md`（含 6 方向候选清单 + 性能评估 + 推荐结论）
- `data/llm/keys.json`（仅 4 个可写入方向，用户确认后更新）
- `memory_set('modelscope_model_update', ...)` 更新进度
- `.agents/skills/browser_lessons/sites/modelscope-cn.md`（首次扫描后写入踩坑经验）
- 用户对话报告（候选清单摘要、性能对比、用户确认结果）

## 参考文档

- [modelscope_site_guide.md](references/modelscope_site_guide.md)：ModelScope 网站 DOM 结构、过滤面板、API 面板识别规则、翻页机制
- [leaderboards_guide.md](references/leaderboards_guide.md)：三大榜单访问方式、查询关键词、性能维度
- [project_coverage_template.md](references/project_coverage_template.md)：6 方向覆盖清单模板

## 与现有 skill 的关系

| skill | 关系 |
|-------|------|
| `web_access` | 入口决策：ModelScope 用 CDP（路径 3，需 JS 渲染），榜单用 WebFetch（路径 2，公开静态） |
| `browser_lessons` | CDP 选中后必读依赖，首次扫描后写入 sites/modelscope-cn.md |
| `niuke_review` | 结构参考：都是"WebFetch/CDP + 评分 + 报告"模式 |
| `apikey_test` | 关联：写入 keys.json 后可调 apikey_test 验证新模型可用性 |

## 风险与限制

1. **ModelScope 改版风险**：DOM 选择器（如 `acss-17aobl4`）为 2026-07 探索结果，失效时重新探索
2. **榜单覆盖限制**：三大榜单主要覆盖 LLM，图片/视频/语音模型常不在榜单上。无榜单数据时参考下载量 + 作者权威性
3. **arena.ai 梯子依赖**：用户可能不想启动梯子，arena 是可选数据源
4. **TTS/ASR 仅记录**：项目当前无 TTS/ASR use_case，扫描结果只写入推荐报告。未来若项目加入 TTS/ASR 能力，可参考报告中的候选模型，再走阶段 7-8 流程写入 keys.json
