# 环境兼容性与浏览器设置
> 本文档是 `AGENTS.md` 中特定环境约束的详细展开。涉及 OCR/Vision/浏览器操作时需要了解这些约束。

## PaddlePaddle 兼容性

- **PaddlePaddle-GPU 3.2.2**（CUDA 12.6），从官方源 `https://www.paddlepaddle.org.cn/packages/stable/cu126/` 安装（PyPI 仅发布 CPU 版 `paddlepaddle`）
- **PaddleOCR 3.7.0**（含 PP-OCRv6 检测识别模型，相比 PP-OCRv5 检测 +4.6% / 识别 +5.1%）
- **GPU 推理（2026-08-31 实机复测，当前生效配置）**：`config.toml [models] paddle_device = "gpu"`，paddle 3.2.2 (cu126) + RTX 4070 Laptop。PP-OCRv6_medium 实测 GPU：320x96 **0.05s** / 1920x1080 **0.51s** / 2560x1440 **0.76s**；对照组 CPU：0.23s / 3.81s / 5.40s，加速比 **4.6x~7.5x**（冷启动含模型下载约 27s）
- **[已过时 · 勿再据此禁用 GPU]** 2026-08-26 曾记录"GPU 首次推理永不返回"并据此把默认值降为 cpu、推断需升级 paddlepaddle 才能恢复。该结论已于 2026-08-31 推翻：同样的 paddle 3.2.2 / 驱动 Driver API 13.3 / RTX 4070 Laptop，三档各 3 轮共 9 次 GPU 预测全部正常返回，零挂死。成因未复现、无法归因，不排除间歇性；若复发，把 `paddle_device` 改回 `"cpu"` 立即回退
- **无 GPU 环境（如虚拟机）**：`paddlepaddle-gpu` 包照常 import，打印 `You are using GPU version Paddle, but your CUDA device is not set properly` 后自动回退 CPU（`paddle.device.get_device() == 'cpu'`），功能不受影响，**不需要**为 CPU 环境改装 `paddlepaddle` CPU 版。实测 PP-OCRv6_medium 在 CPU 上：模型加载 1.3s（权重已在本地），720×480 图稳态推理 **~1.1s/张**，1704×694 图约 1.5s
- **模型包格式**：paddlex 官方包解包后是 `inference.json` + `inference.pdiparams` + `inference.yml`（Paddle 3.x 的 PIR 格式，**没有** `inference.pdmodel`）。PaddleOCR 2.x 时代的旧 whl 包（`ch_PP-OCRv4_*_infer`，只有 pdmodel/pdiparams、无 yml）**不能**直接给 3.x 用；`*_infer.pth`（PyTorch 权重，如 PDF-Extract-Kit 的 `paddleocr_torch/`）同样不适用。默认 `lang="ch"` 需要三个模型：`PP-OCRv6_medium_det` / `PP-OCRv6_medium_rec` / `PP-LCNet_x1_0_textline_ori`，合计约 146 MB，源 `https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/<模型名>_infer.tar`，解包后目录需改名为去掉 `_infer` 后缀的模型名（paddlex 缓存目录读 `<PADDLE_PDX_CACHE_HOME>\official_models\<模型名>\`，见 `docs/model-paths.md`）
- **必须先 import torch 再 import paddle**，否则 Windows 上 `shm.dll` 冲突导致 torch 加载失败（GPU 版同样存在此问题）。后端代码结构天然满足此顺序：`main.py` 不在顶部 import paddle，torch 由 vision/transformers 等模块先加载，paddle 在 `server/ocr.py` 的 `get_ocr()` 中懒加载
- **PIR monkey-patch 已移至 `server/ocr.py` 的 `ModelManager._apply_pir_patch()`**，在首次 OCR 调用时懒加载执行。原本在 `main.py` 顶部同步 import paddlex 会阻塞启动 4.3s，移除后后端启动降至 ~0.6s
- **bbox 坐标**：PaddleOCR v3 的默认 UVDoc 去畸变会改变 UI 截图几何，而输出 bbox 不会逆映射；本服务在 `server/ocr.py` 显式关闭 `use_doc_unwarping`。当前 `config.toml [ocr]` 为恒等变换，网格回归在 800x600、1920x1080、2560x1440 均为 1-3px。升级模型后运行 `tools/debug/verify_bbox_affine.py` 验证；详见 `.agents/wip/ocr_bbox_offset.md`。
- **Computer Use 定位策略**：浏览器优先 DOM locator；桌面/截图文字优先经典 OCR bbox；纯图标/无文字元素用 `understand_image` 描述；这些都失败时才调用 `vision_locate`。远程 VL 默认用于布局、选中状态、遮挡和异常画面描述，不承担普通文字坐标定位。
- **升级门禁**：升级 PaddleOCR/PaddleX、切换 det 模型或更改预处理后，必须确认 `use_doc_unwarping=False`，保持 affine 恒等并跑三档回归。目标为 25/25 匹配、mean≈0、RMS 1-3px、最大误差≤5px。失败先比较直接 detector 与完整 pipeline，禁止先恢复历史 `kx/ky` 参数。
- OCR 模型卸载后重载可能触发 PIR bug，建议保持 `keep_models=true`（默认已开启；通过 ModelLifecycleManager per-model `restore_preload` 配置生效，详见 [Model Lifecycle Manager](#model-lifecycle-manager-显存释放实测) 段）

## Model Lifecycle Manager 显存释放实测

**Model Lifecycle Manager（`server/model_manager/`）** 是统一管理后端进程内 4 个本地模型生命周期的薄编排层——PaddleOCR（GPU，可逐出）/ 记忆 embedding（CPU，默认钉住）/ guide embedding（CPU，默认钉住）/ MindForge 搜索引擎（CPU，可逐出）。设计文档：`temp/sdd/model-lifecycle-manager/design.md`。

### 显存释放实测锚点（继承 ocr-vram-guard §3）

OCR 增强卸载 = `del + gc.collect() + paddle.device.cuda.empty_cache()`，实测：

| 操作 | 显存变化 | 说明 |
|------|----------|------|
| 加载 PaddleOCR | +484-597 MiB | 稳态占用 |
| 增强卸载 | -434/-597 MiB | 残留 ~163 MiB CUDA context |
| 同进程重载（热） | 稳定 ~1.5-2s | PIR bug 不影响热重载 |
| 冷启动重载 | ~27s | 含模型下载 |

**WDDM 限制**：WDDM 模式下只能查整卡 `memory.used`，无法跨进程归因到具体模型占用。ModelLifecycleManager 的 GPU 监控用 `nvidia-smi --query-gpu=memory.used,memory.total`（子进程 `timeout=3` + `asyncio.to_thread`），策略语义是"缓解整卡压力"，不是"谁占了显存"。

### 压力监控状态机（每资源独立）

```
NORMAL → REFUSING  （高压持续 ≥high_watermark×high_sustain 个周期，或 ≥critical_watermark 单样本逃生门）
REFUSING → ARMED   （低压持续 ≥low_sustain 个周期）
ARMED → NORMAL     （下次加载成功）
ARMED → REFUSING   （再次飙高）
卸载完成 → NORMAL
特殊态：PAUSED（手动 /models/pause）/ PROBE_DEGRADED（探测失败保持保护）
```

**默认配置**（`[model_manager.gpu]` 段，继承 ocr-vram-guard 全部默认值）：
- `enabled=true` / `high_watermark=0.90` / `high_sustain=3` / `critical_watermark=0.97`
- `low_watermark=0.70` / `low_sustain=6` / `min_loaded_seconds=60` / `unload_timeout_sec=15`

**CPU 监控**（`[model_manager.cpu]` 段）默认关闭（D2 决策：8GB+ 主机内存下 ~2GB 搜索引擎不构成压力，且误杀代价高）。

### 准入与逐出（在压力态下生效）

- **准入门控**：模块加载前调 `manager.admit(model_id)`，按目标资源压力态放行/拒绝。拒绝时抛 `ModelUnavailableError` → 503（调用方应降级处理，不重试）。`PAUSED` 期间放行；手动 load 端点豁免冷却与降级但仍受压力态约束。
- **电平式逐出**：`REFUSING` 期间每采样周期持续评估候选（已加载 + `evictable=True` + 过 `min_loaded_seconds` 保护 + 不在冷却期），按 `(priority, -footprint_mb, reload_cost_sec)` 排序选 victim 串行卸载。无候选 → 维持 `REFUSING` 仅拒绝。当前 GPU 逐出唯一实例就是 OCR。
- **per-model 冷却隔离**：某模型加载失败冷却不牵连同资源其他模型的准入（设计 v2 修订，避免记忆嵌入失败连带拒绝 guide embedding 加载）。连续 ≥3 次失败 → `reload_degraded` 标记（提示重启后端）。

### GPU→CPU 条件降级（2026-08-31 已实现）

`OcrDriver` 的 `resource` 已从静态 `"gpu"` 改为**动态 property**，跟随 OCR 实际设备
（`server/ocr.py` `ModelManager._decide_device()` 的决策结果 `_active_device`）。

当前行为：

```
GPU 压力拒绝态（gpu monitor REFUSING）时新加载请求到来
  → _decide_device() 现场采样主机内存/CPU（psutil）
    → 可用内存 ≥ gpu_fallback_min_avail_gb(2.0) 且 CPU ≤ gpu_fallback_max_cpu_pct(70)
      → 降级：_active_device = "cpu"，引擎以 CPU 加载，OCR 继续服务
    → 条件不达标（如游戏满载可用内存仅 0.46GB）
      → 维持拒绝（503），宁可不可用也不降级（降级会 swap/OOM 更糟）
```

关键机制（实现约束，改代码前必读）：

- **降级判断必须在加载之前**（GPU 挂死是永不返回而非抛异常，无法 try/except 兜底）
- **GPU 压力信号直接读 gpu monitor 的 `REFUSING` 态**，不能走 `check_admission()`——
  其按 `driver.resource` 选 monitor，未加载/已复位时 resource 为 "cpu" 会恒放行，
  `pressure_refusing` 根本到不了降级判断
- **只对 pressure_refusing 降级**：cooldown / reload_degraded / awaiting_first_sample
  是模型自身状态，换设备解决不了，由 `admit()` 原样拒绝
- **动态 resource 的两个作用**：① GPU 逐出器不再选中已降级的 OCR（否则死循环：
  卸载 CPU 版释放不了显存 → 水位不降 → 继续 refusing → 再降级 → 再被逐出）；
  ② admission 走 cpu monitor（默认 disabled → 放行）
- **不要启用 `[model_manager.cpu] enabled=true`**：启用后降级的 OCR 会走 CPU monitor
  准入，游戏满载 RAM 98% 时照样被拒，降级完全失效。保持关闭让 `check_admission`
  返回 `monitor_disabled` 放行才是期望行为
- **迟滞恢复（防抖）**：降级后至少保持 `gpu_fallback_hold_sec`(300s) 且 GPU 水位回落到
  `low_watermark`(0.70) 以下，下次加载才回 GPU；恢复时机是"下次加载时重新评估"，
  不主动卸载正在运行的引擎。迟滞状态跨卸载保留（防止卸载后水位临界反复切换）
- **卸载后 `_active_device` 复位为 config 期望设备**，下次加载重新决策
- **可观测性**：`/models` 的 `resource` 字段、`/ocr/status` 与 `/health` 的
  `active_device` 字段反映当前实际设备

配置（config.toml `[models]`，阈值依据 2026-08-31 游戏满载实测标定）：

| 键 | 默认 | 说明 |
|---|---|---|
| `gpu_fallback_min_avail_gb` | 2.0 | 降级所需最小可用内存（GB） |
| `gpu_fallback_max_cpu_pct` | 70.0 | 降级允许的最大 CPU 占用（%） |
| `gpu_fallback_hold_sec` | 300.0 | 降级后最短保持秒数（迟滞） |

若要重新验证或调整：**GPU 挂死无法 try/except 兜底**（永不返回，不是抛异常），
所以降级判断必须发生在**加载前**。实时监控压力态：`GET /models/pressure`
（判断降级是否触发要读 `recent_events`，`refusing` 态可能只持续亚秒级）。

### 实测验证（2026-08-31，游戏满载 8 分钟）

采集器 `temp/monitor_gpu_pressure.py`，原始数据 `temp/gpu_pressure_experiment_20260831.jsonl`
（159 样本 / 480s，含 GPU 水位、state、OCR 存亡、CPU%、内存）。

| 时间 | GPU 水位 | 事件 | OCR | 可用内存 |
|---|---|---|---|---|
| 01:45:34 | 44.4% | 游戏启动 | ✓ | 14.54 GB |
| 01:48:03 | 90.4% | 首次破线 | ✓ | ~0.6 GB |
| 01:49:02 | **90.10%** | `normal → refusing`，cause=`high_sustain` | ✓ | — |
| 01:49:02 | — | `refusing → normal`，cause=`eviction_complete`（**间隔仅 0.38s**） | — | — |
| 01:49:06 | 90.1% | — | **✗ 已卸载** | 0.81 GB |
| 01:49:48 | 73.5% | 卸载释放约 1.35 GB | ✗ | — |

结论：

- **后端压力逻辑确实会触发**，行为与设计一致（`high_sustain=3` × `poll_interval=5s`）
- **确认只有卸载、没有降级**：OCR 从 `loaded_ids` 中消失，直接不可用
- ⚠️ **`refusing` 态只持续 0.38 秒**，2 秒轮询**完全采不到**（脚本日志里 `state` 一路 `normal`）。
  判断有没有触发**必须读 `recent_events`，不能只看 `state` 字段**

### CPU 降级可行性（实测判定）

按"内存够 + CPU 压力不高"两个条件实测：

| 条件 | 实测值 | 判定 |
|---|---|---|
| CPU 压力不高 | 中位 23.0%，峰值 47.7% | ✓ 达标 |
| 内存够 | **最低可用 0.46 GB**（峰值占用 98.5%） | ✗ **不达标** |

**结论：游戏满载时瓶颈是主机内存，不是 CPU。** 可用内存仅 0.46 GB，
CPU 模式的 OCR 加载会直接触发 swap 甚至 OOM，比"不可用"更糟。
因此降级条件必须是"可用内存 ≥ 阈值"且阈值真正生效（按本次数据，余量至少 2 GB 以上）。
已按此实现条件降级（见上文「GPU→CPU 条件降级」），不达标时维持拒绝。

> 另注：后端 CPU 监控默认关闭（`get_model_manager_config()` 兜底为 gpu=True / cpu=False），
> `/models/pressure` 返回 `cpu.ratio=None`、`samples_taken=0`。这是**有意为之**：
> 降级后的 OCR 归属 cpu monitor，若启用且 RAM 高压会照样拒绝降级模型，降级完全失效。
> "CPU 压力不高"条件由 `server/ocr.py` 的 `_can_fallback_to_cpu()` 用 psutil 现场采样，
> 不依赖常驻监控循环（降级是低频事件）。

### 控制面端点（全部纳入审批分类）

新控制面 `/models/*` 端点已在 `server/route_tags.py` 的 `_APPROVAL_POST_PATTERNS` 增 `/models` 前缀 + `_MODERATE_EXCLUDE_PATTERNS` 镜像，**所有 POST 端点判为 `approval_required`**（避免 fail-open 默认让 agent 无审批卸载 OCR 瘫痪 Computer Use 主路径）：

| 端点 | 审批级别 | 用途 |
|------|----------|------|
| `GET /models` | read_only | 列出全部注册模型状态 |
| `POST /models/{id}/load` | approval_required | 手动加载模型（豁免冷却/降级，仍受压力约束） |
| `POST /models/{id}/unload` | approval_required | 手动卸载模型 |
| `POST /models/pause` | approval_required | 暂停压力监控（手动超驰通道） |
| `POST /models/resume` | approval_required | 恢复压力监控 |
| `GET /models/pressure` | read_only | 各资源压力态 + used/total + 最近迁移事件 |

兼容：`/ocr/models/*` 与 `/mindforge/preload|unload` 保留并委托管理器（调用方零改动）；`/ocr/status` 与 `/health.ocr` 追加 `mlm_state` 摘要。

### keep_models 写回链

原 OCR 与 MindForge 的 `keep_models` 死旋钮已统一迁移到 ModelLifecycleManager per-model `restore_preload` 配置（语义所有权迁移，单例所有权不变）：

- **写入口**：`/ocr/models/keep`（OCR）/ MindForge 同款旋钮 → 写入 `[model_manager.models.<id>].restore_preload`
- **读出口**：`/health.ocr`、`/ocr/status`、`/mindforge/status`、GUI `client/panels/llm_pool.py` → 全部回读管理器生效值
- 配置位置：`[model_manager.models.ocr]` 段（覆盖默认值）

### Health 聚合

`/health.model_manager` 低频枚举（不放高频计数器避免 client 指纹漂移）：

```json
{
  "enabled": true,
  "gpu_state": "NORMAL",       // NORMAL/REFUSING/ARMED/PAUSED/PROBE_DEGRADED
  "cpu_state": "disabled",      // 默认关闭
  "loaded_ids": ["memory_embedding", "ocr"],   // 当前已加载的 model_id 排序列表
  "reload_degraded_ids": [],   // 连续失败 ≥3 次的 model_id 列表（提示重启后端）
  "last_transition_ts": 1234567890.0   // 最近一次状态机迁移时间戳
}
```

`HealthResponse` 已显式声明 `model_manager` 字段（schema `extra='forbid'` 强制，避免漏写触发 schema 错误）。

### MindForge 搜索引擎已知局限

- 搜索端点持局部引用（mindforge.py），`unload()` 只删属性，1-2GB 实际要等**在途搜索结束**才释放；无 OCR 那样的 `forced` + 延迟补释放语义
- `load()` 无参协议与 `get_searcher(index_dir)` 必填参数冲突的解决：`unload()` 前自留 `last_index_dir` 快照（`unload()` 会清空 `_index_dir`），`load()` 优先用快照；从未加载过则复刻 `/mindforge/preload` 解析逻辑（`_resolve_mindforge_dir()` + meta.json 存在性检查）

### 记忆 embedding 手动卸载后果（默认钉住规避）

- 默认 `evictable=False`（钉住）：记忆 embedding 在每个 MCP 请求路径上，逐出后必然立即被下一请求拉回形成慢循环，且卸载窗口内写入的消息**永久缺失向量索引**——逐出收益为负
- 手动卸载（走审批门槛）后记忆语义检索**静默降级为 BM25-only**（无 503，消费者都先查 `ready`，不会写入零向量）
- **恢复步骤**：`POST /models/memory_embedding/load` + `POST /memory/rebuild_vector_index`（补卸载窗口缺口）

## 远程 VL（ModelScope Qwen3-VL-235B）

本地 VL（Qwen2.5-VL-3B + `envs/vl/`）已于 2026-07-07 下线，原因：8 GB VRAM 显存不足 + 3B 模型质量不达标（空表格/understand 503）+ 维护成本高。设计思路与删除环境清单见 `.agents/wip/local_vl_decommissioned.md`。

当前 VL 能力由远程 API 提供：
- **默认 provider**：魔搭社区 ModelScope（`https://api-inference.modelscope.cn/v1`），模型 `Qwen/Qwen3-VL-235B-A22B-Instruct`
- **OpenAI 兼容 chat completions API**，图像以 JPEG base64 data URL 内嵌 messages content
- **RPM ≈ 5**，`max_concurrency=1` 序列化调用避免触发限流；429/5xx 触发 cooldown（默认 60s，可通过 keys.json 中 VL key 的 `vision.rate_limit_cooldown` 配置）+ 退避重试 (2, 4, 8, 16, 32, 60) 秒（可通过 `[vision] vl_retry_backoffs` 配置）
- **多 provider 自动 failover**：通过 `key_store.resolve_keys(use_case)` 路由（`vl_ocr`/`vl_vision`/`vl_activity_tracker`），按 tier 升序遍历候选 key，第一个不在冷却中且并发未满的胜出；全部冷却时返回下次可用时间
- **配置（v9）**：VL provider 参数（`base_url`/`api_key`/`model`/`max_concurrency`/`timeout`/`rate_limit_cooldown`）统一存放在 `data/llm/keys.json` 中含 `vl` scope model 的 key 记录的 `vision` 段；`config.toml [vision]` 段只保留全局开关和编码参数。新增 VL provider：在 keys.json 中加一条含 `vl` scope model 的 key，并附 `vision` 段即可
- **禁用**：`vl_enabled=false` 可完全关闭远程 VL
- **模块**：`server/vl/remote_vl.py`（`RemoteVLClient` 单例，接口与旧 `QwenVLBridge` 兼容）

## Florence2 兼容性

- transformers 版本锁定 `>=4.45,<4.47`（5.x 和 4.57.x 不兼容）
- Florence2 processor 从 `Florence-2-base` 加载（仅 `tools/image_organizer/` 工具使用，从 HF cache 加载；OmniParser 移除后主后端不再加载 Florence2）
- 主 `.venv` 仍含 `transformers`（embeddings 模块用）和 `torch`（embeddings + STT 用），Florence2 processor 可正常加载

## 管理员权限

- 键鼠操控需要管理员权限，否则 Windows UIPI 阻止 `SetCursorPos`
- `start.bat` 自动检测并 UAC 提权，启动前杀掉占用 8766 端口的旧进程
- `/screen/status` 的 `admin_privileges` 字段指示当前权限状态

## Playwright 浏览器操作

- **Playwright 已安装**：`playwright` 已在 .venv 中，**禁止执行 `pip install playwright` 或 `playwright install`**
- **连接方式**：所有浏览器操作必须通过 `connect_over_cdp("http://127.0.0.1:9222")` 连接已运行的调试浏览器实例，**禁止启动新浏览器**
- **反检测：不要注入任何 JS 伪装补丁**。本项目连的是用户自启的**真实有头 Chrome**，
  指纹天然合规；`playwright-stealth` 之类的补丁库是为「Playwright 自己 launch 的无头
  Chromium」设计的，在本项目场景下是负资产（实测详见 `docs/browser-anti-detection.md`）。
  该依赖已于 2026-09-03 从 `server/`、全部 workspace 脚本中移除。
- **标准模式**：
  ```python
  from playwright.async_api import async_playwright

  async with async_playwright() as p:
      browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
      context = browser.contexts[0]
      page = await context.new_page()
      # ... 操作页面（无需任何 stealth 包装）
  ```
- **为什么不加补丁**（一句话版）：伪装要做**减法**。真实浏览器上 `navigator.webdriver`
  本来就是 `false`、`plugins` 原生合规，加补丁只会把 `languages` 从 `zh-CN` 改成
  `en-US`（与 HTTP 头 `Accept-Language: zh-CN` 矛盾）、把 WebGL renderer 从真实 GPU
  改成 `Intel Iris`（与其他硬件信号矛盾），并且会**永久污染用户 tab 的后续导航**。
- **下载行为（2026-09-03 实测，CDP 关键坑）**：`connect_over_cdp` 连真实 Chrome 后
  **从不自动配置下载行为**——浏览器一切下载默认落盘用户系统下载目录（实测多轮 e2e 在
  `<data_drive>:\<system_data_root>\<data_drive>:/Downloads` 累积泄漏 10 个 `test_download*.txt`，12 字节）。
  两条相关事实：
  1. `Download.save_as` **拦不住先落盘**：本地小文件瞬时下载完成，Chrome 已先写入默认
     目录，`save_as` 只是另存一份（对照实验：沙箱清空重跑仍泄漏 + 沙箱 0 文件）。
     `Download.cancel()` 能拦（阻止落盘），但只对"无 download_dir 的 wait_for"路径生效。
  2. **`Browser.setDownloadBehavior` 不能加"已配置就跳过"的幂等短路**：Playwright 的
     `Download.save_as` 在 CDP 模式下会自己重设 downloadPath，首次配置只对第一次下载
     生效，之后又回落默认目录（实测幂等版第二次会话必漏 1 个）。
- **下载沙箱根治模式**：每次 `create_session` 拿到 page 后**强制重发**（勿幂等）：
  ```python
  dl_dir = os.path.join(PROJECT_ROOT, "temp", "browser_<data_drive>:/Downloads")
  os.makedirs(dl_dir, exist_ok=True)
  cdp = await ctx.new_cdp_session(page)
  await cdp.send("Browser.setDownloadBehavior", {
      "behavior": "allow", "downloadPath": dl_dir, "eventsEnabled": True})
  ```
  实测：去幂等每次强制重发后，连续两次 download 会话 + e2e_full 全量 110 用例，
  <data_drive>:/Downloads 泄漏 0/0/0。实现见 `server/browser/session/manager.py` `_configure_download_behavior`。
  另：`wait_for(download)` 在未提供 `download_dir` 时应 `download.cancel()` 而非放任挂起。
- **操作原则**：`page.evaluate` 只读取DOM数据，`mouse.move + mouse.click` 执行交互操作（模拟人类）
- **已有工具脚本**：`workspace/bilibili_gacha/bilibili_gacha.py`、`workspace/arknights_gacha/arknights_gacha.py`、`workspace/endfield_gacha/endfield_gacha.py` 等都使用此模式，参考它们而不是从零开始

## 调试浏览器实例（通用，支持 Chromium 内核）

**项目有专用的调试浏览器实例，agent 应自主启动和管理，不要问用户。** 支持任何 Chromium 内核浏览器（Chrome / Edge / Brave / Vivaldi 等），脚本会自动探测已安装的浏览器。

- **启动脚本**：`tools/browser/start_debug_browser.py`（旧名 `start_debug_chrome.py` 作为薄包装保留）
- **启动命令**：
  - 默认（非交互，空白配置）：`uv run python tools/browser/start_debug_browser.py`
  - 复制登录态：`uv run python tools/browser/start_debug_browser.py --copy-user-data`（建议先关闭源浏览器）
  - 显式指定浏览器：`uv run python tools/browser/start_debug_browser.py --browser-path "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"`
  - 自定义端口/目录：`--port 9223 --user-data-dir /path/to/dir`
- **工作原理**：
  1. 检查 9222 端口是否已有浏览器在监听
  2. 默认非交互：若 `chrome_debug/` 不存在则创建空目录（空白配置）；`--copy-user-data` 时从源浏览器（按 browser_path 推断）复制用户数据（保留登录态和扩展）
  3. 以 `--remote-debugging-port=9222 --user-data-dir=chrome_debug` 启动独立浏览器实例
  4. 等待就绪后返回
- **浏览器探测优先级**：未指定 `--browser-path` 时，按 Chrome → Edge → Brave → Vivaldi 顺序探测默认安装路径
- **移植配置与浏览器版本的兼容性（2026-09-05 实测）**：本机调试浏览器生产配对是 **Edge 152**（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；用户目录下另有旧版 Chrome 135）。新版浏览器写入的用户数据目录，旧版 Chrome 启动即闪退（能打出 `DevTools listening` 但立刻 exit 3）。配置目录跨浏览器/跨版本移植后必须用目标浏览器实测；`chrome_debug/` 为 Edge 谱系配置，config.toml 未钉 `browser_path` 时自动探测会选中 Chrome 导致闪退，需显式 `--browser-path` 指向 Edge
- **与工作浏览器隔离**：调试浏览器使用独立的 `chrome_debug/` 用户数据目录（可在 config.toml `[browser] user_data_dir` 配置），**绝对不会影响用户正在使用的工作浏览器**
- **自主操作流程**：
  1. 用 `browser_status` 检查调试浏览器是否已运行
  2. 如果未运行：`exec_python` 执行 `tools/browser/start_debug_browser.py` 启动
  3. 如果启动失败：提醒用户检查 Chromium 内核浏览器是否安装，或用 `--browser-path` 显式指定
  4. **不要问用户"要我来开还是你来开"**——直接开
  5. **不要打开用户的工作浏览器**——只用调试实例

## 网络与环境查询技巧

本节是跨任务可复用的环境经验（不局限于 OCR）。

### 国内网络查 HuggingFace 的正确姿势

- `huggingface.co` 直连**必超时**（`WinError 10060`）
- `hf-mirror.com` 用裸 urllib 请求 API **返回 403**
- 唯一稳的姿势：用项目 venv 里的 `huggingface_hub` 并显式指定 endpoint

```python
from huggingface_hub import HfApi
files = HfApi(endpoint="https://hf-mirror.com").list_repo_files("<repo_id>")
```

这样能拿到含 `1_Pooling/`、`modules.json` 之类子路径的完整文件清单，
可用来验证 `allow_patterns` / `ignore_patterns` 到底能不能匹配到目标文件。

### 查 PaddlePaddle 可用 wheel 版本

GPU 版**只发在官方索引**，PyPI 上只有 CPU 版：

```
https://www.paddlepaddle.org.cn/packages/stable/<cu118|cu126|cu129|cu130>/paddlepaddle-gpu/
```

直接抓目录页用正则 `paddlepaddle_gpu-(\d+\.\d+\.\d+)-cp3XX-cp3XX-win_amd64\.whl` 提取最准。

⚠️ **GitHub tag 上有 ≠ 已发 wheel**：例如 3.4.0 有 tag，但四个 CUDA 索引全都没有 wheel。
判断"装的版本过不过时"要分三层看：PyPI 最新 / 官方索引 wheel 实际上限 / GitHub tag，
**取 wheel 实际上限才是真正可升级的目标**。

### 测"可能挂死"的推理调用

怀疑某个调用永不返回（如 GPU 推理挂死）时：

- **不要用主进程直接跑**。放独立脚本用 `timeout N <cmd>` 跑，靠退出码判断：
  **124 = 超时挂死，0 = 正常返回**
- **分层探测**定位挂死层级：① 框架基础算子（如 `paddle.matmul` 打到 `gpu:0`）
  → ② 完整 pipeline 首次推理 → ③ 多档分辨率多轮稳定性。
  **① 通过不能说明 ③ 没问题**
- 验证"历史 bug 已修复"要有铁证：`git show HEAD:<file>` 导出旧版到临时文件，
  用 `importlib.util.spec_from_file_location` 动态加载，新旧两版跑同一用例对比**调用计数**，
  而不是改完代码就宣称修好了。

### 确认后端是否真的在用 GPU

`/health` 接口不暴露 device 字段。最硬的证据是查 GPU 计算进程列表：

```bash
nvidia-smi --query-compute-apps=pid --format=csv,noheader
```

**后端 PID 出现在这个列表里**才说明真的在用 GPU —— CPU 模式下后端进程不会进该列表。
配合显存占用前后对比（OCR 上 GPU 约 +500 MiB）一起看。

### 采样 `/health` 的 `last_inference_ms`

调完 `/ocr/base64` **立刻**读 `/health` 会拿到**上一次**调用的值（实测偏差可达 2 倍以上）。
隔几秒再读才准。正确值应与端到端耗时接近（差十几毫秒是 HTTP 开销）。

### torch 与 paddle 的 import 顺序

必须先 `import torch` 再 `import paddle` / `import paddleocr`，否则 Windows 上 `shm.dll`
冲突导致 torch 加载失败（`WinError 127`）。
注意 `import paddleocr` 会走 paddlex → modelscope → torch 链，
所以实际约束是 **torch 先于 paddleocr**。
