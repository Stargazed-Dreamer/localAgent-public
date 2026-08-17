"""后端代理调用客户端（供外部脚本通过 HTTP 调用后端 LLM 池）

脚本通过 HTTP 调用后端的 /llm/pool/call 端点，由后端统一管理并发池。
这样多个脚本共享同一个池，实现"拉链排队"：
  - 脚本A 的请求和脚本B 的请求都进入后端的同一个 pool
  - pool 的 round-robin + per-key 并发限制自然交错调度所有来源的请求
  - 脚本A 完成后，其请求释放 key 槽位，其他脚本的待处理请求自动获得更多并发

用法（脚本端）:
    from server.llm_pool import call_via_backend, check_backend_pool
    check_backend_pool()  # 启动时检查后端池就绪
    result = call_via_backend("分析代码", project="MuseArc")
"""


import httpx

from lib.async_http import get_sync_client

_DEFAULT_BACKEND_URL = "http://127.0.0.1:8766"


def backend_url() -> str:
    """后端根 URL（外部脚本环境可能无 server.config，使用默认值）"""
    try:
        from server.config import get_server_config
        cfg = get_server_config()
        return f"http://{cfg['host']}:{cfg['port']}"
    except Exception:
        return _DEFAULT_BACKEND_URL


def check_backend_pool(timeout: int = 15) -> bool:
    """检查后端是否运行且 LLM 池已初始化

    如果池未初始化，会触发后端自动初始化。
    返回 True 表示后端池可用。
    """
    try:
        # 先检查后端是否存活（/health 可能较慢，因为检查多个模块）
        # T07：同步 httpx.Client（连接池复用）
        r = get_sync_client().get(f"{backend_url()}/health", timeout=timeout)
        if r.status_code != 200:
            print(f"[backend] 后端健康检查失败: HTTP {r.status_code}")
            return False
        health = r.json()
        pool_info = health.get("llm_pool", {})
        if not pool_info.get("initialized"):
            # 触发自动初始化
            print("[backend] LLM 池未初始化，触发 /llm/pool/init ...")
            r2 = get_sync_client().post(f"{backend_url()}/llm/pool/init", timeout=30)
            if r2.status_code != 200:
                print(f"[backend] 池初始化失败: HTTP {r2.status_code}")
                return False
            data = r2.json()
            if data.get("status") != "ok":
                print(f"[backend] 池初始化失败: {data.get('message', '?')}")
                return False
            print(f"[backend] 池已初始化，加载 {data.get('keys_loaded', 0)} 个 key")
        else:
            print(f"[backend] LLM 池就绪: "
                  f"{pool_info.get('active_keys', 0)}/"
                  f"{pool_info.get('total_keys', 0)} keys, "
                  f"并发 {pool_info.get('current_active', 0)}/"
                  f"{pool_info.get('total_max_concurrency', 0)}")
        return True
    except httpx.ConnectError:
        print(f"[backend] 无法连接后端 {backend_url()}，请先启动: start.bat 或 "
              f".venv\\Scripts\\python.exe -m server.main")
        return False
    except Exception as e:
        print(f"[backend] 检查失败: {type(e).__name__}: {e}")
        return False


def call_via_backend_full(messages: list[dict], temperature: float = 0.3,
                          max_tokens: int = 4096, timeout: int = 120,
                          retries: int = 4, project: str = "default",
                          model_tier: str | None = None,
                          http_timeout: int = 300) -> dict:
    """通过后端代理调用 LLM（完整版，返回 usage 等元信息）

    Args:
        messages: OpenAI 消息格式
        project: 项目标签，用于 per-project token 统计
        model_tier: 1-5 数字（v8 tier）或兼容的 "cheap"/"default"/"powerful" — 按
            keys.json 中 model.tier 硬匹配 key。None 时按 use_case 的 default_tier 选。
        http_timeout: HTTP 请求超时（含后端等待 key 的时间，默认 300s）

    Returns:
        {"ok": True, "content": str, "usage": dict, "model": str, "key_name": str}
        {"ok": False, "error": str}
    """
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "retries": retries,
        "project": project,
    }
    if model_tier:
        payload["model_tier"] = model_tier
    try:
        # T07：同步 httpx.Client（连接池复用）
        r = get_sync_client().post(f"{backend_url()}/llm/pool/call", json=payload,
                                   timeout=http_timeout)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return r.json()
    except httpx.ConnectError:
        return {"ok": False, "error": f"后端不可达: {backend_url()}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def call_via_backend(prompt: str, system_prompt: str | None = None,
                     temperature: float = 0.3, max_tokens: int = 4096,
                     timeout: int = 120, retries: int = 4,
                     project: str = "default",
                     model_tier: str | None = None,
                     http_timeout: int = 300) -> str | None:
    """通过后端代理调用 LLM（简化版，返回文本或 None）

    Args:
        prompt: 用户提示文本
        project: 项目标签，用于 per-project token 统计
        model_tier: 1-5 数字（v8 tier）或兼容的 "cheap"/"default"/"powerful" — 按
            keys.json 中 model.tier 硬匹配 key。None 时按 use_case 的 default_tier 选。
        http_timeout: HTTP 请求超时（含后端等待 key 的时间，默认 300s）

    Returns:
        生成的文本，失败返回 None
    """
    payload = {
        "prompt": prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "retries": retries,
        "project": project,
    }
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if model_tier:
        payload["model_tier"] = model_tier
    try:
        # T07：同步 httpx.Client（连接池复用）
        r = get_sync_client().post(f"{backend_url()}/llm/pool/call-simple", json=payload,
                                   timeout=http_timeout)
        if r.status_code != 200:
            print(f"  [backend] 调用失败: HTTP {r.status_code}: {r.text[:150]}")
            return None
        data = r.json()
        if data.get("ok"):
            return data.get("content", "")
        print(f"  [backend] 调用失败: {data.get('error', '?')}")
        return None
    except httpx.ConnectError:
        print(f"  [backend] 后端不可达: {backend_url()}")
        return None
    except Exception as e:
        print(f"  [backend] 调用异常: {type(e).__name__}: {str(e)[:150]}")
        return None


def get_backend_pool_status(timeout: int = 10) -> dict:
    """从后端获取 LLM 池状态（供脚本打印状态用）"""
    try:
        # T07：同步 httpx.Client（连接池复用）
        r = get_sync_client().get(f"{backend_url()}/llm/pool/status", timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return {"initialized": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"initialized": False, "error": str(e)[:150]}


def print_backend_pool_status():
    """打印后端池状态（终端友好）"""
    s = get_backend_pool_status()
    if not s.get("initialized"):
        print(f"[backend] 池未初始化: {s.get('error', s.get('message', '?'))}")
        return
    print(f"\n{'='*60}")
    print(f"[Backend Pool] {s.get('active_keys', 0)}/{s.get('total_keys', 0)} keys, "
          f"并发 {s.get('current_active', 0)}/{s.get('total_max_concurrency', 0)}, "
          f"tokens={s.get('total_tokens_consumed', 0)}")
    for k in s.get("keys", []):
        status = "EXPIRED" if k.get("expired") else (
            f"CD {k.get('cooldown_remaining', 0)}s" if k.get("cooldown_remaining", 0) > 0
            else f"{k.get('active_count', 0)}/{k.get('max_concurrency', 0)}"
        )
        st = k.get("stats", {})
        print(f"  {k.get('name',''):15s} {k.get('model',''):16s} {status:12s} "
              f"ok={st.get('ok',0):4d} fail={st.get('fail',0):3d} "
              f"429={st.get('rate_limited',0):3d} tok={st.get('total_tokens',0)}")
        if k.get('privacy_warning'):
            print(f"    [!] 隐私警告: {k['privacy_warning']}")
    print(f"{'='*60}\n")
