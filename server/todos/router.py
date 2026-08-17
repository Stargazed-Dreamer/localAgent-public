"""待办模块 FastAPI 路由

WIP 任务拆到独立 wip_router（prefix=/wip），与周期待办分离。
todos_router 的 /due 等固定路径仍需在 /{todo_id} 动态路由之前注册。
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from server.todos.models import (
    TodoCreate, TodoUpdate, TodoDoneRequest,
    WipTaskCreate, WipTaskUpdate,
)
from server.todos.store import TodosStore

logger = logging.getLogger("localagent.todos.router")
router = APIRouter(prefix="/todos", tags=["todos"])
wip_router = APIRouter(prefix="/wip", tags=["wip"])

_todos_store: Optional[TodosStore] = None
_todos_store_lock = threading.Lock()


def get_todos_store() -> TodosStore:
    global _todos_store
    with _todos_store_lock:
        if _todos_store is None:
            from server.memory.config import get_memory_config
            cfg = get_memory_config()
            # 相对路径相对于项目根目录解析（不依赖 cwd；router.py 在 server/todos/，3 级 parent 到项目根）
            db_path = Path(cfg.db_path)
            if not db_path.is_absolute():
                db_path = Path(__file__).resolve().parent.parent.parent / db_path
            _todos_store = TodosStore(str(db_path))
            _todos_store.initialize()
        return _todos_store


# ─── 周期待办（todos_router）──────────────────────────────

@router.get("", operation_id="todos_list", summary="列出所有待办")
async def todos_list(
    status: Optional[str] = Query(None, description="按状态过滤"),
    todo_type: Optional[str] = Query(None, description="按类型过滤（当前仅支持 recurring）"),
):
    """列出所有待办，可按状态/类型过滤。"""
    store = get_todos_store()
    todos = await asyncio.to_thread(store.list_todos, status=status, todo_type=todo_type)
    return {"todos": todos, "total": len(todos)}


@router.post("", operation_id="todos_create", summary="创建待办")
async def todos_create(req: TodoCreate):
    """创建一个新待办。"""
    store = get_todos_store()
    todo = store.create_todo(req.model_dump())
    return todo


@router.get("/due", operation_id="todos_due", summary="获取到期待办")
async def todos_due():
    """返回所有到期的周期任务（含从未完成和已过 next_due_at 的）。"""
    store = get_todos_store()
    due = await asyncio.to_thread(store.get_due_todos)
    return {"due": due, "total": len(due)}


@router.get("/{todo_id}", operation_id="todos_get", summary="获取待办详情")
async def todos_get(todo_id: str):
    store = get_todos_store()
    todo = await asyncio.to_thread(store.get_todo, todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return todo


@router.put("/{todo_id}", operation_id="todos_update", summary="更新待办")
async def todos_update(todo_id: str, req: TodoUpdate):
    store = get_todos_store()
    updates = req.model_dump(exclude_none=True)
    todo = store.update_todo(todo_id, updates)
    if not todo:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return todo


@router.delete("/{todo_id}", operation_id="todos_delete", summary="删除待办")
async def todos_delete(todo_id: str):
    store = get_todos_store()
    deleted = store.delete_todo(todo_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return {"status": "deleted", "id": todo_id}


@router.post("/{todo_id}/done", operation_id="todos_mark_done", summary="标记待办完成")
async def todos_mark_done(todo_id: str, req: Optional[TodoDoneRequest] = None):
    """标记待办完成（更新 last_done_at + 计算 next_due_at，status 重置为 pending）。

    对周期任务：完成本次周期，等待下次到期。
    对一次性任务：可后续手动将 status 改为 done。
    对 phased_recurring：超出 end_date 后置 archived。
    对 triggered：重置 next_due_at=NULL，等待下次事件触发。
    """
    store = get_todos_store()
    done_date = req.done_date if req else None
    todo = store.mark_done(todo_id, done_date)
    if not todo:
        raise HTTPException(status_code=404, detail=f"待办 '{todo_id}' 不存在")
    return todo


@router.post("/{todo_id}/check_trigger", operation_id="todos_check_trigger",
             summary="检查 triggered 任务触发条件")
async def todos_check_trigger(todo_id: str, payload: dict):
    """手动检查 triggered 类型任务的触发条件是否满足。

    通常由 loop 的 todos_trigger_check action 自动调用；此端点供 agent 主动触发或测试。
    payload 示例：{"event": "file_arrived", "path": "E:/test/data.csv", "filename": "data.csv"}
    """
    store = get_todos_store()
    result = store.check_trigger(todo_id, payload)
    return result


# ─── WIP 任务（wip_router，prefix=/wip）──────────────────

@wip_router.get("", operation_id="wip_list", summary="列出所有 WIP 任务")
async def wip_list(
    status: Optional[str] = Query(None, description="按状态过滤"),
    summary: bool = Query(False, description="true=只返回 id/title/status/priority/progress/updated_at 轻量字段，避免长输出污染上下文。需要详情再用 wip_get(task_id) 取"),
):
    """列出所有 WIP 任务，可按状态过滤。

    - 默认返回完整字段（goal/next_steps/current_state/related_*/extra_data 等）
    - summary=true 只返回轻量摘要字段，适合 agent 会话启动快速浏览未完成工作
    """
    store = get_todos_store()
    tasks = await asyncio.to_thread(store.list_wip, status=status, summary=summary)
    return {"tasks": tasks, "total": len(tasks), "summary": summary}


@wip_router.post("", operation_id="wip_create", summary="创建 WIP 任务")
async def wip_create(req: WipTaskCreate):
    """创建一个新 WIP 任务。"""
    store = get_todos_store()
    task = store.create_wip(req.model_dump())
    return task


@wip_router.get("/{task_id}", operation_id="wip_get", summary="获取 WIP 任务详情")
async def wip_get(task_id: str):
    store = get_todos_store()
    task = await asyncio.to_thread(store.get_wip, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"WIP 任务 '{task_id}' 不存在")
    return task


@wip_router.put("/{task_id}", operation_id="wip_update", summary="更新 WIP 任务")
async def wip_update(task_id: str, req: WipTaskUpdate):
    store = get_todos_store()
    updates = req.model_dump(exclude_none=True)
    task = store.update_wip(task_id, updates)
    if not task:
        raise HTTPException(status_code=404, detail=f"WIP 任务 '{task_id}' 不存在")
    return task


@wip_router.delete("/{task_id}", operation_id="wip_delete", summary="删除 WIP 任务")
async def wip_delete(task_id: str):
    store = get_todos_store()
    deleted = store.delete_wip(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"WIP 任务 '{task_id}' 不存在")
    return {"status": "deleted", "id": task_id}


def get_status() -> dict:
    """供 /health 聚合"""
    try:
        store = get_todos_store()
        return store.get_stats()
    except Exception as e:
        logger.warning(f"todos 状态获取失败: {e}", exc_info=True)
        return {"available": False, "error": str(e)}
