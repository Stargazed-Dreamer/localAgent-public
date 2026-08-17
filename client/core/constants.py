"""客户端全局常量"""

from pathlib import Path

# 密钥文件路径从 lib/secret 获取（单一真源，禁止硬编码 "data/llm/keys.json"）
from lib.secret import get_config_path, get_llm_keys_path

# 项目根目录：client/core/constants.py → parents[2] = localAgent/
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 服务端 URL
SERVER_URL = "http://127.0.0.1:8766"
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

# 轮询周期
BACKEND_POLL_INTERVAL_MS = 3000      # /health 3s（缩短以更快恢复）
FAKE_PROXY_POLL_INTERVAL_MS = 10000  # fake proxy 10s
TODOS_POLL_INTERVAL_MS = 60000       # /todos/due 60s

# HTTP 超时
HTTP_TIMEOUT_S = 3.0

# 后端状态检测防抖动参数
BACKEND_HEALTH_TIMEOUT_S = 5.0       # /health 专用超时（比通用 3s 长，容忍偶发慢请求）
BACKEND_RETRY_DELAY_MS = 800         # 单次失败后快速重试间隔
BACKEND_FAIL_THRESHOLD = 2           # 连续失败多少次才切 offline（含重试）

# 后端启动等待
BACKEND_STARTUP_DELAY_S = 2.5
BACKEND_RESTART_WAIT_S = 5.0
