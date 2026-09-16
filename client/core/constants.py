"""客户端全局常量"""

import sys
from pathlib import Path

# 密钥文件路径从 lib/secret 获取（单一真源，禁止硬编码 "data/llm/keys.json"）
from lib.config_reader import load_config
from lib.secret import get_config_path, get_llm_keys_path

# 项目根目录：client/core/constants.py → parents[2] = localAgent/
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _derive_server_url() -> str:
    """从 config.toml [server] port 派生后端 URL（8-9：端口收敛单一真源）。

    用户改 config.toml 端口后主客户端与审批面板不再静默失联；
    读取失败/缺省回退 8766。
    """
    try:
        port = int(load_config().get("server", {}).get("port", 8766))
    except Exception:
        port = 8766
    return f"http://127.0.0.1:{port}"


# 服务端 URL
SERVER_URL = _derive_server_url()
SERVER_HEALTH_PATH = "/health"

# Fake LLM Proxy URL（可选进程）
FAKE_PROXY_URL = "http://127.0.0.1:9999"
FAKE_PROXY_STATS_PATH = "/api/stats"
FAKE_PROXY_SHUTDOWN_PATH = "/shutdown"

# 路径（密钥文件路径走 lib/secret 单一真源）
KEYS_FILE_PATH = get_llm_keys_path()
TOOLS_MANIFEST_PATH = PROJECT_ROOT / "tools_manifest.json"
CONFIG_PATH = get_config_path()
RESOURCES_PATH = PROJECT_ROOT / "client" / "resources"
START_BAT_PATH = PROJECT_ROOT / "start.bat"
FAKE_PROXY_SCRIPT_PATH = PROJECT_ROOT / "tools" / "fake_llm_proxy.py"
FAKE_PROXY_LOG_PATH = PROJECT_ROOT / "temp" / "fake_llm.log"

# 拉起子进程（fake proxy 等）用的解释器，与 tools_manifest.json 的 command 保持一致
VENV_PYTHON_PATH = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def resolve_launch_python() -> str:
    """子进程启动解释器：优先 .venv，缺失时回退当前解释器。

    不能直接用 sys.executable —— 项目工具依赖（fastapi/uvicorn 等）只装在 .venv 里，
    若 client 由系统 Python 启动，子进程会 ModuleNotFoundError 秒退且不留痕迹。
    """
    if VENV_PYTHON_PATH.exists():
        return str(VENV_PYTHON_PATH)
    return sys.executable

# 轮询周期
BACKEND_POLL_INTERVAL_MS = 3000      # /health 3s（缩短以更快恢复）
FAKE_PROXY_POLL_INTERVAL_MS = 10000  # fake proxy 10s
TODOS_POLL_INTERVAL_MS = 60000       # /todos/due 60s

# HTTP 超时
# 通用面板请求超时。3s 过短：后端部分写操作（如 /apikey/keys 添加前会同步跑一次
# 连通性测试，最长 ~15s）会在客户端超时→面板误报"后端返回错误"，但服务端随后仍完成写入。
# 提到 8s 覆盖绝大多数常规请求；确知更慢的增/测类调用在面板侧单独传更大 timeout。
HTTP_TIMEOUT_S = 8.0

# 后端状态检测防抖动参数
BACKEND_HEALTH_TIMEOUT_S = 5.0       # /health 专用超时（比通用 8s 短，快速探测后端掉线）
BACKEND_RETRY_DELAY_MS = 800         # 单次失败后快速重试间隔
BACKEND_FAIL_THRESHOLD = 2           # 连续失败多少次才切 offline（含重试）

# 后端启动等待
BACKEND_STARTUP_DELAY_S = 2.5
BACKEND_RESTART_WAIT_S = 5.0
