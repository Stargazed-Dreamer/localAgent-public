# 环境兼容性与浏览器设置
> 本文档是 `AGENTS.md` 中特定环境约束的详细展开。涉及 OCR/Vision/浏览器操作时需要了解这些约束。

## PaddlePaddle 兼容性

- **PaddlePaddle-GPU 3.2.2**（CUDA 12.6），从官方源 `https://www.paddlepaddle.org.cn/packages/stable/cu126/` 安装（PyPI 仅发布 CPU 版 `paddlepaddle`）
- **PaddleOCR 3.7.0**（含 PP-OCRv6 检测识别模型，相比 PP-OCRv5 检测 +4.6% / 识别 +5.1%）
- GPU 推理：`paddle.is_compiled_with_cuda() == True`，`paddle.device.get_device() == 'gpu:0'`，稳态 OCR 推理约 100-250ms/张（首次加载含模型下载约 27s）
- **必须先 import torch 再 import paddle**，否则 Windows 上 `shm.dll` 冲突导致 torch 加载失败（GPU 版同样存在此问题）。后端代码结构天然满足此顺序：`main.py` 不在顶部 import paddle，torch 由 vision/transformers 等模块先加载，paddle 在 `server/ocr.py` 的 `get_ocr()` 中懒加载
- **PIR monkey-patch 已移至 `server/ocr.py` 的 `ModelManager._apply_pir_patch()`**，在首次 OCR 调用时懒加载执行。原本在 `main.py` 顶部同步 import paddlex 会阻塞启动 4.3s，移除后后端启动降至 ~0.6s
- **bbox 坐标**：PaddleOCR v3 的默认 UVDoc 去畸变会改变 UI 截图几何，而输出 bbox 不会逆映射；本服务在 `server/ocr.py` 显式关闭 `use_doc_unwarping`。当前 `config.toml [ocr]` 为恒等变换，网格回归在 800x600、1920x1080、2560x1440 均为 1-3px。升级模型后运行 `tools/debug/verify_bbox_affine.py` 验证；详见 `.agents/wip/ocr_bbox_offset.md`。
- **Computer Use 定位策略**：浏览器优先 DOM locator；桌面/截图文字优先经典 OCR bbox；纯图标/无文字元素用 `understand_image` 描述；这些都失败时才调用 `vision_locate`。远程 VL 默认用于布局、选中状态、遮挡和异常画面描述，不承担普通文字坐标定位。（OmniParser 已于 2026-07-31 移除）
- **升级门禁**：升级 PaddleOCR/PaddleX、切换 det 模型或更改预处理后，必须确认 `use_doc_unwarping=False`，保持 affine 恒等并跑三档回归。目标为 25/25 匹配、mean≈0、RMS 1-3px、最大误差≤5px。失败先比较直接 detector 与完整 pipeline，禁止先恢复历史 `kx/ky` 参数。
- OCR 模型卸载后重载可能触发 PIR bug，建议保持 `keep_models=true`

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

- **Playwright 已安装**：`playwright` 和 `playwright-stealth` 已在 .venv 中，**禁止执行 `pip install playwright` 或 `playwright install`**
- **连接方式**：所有浏览器操作必须通过 `connect_over_cdp("http://127.0.0.1:9222")` 连接已运行的调试浏览器实例，**禁止启动新浏览器**
- **反检测**：必须使用 `playwright_stealth` 的 `Stealth` 类包装 page，隐藏自动化痕迹
- **标准模式**：
  ```python
  from playwright.async_api import async_playwright
  from playwright_stealth import Stealth

  async with async_playwright() as p:
      browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
      context = browser.contexts[0]
      page = await context.new_page()
      await Stealth().apply(page)
      # ... 操作页面
  ```
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
- **与工作浏览器隔离**：调试浏览器使用独立的 `chrome_debug/` 用户数据目录（可在 config.toml `[browser] user_data_dir` 配置），**绝对不会影响用户正在使用的工作浏览器**
- **自主操作流程**：
  1. 用 `browser_status` 检查调试浏览器是否已运行
  2. 如果未运行：`exec_python` 执行 `tools/browser/start_debug_browser.py` 启动
  3. 如果启动失败：提醒用户检查 Chromium 内核浏览器是否安装，或用 `--browser-path` 显式指定
  4. **不要问用户"要我来开还是你来开"**——直接开
  5. **不要打开用户的工作浏览器**——只用调试实例
