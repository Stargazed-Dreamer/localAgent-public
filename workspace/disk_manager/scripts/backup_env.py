"""系统环境备份工具

备份浏览器数据、游戏存档、桌面布局、系统设置、软件列表等。
生成一键恢复脚本和手动确认清单。

用法：
  uv run python workspace/disk_manager/scripts/backup_env.py              # 全量备份
  uv run python workspace/disk_manager/scripts/backup_env.py --browser     # 只备份浏览器
  uv run python workspace/disk_manager/scripts/backup_env.py --games       # 只备份游戏存档
  uv run python workspace/disk_manager/scripts/backup_env.py --system      # 只备份系统设置
  uv run python workspace/disk_manager/scripts/backup_env.py --software    # 只备份软件列表
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
from pathlib import Path
from datetime import datetime


BACKUP_BASE = Path(r"E:\<data_drive>:\<backup_root>\SystemBackup")
TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP_DIR = BACKUP_BASE / TIMESTAMP


def fmt_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def safe_copy(src: Path, dst: Path, label: str = ""):
    """安全复制文件/目录"""
    if not src.exists():
        print(f"  [跳过] {label or src.name} 不存在")
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file()) if dst.is_dir() else dst.stat().st_size
        print(f"  [已备份] {label or src.name} → {dst.relative_to(BACKUP_DIR)} ({fmt_size(size)})")
        return True
    except Exception as e:
        print(f"  [失败] {label or src.name}: {e}")
        return False


def reg_export(key: str, dst: Path, label: str = ""):
    """导出注册表项"""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["reg", "export", key, str(dst), "/y"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"  [已导出] {label or key}")
            return True
        else:
            print(f"  [失败] {label or key}: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  [失败] {label or key}: {e}")
        return False


def run_cmd(cmd: str, label: str = "") -> str:
    """运行命令并返回输出"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout.strip()
        if output:
            print(f"  [已获取] {label}")
        else:
            print(f"  [空] {label}")
        return output
    except Exception as e:
        print(f"  [失败] {label}: {e}")
        return ""


# ========== 浏览器备份 ==========

def backup_browser():
    """备份Chrome浏览器数据"""
    print("\n" + "="*50)
    print("备份浏览器数据")
    print("="*50)

    dst = BACKUP_DIR / "browser"
    chrome_base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "Default"

    if not chrome_base.exists():
        print("  Chrome默认配置目录不存在，跳过")
        return

    # 书签
    safe_copy(chrome_base / "Bookmarks", dst / "Bookmarks", "书签")
    safe_copy(chrome_base / "Bookmarks.bak", dst / "Bookmarks.bak", "书签备份")

    # 偏好设置
    safe_copy(chrome_base / "Preferences", dst / "Preferences", "偏好设置")

    # 搜索引擎
    # 从Preferences中提取
    prefs = chrome_base / "Preferences"
    if prefs.exists():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8"))
            search_engines = data.get("default_search_provider_data", {})
            with open(dst / "search_engines.json", "w", encoding="utf-8") as f:
                json.dump(search_engines, f, ensure_ascii=False, indent=2)
            print("  [已提取] 搜索引擎设置")
        except Exception:
            pass

    # 扩展列表
    extensions_dir = chrome_base.parent / "Extensions"
    if extensions_dir.exists():
        ext_list = []
        for ext_id in extensions_dir.iterdir():
            if ext_id.is_dir():
                # 读取manifest获取扩展名
                versions = sorted(ext_id.iterdir(), reverse=True)
                for ver_dir in versions:
                    manifest = ver_dir / "manifest.json"
                    if manifest.exists():
                        try:
                            m = json.loads(manifest.read_text(encoding="utf-8"))
                            ext_list.append({
                                "id": ext_id.name,
                                "name": m.get("name", "未知"),
                                "version": m.get("version", ""),
                            })
                        except Exception:
                            ext_list.append({"id": ext_id.name, "name": "未知", "version": ""})
                        break
        with open(dst / "extensions_list.json", "w", encoding="utf-8") as f:
            json.dump(ext_list, f, ensure_ascii=False, indent=2)
        print(f"  [已提取] 扩展列表 ({len(ext_list)}个)")

    # 油猴脚本
    for ext_dir in (chrome_base.parent / "Extensions").iterdir():
        if "tampermonkey" in ext_dir.name.lower() or "violentmonkey" in ext_dir.name.lower():
            safe_copy(ext_dir, dst / f"extension_{ext_dir.name}", "油猴扩展数据")

    print("  ⚠️ Chrome密码无法自动导出，请手动导出: chrome://password-manager/settings")


# ========== 游戏存档备份 ==========

def backup_game_saves():
    """备份游戏存档"""
    print("\n" + "="*50)
    print("备份游戏存档")
    print("="*50)

    dst = BACKUP_DIR / "game_saves"
    backed_up = 0

    # Steam存档
    steam_userdata = Path(r"C:\Program Files (x86)\Steam\userdata")
    if steam_userdata.exists():
        safe_copy(steam_userdata, dst / "Steam_userdata", "Steam用户数据")
        backed_up += 1

    # <data_drive>:\Documents/My Games
    my_games = Path(os.environ.get("USERPROFILE", "")) / "<data_drive>:\Documents" / "My Games"
    if my_games.exists():
        safe_copy(my_games, dst / "My_Games", "My Games")
        backed_up += 1

    # AppData常见游戏存档路径
    appdata_local = Path(os.environ.get("LOCALAPPDATA", ""))
    appdata_roaming = Path(os.environ.get("APPDATA", ""))

    game_patterns = [
        ("Epic Games", appdata_local / "Epic Games"),
        ("GOG Galaxy", appdata_local / "GOG.com"),
        ("NVIDIA", appdata_local / "NVIDIA"),
    ]

    for name, path in game_patterns:
        if path.exists():
            safe_copy(path, dst / name, name)
            backed_up += 1

    if backed_up == 0:
        print("  未找到常见游戏存档路径")
    print("  ⚠️ 部分游戏存档在游戏安装目录下，需手动确认")


# ========== 系统设置备份 ==========

def backup_system():
    """备份系统设置"""
    print("\n" + "="*50)
    print("备份系统设置")
    print("="*50)

    dst = BACKUP_DIR / "system"
    dst.mkdir(parents=True, exist_ok=True)

    # 注册表导出
    reg_items = [
        ("HKCU\\Environment", dst / "registry" / "env_user.reg", "用户环境变量"),
        ("HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment", dst / "registry" / "env_system.reg", "系统环境变量"),
        ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Taskband", dst / "registry" / "taskband.reg", "任务栏"),
        ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced", dst / "registry" / "explorer_advanced.reg", "资源管理器设置"),
    ]

    for key, path, label in reg_items:
        reg_export(key, path, label)

    # 桌面图标位置
    reg_export(
        "HKCU\\Software\\Microsoft\\Windows\\Shell\\Bags",
        dst / "registry" / "desktop_layout.reg",
        "桌面布局",
    )

    # 电源计划
    run_cmd(f"powercfg /export \"{dst / 'power_plan.pow'}\"", "电源计划")

    # hosts文件
    hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
    if hosts.exists():
        try:
            shutil.copy2(hosts, dst / "hosts")
            print("  [已备份] hosts文件")
        except PermissionError:
            print("  [跳过] hosts文件需要管理员权限")

    # Windows Terminal配置
    wt_packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
    if wt_packages.exists():
        for pkg in wt_packages.iterdir():
            if "WindowsTerminal" in pkg.name:
                settings = pkg / "LocalState" / "settings.json"
                if settings.exists():
                    safe_copy(settings, dst / "terminal_settings.json", "Windows Terminal配置")

    # 开始菜单布局
    layout_path = dst / "start_layout.xml"
    run_cmd(
        f'powershell -Command "Export-StartLayout -Path \'{layout_path}\'"',
        "开始菜单布局",
    )


# ========== 软件列表备份 ==========

def backup_software():
    """备份已安装软件列表"""
    print("\n" + "="*50)
    print("备份软件列表")
    print("="*50)

    dst = BACKUP_DIR / "software"
    dst.mkdir(parents=True, exist_ok=True)

    # 已安装程序
    output = run_cmd(
        'powershell -Command "Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* | '
        'Where-Object { $_.DisplayName } | Select-Object DisplayName, DisplayVersion, Publisher | '
        'ConvertTo-Json"',
        "已安装程序列表",
    )
    if output:
        (dst / "installed_programs.json").write_text(output, encoding="utf-8")

    # Chocolatey
    run_cmd("choco list --local-only", "Chocolatey包")
    output = run_cmd("choco list --local-only --limit-output", "Chocolatey包(简洁)")
    if output:
        (dst / "choco_list.txt").write_text(output, encoding="utf-8")

    # Scoop
    output = run_cmd("scoop list", "Scoop包")
    if output:
        (dst / "scoop_list.txt").write_text(output, encoding="utf-8")

    # pip
    output = run_cmd("pip list --format=freeze", "pip包")
    if output:
        (dst / "pip_requirements.txt").write_text(output, encoding="utf-8")

    # uv tools
    output = run_cmd("uv tool list", "uv工具")
    if output:
        (dst / "uv_tools.txt").write_text(output, encoding="utf-8")

    # npm全局
    output = run_cmd("npm list -g --depth=0", "npm全局包")
    if output:
        (dst / "npm_global.txt").write_text(output, encoding="utf-8")


# ========== 桌面备份 ==========

def backup_desktop():
    """备份桌面快捷方式和文件列表"""
    print("\n" + "="*50)
    print("备份桌面")
    print("="*50)

    dst = BACKUP_DIR / "desktop"
    desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"

    if not desktop.exists():
        print("  桌面路径不存在")
        return

    # 记录桌面文件列表（不复制文件本身，filesync会处理）
    items = []
    for item in desktop.iterdir():
        items.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "suffix": item.suffix if item.is_file() else "",
        })

    (dst / "desktop_items.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  [已记录] 桌面文件列表 ({len(items)}项)")

    # 复制快捷方式（.lnk文件）
    lnk_count = 0
    for lnk in desktop.glob("*.lnk"):
        try:
            shutil.copy2(lnk, dst / lnk.name)
            lnk_count += 1
        except Exception:
            pass
    if lnk_count:
        print(f"  [已备份] 快捷方式 ({lnk_count}个)")


# ========== 生成恢复脚本 ==========

def generate_restore_script():
    """生成一键恢复脚本"""
    print("\n" + "="*50)
    print("生成恢复脚本")
    print("="*50)

    dst = BACKUP_DIR / "restore_env.bat"

    script = f"""@echo off
chcp 65001 >nul
echo ============================================
echo   系统环境恢复脚本
echo   备份时间: {TIMESTAMP}
echo ============================================
echo.
echo ⚠️ 此脚本将恢复注册表和系统设置！
echo ⚠️ 请以管理员身份运行！
echo.
pause

echo.
echo [1/6] 恢复注册表...
"""

    # 注册表恢复
    reg_dir = BACKUP_DIR / "system" / "registry"
    if reg_dir.exists():
        for reg_file in reg_dir.glob("*.reg"):
            script += f'echo 导入 {reg_file.name}...\n'
            script += f'reg import "{reg_file}"\n'

    script += f"""
echo.
echo [2/6] 恢复hosts文件...
set /p restore_hosts="是否恢复hosts文件? (y/n): "
if /i "%restore_hosts%"=="y" (
    copy /y "{BACKUP_DIR / "system" / "hosts"}" "C:\\Windows\\System32\\drivers\\etc\\hosts"
    echo hosts已恢复
)

echo.
echo [3/6] 恢复Windows Terminal配置...
echo 请手动将 terminal_settings.json 复制到:
echo   %%LOCALAPPDATA%%\\Packages\\Microsoft.WindowsTerminal_8wekyb3d8bbwe\\LocalState\\settings.json

echo.
echo [4/6] 恢复浏览器书签...
echo 请手动将 Bookmarks 文件复制到:
echo   %%LOCALAPPDATA%%\\Google\\Chrome\\User Data\\Default\\Bookmarks

echo.
echo [5/6] 恢复游戏存档...
echo 请确认游戏已安装后，将 game_saves 目录下的存档复制到对应位置

echo.
echo [6/6] 安装软件...
echo 请参考 software/ 目录下的列表手动安装

echo.
echo ============================================
echo   恢复完成！
echo   详细清单请查看 manual_checklist.md
echo ============================================
pause
"""

    dst.write_text(script, encoding="utf-8")
    print(f"  [已生成] {dst}")


def generate_checklist():
    """生成手动确认清单"""
    print("\n" + "="*50)
    print("生成手动确认清单")
    print("="*50)

    dst = BACKUP_DIR / "manual_checklist.md"

    content = f"""# 系统环境恢复 - 手动确认清单

备份时间: {TIMESTAMP}

## 需手动恢复的项目

### 浏览器
- [ ] Chrome密码: chrome://password-manager/settings → 导出 → 在新电脑导入
- [ ] Chrome扩展: 参考 browser/extensions_list.json 逐个安装
- [ ] 油猴脚本: 检查是否需要重新安装

### 游戏存档
- [ ] 确认游戏已安装
- [ ] 复制 game_saves/ 下对应目录到存档位置
- [ ] Steam云存档会自动同步，本地存档需手动复制

### 系统设置
- [ ] Windows激活
- [ ] 显示缩放比例
- [ ] 深色/浅色模式
- [ ] 输入法配置
- [ ] 默认浏览器
- [ ] 默认文件关联（.py/.md/.json等）
- [ ] 任务栏位置和固定项

### 软件
- [ ] 参考 software/installed_programs.json 安装必要软件
- [ ] pip: pip install -r software/pip_requirements.txt
- [ ] Chocolatey: 参考 software/choco_list.txt
- [ ] Scoop: 参考 software/scoop_list.txt

### 其他
- [ ] VPN/代理设置
- [ ] Git配置: git config --global user.name/email
- [ ] SSH密钥: ~/.ssh/
- [ ] 自定义字体
- [ ] 右键菜单增强工具
"""

    dst.write_text(content, encoding="utf-8")
    print(f"  [已生成] {dst}")


# ========== 主入口 ==========

def main():
    parser = argparse.ArgumentParser(description="系统环境备份工具")
    parser.add_argument("--browser", action="store_true", help="只备份浏览器")
    parser.add_argument("--games", action="store_true", help="只备份游戏存档")
    parser.add_argument("--system", action="store_true", help="只备份系统设置")
    parser.add_argument("--software", action="store_true", help="只备份软件列表")
    parser.add_argument("--desktop", action="store_true", help="只备份桌面")
    args = parser.parse_args()

    specific = args.browser or args.games or args.system or args.software or args.desktop
    if not specific:
        print("=" * 60)
        print("⚠️  全面备份功能未经测试！执行过程中可能遇到问题。")
        print("    请全程跟进并记录遇到的任何错误。")
        print("=" * 60)

    print(f"备份目标: {BACKUP_DIR}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if args.browser or not specific:
        backup_browser()
    if args.games or not specific:
        backup_game_saves()
    if args.system or not specific:
        backup_system()
    if args.software or not specific:
        backup_software()
    if args.desktop or not specific:
        backup_desktop()

    if not specific:
        generate_restore_script()
        generate_checklist()

    # 统计备份大小
    total = sum(f.stat().st_size for f in BACKUP_DIR.rglob("*") if f.is_file())
    print(f"\n{'='*50}")
    print(f"备份完成！总大小: {fmt_size(total)}")
    print(f"备份位置: {BACKUP_DIR}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
