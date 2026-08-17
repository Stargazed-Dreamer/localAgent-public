"""应用生命周期：启动初始化 + 关闭清理"""

import logging
import time

from server.config import validate_config
from server.screen import on_shutdown as screen_on_shutdown
from server.screen import on_startup as screen_on_startup
from server.system import on_shutdown as system_on_shutdown
from server.vl.vision import on_shutdown as vision_on_shutdown

logger = logging.getLogger("localagent.lifecycle")

_start_time = None


def get_start_time() -> float | None:
    """启动时间戳（供 /health 计算 uptime，不暴露 raw 私有变量）"""
    return _start_time


def _ensure_pool_initialized() -> bool:
    """确保 LLM 并发池已初始化（供 startup 和 /llm/pool/call 自动调用）

    从 data/llm/keys.json（v5 unified key 库）加载 keys 并初始化全局池。
    如果池已初始化则直接返回 True。
    """
    from server.llm_pool import (
        _load_default_policy,
        _resolve_default_stats_file,
        init_pool,
        is_initialized,
        load_provider_keys,
    )
    if is_initialized():
        return True
    keys = load_provider_keys()
    if not keys:
        logger.warning(
            "LLM 池初始化失败: 未加载到任何 key。"
            "请检查 data/llm/keys.json 是否存在且含 scope=llm 的记录。"
        )
        return False
    # v14：加载全局默认 pool 策略（从 config.toml [llm.pool] 段）
    default_policy = _load_default_policy()
    # 后端使用统一的 stats 文件（不再按脚本分文件，因为所有调用都经过后端）
    # _resolve_default_stats_file 已处理相对路径相对于项目根目录的解析
    init_pool(
        keys,
        stats_file=_resolve_default_stats_file(),
        default_policy=default_policy,
    )
    logger.info(
        f"LLM 并发池已自动初始化: {len(keys)} 个 key, "
        f"policy={default_policy.name} retry={default_policy.retry_count} "
        f"cooldown={default_policy.rate_limit_cooldown_seconds}s"
    )
    return True


def register_lifecycle(app):
    """注册启动和关闭事件处理器"""

    @app.on_event("startup")
    async def _on_startup():
        """应用启动：初始化屏幕、LLM 池、待办迁移、Loop 调度。"""
        global _start_time
        _start_time = time.perf_counter()

        config_warnings = validate_config()
        if config_warnings:
            for w in config_warnings:
                logger.warning(f"配置验证: {w}")
        else:
            logger.info("配置验证通过")

        screen_on_startup()

        # T18：显式初始化 SessionManager 单例（首次调用会从 config 读取 TimePolicy + 启动 worker）
        # 不依赖 overlay_client 模块级 import 时的隐式初始化，确保 SessionManager 在任何模块访问前已就绪
        try:
            from server.screen.session import get_session_manager
            get_session_manager()
        except Exception as e:
            logger.warning(f"SessionManager 初始化失败: {e}")

        # 提高 anyio 默认线程池上限（默认 40），以支持大量并发的 LLM 池代理调用
        # 多个脚本同时通过 /llm/pool/call 发起请求时，每个请求占用一个线程
        try:
            import anyio.to_thread
            limiter = anyio.to_thread.current_default_thread_limiter()
            old = limiter.total_tokens
            limiter.total_tokens = 200
            logger.info(f"anyio 线程池上限: {old} → {limiter.total_tokens}")
        except Exception as e:
            logger.warning(f"调整 anyio 线程池上限失败: {e}")

        # 初始化 LLM 并发池（自动从 data/llm/keys/ 加载）
        try:
            _ensure_pool_initialized()
        except Exception as e:
            logger.warning(f"LLM 并发池自动初始化失败（可手动调用 /llm/pool/init）: {e}")

        # 迁移待办数据（从 memory task_reminders/wip_index → todos 表，幂等）
        try:
            from server.memory.manager import get_memory_manager
            from server.todos.migration import backfill_extra_data, migrate_from_memory
            mgr = get_memory_manager()
            result = migrate_from_memory(mgr)
            if not result.get("skipped"):
                logger.info(f"待办迁移完成: {result}")
            # 回填 wip_tasks.extra_data 并移动旧 .json 到 temp/wip_migrated_backup/
            backfill_result = backfill_extra_data(mgr)
            if not backfill_result.get("skipped"):
                logger.info(f"extra_data 回填完成: {backfill_result}")
        except Exception as e:
            logger.error(f"待办迁移失败: {e}", exc_info=True)

        # 启动 Loop 自动触发系统（asyncio 后台调度协程）
        try:
            import asyncio as _asyncio

            from server.activity_tracker.loop_actions import register_loop_actions
            from server.activity_tracker.loop_manager import get_manager as _get_loop_manager
            mgr = _get_loop_manager()
            register_loop_actions(mgr)
            # 保存 task 引用防止 GC 回收（Python 文档要求）
            app.state.loop_start_task = _asyncio.create_task(mgr.start())
        except Exception as e:
            logger.warning(f"Loop 系统启动失败: {e}")

        # P1-A：可选预热 OCR（后台任务，不阻塞 startup；首次调用可省 ~10s 加载）
        try:
            from server.config import get_ocr_config as _get_ocr_cfg
            if _get_ocr_cfg().get("preload_on_startup", False):
                import asyncio as _asyncio

                async def _preload_ocr_bg():
                    try:
                        from server.ocr import models as ocr_models
                        logger.info("OCR preload_on_startup=true，后台预热 PaddleOCR...")
                        await _asyncio.to_thread(ocr_models.get_ocr)
                        logger.info(f"OCR 预热完成: load_elapsed_ms={ocr_models._load_elapsed_ms}")
                    except Exception as e:
                        logger.warning(f"OCR 后台预热失败: {e}")

                app.state.ocr_preload_task = _asyncio.create_task(_preload_ocr_bg())
        except Exception as e:
            logger.warning(f"OCR 预热配置读取失败: {e}")

        # T00: 启动 terminal TTL 清理后台任务（spec D11 第三层保障）
        try:
            from server.exec import start_terminal_ttl_cleanup
            start_terminal_ttl_cleanup()
        except Exception as e:
            logger.warning(f"terminal TTL 清理任务启动失败: {e}")

        logger.info("LocalAgent API 启动完成")

    @app.on_event("shutdown")
    async def _on_shutdown():
        """应用关闭：清理屏幕、系统、LLM 池统计、记忆、GUI、Loop、浏览器 session。"""
        # T18：先撤销 SessionManager 授权 + 停止 worker，再清理 GUI 子进程
        # （避免 worker 在 GUI 已关闭后仍 publish 事件到无人订阅的总线）
        try:
            from server.screen.session import get_session_manager
            get_session_manager().shutdown()
        except Exception:
            pass
        screen_on_shutdown()
        system_on_shutdown()
        # 批量保存模式下 shutdown 时强制落盘
        try:
            from server.llm_pool import get_pool
            get_pool().flush_stats()
        except Exception:
            pass
        try:
            vision_on_shutdown()
        except Exception:
            pass
        try:
            from server.memory.manager import reset_memory_manager
            reset_memory_manager()
        except Exception:
            pass
        # T18：overlay_client.shutdown() 已在 screen_on_shutdown() 内调用，此处不再重复
        # （重复调用会导致 multiprocessing.Queue put 后再 terminate，子进程可能死锁）
        try:
            from server.activity_tracker.loop_manager import get_manager as _get_loop_manager
            from server.activity_tracker.loop_manager import reset_manager as _reset_loop_manager
            mgr = _get_loop_manager()
            await mgr.stop()
            _reset_loop_manager()
        except Exception:
            pass
        try:
            from server.inbox import reset_store as _reset_inbox_store
            _reset_inbox_store()
        except Exception:
            pass
        # 清理持久浏览器 session（评估文档 P0）：关闭长连接 Playwright
        try:
            from server.browser.session.manager import reset_session_manager
            await reset_session_manager()
        except Exception as e:
            logger.warning(f"清理浏览器 session 失败: {e}")
        # Ticket 06: VL 配额批量写盘模式下 shutdown 时强制 flush，避免 dirty 状态丢失
        try:
            from server.activity_tracker.vl_quota import vl_quota
            vl_quota.flush()
        except Exception as e:
            logger.warning(f"VL 配额 flush 失败: {e}")
        # T07：关闭模块级 httpx client 单例（连接池复用清理）
        # close_async_client 是 async（aclose），close_sync_client 是 sync（close）
        try:
            from lib.async_http import close_async_client, close_sync_client
            await close_async_client()
            close_sync_client()
        except Exception as e:
            logger.warning(f"httpx client 关闭失败: {e}")
        logger.info("LocalAgent API 已关闭")
