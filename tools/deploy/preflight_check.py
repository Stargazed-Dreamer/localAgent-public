"""LocalAgent 部署前置自检脚本。

一键检查 LocalAgent 部署所需的前置条件，每项输出 ✅/⚠️/❌ 状态及具体修复命令。

用法：
    python tools/deploy/preflight_check.py

检查项：
    - Python 版本 ≥ 3.12（优先检测 .venv / uv，避免对用 uv 管理 Python 的用户误报）
    - uv 已安装
    - 磁盘剩余空间 ≥ 5 GB（项目所在盘）
    - config.toml 存在
    - data/llm/keys.json 存在且为有效 JSON
    - 占位符残留检测（<project_root> 等）
    - GPU 探测（nvidia-smi 可调用性）
    - 端口 8766 未被占用

退出码：
    全过或仅 ⚠️ 警告 → 0；存在 ❌ 失败 → 1

本脚本只读检查，不修改任何文件。仅使用 Python 标准库。
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# 检查项状态
PASS = "pass"
WARN = "warn"
FAIL = "fail"

ICONS = {PASS: "✅", WARN: "⚠️", FAIL: "❌"}

# 项目根目录：本脚本位于 tools/deploy/，向上两级即项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 占位符扫描：跳过的目录（匹配目录名，出现在任意层级均跳过）
SKIP_DIRS = {".venv", "weights", "data", "chrome_debug", ".git", "temp"}

# 占位符扫描：处理的文件扩展名
SCAN_EXTS = {".py", ".toml", ".bat", ".md", ".json"}

# 部署占位符清单（与 apply_placeholders.py 的 PLACEHOLDERS 同步）。
# 只检测这些占位符，避免误匹配 markdown 通用模板（<domain>/<slug>/<operation_id> 等）。
DEPLOY_PLACEHOLDERS = frozenset({
    "<username>", "<project_root>", "<data_drive>",
    "<project_root_parent>", "<external_project_root>",
    "<source_images_root>", "<classified_root>", "<image_organize_output>",
    "<bilibili_videos>", "<bilibili_videos_alt>", "<ipad_backup>",
    "<system_data_root>", "<backup_root>", "<miniconda_root>",
    "<academic_root>", "<articles_root>", "<working_root>",
    "<projects_root>", "<bilibili_organized_output>",
    "<game_install_root>", "<copyright_holder>",
})

# 占位符候选正则：先粗筛 <lowercase>，再用 DEPLOY_PLACEHOLDERS 精确过滤
PLACEHOLDER_RE = re.compile(r"<[a-z_]+>")

# 阈值常量
MIN_PYTHON = (3, 12)
MIN_DISK_GB = 5
MIN_DISK_BYTES = MIN_DISK_GB * 1024 * 1024 * 1024
BACKEND_PORT = 8766


def check_python_version():
    """检查 Python 版本 ≥ 3.12。

    检测优先级：
    1. 当前运行的就是 .venv 的 Python → 直接判断
    2. .venv 存在但当前不是用它跑的 → 检查 .venv 的 Python 版本
    3. uv 可用 → 检查 uv python list 是否有 3.12+（uv sync 会自动装）
    4. 回退到系统 Python（降级为 WARN，因 uv sync 可能装 3.12）

    避免对用 uv 管理 Python 的用户误报（系统 Python 3.10 但 uv 装了 3.12）。
    """
    v = sys.version_info
    min_str = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    venv_dir = PROJECT_ROOT / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"

    # 1. 当前运行的就是 .venv 的 Python
    running_in_venv = str(venv_dir) in sys.executable
    if running_in_venv:
        if (v.major, v.minor) >= MIN_PYTHON:
            return PASS, f"Python {v.major}.{v.minor}.{v.micro}（.venv）", ""
        return (
            FAIL,
            f".venv Python {v.major}.{v.minor}（需 ≥ {min_str}）",
            f"删除 .venv 后 `uv python install {min_str}` && `uv sync` 重建",
        )

    # 2. .venv 存在但当前不是用它跑的，检查 .venv 的 Python 版本
    if venv_python.exists():
        try:
            result = subprocess.run(
                [str(venv_python), "-c",
                 "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                ver_str = result.stdout.strip()
                parts = ver_str.split(".")
                if len(parts) >= 2:
                    major, minor = int(parts[0]), int(parts[1])
                    if (major, minor) >= MIN_PYTHON:
                        return PASS, f"Python {major}.{minor}（.venv）", ""
                    return (
                        FAIL,
                        f".venv Python {major}.{minor}（需 ≥ {min_str}）",
                        "删除 .venv 后 `uv sync` 重建",
                    )
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    # 3. .venv 不存在，检查 uv 是否能提供 3.12+
    uv_path = shutil.which("uv")
    if uv_path:
        try:
            result = subprocess.run(
                [uv_path, "python", "list"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    m = re.search(r"cpython-(\d+)\.(\d+)\.", line)
                    if m and (int(m.group(1)), int(m.group(2))) >= MIN_PYTHON:
                        return (
                            PASS,
                            f"uv 可提供 Python {m.group(1)}.{m.group(2)}"
                            f"（uv sync 自动安装到 .venv）",
                            "",
                        )
        except (subprocess.TimeoutExpired, OSError):
            pass

    # 4. 回退到系统 Python（降级为 WARN：uv sync 可能装 3.12）
    if (v.major, v.minor) >= MIN_PYTHON:
        return PASS, f"Python {v.major}.{v.minor}.{v.micro}", ""
    return (
        WARN,
        f"系统 Python {v.major}.{v.minor}.{v.micro}（需 ≥ {min_str}）",
        f"运行 `uv python install {min_str}` 后 `uv sync`（自动创建 .venv）",
    )


def check_uv_installed():
    """检查 uv 是否已安装。"""
    uv_path = shutil.which("uv")
    if uv_path:
        return PASS, f"uv 已安装（{uv_path}）", ""
    return (
        FAIL,
        "uv 未安装",
        'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"',
    )


def check_disk_space():
    """检查项目所在盘剩余空间 ≥ 5 GB。"""
    usage = shutil.disk_usage(PROJECT_ROOT)
    free_gb = usage.free / (1024 ** 3)
    if usage.free >= MIN_DISK_BYTES:
        return PASS, f"剩余 {free_gb:.1f} GB", ""
    return (
        FAIL,
        f"剩余 {free_gb:.1f} GB（需 ≥ {MIN_DISK_GB} GB）",
        f"请清理 {PROJECT_ROOT.anchor} 盘空间或更换安装目录",
    )


def check_config_toml():
    """检查 config.toml 是否存在。"""
    p = PROJECT_ROOT / "config.toml"
    if p.exists():
        return PASS, "config.toml 已存在", ""
    return (
        WARN,
        "config.toml 不存在",
        "Copy-Item config.example.toml config.toml",
    )


def check_keys_json():
    """检查 data/llm/keys.json 是否存在且为有效 JSON。"""
    # 路径常量从 lib/secret 获取（单一真源）
    from lib.secret import get_llm_keys_path
    p = get_llm_keys_path()
    if not p.exists():
        return (
            WARN,
            "data/llm/keys.json 不存在",
            "按 DEPLOYMENT.md 步骤 7 配置 LLM 密钥",
        )
    # 检查文件是否为 0 字节或空内容（偶发写入异常，如编辑器未刷新）
    file_size = p.stat().st_size
    if file_size == 0:
        return (
            WARN,
            "keys.json 为 0 字节（文件为空，可能写入异常）",
            '写入最小配置：Set-Content data/llm/keys.json \'{"keys": []}\'',
        )
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return (
            WARN,
            f"keys.json 解析失败：{e}",
            '重写为有效 JSON：Set-Content data/llm/keys.json \'{"keys": []}\'',
        )
    if isinstance(data, dict):
        keys = data.get("keys", [])
    elif isinstance(data, list):
        keys = data
    else:
        keys = []
    if keys:
        return PASS, f"keys.json 已配置（{len(keys)} 个密钥）", ""
    return (
        WARN,
        "keys.json 的 keys 数组为空",
        "按 DEPLOYMENT.md 步骤 7 配置 LLM 密钥",
    )


def check_placeholders():
    """扫描项目文本文件是否仍含必填占位符。"""
    residuals = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        # 原地剪枝：跳过指定目录（os.walk 标准用法）
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in SCAN_EXTS:
                continue
            fp = os.path.join(dirpath, name)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue
            matches = PLACEHOLDER_RE.findall(content)
            # 精确过滤：只保留部署占位符清单中的项，丢弃 <domain>/<slug> 等通用模板
            deploy_matches = [m for m in matches if m in DEPLOY_PLACEHOLDERS]
            if deploy_matches:
                rel = os.path.relpath(fp, PROJECT_ROOT)
                residuals.append((rel, sorted(set(deploy_matches))))
    if not residuals:
        return PASS, "未检测到占位符残留", ""
    preview = "; ".join(
        f"{rel}({','.join(m)})" for rel, m in residuals[:5]
    )
    suffix = f" 等 {len(residuals)} 个文件" if len(residuals) > 5 else ""
    return (
        WARN,
        f"检测到占位符残留：{preview}{suffix}",
        "运行 python tools/deploy/apply_placeholders.py 替换占位符",
    )


def check_gpu():
    """探测 GPU（nvidia-smi 可调用性）。"""
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            result = subprocess.run(
                [nvidia],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return PASS, "检测到 NVIDIA GPU，可走 CUDA 路径", ""
        except (subprocess.TimeoutExpired, OSError):
            pass
    return (
        WARN,
        "未检测到 NVIDIA GPU",
        "无 GPU 时走 CPU-only 路径：把 paddlepaddle-gpu 改为 paddlepaddle",
    )


def check_port():
    """检查端口 8766 是否被占用。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        result = sock.connect_ex(("127.0.0.1", BACKEND_PORT))
    finally:
        sock.close()
    if result != 0:
        return PASS, f"端口 {BACKEND_PORT} 未被占用", ""
    return (
        WARN,
        f"端口 {BACKEND_PORT} 已被占用",
        f"netstat -ano | findstr :{BACKEND_PORT} 然后 taskkill /PID <PID> /F",
    )


def run_all_checks():
    """运行所有检查项，返回 (名称, 状态, 详情, 修复命令) 列表。"""
    checks = [
        ("Python 版本", check_python_version),
        ("uv 安装", check_uv_installed),
        ("磁盘剩余空间", check_disk_space),
        ("config.toml", check_config_toml),
        ("keys.json", check_keys_json),
        ("占位符残留", check_placeholders),
        ("GPU 探测", check_gpu),
        ("端口 8766", check_port),
    ]
    results = []
    for name, fn in checks:
        try:
            status, state, fix = fn()
        except Exception as e:  # 防御性兜底，单点异常不影响其他检查
            status, state, fix = FAIL, f"检查异常：{e}", ""
        results.append((name, status, state, fix))
    return results


def main():
    # 确保 Windows 控制台能正确输出 emoji / 中文
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    print("=" * 60)
    print("LocalAgent 部署前置自检")
    print(f"项目根目录：{PROJECT_ROOT}")
    print("=" * 60)
    print()

    results = run_all_checks()
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for name, status, state, fix in results:
        counts[status] += 1
        icon = ICONS[status]
        line = f"{icon} {name}: {state}"
        if fix:
            line += f"  (修复: {fix})"
        print(line)

    print()
    print("-" * 60)
    print(
        f"汇总：通过 {counts[PASS]} 项，警告 {counts[WARN]} 项，失败 {counts[FAIL]} 项"
    )

    if counts[FAIL] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
