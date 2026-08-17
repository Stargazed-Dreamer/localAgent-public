"""后端诊断脚本（部署后诊断）。

调 http://127.0.0.1:8766/health 拉取后端状态，逐项解读并给出修复建议。
与 preflight_check.py（部署前检查）互补：本脚本在后端启动后运行，
用于快速定位"为什么 LLM 不可用""哪个模块异常"等问题。

用法:
    python tools/deploy/diagnose.py
    python tools/deploy/diagnose.py --url http://127.0.0.1:8766
    python tools/deploy/diagnose.py --anonymize        # 脱敏输出，便于分享给朋友

诊断项:
    1. 后端可达性     - 连不上 /health 则提示启动命令
    2. LLM Pool      - 未初始化则解析 keys.json 定位具体问题
    3. Memory        - 嵌入模型未就绪则提示配置 hf_mirror
    4. Vision        - 远程 VL 状态解读
    5. Screen        - 非管理员则提示用 start.bat 启动
    6. Loops         - 启用/跳过数量，跳过原因
    7. MCP           - 直连 + 网关工具数量

退出码:
    0 - 全部正常或仅 ⚠️ 警告
    1 - 有 ❌ 严重异常
    2 - 后端不可达

约束:
    - 仅用 Python 标准库（urllib.request / json / argparse / re / pathlib）
    - 后端未启动时也能优雅运行（不崩溃，给出启动提示）
    - Windows PowerShell 下可直接运行
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


# 项目根（tools/deploy/diagnose.py → 3 级 parent 到项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 路径常量从 lib/secret 获取（单一真源，禁止硬编码 "data/llm/keys.json"）
from lib.secret import get_llm_keys_path, get_config_path
KEYS_FILE = get_llm_keys_path()
CONFIG_FILE = get_config_path()

# 状态图标
OK = "✅"
WARN = "⚠️"
ERR = "❌"

# 默认后端地址
DEFAULT_URL = "http://127.0.0.1:8766"

# 全局脱敏开关（由 main() 设置）
_ANONYMIZE = False


# ========== 脱敏 ==========

def anonymize_text(text: str) -> str:
    """对输出文本进行脱敏：用户名、API key 前缀、个人路径。"""
    if not isinstance(text, str):
        text = str(text)
    # C:\Users\xxx → C:\Users\<user>（Windows 路径，大小写兼容）
    text = re.sub(r'([Cc]:\\Users\\)[^\\"]+', r'\1<user>', text)
    # /home/xxx → /home/<user>（Linux 路径，防御性）
    text = re.sub(r'(/home/)[^/"]+', r'\1<user>', text)
    # sk-xxxxxx... → sk-***（API key 前缀，OpenAI 风格）
    text = re.sub(r'(sk-)[A-Za-z0-9_\-]{4,}', r'\1***', text)
    # Bearer xxxxxx → Bearer ***（Authorization 头）
    text = re.sub(r'(Bearer\s+)[A-Za-z0-9_\-\.]{4,}', r'\1***', text)
    return text


def out(*args, **kwargs):
    """print 包装：--anonymize 时对每行输出脱敏。"""
    if _ANONYMIZE:
        args = tuple(anonymize_text(a) if isinstance(a, str) else a for a in args)
    print(*args, **kwargs)


# ========== HTTP ==========

def fetch_health(base_url: str, timeout: float = 5.0):
    """拉取 /health 端点。成功返回 dict，失败返回 None。"""
    health_url = base_url.rstrip("/") + "/health"
    try:
        req = urllib.request.Request(
            health_url,
            headers={"Accept": "application/json", "User-Agent": "diagnose.py/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"_fetch_error": f"连接失败: {e.reason}"}
    except Exception as e:
        return {"_fetch_error": f"请求异常: {type(e).__name__}: {e}"}


# ========== keys.json 解析 ==========

def load_keys_json():
    """加载 keys.json。返回 (data_dict_or_None, error_msg_or_None)。

    keys.json 标准结构: {"keys": [{"key": "sk-xxx", "base_url": "...", "models": [...]}]}
    """
    if not KEYS_FILE.exists():
        return None, f"文件不存在: {KEYS_FILE}"
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败 (line {e.lineno} col {e.colno}): {e.msg}"
    except Exception as e:
        return None, f"读取失败: {type(e).__name__}: {e}"


def diagnose_keys_json():
    """诊断 keys.json 配置。返回 (status, lines) - lines 是详细输出行列表。"""
    data, err = load_keys_json()
    if err:
        return ERR, [f"keys.json 读取失败: {err}",
                     "修复: 请按 DEPLOYMENT.md 步骤 7 创建 data/llm/keys.json 并配置 API key"]

    if not isinstance(data, dict) or "keys" not in data:
        return ERR, [f"keys.json 结构异常: 缺少 'keys' 顶层字段（实际类型: {type(data).__name__}）",
                     "修复: 请按 DEPLOYMENT.md 步骤 7 模板重写 keys.json"]

    keys = data.get("keys")
    if not isinstance(keys, list):
        return ERR, [f"'keys' 字段类型异常: 期望 list，实际 {type(keys).__name__}",
                     "修复: 请按 DEPLOYMENT.md 步骤 7 模板重写 keys.json"]

    if len(keys) == 0:
        return ERR, ["keys 数组为空，请按 DEPLOYMENT.md 步骤 7 配置至少一个 API key",
                     "模板: {\"keys\": [{\"key\": \"sk-xxx\", \"base_url\": \"https://api.deepseek.com\", \"models\": [{\"name\": \"deepseek-chat\", \"tier\": 3}]}]}"]

    # 逐个检查
    problems = []
    for i, k in enumerate(keys):
        if not isinstance(k, dict):
            problems.append(f"key #{i + 1}: 类型异常（期望 object，实际 {type(k).__name__}）")
            continue
        missing = []
        if "key" not in k or not k.get("key"):
            missing.append("key")
        if "base_url" not in k or not k.get("base_url"):
            missing.append("base_url")
        if "models" not in k or not k.get("models"):
            missing.append("models")
        if missing:
            problems.append(f"key #{i + 1}: 缺少字段 {', '.join(missing)}")
        # 检查 key 前缀格式（仅提示，不报错）
        key_val = k.get("key", "")
        if key_val and not key_val.startswith(("sk-", "Bearer ", "Bearer-")):
            # 部分提供商（如智谱、Anthropic）key 不以 sk- 开头，仅当 key 长度异常短时提示
            if len(key_val) < 10:
                problems.append(f"key #{i + 1}: key 字段长度异常 ({len(key_val)} 字符)，请确认是否完整")

    if problems:
        return ERR, problems
    # keys.json 配置完整但池未初始化 - 可能后端启动时序问题
    return WARN, [f"keys.json 配置完整（{len(keys)} 个 key），但 LLM Pool 未初始化",
                  "可能原因: 后端启动时序问题 / keys.json 字段类型不匹配 / 查看后端日志"]


# ========== 各模块诊断 ==========

def diag_backend(health, base_url):
    """1. 后端可达性"""
    out()
    out("=" * 60)
    out("  1. 后端可达性")
    out("=" * 60)

    if "_fetch_error" in health:
        out(f"  {ERR} 后端不可达: {health['_fetch_error']}")
        out(f"     修复: 启动后端 — .venv\\Scripts\\python.exe -m server.main")
        out(f"           或运行 start.bat（自动 UAC 提权）")
        out(f"     检查: 端口 8766 是否被占用 — netstat -ano | findstr :8766")
        return ERR

    version = health.get("version", "?")
    uptime = health.get("uptime_seconds", 0)
    status = health.get("status", "?")
    out(f"  {OK} 后端可达 ({base_url})")
    out(f"     版本: {version} | 状态: {status} | 运行时长: {uptime:.1f}s")
    return OK


def diag_llm_pool(health):
    """2. LLM Pool"""
    out()
    out("=" * 60)
    out("  2. LLM Pool")
    out("=" * 60)

    pool = health.get("llm_pool") or {}
    if pool.get("initialized"):
        total = pool.get("total_keys", "?")
        active = pool.get("active_keys", "?")
        total_models = pool.get("total_models", "?")
        out(f"  {OK} LLM Pool 已初始化")
        out(f"     keys: {active}/{total} 活跃 | 模型总数: {total_models}")
        # 附加状态：熔断器、压缩、模型健康度
        if pool.get("circuit_breaker_open_count"):
            out(f"     ⚠️ {pool['circuit_breaker_open_count']} 个 Provider 熔断器 OPEN（临时停止调用）")
        if pool.get("model_health_disabled_count"):
            out(f"     ⚠️ {pool['model_health_disabled_count']} 个模型被健康度机制禁用")
        return OK

    # 未初始化 - 深入诊断
    out(f"  {ERR} LLM Pool 未初始化 (initialized=false)")
    out(f"     正在解析 keys.json 定位具体原因...")
    out()
    status, lines = diagnose_keys_json()
    for line in lines:
        out(f"     - {line}")
    return status


def diag_memory(health):
    """3. Memory"""
    out()
    out("=" * 60)
    out("  3. Memory（三层记忆系统）")
    out("=" * 60)

    mem = health.get("memory") or {}
    if not mem.get("available", False):
        out(f"  {ERR} 记忆系统不可用")
        out(f"     修复: 检查后端日志，可能是数据库迁移失败或权限问题")
        return ERR

    messages = mem.get("messages", 0)
    facts = mem.get("facts", 0)
    summaries = mem.get("summaries", 0)
    db_size = mem.get("db_size_mb", 0)
    embedding_ready = mem.get("embedding_ready", False)

    out(f"  记忆数据库: {messages} 条消息 / {facts} 条事实 / {summaries} 条摘要 / {db_size:.1f} MB")

    if embedding_ready:
        out(f"  {OK} 嵌入模型已就绪，语义检索可用")
        return OK

    out(f"  {WARN} 嵌入模型未就绪，语义检索不可用，BM25 兜底可用")
    out(f"     修复: 检查网络连通性；或在 config.toml [memory] 段配置 hf_mirror = \"https://hf-mirror.com\"")
    out(f"           (国内用户推荐 hf-mirror.com；海外用户可改回 https://huggingface.co 或留空)")
    out(f"           模型: BAAI/bge-small-zh-v1.5 (~100MB)")
    return WARN


def diag_vision(health):
    """4. Vision"""
    out()
    out("=" * 60)
    out("  4. Vision（视觉模块）")
    out("=" * 60)

    vision = health.get("vision") or {}
    vl_available = vision.get("vl_available", False)
    vl_provider = vision.get("vl_provider", "")
    vl_model = vision.get("vl_model", "")

    has_issue = False

    # 远程 VL
    if vl_available:
        out(f"  {OK} 远程 VL 可用 (provider={vl_provider}, model={vl_model})")
    else:
        out(f"  {WARN} 远程 VL 不可用 (vl_available=false)")
        out(f"     修复: 检查 config.toml [vision] vl_enabled 配置；确认网络可访问 ModelScope")
        out(f"           或 API key 是否配置正确（见 data/llm/keys.json）")
        has_issue = True

    if not has_issue:
        return OK
    # 全部不可用算 ❌，部分不可用算 ⚠️
    if not omni_enabled and not vl_available:
        out(f"  {ERR} Vision 模块全部不可用 — UI 元素解析能力完全丧失")
        return ERR
    return WARN


def diag_screen(health):
    """5. Screen"""
    out()
    out("=" * 60)
    out("  5. Screen（屏幕操控）")
    out("=" * 60)

    screen = health.get("screen") or {}
    # 兼容字段名: spec 写 screen.admin，实际 health 返回 screen.admin_privileges
    admin = screen.get("admin_privileges", screen.get("admin", False))
    emergency = screen.get("emergency_stopped", False)

    if emergency:
        out(f"  {ERR} 紧急停止已触发 (emergency_stopped=true)")
        out(f"     修复: 调用 /screen/emergency-release 或重启后端")
        return ERR

    if admin:
        uia = (screen.get("uia") or {}).get("available", False)
        out(f"  {OK} 管理员权限可用，键鼠操控可用 (UIA 语义层: {'on' if uia else 'off'})")
        return OK

    out(f"  {WARN} 非管理员权限 (admin_privileges=false)")
    out(f"     影响: 键鼠操控会被 Windows UIPI 静默阻止 (SetCursorPos/SetForegroundWindow 失败)")
    out(f"     修复: 用 start.bat 启动（自动 UAC 提权），或右键以管理员身份运行 PowerShell")
    return WARN


def diag_loops(health):
    """6. Loops"""
    out()
    out("=" * 60)
    out("  6. Loops（自动触发系统）")
    out("=" * 60)

    loops = health.get("loops") or {}
    if not loops.get("enabled", True):
        out(f"  {WARN} Loop 系统已禁用 (config.toml [activity_tracker] enabled=false)")
        return WARN

    if not loops.get("running", False):
        out(f"  {WARN} Loop 系统未运行")
        return WARN

    tasks = loops.get("tasks", []) or []
    total = len(tasks)
    enabled = [t for t in tasks if t.get("enabled") and not t.get("paused")]
    paused = [t for t in tasks if t.get("paused")]
    disabled = [t for t in tasks if not t.get("enabled")]

    out(f"  Loop 任务: 共 {total} 个 | 启用 {len(enabled)} | 暂停 {len(paused)} | 禁用 {len(disabled)}")

    if not tasks:
        out(f"  {WARN} 无 Loop 任务注册（可能 config.toml 未配置 [loops.*] 段）")
        return WARN

    has_issue = False

    # 显示暂停的任务（含 watch_dir 不存在等）
    for t in paused:
        tid = t.get("task_id", "?")
        reason = t.get("paused_reason", "(无原因)")
        out(f"  {WARN} 暂停: {tid} — 原因: {reason}")
        if "watch_dir" in reason.lower() or "目录" in reason or "not exist" in reason.lower():
            out(f"        修复: config.toml 中该 loop 的 watch_dir 路径不存在，请改为有效路径或禁用该 loop")
        has_issue = True

    # 显示失败次数超阈值的任务
    for t in enabled:
        fail_count = t.get("fail_count", 0)
        threshold = t.get("fail_threshold", 5)
        if fail_count >= threshold:
            tid = t.get("task_id", "?")
            last_result = t.get("last_result", "?")
            out(f"  {ERR} 失败超阈值: {tid} (fail={fail_count}/{threshold}, last={last_result})")
            has_issue = True

    if not has_issue:
        out(f"  {OK} Loop 系统正常")
        return OK
    # 暂停任务多算 ⚠️，失败超阈值算 ❌
    has_err = any(
        (t.get("fail_count", 0) >= t.get("fail_threshold", 5))
        for t in enabled
    )
    return ERR if has_err else WARN


def diag_mcp(health):
    """7. MCP"""
    out()
    out("=" * 60)
    out("  7. MCP（Model Context Protocol）")
    out("=" * 60)

    mcp = health.get("mcp") or {}
    direct = mcp.get("direct_tools_count", 0)
    direct_limit = mcp.get("direct_tools_limit", 40)
    gateway = mcp.get("gateway_tools_count", 0)
    template_count = mcp.get("template_count", 0)

    out(f"  {OK} MCP 工具统计:")
    out(f"     直连工具: {direct}/{direct_limit} (白名单，免审批)")
    out(f"     网关工具: {gateway} (localagent_advanced_tool 网关，GET 类免审批)")
    out(f"     模板: {template_count} (localagent_template_tool 预定义工作流)")

    if direct >= direct_limit:
        out(f"  {WARN} 直连工具已达上限 ({direct}/{direct_limit})，新增工具需走网关")
        return WARN
    return OK


# ========== 主流程 ==========

def main():
    parser = argparse.ArgumentParser(
        description="后端诊断脚本 — 拉取 /health 状态并给出修复建议",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python tools/deploy/diagnose.py\n"
               "  python tools/deploy/diagnose.py --url http://127.0.0.1:8766\n"
               "  python tools/deploy/diagnose.py --anonymize  # 脱敏输出，便于分享",
    )
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"后端地址（默认 {DEFAULT_URL}）")
    parser.add_argument("--anonymize", action="store_true",
                        help="脱敏输出（隐藏用户名/路径/API key 前缀），便于分享给朋友")
    args = parser.parse_args()

    base_url = args.url

    # 标题
    out()
    out("#" * 60)
    out("#  LocalAgent 后端诊断报告")
    out(f"#  目标: {anonymize_text(base_url) if args.anonymize else base_url}")
    out(f"#  脱敏: {'是' if args.anonymize else '否'}")
    out("#" * 60)

    # 拉取 /health
    health = fetch_health(base_url)

    # 后端不可达 - 直接退出
    if "_fetch_error" in health:
        diag_backend(health, base_url)
        out()
        out("=" * 60)
        out(f"  汇总: 正常 0 项 | 异常 1 项（后端不可达）")
        out("=" * 60)
        out()
        out("  后端未启动或不可达，无法继续诊断。请先启动后端:")
        out("    .venv\\Scripts\\python.exe -m server.main")
        out("    或运行 start.bat")
        sys.exit(2)

    # 逐项诊断
    results = []
    results.append(diag_backend(health, base_url))
    results.append(diag_llm_pool(health))
    results.append(diag_memory(health))
    results.append(diag_vision(health))
    results.append(diag_screen(health))
    results.append(diag_loops(health))
    results.append(diag_mcp(health))

    # 汇总
    ok_count = results.count(OK)
    warn_count = results.count(WARN)
    err_count = results.count(ERR)

    out()
    out("=" * 60)
    out("  汇总")
    out("=" * 60)
    out(f"  正常: {ok_count} 项 | 警告: {warn_count} 项 | 严重: {err_count} 项")
    out()

    if err_count > 0:
        out(f"  {ERR} 存在严重异常，请按上述修复建议处理")
        sys.exit(1)
    elif warn_count > 0:
        out(f"  {WARN} 存在警告，部分功能可能受限（不影响核心运行）")
        sys.exit(0)
    else:
        out(f"  {OK} 全部正常")
        sys.exit(0)


if __name__ == "__main__":
    main()

