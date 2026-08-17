"""启动调试浏览器（独立实例，不干扰日常浏览器）

通用化版本：支持任何 Chromium 内核浏览器（Chrome / Edge / Brave 等）。
默认按优先级自动探测已安装的浏览器，也支持 --browser-path 显式指定。

用法:
  uv run python tools/browser/start_debug_browser.py                    # 默认非交互，不复制用户数据
  uv run python tools/browser/start_debug_browser.py --copy-user-data   # 从源浏览器复制登录态
  uv run python tools/browser/start_debug_browser.py --no-copy          # 显式不复制（同默认）
  uv run python tools/browser/start_debug_browser.py --browser-path "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"
  uv run python tools/browser/start_debug_browser.py --port 9223        # 自定义调试端口

设计原则：非交互（无 input()），适合 agent 通过 exec_python 调用。
首次使用若想保留登录态，显式传 --copy-user-data（建议先关闭源浏览器）。

配置优先级（与 server/config.py 的 [browser] 段一致）：
  1. 命令行 --browser-path / --port / --user-data-dir
  2. config.toml [browser] 段
  3. config.toml [chrome] 段（向后兼容）
  4. 内置默认值（端口 9222，目录 <项目根>/chrome_debug/）
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


# 默认配置（与 config.example.toml 一致）
DEFAULT_PORT = 9222
DEFAULT_USER_DATA_DIR = Path(__file__).parent.parent.parent / "chrome_debug"

# Chromium 内核浏览器候选路径（按优先级排序）
# 第一个存在的路径将被使用；可用 --browser-path 覆盖
BROWSER_CANDIDATES = [
    # Google Chrome
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{user}\AppData\Local\Google\Chrome\Application\chrome.exe",
    # Microsoft Edge
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Users\{user}\AppData\Local\Microsoft\Edge\Application\msedge.exe",
    # Brave
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    # Vivaldi
    r"C:\Users\{user}\AppData\Local\Vivaldi\Application\vivaldi.exe",
]


def _expand_user(path: str) -> str:
    """展开路径中的 {user} 占位符为当前用户名"""
    return path.replace("{user}", os.environ.get("USERNAME", ""))


def detect_browser_path() -> str | None:
    """按优先级探测已安装的 Chromium 内核浏览器

    返回第一个找到的可执行文件路径，找不到返回 None。
    """
    for candidate in BROWSER_CANDIDATES:
        expanded = _expand_user(candidate)
        if Path(expanded).exists():
            return expanded
    return None


def load_config_from_toml() -> dict:
    """从 config.toml 读取 [browser] / [chrome] 段配置（向后兼容）"""
    config_path = Path(__file__).parent.parent.parent / "config.toml"
    if not config_path.exists():
        return {}
    try:
        import toml
        config = toml.load(str(config_path))
        # 优先 [browser]，回退 [chrome]（向后兼容）
        section = config.get("browser", {})
        if not section and "chrome" in config:
            section = config.get("chrome", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def is_port_listening(port: int) -> bool:
    """检查端口是否在监听（必须同一行同时含 127.0.0.1:port 和 LISTENING）"""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        )
        needle = f"127.0.0.1:{port}"
        return any(needle in line and "LISTENING" in line for line in result.stdout.splitlines())
    except Exception:
        return False


def detect_source_user_data(browser_path: str) -> Path | None:
    """根据浏览器路径推断源用户数据目录（用于 --copy-user-data）

    返回源用户数据目录路径，找不到返回 None。
    """
    name = browser_path.lower()
    user = os.environ.get("USERNAME", "")
    if "chrome" in name:
        return Path(rf"C:\Users\{user}\AppData\Local\Google\Chrome\User Data")
    if "edge" in name or "msedge" in name:
        return Path(rf"C:\Users\{user}\AppData\Local\Microsoft\Edge\User Data")
    if "brave" in name:
        return Path(rf"C:\Users\{user}\AppData\Local\BraveSoftware\Brave-Browser\User Data")
    if "vivaldi" in name:
        return Path(rf"C:\Users\{user}\AppData\Local\Vivaldi\User Data")
    return None


def copy_user_data(src: Path, dst: Path) -> bool:
    """复制用户数据以保留登录态和扩展"""
    if not src.exists():
        print(f"[!] 源浏览器用户数据不存在: {src}")
        return False

    print(f"\n[*] 从 {src} 复制用户数据（保留登录态和扩展）...")
    print("  这可能需要1-2分钟，请耐心等待")

    exclude_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
    exclude_dirs = ["Service Worker", "ShaderCache", "GPUCache"]

    cmd = [
        "robocopy", str(src), str(dst), "/E",
        "/XF", *exclude_files,
        "/XD", *exclude_dirs,
        "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS",
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=300)
        print("[OK] 复制完成")
        return True
    except Exception as e:
        print(f"[!] 复制失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="启动调试浏览器（CDP 协议，独立实例，支持 Chromium 内核浏览器）"
    )
    parser.add_argument(
        "--copy-user-data", action="store_true",
        help="从源浏览器复制用户数据（登录态/扩展）。建议先关闭源浏览器",
    )
    parser.add_argument(
        "--no-copy", action="store_true",
        help="不复制用户数据，使用空白配置（默认行为）",
    )
    parser.add_argument(
        "--browser-path", type=str, default=None,
        help="浏览器可执行文件路径（不指定则自动探测 Chrome/Edge/Brave 等）",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help=f"调试端口（默认 {DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--user-data-dir", type=str, default=None,
        help="用户数据目录（默认 <项目根>/chrome_debug）",
    )
    args = parser.parse_args()

    # 互斥参数校验
    if args.copy_user_data and args.no_copy:
        print("[!] --copy-user-data 和 --no-copy 互斥")
        sys.exit(1)

    # 合并配置：命令行 > config.toml > 默认值
    toml_cfg = load_config_from_toml()
    browser_path = args.browser_path or toml_cfg.get("browser_path", "") or None
    port = args.port or toml_cfg.get("debug_port", DEFAULT_PORT)
    user_data_dir_str = args.user_data_dir or toml_cfg.get("user_data_dir", "") or str(DEFAULT_USER_DATA_DIR)
    user_data_dir = Path(user_data_dir_str)

    print("=" * 50)
    print("  启动调试浏览器（Chromium 内核，CDP 协议）")
    print("=" * 50)

    # 检查端口是否已在监听
    if is_port_listening(port):
        print(f"\n[OK] 调试浏览器已在运行（端口 {port}）")
        print("  日常浏览器可同时使用，互不干扰")
        return

    # 探测浏览器路径
    if not browser_path:
        browser_path = detect_browser_path()
    if not browser_path or not Path(browser_path).exists():
        print("[!] 未找到 Chromium 内核浏览器（Chrome / Edge / Brave / Vivaldi）")
        print("  请用 --browser-path 显式指定可执行文件路径")
        sys.exit(1)

    print(f"\n[*] 使用浏览器: {browser_path}")

    # 处理用户数据目录
    if not user_data_dir.exists():
        if args.copy_user_data:
            src = detect_source_user_data(browser_path)
            if src:
                if not copy_user_data(src, user_data_dir):
                    print("[!] 复制失败，改用空白配置")
                    user_data_dir.mkdir(parents=True, exist_ok=True)
            else:
                print(f"[!] 无法识别源浏览器用户数据目录，使用空白配置")
                user_data_dir.mkdir(parents=True, exist_ok=True)
        else:
            # 默认非交互：直接用空白配置
            print(f"\n[*] 首次使用，使用空白配置（如需保留登录态请加 --copy-user-data）")
            user_data_dir.mkdir(parents=True, exist_ok=True)
    else:
        if args.copy_user_data:
            src = detect_source_user_data(browser_path)
            if src:
                print(f"\n[*] {user_data_dir.name}/ 已存在，--copy-user-data 将覆盖更新")
                if not copy_user_data(src, user_data_dir):
                    print("[!] 复制失败，继续使用现有配置")

    # 启动浏览器
    print(f"\n[*] 启动调试浏览器（端口 {port}）...")

    cmd = [
        browser_path,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
    ]

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print(f"[!] 浏览器不存在: {browser_path}")
        sys.exit(1)

    # 等待启动
    print("[*] 等待浏览器启动...")
    for i in range(10):
        time.sleep(1)
        if is_port_listening(port):
            print(f"\n[OK] 调试浏览器已就绪（端口 {port}）")
            print("  日常浏览器可同时使用，互不干扰")
            return

    print(f"[!] 调试端口未就绪，请稍后重试")
    sys.exit(1)


if __name__ == "__main__":
    main()
