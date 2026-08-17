"""Playwright 执行器：构建和执行 Playwright 子进程脚本。

包含 CDP 端点 helper、脚本头部构建器、完整脚本构建器和执行器。

从 server/browser.py 迁移至 server/browser/ 包（Ticket 01）。
代码与原定义完全一致，仅调整 import 路径：
- `from .config import ...` → `from ..config import ...`（包嵌套层级变化）
- `from .exec import ...` → `from ..exec import ...`（包嵌套层级变化）
"""

import json
import time

from ..config import get_browser_config


def _cdp_endpoint() -> str:
    """浏览器 CDP 调试端点 URL（从 config 读取，支持热更新）"""
    return f'http://127.0.0.1:{get_browser_config()["debug_port"]}'


def _build_playwright_header() -> str:
    """生成 Playwright 代码头部（含 stealth 包装）

    注入 _cdp_endpoint() 函数定义，避免 body 模板调用未定义函数（bug 修复）。
    URL 在模板渲染时计算，避免运行时依赖 server 模块。
    """
    cdp_url = _cdp_endpoint()
    return f"""
import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

def _cdp_endpoint():
    return '{cdp_url}'

async def _find_page(context, url_pattern=None, tab_id=None):
    '''返回 (page, error_code)。error_code 为 None 表示正常。

    优先级：tab_id（精确 CDP target id 经 URL 反查）> url_pattern（子串）> 第一个 tab。

    P0-2 修复（评估文档 P0 第 2 项）：url_pattern 匹配多个 page 时返回
    (None, 'AMBIGUOUS_TAB')，禁止静默选第一个；调用方应改用 tab_id 或 session_id。
    '''
    if tab_id:
        # tab_id 是 CDP target id，子进程内无直接映射，需调用方传入对应 URL 精确匹配
        # 这里仅做 URL 精确匹配（_tab_id_to_url 已在调用方完成）
        for page in context.pages:
            if page.url == tab_id:
                return page, None
        return None, 'TAB_NOT_FOUND'
    if not url_pattern:
        return (context.pages[0] if context.pages else None), None
    matches = [p for p in context.pages if url_pattern in p.url]
    if len(matches) == 0:
        return None, 'TAB_NOT_FOUND'
    if len(matches) > 1:
        return None, 'AMBIGUOUS_TAB'
    return matches[0], None
"""


def _build_playwright_script(params: dict, body_template: str) -> str:
    """构建完整 Playwright 执行脚本：header + params_literal + body

    用户输入通过 JSON 二次转义注入（json.dumps(json.dumps(...))），避免 f-string 代码注入。
    """
    params_literal = json.dumps(json.dumps(params, ensure_ascii=False))
    return _build_playwright_header() + f"""
import json
_PARAMS = json.loads({params_literal})
{body_template}
"""


async def _execute_playwright(code: str, timeout: int) -> tuple[bool, str, int]:
    """执行 Playwright 代码并解析 OK/ERROR 响应

    约定子进程 stdout 以 'OK:' 或 'ERROR:' 前缀标识结果。
    Returns: (success, message, elapsed_ms)
    """
    from ..exec import ExecRequest, exec_python
    t0 = time.perf_counter()
    # ExecRequest 无 timeout 字段（spec D5：exec_python 不支持 timeout 参数）。
    # 之前传 timeout= 会被 Pydantic 默认 extra='ignore' 静默丢弃；改 BaseSchema 后会报
    # ValidationError。此处删去错误字段名。timeout 暂未通过 exec_inspect(tid, timeout=N) 实施，
    # 依赖 exec_python 默认 inline_wait_secs 内联等待（见 server/exec.py exec_python）。
    result = await exec_python(ExecRequest(code=code))
    elapsed = int((time.perf_counter() - t0) * 1000)
    if result.success:
        out = (result.stdout or "").strip()
        if out.startswith("OK:"):
            return True, out[3:], elapsed
        elif out.startswith("ERROR:"):
            return False, out[6:], elapsed
        return True, out or "完成", elapsed
    return False, result.stderr or "执行失败", elapsed
