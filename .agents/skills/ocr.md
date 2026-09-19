---
name: ocr
description: >
  OCR 文字识别服务，支持经典 PaddleOCR 文字识别和远程 VL（ModelScope API-Inference）文档解析。
  触发词：识别图片、OCR、读取图片、文字识别、图片转文字。当用户需要
  从图片中提取文字、识别截图内容、解析文档结构时触发。
task_type: adhoc.ocr
---

# OCR 识别技能 (ocr)

## 触发词
识别图片、OCR、读取图片、文字识别、图片转文字

## 概述
LocalAgent 提供两种 OCR 引擎：**经典 PaddleOCR**（本地、快速、输出可靠文字坐标）和**远程 VL**（文档解析、复杂版面理解、输出 Markdown）。截图/Computer Use 的文字定位默认使用经典 OCR；远程 VL 主要用于描述和文档结构，不作为普通文字定位默认路径。

## 前置条件
- 后端已启动：`uv run python -m server.main`（端口 8766）
- 远程 VL 需在 `data/llm/keys.json` 配置含 `vl` scope 的 provider key
- 经典 OCR 首次调用会自动下载模型，耗时较长

## 工作流

### 1. 选择引擎

| 场景 | 推荐引擎 | 原因 |
|------|----------|------|
| 文档/表格/复杂版面 | 远程 VL | 输出结构化 Markdown，保留版面语义 |
| 纯文字快速识别 | 经典 PaddleOCR | 速度快，返回逐行文本+置信度+坐标 |
| 需要文字坐标定位 | 经典 PaddleOCR | 返回每个文字块的 bbox 坐标 |
| UI 截图状态描述 | 经典 OCR 优先，必要时远程 VL | 能用文字判断时避免远程调用 |

### 2. 调用接口

**VL 模型**（文档解析）：
```
POST /ocr/vl/path   {path: "本地图片路径"}
POST /ocr/vl/base64 {data: "base64图片"}
POST /ocr/vl/file   {file: 上传图片文件}
→ {success, markdown, blocks: [{label, content, bbox}], image_size, elapsed_ms}
```

**经典模型**（文字识别）：
```
POST /ocr/path   {path: "本地图片路径", max_height: 3000}
POST /ocr/base64 {data: "base64图片", max_height: 3000}
POST /ocr/file   {file: 上传图片文件, max_height: 3000}
→ {success, text, details: [{text, confidence, box}], image_size, split_count, elapsed_ms}
```

**截图直接定位**（Computer Use 首选）：
```
POST /screen/ocr {mode:"window", hwnd:123, engine:"ocr"}
→ {success, text, details:[{text, confidence, box}], image_size, window_title}
```

### 3. 获取结果

- **VL 响应**：`markdown` 字段包含结构化 Markdown，`blocks` 字段包含每个版面块的标签/内容/坐标
- **经典响应**：`text` 字段包含全部识别文本（换行分隔），`details` 字段包含每行文字的置信度和坐标

### 4. 将 OCR bbox 转为点击坐标

- `details[].box` 是输入截图内的四点物理像素坐标；文字中心取四点坐标均值。
- 窗口截图：`screen_x = window_bbox.left + center_x`，`screen_y = window_bbox.top + center_y`。
- 全屏/双屏截图：bbox 相对虚拟屏拼接图，必须加虚拟屏原点；普通定位优先窗口模式。
- 文字本身就是按钮/菜单项时可直接点击中心；复选框、图标等在文字旁边时按布局偏移。
- 浏览器有 DOM 时优先 selector/visible text locator，OCR 用于 canvas、图片化页面和远程桌面。
- 纯图标或 OCR 无法消歧时才使用 `vision_locate`；VL 默认只描述布局和状态。

## 接口列表

### 识别接口

| 路径 | 方法 | 引擎 | 输入方式 | 说明 |
|------|------|------|----------|------|
| `/ocr/vl/file` | POST | VL | 上传文件 | VL 文档解析（上传） |
| `/ocr/vl/base64` | POST | VL | base64 | VL 文档解析（base64） |
| `/ocr/vl/path` | POST | VL | 本地路径 | VL 文档解析（路径） |
| `/ocr/file` | POST | 经典 | 上传文件 | 经典 OCR（上传） |
| `/ocr/base64` | POST | 经典 | base64 | 经典 OCR（base64） |
| `/ocr/path` | POST | 经典 | 本地路径 | 经典 OCR（路径） |

> 以上每个接口均有对应的 `/json` 后缀版本（如 `/ocr/vl/path/json`），接受 JSON Body，MCP 兼容。

### 模型管理接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/ocr/status` | GET | 查询模型加载状态（含 `mlm_state` ModelLifecycleManager 状态摘要） |
| `/ocr/models/keep` | POST | 设置模型常驻内存（`keep: true/false`）—— 写回 ModelLifecycleManager per-model `restore_preload` 配置 |
| `/ocr/models/unload` | POST | 卸载模型（`engine: "vl"/"ocr"/null`，null 卸载全部）—— 委托 `manager.manual_unload("ocr")`（增强卸载：排空 in_flight + del + gc + empty_cache + 延迟补释放） |
| `/ocr/models/preload` | POST | 预加载模型（`engine: "vl"/"ocr"`） |

> 模型管理接口同样有 `/json` 后缀版本，MCP 兼容。

### Model Lifecycle Manager 统一控制面（推荐）

OCR 作为 ModelLifecycleManager 注册的 4 个驱动之一（其余：memory_embedding / guide_embedding / mindforge_searcher），可通过统一 `/models/*` 端点手动控制：

| 路径 | 方法 | 说明 | 审批级别 |
|------|------|------|----------|
| `GET /models` | GET | 列出全部注册模型状态（model_id/resource/loaded/footprint_mb/priority/evictable/in_flight/loaded_at/last_load_ms） | read_only（免审批） |
| `POST /models/ocr/load` | POST | 手动加载 OCR 模型（豁免冷却与降级，但仍受压力态约束） | approval_required |
| `POST /models/ocr/unload` | POST | 手动卸载 OCR 模型（与 `/ocr/models/unload` 等效） | approval_required |
| `POST /models/pause` | POST | 暂停 GPU/CPU 压力监控（手动超驰通道，PAUSED 期间准入放行且暂停逐出） | approval_required |
| `POST /models/resume` | POST | 恢复压力监控 | approval_required |
| `GET /models/pressure` | GET | 各资源压力态 + used/total + 最近迁移事件 | read_only（免审批） |

**准入门控语义**：OCR 加载前内部调 `manager.admit("ocr")`，按 GPU 压力态放行/拒绝——
- `NORMAL`/`ARMED`/`PAUSED`：放行
- `REFUSING`：拒绝（抛 `ModelUnavailableError` → 503，调用方降级处理，不重试）
- `PROBE_DEGRADED`：拒绝（探测失败保持保护姿态）
- OCR 模型重载冷却期：拒绝（手动 load 端点豁免冷却）
- 连续 ≥3 次加载失败标记 `reload_degraded`：拒绝（提示重启后端）

**自动逐出语义**：GPU 进入 `REFUSING` 期间每采样周期持续评估候选（已加载 + `evictable=True` + 过 `min_loaded_seconds=60s` 保护），按 `(priority, -footprint_mb, reload_cost_sec)` 选 victim 串行卸载。OCR 是当前唯一 GPU 模型，所以 GPU 逐出实际就是逐 OCR；算法通用，为未来本地模型（本地 VL/图标检测）准备。

**keep_models 语义**：原 OCR `/ocr/models/keep` 的死旋钮已迁移到 ModelLifecycleManager per-model `restore_preload` 配置（语义所有权迁移，单例所有权不变）。写入口 `manager.set_keep_models("ocr", True/False)`；读出口 `manager.keep_models("ocr")` 供 `/health.ocr`、`/ocr/status`、GUI 回读生效值。配置位置：`[model_manager.models.ocr]` 段。

## 关键规则

1. **import 顺序**：Windows 上必须先 `import torch` 再 `import paddle`，否则 `shm.dll` 冲突导致 torch 加载失败（GPU 版同样存在）。后端代码结构天然满足此顺序：`main.py` 不在顶部 import paddle，torch 由 vision/transformers 等模块先加载，paddle 在 `server/ocr.py` 的 `get_ocr()` 中懒加载
2. **模型常驻**：OCR 模型卸载后重载可能触发 PaddlePaddle PIR bug，建议保持 `keep_models=true`（默认已开启）；远程 VL 是无状态 API 调用，无需常驻
3. **bbox 坐标**：截图 OCR 必须保持 `use_doc_unwarping=False`。UVDoc 会改变 UI 截图几何，而 v3 返回的 `rec_polys` 不会逆映射回原图。当前 800x600 至 2560x1440 网格验证误差为 1-3px；`config.toml [ocr]` 必须保持恒等变换，除非新模型经原始坐标 A/B 测试明确证明需要补偿。详见 `.agents/wip/ocr_bbox_offset.md`
4. **长图分割**：经典 OCR 自动将超长图片按 `max_height`（默认 3000px）分段识别，重叠 200px 防止截断，`split_count` 字段指示分段数
5. **远程 VL 限流**：ModelScope RPM ≈ 5，`max_concurrency=1`；429/5xx 触发 60s cooldown + 退避重试；多 provider 自动 failover
6. **首次加载慢**：经典模型首次调用需 PIR monkey-patch（~4s）+ 模型加载（~6s）+ PP-OCRv6 模型下载（首次约 27s）；GPU 稳态推理约 100-250ms/张。远程 VL 无需加载，首次调用即用
7. **PIR monkey-patch**：`ModelManager._apply_pir_patch()` 在首次 OCR 调用时懒加载执行，强制禁用 PaddleStaticRunner 的 `enable_new_ir`（PIR + oneDNN 有 bug）。原本在 `main.py` 顶部同步执行会阻塞启动 4.3s，移至懒加载后后端启动降至 ~0.6s

## PaddleOCR/PaddleX 更新门禁

升级版本、切换检测模型或改动预处理参数时，合并/部署前必须：

1. 确认 `server/ocr.py` 仍显式传 `use_doc_unwarping=False`；截图管线禁止默认启用 UVDoc。
2. 保持 `[ocr]` 为恒等值，先运行 `tools/debug/verify_bbox_affine.py` 做 800x600、1920x1080、2560x1440 回归。
3. 基线要求：25/25 标记匹配，X/Y mean 接近 0，RMS 约 1-3px，最大误差不应超过 5px。
4. 若失败，先做“直接检测器 vs 完整 PaddleOCR 管线”A/B；检查 UVDoc、方向分类、resize/padding 和截图坐标转换。
5. 只有直接检测器本身出现稳定系统误差时，才运行 `sample_bbox_offset.py` 拟合可选 affine；禁止先套旧参数。
6. 同步更新本 skill、`computer_use.md`、`docs/environment-constraints.md`、WIP/记忆和 CHANGELOG，并重跑 OCR/screen/browser/vision 测试。

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 后端接口 | POST /ocr/vl/* | VL 文档解析 |
| 后端接口 | POST /ocr/* | 经典 OCR 识别 |
| 后端接口 | GET /ocr/status | 模型状态查询 |
| 后端接口 | POST /ocr/models/* | 模型管理 |
| OCR 模块 | server/ocr.py | 路由 + ModelManager |
| 回归工具 | tools/debug/verify_bbox_affine.py | 截图 bbox 三档分辨率坐标回归 |
| 校准工具 | tools/debug/sample_bbox_offset.py | 仅在直接检测器确认有系统误差时拟合可选 affine |
| 配置 | config.toml [ocr] | 默认恒等；非恒等值必须有新模型校准证据 |

## 输入/输出

- **输入**：`temp/img_test/` 或任意本地图片路径，或 base64 编码图片
- **输出**：通过 API 返回 JSON，不写文件
