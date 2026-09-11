"""LocalAgent FastAPI 后端 - 统一入口

路由拆分:
  - /ocr/*    → server.ocr     (远程 VL / 经典OCR)
  - /agent/*  → server.agent   (LLM评分/对话/社群总结)
  - /browser/*→ server.browser (Chrome CDP控制)
  - /exec/*   → server.exec    (实时执行Python代码)
  - /screen/* → server.screen  (截图/窗口/键鼠操控)
  - /vision/* → server.vision  (视觉AI/远程 VL)
  - /health, /config, /shutdown, /mcp/stats → server.core.*
  - /llm/pool/* → server.core.llm_endpoints
"""

# PaddlePaddle 3.x PIR执行引擎 + oneDNN 兼容性修复
# 必须在import paddle之前设置，否则predict会报
import os

os.environ.setdefault("FLAGS_use_mkldnn", "0")

import ctypes
import sys
import uuid
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # System DPI Aware
        except Exception:
            pass

import logging

# Windows: 强制 stdout/stderr 用 UTF-8，避免 GBK 导致中文日志/响应在 chcp 65001 终端乱码
# 必须在 colorama.init() 之前执行，让 colorama 包装的是已 reconfigure 的 UTF-8 stream
# 治本：start.bat 设了 chcp 65001（终端按 UTF-8 解码），但 Python 主进程的 sys.stderr
# 默认按 locale 编码（GBK）写入，导致中文日志字节序列不匹配 → 乱码（如 "服务器正在关闭" → "æå¡å¨æ­£å¨å³é­"）
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

# Windows: 初始化 colorama，让 uvicorn/click 输出的 ANSI 颜色码在 cmd.exe 中正确渲染
# 必须在 logging.basicConfig 之前调用，否则 logging 会持有未包装的原始 stream
try:
    import colorama
    colorama.init()
except ImportError:
    pass

# truststore: 让 Python 用 Windows 系统证书库（而非 certifi），修复 GitHub API SSL 验证失败
# 必须在任何 HTTPS 请求发出前注入（workspace 可选组件、远程 VL 等）
# 缺失时静默降级会导致 GitHub API 调用全部 SSL 失败但不报错，
# 因此显式记录 warning 让缺失可观测（缺 truststore 时 backend 仍能启动其他功能）。
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    logging.getLogger(__name__).warning(
        "truststore 未安装：GitHub API SSL 验证会失败（certifi CA bundle 不完整）。"
        "请 `uv pip install truststore` 后重启后端。"
    )

# PaddleX PIR monkey-patch 已移至 server.ocr.ModelManager._apply_pir_patch()
# 此处在启动时同步 import paddlex 会阻塞 4.3 秒，改为在首次 OCR 调用时懒加载。

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.activity_tracker.activity import router as activity_router
from server.activity_tracker.headless_endpoints import router as headless_router
from server.activity_tracker.loop_manager import router as loop_router
from server.advanced import router as advanced_router
from server.agent import router as agent_router
from server.agent_guide import router as agent_guide_router
from server.apikey import router as apikey_router
from server.approval_panel_router import router as approval_panel_router
from server.auto_shutdown import router as auto_shutdown_router
from server.browser import router as browser_router
from server.command_guard import router as command_guard_router
from server.docviewer import router as docviewer_router
from server.exec import output_router as exec_output_router
from server.exec import router as exec_router
from server.exec import terminal_router as exec_terminal_router
from server.inbound_gateway.router import router as inbound_gateway_router
from server.inbox import router as inbox_router
from server.memory.router import router as memory_router
from server.mindforge import router as mindforge_router
from server.model_manager.routes import router as model_manager_router
from server.model_manager.types import ModelUnavailableError

# 子路由
from server.ocr import router as ocr_router
from server.probe.router import router as probe_router
from server.screen import router as screen_router
from server.system import router as system_router
from server.templates import router as templates_router

# 所有 /memory/* 端点由 server/memory 提供（三层记忆系统）
from server.todos import router as todos_router
from server.todos import wip_router as todos_wip_router
from server.user_message import router as user_message_router
from server.vl.vision import router as vision_router

logger = logging.getLogger("localagent")


class _TraeNotificationFilter(logging.Filter):
    """D10: 抑制 trae/session_stop 通知的 Pydantic 校验错误 WARNING。

    MCP 库 (mcp.shared.session._receive_loop) 对未知通知方法（如
    notifications/trae/session_stop）执行 Pydantic 校验时失败，
    每次都调用 logging.warning("Failed to validate notification: ...")，
    产生数万行噪音。此 filter 匹配并静默这些特定消息。

    保留其他 WARNING 消息（只过滤含 "Failed to validate notification"
    且含 "trae" 的记录）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not ("Failed to validate notification" in msg and "trae" in msg.lower())


def _setup_logging():
    """根据 config.toml [logging] 段配置根 logger。

    - 控制台 handler：按 console_level 输出
    - 文件 handler：file_enabled=true 时追加 RotatingFileHandler（按 file_level）
    - 日志格式统一：%(asctime)s [%(name)s] %(levelname)s: %(message)s
    - D10: 过滤 trae/session_stop 通知的 Pydantic 校验错误 WARNING
    """
    from server.config import get_logging_config

    cfg = get_logging_config()
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    root = logging.getLogger()
    # 清空 basicConfig 默认 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)

    # D10: 过滤 trae 通知校验噪音
    # MCP 库 (mcp.shared.session) 对未知通知方法（如 notifications/trae/session_stop）
    # 执行 Pydantic 校验时失败，每次都 log WARNING，产生数万行噪音
    trae_filter = _TraeNotificationFilter()

    # 控制台
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(getattr(logging, cfg["console_level"], logging.INFO))
    console.addFilter(trae_filter)
    root.addHandler(console)

    # 文件（默认开启，崩溃后可追溯）
    if cfg["file_enabled"]:
        try:
            from logging.handlers import RotatingFileHandler

            log_path = Path(__file__).parent.parent / cfg["file_path"]
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_h = RotatingFileHandler(
                log_path,
                maxBytes=cfg["max_bytes"],
                backupCount=cfg["backup_count"],
                encoding="utf-8",
            )
            file_h.setFormatter(fmt)
            file_h.setLevel(getattr(logging, cfg["file_level"], logging.INFO))
            file_h.addFilter(trae_filter)
            root.addHandler(file_h)
            root.setLevel(min(
                getattr(logging, cfg["console_level"], logging.INFO),
                getattr(logging, cfg["file_level"], logging.INFO),
            ))
            logger.info(f"文件日志已启用：{log_path} (maxBytes={cfg['max_bytes']}, backupCount={cfg['backup_count']})")
        except Exception as e:
            # 文件日志初始化失败不应阻塞启动，控制台仍可用
            print(f"[WARN] 文件日志初始化失败，仅控制台输出：{e}", file=sys.stderr)
            root.setLevel(getattr(logging, cfg["console_level"], logging.INFO))
    else:
        root.setLevel(getattr(logging, cfg["console_level"], logging.INFO))


_setup_logging()

VERSION = "0.46.0"

app = FastAPI(title="LocalAgent API", version=VERSION)


# ========== 异常处理 ==========

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """将 Pydantic 验证错误转为可读的 JSON 响应，明确指出哪个字段出错"""
    errors = []
    for err in exc.errors():
        loc = " -> ".join(str(part) for part in err.get("loc", []))
        msg = err.get("msg", "")
        err_type = err.get("type", "")
        # 提供更友好的提示
        hint = ""
        if err_type == "int_type" or "int" in err_type:
            hint = " (需要整数，不能传浮点数)"
        elif err_type == "missing":
            hint = " (必填字段缺失)"
        errors.append({"field": loc, "message": msg, "type": err_type, "hint": hint})
    logger.warning(f"请求验证失败 {request.url.path}: {errors}")
    return JSONResponse(
        status_code=422,
        content={"detail": "请求参数验证失败", "errors": errors},
    )


@app.exception_handler(ModelUnavailableError)
async def model_unavailable_handler(request, exc: ModelUnavailableError):
    """模型存活管理器准入拒绝 → 503（与现有"远程 VL 不可用"503 语义对齐）。

    调用方可程序化区分"暂时不可用"（503，可重试）与"代码错误"（500）。
    """
    hints = {
        "pressure_refusing": "资源压力过高，该模型已被暂停；压力回落后重试",
        "probe_degraded": "资源探测失败，保持保护姿态；请稍后重试",
        "cooldown": "该模型近期加载失败，处于冷却期；请稍后重试",
        "awaiting_first_sample": "监控尚未完成首次采样；请稍后重试",
        "reload_degraded": "该模型连续加载失败，建议重启后端",
        "unloading_in_progress": "模型正在卸载中；请稍后重试",
    }
    logger.warning(
        f"模型不可用 {request.url.path}: model={exc.model_id} reason={exc.reason}"
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "model_unavailable",
            "model_id": exc.model_id,
            "reason": exc.reason,
            "hint": hints.get(exc.reason, "模型暂不可用，请稍后重试"),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception):
    """全局兜底：未捕获异常返回 500 + trace_id，记录完整堆栈到日志（含文件）。

    trace_id 由 trace_id_middleware 预先生成并存于 request.state；
    客户端收到 trace_id 后可在 data/logs/server.log 中检索对应请求。
    """
    import traceback

    trace_id = getattr(request.state, "trace_id", None) or uuid.uuid4().hex[:12]
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error(
        f"未捕获异常 trace_id={trace_id} {request.method} {request.url.path}: {exc}\n{tb}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "trace_id": trace_id,
            "error_type": type(exc).__name__,
            "message": str(exc),
        },
    )


# ========== CORS ==========

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 中间件 ==========

from server.core.middleware import _load_mcp_stats, register_middleware

register_middleware(app)
_load_mcp_stats()  # 启动时加载 MCP 调用统计


# ========== 子路由注册 ==========

app.include_router(ocr_router)
app.include_router(agent_router)
app.include_router(auto_shutdown_router)
app.include_router(apikey_router)
app.include_router(inbound_gateway_router)  # 入站网关：/v1/* OpenAI 兼容 + /inbound/* 管理
app.include_router(browser_router)
app.include_router(exec_router)
app.include_router(exec_terminal_router)  # 终端会话（独立 prefix /terminals）
app.include_router(exec_output_router)    # 执行输出查看（独立 prefix /output）
app.include_router(command_guard_router)
app.include_router(approval_panel_router)  # 审批面板（/approvals/*）
app.include_router(screen_router)
app.include_router(vision_router)
app.include_router(memory_router)     # 三层记忆系统 v2（向后兼容旧 KV 接口）
app.include_router(todos_router)       # 待办模块（周期任务）
app.include_router(todos_wip_router)   # WIP 任务追踪（独立 prefix /wip）
app.include_router(mindforge_router)
app.include_router(model_manager_router)   # Model Lifecycle Manager 控制面（design §10）
app.include_router(docviewer_router)
app.include_router(system_router)
app.include_router(advanced_router)
app.include_router(templates_router)
app.include_router(agent_guide_router)
app.include_router(user_message_router)
app.include_router(loop_router)          # Loop 自动触发系统
app.include_router(headless_router)      # headless-agent-session（中断端点）
app.include_router(inbox_router)          # 收件箱
app.include_router(activity_router)       # Activity 数据查看/编辑/审核
app.include_router(probe_router)          # chat-mgmt-probe（对话管理探针）


# ========== 核心路由（health/config/shutdown/mcp_stats/llm_pool） ==========

from server.core.config_routes import register_config_routes
from server.core.health import register_health_routes
from server.core.llm_endpoints import register_llm_routes

register_health_routes(app, version=VERSION)
register_config_routes(app)
register_llm_routes(app)


# ========== 路由安全标签 ==========

from server.route_tags import auto_tag_routes, build_safety_lookup, init_op_path_map

auto_tag_routes(app)
app.state.safety_lookup = build_safety_lookup(app)
# 构建 operation_id → (method, path) 反查表（MCP 网关二次确认审批级别时用）
init_op_path_map(app)

# 构建路径 → operation_id 映射（三层审批用，必须在所有路由注册后）
from server.approval_review import init_path_map

init_path_map(app)


# ========== 生命周期（startup/shutdown） ==========

from server.core.lifecycle import register_lifecycle

register_lifecycle(app)


# ========== MCP 工具接口 ==========

from server.core.mcp_gateway import setup_mcp

setup_mcp(app)


# ========== 静态文件服务 ==========

# workspace 数据（供可视化页面读取）
WORKSPACE_DIR = Path(__file__).parent.parent / "workspace"
if WORKSPACE_DIR.exists():
    app.mount("/workspace", StaticFiles(directory=str(WORKSPACE_DIR)), name="workspace")
    logger.info("工作区目录: http://127.0.0.1:8766/workspace/")


if __name__ == "__main__":
    import uvicorn

    from server.config import get_server_config
    cfg = get_server_config()
    # 传 app 对象而非字符串 "server.main:app"：避免 uvicorn 重新 import server.main 模块
    # 导致顶层代码（auto_tag_routes/setup_mcp/静态挂载等）执行两次（重复启动 + 重复打 tag）
    uvicorn.run(app, host=cfg["host"], port=cfg["port"])
