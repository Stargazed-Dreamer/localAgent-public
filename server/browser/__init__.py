"""browser 包：浏览器控制路由与端点（Ticket 07 完成）。

本包接管原 server/browser.py + browser_action_endpoints.py +
browser_wait_endpoints.py + browser_session.py + browser_stats.py 的全部职能。
原 5 个文件已删除，本包是唯一定义。

导入 ``server.browser`` 会触发：
1. ``routes.router`` 定义（APIRouter(prefix="/browser")）
2. routes.py 末尾 import 各端点模块 → @router.post/@router.get 注册端点
3. 本 __init__.py re-export 所有公开符号，供 ``from server.browser import X`` 使用

6 个 legacy 端点（browser_open/click_element/fill_input/wait_for_load/
extract_text/screenshot_element）已在 Ticket 07 删除（含模型、函数、测试）。
功能由新端点替代：browser_action / browser_navigate / browser_screenshot /
browser_evaluate / browser_wait_for / browser_snapshot / browser_console_logs。

循环导入安全性：
- routes.py 顶部定义 router，末尾 import 各端点模块（此时 router 已存在）
- 各端点模块 ``from .routes import router`` 拿到 router 引用
- 本 __init__.py 先 import routes（触发全部端点注册），再 import 其他模块
"""

# 1. error_model：错误码 + 错误响应模型 + 错误分类器
# 5. action_endpoints：browser_action 端点
from .action_endpoints import (
    BrowserActionRequest,
    BrowserActionResponse,
    BrowserActionTarget,
    browser_action,
)

# 13. dialog_endpoints：browser_handle_dialog 端点 + _check_dialog_block helper（2026-08-06 新增）
#     Ticket 05/06: set_http_credentials + grant_permissions 端点（同文件）
from .dialog_endpoints import (
    BrowserGrantPermissionsRequest,
    BrowserGrantPermissionsResponse,
    BrowserHandleDialogRequest,
    BrowserHandleDialogResponse,
    BrowserSetHttpCredentialsRequest,
    BrowserSetHttpCredentialsResponse,
    _check_dialog_block,
    browser_grant_permissions,
    browser_handle_dialog,
    browser_set_http_credentials,
)
from .error_model import (
    BROWSER_ERROR_CODES,
    BROWSER_PHASES,
    BrowserErrorResponse,
    _classify_error,
)

# 4. evaluate_endpoints：browser_evaluate 端点 + 写操作判定
from .evaluate_endpoints import (
    _READ_ALLOWLIST,
    _WRITE_PATTERNS_RE,
    BrowserEvaluateRequest,
    BrowserEvaluateResponse,
    _is_write_expression,
    _normalize_expression,
    browser_evaluate,
)

# 2. playwright_executor：CDP 端点 + Playwright 脚本构建/执行
from .playwright_executor import (
    _build_playwright_header,
    _build_playwright_script,
    _cdp_endpoint,
    _execute_playwright,
)

# 3. routes：router 定义 + 共享 helper + 基础端点
#    import routes 触发其末尾的端点模块 import（action/wait/evaluate/snapshot/
#    vl_feedback/site_lessons/session/stats），完成所有 @router 注册。
from .routes import (
    BrowserCloseRequest,
    BrowserConsoleLogsRequest,
    BrowserConsoleLogsResponse,
    BrowserListResponse,
    BrowserNavigateRequest,
    BrowserNavigateResponse,
    BrowserResponse,
    BrowserScreenshotRequest,
    BrowserScreenshotResponse,
    BrowserStatusResponse,
    _get_session_page,
    _new_shot_path,
    _resolve_tab_id_to_url,
    _shot_dir,
    browser_close,
    browser_console_logs,
    browser_list_tabs,
    browser_navigate,
    browser_screenshot,
    browser_status,
    router,
)

# 11. session 子包：TabSession 数据类 + SessionManager 单例
from .session.manager import (
    SessionManager,
    _manager,
    get_session_manager,
    reset_session_manager,
)
from .session.state import TabSession

# 9. session_endpoints：持久 session create/list/close 端点
from .session_endpoints import (
    BrowserSessionCloseRequest,
    BrowserSessionCloseResponse,
    BrowserSessionCreateRequest,
    BrowserSessionCreateResponse,
    BrowserSessionListResponse,
    browser_session_close,
    browser_session_create,
    browser_session_list,
)

# 8. site_lessons：站点经验匹配 + find_url 端点
from .site_lessons import (
    FindUrlRequest,
    FindUrlResponse,
    MatchSiteRequest,
    MatchSiteResponse,
    browser_find_url,
    browser_match_site,
    build_site_lessons_hint,
    match_site_for_domain,
    match_site_for_url,
)

# 7. snapshot_endpoints：browser_snapshot 端点 + ARIA/DOM hash 工具
from .snapshot_endpoints import (
    _ARIA_SNAPSHOT_JS,
    _DOM_HASH_JS,
    BrowserSnapshotRequest,
    BrowserSnapshotResponse,
    browser_snapshot,
    compute_aria_snapshot,
    compute_dom_hash,
)

# 10. stats：选择器统计 + browser_selector_stats 端点
from .stats import (
    BrowserSelectorStatsRequest,
    BrowserSelectorStatsResponse,
    BrowserStatsStore,
    browser_selector_stats,
    get_stats_store,
    record_selector_call,
)

# 12. vl_feedback：操作后 VL 反馈 helper（不注册端点）
from .vl_feedback import _maybe_vl_feedback

# 6. wait_endpoints：browser_wait_for + browser_wait_and_action 端点
from .wait_endpoints import (
    BrowserWaitAndActionRequest,
    BrowserWaitAndActionResponse,
    BrowserWaitRequest,
    BrowserWaitResponse,
    _build_trigger_locator,
    browser_wait_and_action,
    browser_wait_for,
)

__all__ = [
    # error_model
    "BROWSER_ERROR_CODES",
    "BROWSER_PHASES",
    "BrowserErrorResponse",
    "_classify_error",
    # playwright_executor
    "_build_playwright_header",
    "_build_playwright_script",
    "_cdp_endpoint",
    "_execute_playwright",
    # routes
    "router",
    "BrowserCloseRequest",
    "BrowserConsoleLogsRequest",
    "BrowserConsoleLogsResponse",
    "BrowserListResponse",
    "BrowserNavigateRequest",
    "BrowserNavigateResponse",
    "BrowserResponse",
    "BrowserScreenshotRequest",
    "BrowserScreenshotResponse",
    "BrowserStatusResponse",
    "_get_session_page",
    "_new_shot_path",
    "_resolve_tab_id_to_url",
    "_shot_dir",
    "browser_close",
    "browser_console_logs",
    "browser_list_tabs",
    "browser_navigate",
    "browser_screenshot",
    "browser_status",
    # evaluate_endpoints
    "BrowserEvaluateRequest",
    "BrowserEvaluateResponse",
    "_is_write_expression",
    "_normalize_expression",
    "_READ_ALLOWLIST",
    "_WRITE_PATTERNS_RE",
    "browser_evaluate",
    # action_endpoints
    "BrowserActionRequest",
    "BrowserActionResponse",
    "BrowserActionTarget",
    "browser_action",
    # wait_endpoints
    "BrowserWaitAndActionRequest",
    "BrowserWaitAndActionResponse",
    "BrowserWaitRequest",
    "BrowserWaitResponse",
    "_build_trigger_locator",
    "browser_wait_and_action",
    "browser_wait_for",
    # snapshot_endpoints
    "_ARIA_SNAPSHOT_JS",
    "_DOM_HASH_JS",
    "BrowserSnapshotRequest",
    "BrowserSnapshotResponse",
    "compute_aria_snapshot",
    "compute_dom_hash",
    "browser_snapshot",
    # site_lessons
    "FindUrlRequest",
    "FindUrlResponse",
    "MatchSiteRequest",
    "MatchSiteResponse",
    "build_site_lessons_hint",
    "browser_find_url",
    "browser_match_site",
    "match_site_for_domain",
    "match_site_for_url",
    # session_endpoints
    "BrowserSessionCloseRequest",
    "BrowserSessionCloseResponse",
    "BrowserSessionCreateRequest",
    "BrowserSessionCreateResponse",
    "BrowserSessionListResponse",
    "browser_session_close",
    "browser_session_create",
    "browser_session_list",
    # stats
    "BrowserSelectorStatsRequest",
    "BrowserSelectorStatsResponse",
    "BrowserStatsStore",
    "browser_selector_stats",
    "get_stats_store",
    "record_selector_call",
    # session 子包
    "SessionManager",
    "TabSession",
    "_manager",
    "get_session_manager",
    "reset_session_manager",
    # vl_feedback
    "_maybe_vl_feedback",
    # dialog_endpoints（2026-08-06 新增）
    "BrowserHandleDialogRequest",
    "BrowserHandleDialogResponse",
    "BrowserSetHttpCredentialsRequest",
    "BrowserSetHttpCredentialsResponse",
    "BrowserGrantPermissionsRequest",
    "BrowserGrantPermissionsResponse",
    "_check_dialog_block",
    "browser_handle_dialog",
    "browser_set_http_credentials",
    "browser_grant_permissions",
]
