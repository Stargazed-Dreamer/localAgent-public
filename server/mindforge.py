"""MindForge 集成路由 - 子进程调用 MindForge 文档处理和知识库搜索

MindForge 是外部项目（默认 <external_project_root>\\MindForge），提供：
  - 文档转 Markdown + LLM 结构化摘要（pipeline）
  - FAISS + BM25 混合知识库搜索（kb_search）

本模块通过子进程调用 MindForge，调用前会检查项目目录是否存在。
搜索引擎支持模型常驻内存，避免每次搜索重新加载（FAISS + sentence-transformers 约 10-30s）。
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from lib.schema import BaseSchema
from server.config import load_config

logger = logging.getLogger("localagent.mindforge")
router = APIRouter(prefix="/mindforge", tags=["MindForge"])


def get_mindforge_config() -> dict:
    """获取 MindForge 配置"""
    config = load_config()
    mf = config.get("mindforge", {})
    return {
        "enabled": mf.get("enabled", False),
        "path": mf.get("path", r"<external_project_root>\MindForge"),
        "python_executable": mf.get("python_executable", ""),  # 空=自动检测
    }


def _resolve_mindforge_dir() -> Path | None:
    """解析 MindForge 项目目录，不存在返回 None"""
    cfg = get_mindforge_config()
    path = Path(cfg["path"])
    return path if path.exists() else None


def _resolve_python(mf_dir: Path) -> str:
    """解析 MindForge 使用的 Python 可执行文件"""
    cfg = get_mindforge_config()
    # 用户指定了就用指定的
    if cfg["python_executable"]:
        return cfg["python_executable"]
    # 优先用 MindForge 自己的 venv
    venv_python = mf_dir / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    # 回退到当前 Python
    return sys.executable


def _resolve_mindforge_python_config(mf_dir: Path) -> str | None:
    """从 MindForge 的 main_config.json 读取 python_executable"""
    config_path = mf_dir / "main_config.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        rel = data.get("python_executable", "")
        if rel:
            abs_path = (mf_dir / rel).resolve()
            if abs_path.exists():
                return str(abs_path)
    except (json.JSONDecodeError, OSError):
        pass
    return None


# ========== 搜索引擎管理 ==========

class SearchEngineManager:
    """管理 MindForge HybridSearcher 的加载/卸载/常驻

    HybridSearcher 加载需要读取 FAISS 索引 + sentence-transformers 模型，
    首次加载约 10-30 秒。常驻策略（原 keep_models 死旋钮）已迁移到
    ModelLifecycleManager 的 per-model restore_preload 配置（design §9 写回链）。
    """

    def __init__(self):
        # HybridSearcher 从 kb_search 目录动态 import（无法静态解析），用 Any 承载
        self._searcher: Any = None
        self._index_dir: Path | None = None  # 当前加载的索引目录，用于检测索引是否更新
        self._index_mtime: float | None = None  # 索引 meta.json 的修改时间
        # keep_models 死旋钮已迁移到 ModelLifecycleManager（design §9 写回链）
        # —— 见 keep_models property

    def get_searcher(self, index_dir: Path) -> Any:
        """获取搜索引擎实例，如果索引已更新则自动重载

        HybridSearcher 从 kb_search 目录动态 import，无法静态解析 → 返回 Any。
        """
        meta_path = index_dir / "meta.json"
        current_mtime = meta_path.stat().st_mtime if meta_path.exists() else None

        # 检查是否需要重载：首次加载 / 索引目录变了 / 索引文件更新了
        need_reload = (
            self._searcher is None
            or self._index_dir != index_dir
            or (current_mtime is not None and current_mtime != self._index_mtime)
        )

        if need_reload:
            self.unload()
            # 将 MindForge 的 kb_search 加入 Python 路径
            kb_search_dir = str(index_dir.parent)
            if kb_search_dir not in sys.path:
                sys.path.insert(0, kb_search_dir)

            from search_engine import HybridSearcher  # type: ignore[import-not-found]
            logger.info(f"正在加载 MindForge 搜索引擎 ({index_dir})...")
            t0 = time.perf_counter()
            self._searcher = HybridSearcher(index_dir, force_cpu=True)
            elapsed = int((time.perf_counter() - t0) * 1000)
            self._index_dir = index_dir
            self._index_mtime = current_mtime
            logger.info(f"MindForge 搜索引擎加载完成, 耗时 {elapsed}ms")

        return self._searcher

    def unload(self):
        """卸载搜索引擎，释放内存"""
        if self._searcher is not None:
            del self._searcher
            self._searcher = None
            self._index_dir = None
            self._index_mtime = None
            logger.info("MindForge 搜索引擎已卸载")

    @property
    def loaded(self) -> bool:
        return self._searcher is not None

    @property
    def index_dir(self) -> Path | None:
        return self._index_dir

    @property
    def keep_models(self) -> bool:
        """keep_models 读出口（design §9 写回链）。

        语义所有权已迁移到 ModelLifecycleManager；此处回读管理器 per-model
        配置（restore_preload），供 /health.mindforge 等消费者拿到生效值。
        管理器未注册时回退 True（与旧行为一致）。
        """
        try:
            from server.model_manager import get_model_manager
            return get_model_manager().keep_models("mindforge_searcher")
        except Exception:
            return True


search_engine = SearchEngineManager()


# ========== 文档转换守护进程管理 ==========

class ConverterDaemon:
    """管理 MindForge 文档转换守护进程

    守护进程（tools/mindforge/converter_daemon.py）作为常驻子进程运行，
    通过 stdin/stdout JSON 协议通信。模型（MineRU、PaddleOCR）在首次转换时加载，
    之后常驻内存，避免每次转换重新加载（首次约 30-60s，后续秒级）。
    """

    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._mf_dir: Path | None = None
        self._python_exe: str | None = None
        self._ready = False

    async def start(self, mf_dir: Path, python_exe: str) -> bool:
        """启动守护进程"""
        if self._proc is not None and self._proc.returncode is None:
            return True  # 已在运行

        daemon_script = Path(__file__).parent.parent / "tools" / "mindforge" / "converter_daemon.py"
        if not daemon_script.exists():
            logger.error(f"守护进程脚本不存在: {daemon_script}")
            return False

        cmd = [python_exe, str(daemon_script)]
        logger.info("启动 MindForge 转换守护进程...")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(mf_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as e:
            logger.error(f"启动守护进程失败: {e}")
            return False

        # 等待 ready 信号
        proc = self._proc
        if proc is None:
            logger.error("守护进程进程句柄不可用")
            await self.stop()
            return False
        stdout = proc.stdout
        if stdout is None:
            logger.error("守护进程 stdout 不可用")
            await self.stop()
            return False
        try:
            ready_line = await asyncio.wait_for(stdout.readline(), timeout=30)
            ready = json.loads(ready_line.decode().strip())
            if ready.get("status") != "ready":
                logger.error(f"守护进程未返回 ready: {ready}")
                await self.stop()
                return False
        except TimeoutError:
            logger.error("守护进程启动超时（30s）")
            await self.stop()
            return False
        except Exception as e:
            logger.error(f"读取守护进程 ready 信号失败: {e}")
            await self.stop()
            return False

        self._mf_dir = mf_dir
        self._python_exe = python_exe
        self._ready = True
        logger.info("MindForge 转换守护进程已启动")
        return True

    async def convert(self, source_path: str, output_dir: str = "") -> dict:
        """发送转换请求到守护进程"""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return {"status": "error", "detail": "守护进程未运行"}

        req = json.dumps({
            "action": "convert",
            "source_path": source_path,
            "output_dir": output_dir,
        }, ensure_ascii=False) + "\n"

        try:
            stdin = proc.stdin
            stdout = proc.stdout
            if stdin is None or stdout is None:
                return {"status": "error", "detail": "守护进程 IO 不可用"}
            stdin.write(req.encode())
            await stdin.drain()

            resp_line = await asyncio.wait_for(stdout.readline(), timeout=300)
            return json.loads(resp_line.decode().strip())
        except TimeoutError:
            return {"status": "error", "detail": "转换超时（300s）"}
        except Exception as e:
            return {"status": "error", "detail": f"通信失败: {e}"}

    async def ping(self) -> dict:
        """检查守护进程状态"""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return {"status": "not_running", "models_loaded": False}

        req = json.dumps({"action": "ping"}) + "\n"
        try:
            stdin = proc.stdin
            stdout = proc.stdout
            if stdin is None or stdout is None:
                return {"status": "not_responding", "models_loaded": False}
            stdin.write(req.encode())
            await stdin.drain()
            resp_line = await asyncio.wait_for(stdout.readline(), timeout=5)
            return json.loads(resp_line.decode().strip())
        except Exception:
            return {"status": "not_responding", "models_loaded": False}

    async def stop(self):
        """停止守护进程"""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                stdin = proc.stdin
                if stdin is not None:
                    req = json.dumps({"action": "shutdown"}) + "\n"
                    stdin.write(req.encode())
                    await stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            finally:
                if proc.returncode is None:
                    proc.kill()
                self._proc = None
                self._ready = False
                logger.info("MindForge 转换守护进程已停止")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def ready(self) -> bool:
        return self._ready and self.running


converter_daemon = ConverterDaemon()


def get_status() -> dict:
    """MindForge 状态概览（供 /health 调用，不暴露 raw 私有变量）"""
    mf_dir = _resolve_mindforge_dir()
    if not mf_dir:
        return {
            "available": False,
            "has_index": False,
            "search_engine_loaded": False,
            "converter_daemon_running": False,
        }
    return {
        "available": True,
        "has_index": (mf_dir / "kb_search" / "index_store" / "meta.json").exists(),
        "search_engine_loaded": search_engine.loaded,
        "converter_daemon_running": converter_daemon.running,
    }


# ========== 请求/响应模型 ==========

class PipelineRequest(BaseSchema):
    source_dir: str  # 源文件目录
    model_profile: str = "null"  # null / ds / xiaomi
    conversion_workers: int = 1
    summary_workers: int = 10
    validate_files: bool = False
    # LLM 覆盖（可选）
    api_key: str | None = None
    base_url: str | None = None
    api_model: str | None = None


class PipelineResponse(BaseSchema):
    task_id: str  # 用进程 PID 标识
    status: str  # running / completed / failed
    message: str


class SearchRequest(BaseSchema):
    query: str
    top_k: int = 10
    mode: str = "hybrid"  # hybrid / dense / bm25
    dense_weight: float = 0.6
    bm25_weight: float = 0.4


class SearchResultItem(BaseSchema):
    title: str
    rel_path: str
    snippet: str
    score: float
    dense_rank: int | None = None
    bm25_rank: int | None = None


class SearchResponse(BaseSchema):
    results: list[SearchResultItem]
    total: int
    mode: str


class BuildIndexRequest(BaseSchema):
    force_cpu: bool = False
    batch_size: int = 16


class BuildIndexResponse(BaseSchema):
    task_id: str
    status: str
    message: str


class MindForgeStatusResponse(BaseSchema):
    available: bool
    path: str
    python_executable: str | None = None
    has_index: bool
    has_pipeline: bool
    search_engine_loaded: bool
    converter_daemon_running: bool


# ========== 运行中的任务追踪 ==========

_running_processes: dict[str, asyncio.subprocess.Process] = {}


# ========== 路由 ==========

@router.get("/status", response_model=MindForgeStatusResponse, operation_id="mindforge_status")
async def mindforge_status():
    """检查 MindForge 可用性"""
    mf_dir = _resolve_mindforge_dir()
    if mf_dir is None:
        cfg = get_mindforge_config()
        return MindForgeStatusResponse(
            available=False,
            path=cfg["path"],
            has_index=False,
            has_pipeline=False,
            search_engine_loaded=False,
            converter_daemon_running=False,
        )

    python_exe = _resolve_python(mf_dir)
    # 优先用 main_config.json 中配置的 python
    config_python = _resolve_mindforge_python_config(mf_dir)
    if config_python:
        python_exe = config_python

    has_pipeline = (mf_dir / "pipeline" / "main_refactored.py").exists()
    has_index = (mf_dir / "kb_search" / "index_store" / "meta.json").exists()

    return MindForgeStatusResponse(
        available=True,
        path=str(mf_dir),
        python_executable=python_exe,
        has_index=has_index,
        has_pipeline=has_pipeline,
        search_engine_loaded=search_engine.loaded,
        converter_daemon_running=converter_daemon.running,
    )


@router.post("/pipeline", response_model=PipelineResponse, operation_id="mindforge_pipeline")
async def run_pipeline(req: PipelineRequest):
    """启动 MindForge 文档处理管线（异步子进程）"""
    mf_dir = _resolve_mindforge_dir()
    if mf_dir is None:
        raise HTTPException(status_code=503, detail="MindForge 项目目录不存在")

    python_exe = _resolve_python(mf_dir)
    config_python = _resolve_mindforge_python_config(mf_dir)
    if config_python:
        python_exe = config_python

    # 检查源目录
    source_dir = Path(req.source_dir)
    if not source_dir.exists():
        raise HTTPException(status_code=400, detail=f"源目录不存在: {req.source_dir}")

    # 构建命令
    cmd = [
        python_exe, "-m", "pipeline.main_refactored",
        "--source-dir", str(source_dir),
        "--prompt-path", "./pipeline/prompt_v2.txt",
        "--model-name", req.model_profile,
        "--conversion-workers", str(req.conversion_workers),
        "--summary-workers", str(req.summary_workers),
    ]
    if req.validate_files:
        cmd.append("--validate-files")
    if req.api_key:
        cmd.extend(["--api-key", req.api_key])
    if req.base_url:
        cmd.extend(["--base-url", req.base_url])
    if req.api_model:
        cmd.extend(["--api-model", req.api_model])

    logger.info("启动 MindForge pipeline: %s...", ' '.join(cmd[:6]))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(mf_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动 MindForge 失败: {e}") from None

    task_id = str(proc.pid)
    _running_processes[task_id] = proc

    # 异步监控进程完成
    async def _watch():
        await proc.wait()
        _running_processes.pop(task_id, None)
        if proc.returncode == 0:
            logger.info("MindForge pipeline 完成 (PID=%s)", task_id)
        else:
            stderr_reader = proc.stderr
            stderr = await stderr_reader.read() if stderr_reader is not None else b""
            logger.error(f"MindForge pipeline 失败 (PID={task_id}): {stderr.decode(errors='ignore')[:500]}")

    asyncio.create_task(_watch())

    return PipelineResponse(
        task_id=task_id,
        status="running",
        message=f"MindForge pipeline 已启动 (PID={task_id})",
    )


@router.get("/pipeline/{task_id}", operation_id="mindforge_pipeline_status")
async def pipeline_status(task_id: str):
    """查询管线运行状态"""
    proc = _running_processes.get(task_id)
    if proc is None:
        return {"task_id": task_id, "status": "unknown", "message": "进程不存在或已结束"}
    if proc.returncode is None:
        return {"task_id": task_id, "status": "running", "message": "管线运行中"}
    return {"task_id": task_id, "status": "completed" if proc.returncode == 0 else "failed", "returncode": proc.returncode}


@router.post("/search", response_model=SearchResponse, operation_id="mindforge_search")
async def search_knowledge_base(req: SearchRequest):
    """搜索 MindForge 知识库（搜索引擎常驻内存，首次加载约 10-30s，后续秒级）"""
    mf_dir = _resolve_mindforge_dir()
    if mf_dir is None:
        raise HTTPException(status_code=503, detail="MindForge 项目目录不存在")

    index_dir = mf_dir / "kb_search" / "index_store"
    if not (index_dir / "meta.json").exists():
        raise HTTPException(status_code=404, detail="知识库索引不存在，请先运行 build-index")

    # 搜索引擎首次加载 10-30s（FAISS + sentence-transformers），searcher.search 也可能
    # 触发模型推理，两者均为同步阻塞调用。包到线程里避免阻塞事件循环。
    # （design §5.3 / T4-2）
    def _do_search():
        searcher = search_engine.get_searcher(index_dir)
        return searcher.search(
            query=req.query,
            top_k=req.top_k,
            mode=req.mode,
            dense_weight=req.dense_weight,
            bm25_weight=req.bm25_weight,
        )

    try:
        results = await asyncio.to_thread(_do_search)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"MindForge 搜索引擎加载失败: {e}") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}") from None

    items = [
        SearchResultItem(
            title=r.title,
            rel_path=r.rel_path,
            snippet=r.snippet,
            score=round(r.score, 6),
            dense_rank=r.dense_rank,
            bm25_rank=r.bm25_rank,
        )
        for r in results
    ]

    return SearchResponse(results=items, total=len(items), mode=req.mode)


@router.post("/build-index", response_model=BuildIndexResponse, operation_id="mindforge_build_index")
async def build_index(req: BuildIndexRequest):
    """构建/更新知识库索引（异步子进程）"""
    mf_dir = _resolve_mindforge_dir()
    if mf_dir is None:
        raise HTTPException(status_code=503, detail="MindForge 项目目录不存在")

    python_exe = _resolve_python(mf_dir)
    config_python = _resolve_mindforge_python_config(mf_dir)
    if config_python:
        python_exe = config_python

    # 读取 MindForge 配置获取参数
    mf_config_path = mf_dir / "main_config.json"
    config_args = []
    if mf_config_path.exists():
        config_args = ["--config", str(mf_config_path)]
    if req.force_cpu:
        config_args.extend(["--force-cpu"])
    config_args.extend(["--batch-size", str(req.batch_size)])

    cmd = [
        python_exe, "-m", "kb_search.update_index",
        *config_args,
    ]

    logger.info("启动 MindForge build-index: %s", ' '.join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(mf_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"启动索引构建失败: {e}") from None

    task_id = str(proc.pid)
    _running_processes[task_id] = proc

    async def _watch():
        await proc.wait()
        _running_processes.pop(task_id, None)
        if proc.returncode == 0:
            logger.info(f"MindForge build-index 完成 (PID={task_id})")
        else:
            stderr_reader = proc.stderr
            stderr = await stderr_reader.read() if stderr_reader is not None else b""
            logger.error(f"MindForge build-index 失败 (PID={task_id}): {stderr.decode(errors='ignore')[:500]}")

    asyncio.create_task(_watch())

    return BuildIndexResponse(
        task_id=task_id,
        status="running",
        message=f"知识库索引构建已启动 (PID={task_id})",
    )


@router.post("/preload", operation_id="mindforge_preload")
async def preload_search_engine():
    """预加载搜索引擎到内存（首次搜索前调用，避免搜索时等待 10-30s）

    design §10 兼容委托：经管理器 manual_load（受压力态约束 + 失败冷却）。
    """
    if search_engine.loaded:
        return {"status": "already_loaded", "message": "搜索引擎已在内存中"}
    # 先做 index_dir 存在性检查，保持原有 403/404 错误码
    mf_dir = _resolve_mindforge_dir()
    if mf_dir is None:
        raise HTTPException(status_code=503, detail="MindForge 项目目录不存在")
    index_dir = mf_dir / "kb_search" / "index_store"
    if not (index_dir / "meta.json").exists():
        raise HTTPException(status_code=404, detail="知识库索引不存在，请先运行 build-index")
    # 委托管理器（manual_load 经驱动 load → search_engine.get_searcher）
    from server.model_manager import get_model_manager
    result = await get_model_manager().manual_load("mindforge_searcher")
    status = result.get("status", "error")
    if status == "ok":
        return {"status": "loaded", "message": "搜索引擎已加载到内存"}
    if status == "refused":
        raise HTTPException(status_code=503, detail=f"加载被拒绝: {result.get('reason')}")
    detail = result.get("detail", "搜索引擎加载失败")
    raise HTTPException(status_code=500, detail=detail) from None


@router.post("/unload", operation_id="mindforge_unload")
async def unload_search_engine():
    """卸载搜索引擎，释放内存（FAISS 索引 + sentence-transformers 约 1-2GB）

    design §10 兼容委托：经管理器 manual_unload（走线程 + 超时保护）。
    """
    if not search_engine.loaded:
        return {"status": "not_loaded", "message": "搜索引擎未加载"}
    from server.model_manager import get_model_manager
    result = await get_model_manager().manual_unload("mindforge_searcher")
    if result.get("status") == "error":
        return {"status": "error", "message": result.get("detail", "卸载失败")}
    return {"status": "unloaded", "message": "搜索引擎已卸载"}


# ========== 文档转换守护进程路由 ==========

class ConvertRequest(BaseSchema):
    source_path: str  # 单个文件路径
    output_dir: str = ""  # 输出目录（空=默认）


class ConvertResponse(BaseSchema):
    status: str
    output_path: str | None = None
    detail: str | None = None


@router.post("/convert", response_model=ConvertResponse, operation_id="mindforge_convert")
async def convert_document(req: ConvertRequest):
    """转换单个文档（通过守护进程，模型常驻内存，首次约30-60s，后续秒级）

    适用于逐个处理文档的场景。守护进程自动启动，模型加载后常驻。
    如需批量处理整个目录，用 /pipeline 接口。
    """
    mf_dir = _resolve_mindforge_dir()
    if mf_dir is None:
        raise HTTPException(status_code=503, detail="MindForge 项目目录不存在")

    source = Path(req.source_path)
    if not source.exists():
        raise HTTPException(status_code=400, detail=f"文件不存在: {req.source_path}")

    # 解析 Python 可执行文件
    python_exe = _resolve_python(mf_dir)
    config_python = _resolve_mindforge_python_config(mf_dir)
    if config_python:
        python_exe = config_python

    # 确保守护进程运行
    if not converter_daemon.running:
        ok = await converter_daemon.start(mf_dir, python_exe)
        if not ok:
            raise HTTPException(status_code=500, detail="转换守护进程启动失败")

    # 发送转换请求
    result = await converter_daemon.convert(req.source_path, req.output_dir)

    if result.get("status") == "error":
        return ConvertResponse(status="error", detail=result.get("detail", "未知错误"))

    return ConvertResponse(
        status="ok",
        output_path=result.get("output_path"),
    )


@router.post("/daemon/stop", operation_id="mindforge_daemon_stop")
async def stop_converter_daemon():
    """停止文档转换守护进程，释放模型内存（MineRU + PaddleOCR 约 2-4GB）"""
    if not converter_daemon.running:
        return {"status": "not_running", "message": "守护进程未运行"}
    await converter_daemon.stop()
    return {"status": "stopped", "message": "转换守护进程已停止"}


@router.get("/daemon/status", operation_id="mindforge_daemon_status")
async def daemon_status():
    """查询守护进程状态（是否运行、模型是否已加载）"""
    if not converter_daemon.running:
        return {"running": False, "models_loaded": False}
    result = await converter_daemon.ping()
    return {"running": True, "models_loaded": result.get("models_loaded", False)}
