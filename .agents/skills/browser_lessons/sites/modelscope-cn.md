---
aliases: [魔搭, 魔搭社区, ModelScope]
---

# ModelScope 魔搭社区 (modelscope.cn)

## 元信息

| 属性 | 值 |
|------|------|
| **主域** | modelscope.cn |
| **首次接触日期** | 2026-07-24 |
| **相关 skill** | modelscope_model_update（扫描前沿模型 + 验证 API 来源 + 写入 keys.json） |
| **登录态要求** | 匿名可浏览模型列表/详情页；调 API 需 ModelScope token（写到 keys.json） |
| **抓取频次** | 周期（按需，新模型发布时） |
| **文件创建日期** | 2026-07-24 |
| **最后更新** | 2026-09-13 |

## 网站概况

阿里达摩院开源模型社区。agent 任务主要是扫描支持 API 调用的前沿模型、验证"魔搭社区"为 API 提供方、收集候选清单供 keys.json 更新。

## 浏览器方案

| 项 | 值 | 备注 |
|----|----|----|
| 连接方式 | `connect_over_cdp("http://127.0.0.1:9222")` | 项目统一 |
| 无头模式 | **禁止** | 用户要求 + 本环境无头报错 |
| 登录态来源 | 匿名浏览即可 | 调 API 才需 token |
| 反风控 | 不主动调站内 API，纯 DOM 提取 | 模拟人类点击+等待 |

## 反爬/风控

- **触发条件**：未观察到主动反爬
- **规避方法**：用 DOM 提取 + 合理等待（3.5 秒/页），不调私有 API
- **风险阈值**：单次扫描 4 方向 × 2 页 × 10+ 模型详情页未触发限制

## DOM 结构与提取规则

### 入口 URL

```
https://modelscope.cn/models?filter=inference_type&page=1&tabKey=task
```

- `filter=inference_type`：**预选**只展示支持 API 调用的模型（必带）
- `tabKey=task`：按任务分类展示
- `page=N`：翻页参数
- `tasks={task_name}`：按任务过滤（点击左侧任务标签后追加，也可直接构造）

### 页面布局

- **列表页**：左侧过滤面板（`div.acss-d9h0gd`，w=256）+ 中间模型卡片列表 + 底部分页
- **详情页**：左侧模型说明正文（left=0, w=872）+ 右侧 API 面板（left=896, w=384）

### 字段提取表

| 字段 | 选择器/规则 | 注意事项 |
|------|--------|---------|
| **模型 ID** | 正则 `^/models/([^/]+/[^/\?]+)` 从 `a[href*="/models/"]` 提取 | **不从卡片标题取**——卡片标题可能含装饰文本；用 Set 去重（同一模型多页重复） |
| **卡片简介** | 卡片 `innerText` 切片前 400 字 | 会混入下载量/点赞数，需清洗 |
| **API 提供方** | 右侧面板 `div.acss-17aobl4` 的 innerText | **必须点开详情页**，列表卡片不显示提供方 |
| **API 面板标题** | 文本含 `推理 API` + `Inference` | 选择器可能改版，用文本特征回退定位（见下方） |
| **魔搭社区标签** | 面板文本含 `魔搭社区` | 仅显示 OpenAI/Anthropic 的不可用 |

### 左侧任务过滤项与项目方向对应

| 项目方向 | ModelScope 任务名 | 位置 |
|---------|------------------|------|
| LLM | "文本生成" | 热门任务区 |
| VL | "视觉多模态理解" | 热门任务区 |
| aigc_image | "文本生成图片" | 热门任务区 |
| aigc_video | "文本生成视频" | 热门任务区 |
| TTS | "语音合成" | 热门任务区 |
| ASR | "语音识别" | **不在热门任务区**，需展开"语音"分类 |

### 选择器优先级与回退

```javascript
// 右侧 API 面板定位（选择器可能改版，必须加文本回退）
let panel = document.querySelector('div.acss-17aobl4');
if (!panel) {
    const all = document.querySelectorAll('div, aside, section');
    for (const el of all) {
        const t = (el.innerText || '');
        if (t.includes('推理 API') && t.includes('Inference') 
            && el.getBoundingClientRect().left > 700) {
            panel = el;
            break;
        }
    }
}
```

### API 来源判断规则

```javascript
const text = panel?.innerText || '';
const hasTitle = text.includes('推理 API') && text.includes('Inference');
const hasModelscope = text.includes('魔搭社区');
// 可用 = hasTitle && hasModelscope
// 仅外部源 = hasTitle && !hasModelscope && (text.includes('OpenAI') || text.includes('Anthropic'))
// 无面板 = !panel
```

## URL 规则与重定向

- **直接构造 tasks URL 比点击过滤项更可靠**：`https://modelscope.cn/models?filter=inference_type&page={N}&tabKey=task&tasks={task_name}` 直接 goto，避免左侧面板点击失败
- **详情页 URL**：`https://modelscope.cn/models/{model_id}`（model_id = `{author}/{name}`）
- **URL 编码**：tasks 参数为中文任务名，Playwright goto 会自动编码

## 图片/资源加载

| 项 | 规则 | 备注 |
|----|------|------|
| 懒加载 | 未观察到 | 卡片图片即时加载 |
| 防盗链 | 未观察到 | 模型示例图可直接下载 |

## 评论/动态内容

（无，ModelScope 模型页无评论系统）

## 已知坑（与正常行为区分）

| 问题 | 错误做法 | 正确做法 | 发现日期 |
|------|---------|---------|---------|
| **`networkidle` 等待超时** | 用 `wait_until="networkidle"` + 短 timeout | 用 `domcontentloaded` + `wait_for_timeout(3500)` 兜底 | 2026-07-24 |
| **右侧 API 面板未及时渲染** | goto 后立即抓面板文本 | `wait_for_timeout(3500~4000)` 后再抓取 | 2026-07-24 |
| **仅看模型卡片判断 API 可用性** | 卡片不显示 API 提供方就跳过 | 必须点开详情页查右侧面板 | 2026-07-24 |
| **从卡片标题取模型 ID** | 直接取卡片标题文本 | 用正则从 `a[href*="/models/"]` 的 href 提取 `{author}/{name}` | 2026-07-24 |
| **同一模型在多页重复出现** | 直接 append 导致重复 | 用 Set 按模型 ID 去重 | 2026-07-24 |
| **卡片 innerText 混入导航/下载数** | 整个卡片 innerText 当简介 | 用正则限定 `/models/{author}/{name}` 模式，简介取前 400 字并清洗 | 2026-07-24 |
| **TTS/ASR 在 `filter=inference_type` 下返回 0 结果** | 当 bug 调试 | **正常**——ModelScope 当前无支持 API 调用的 TTS/ASR 模型，语音模型均需本地部署 | 2026-07-24 |
| **ASR 不在热门任务区** | 在热门任务区找"语音识别" | 展开"语音"分类才看到"语音识别"子任务 | 2026-07-24 |
| **点击左侧任务标签失败** | 反复重试 click | 直接构造 `&tasks={task_name}` URL goto | 2026-07-24 |
| **右侧面板选择器 `acss-17aobl4` 失效** | 报错退出 | 用文本特征（"推理 API" + "Inference" + left>700）回退定位 | 2026-07-24 |
| **仅显示 OpenAI/Anthropic 的模型被误采** | 看到 API 面板就认为可用 | 必须验证面板含"魔搭社区"标签 | 2026-07-24 |
| **面板"魔搭社区"标签漏判（反向误杀）** | 面板判 external_only 就放弃该模型 | 面板仅初筛，最终以真实 token API 实测（stream=true）为准——Qwen/Qwen3.5-35B-A3B 面板无魔搭标签但 API 实测 200 可用 | 2026-09-13 |
| **已上架模型可能转付费** | 404 当成下线反复调试 | 404 "This model is unavailable for free. The paid version is available now" = 模型转付费（Tencent-Hunyuan/Hy3），免费 token 无法调用 | 2026-09-13 |
| **inclusionAI Ring 系 401 ling_auth_not_exist** | 以为是 token 失效去换 token | Ring 系走 inclusionAI ling 独立授权层，通用 ModelScope token 不通（同 token 调 Ling-3.0-flash 正常） | 2026-09-13 |

## 遗留问题

- **TTS/ASR 方向无 API 模型**：ModelScope 在 `filter=inference_type` + 语音合成/语音识别过滤下"共找到 0 个结果"。未来若项目加入 TTS/ASR 能力，需考虑其他供应商（阿里云语音 NLS、讯飞开放平台等）
- **aigc_image/aigc_video 模型 API 调用方式不同**：图片生成是异步任务（`POST /v1/images/generations` + `GET /v1/tasks/{task_id}` 轮询），与 LLM 的 OpenAI 兼容接口不同。写入 keys.json 时需确认项目的 aigc 调用层支持异步任务模式
- **DOM 选择器为 2026-07 探索结果**：`acss-17aobl4` / `acss-d9h0gd` 等为构建产物类名，改版后会失效。已加文本特征回退，但回退本身也需未来验证

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-07-24 | 初始创建，汇总自 `temp/modelscope_explore/` 探索结果和 `modelscope_model_update` skill 首次扫描 |
| 2026-09-13 | 二次扫描（LLM+VL）新增 3 坑：面板魔搭标签漏判须 API 实测兜底、模型转付费 404 特征、Ring 系 ling 独立授权 401 |
