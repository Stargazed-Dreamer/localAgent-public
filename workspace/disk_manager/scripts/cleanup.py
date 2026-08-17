"""磁盘清理脚本 - dry-run 回执 + 回收站删除

工作流：--dry-run 出逐项可回收字节回执 → agent 用 AskUserQuestion 让用户勾选
       → --delete --items <ids> 走 ctypes 回收站删除 → 出回执

设计：
  - 路径通用化：~ 替换用户目录，%ProgramData% 替换系统目录（D4/G1）
  - 删除走 ctypes SHFileOperationW（FOF_ALLOWUNDO，回收站可恢复），禁用 shutil.rmtree（D3）
  - --delete 必须基于 dry-run 回执的 item id，不扩大范围（Anti-Cheat）
  - 旧 input("yes") 整体确认已移除（D2）

用法：
  python cleanup.py --dry-run --json
  python cleanup.py --delete --items cache-1,cache-3,uninst-0 --json
"""

import os
import sys
import json
import argparse
import subprocess
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Optional, List

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# ========== ctypes 回收站（SHFileOperationW）==========

FO_DELETE = 0x0003
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004

_shell32 = ctypes.windll.shell32


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_recycle(path):
    """送文件/目录到回收站。返回 (ok, err_msg)。纯 Python，无子进程依赖。"""
    try:
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = path + "\0\0"  # 必须双 \0 结尾
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None
        res = _shell32.SHFileOperationW(ctypes.byref(op))
        if res != 0:
            return False, f"SHFileOperationW 返回 {res}"
        if op.fAnyOperationsAborted:
            return False, "用户取消"
        return True, ""
    except Exception as e:
        return False, str(e)[:80]


# ========== 清理项目定义（路径模板，运行时通用化解析）==========

# [1] 缓存/临时文件 - 安全删除，自动重建
CACHE_ITEMS = [
    (r"%ProgramData%\NVIDIA Corporation\NVIDIA app\UpdateFramework\ota-artifacts\grd", "NVIDIA驱动更新缓存"),
    (r"~\AppData\Local\pip\cache", "pip缓存"),
    (r"~\AppData\Local\CrashDumps", "崩溃转储"),
    (r"~\AppData\Local\npm-cache", "npm缓存"),
    (r"~\AppData\Roaming\Tencent\WeChat\log", "微信日志 (建议关微信)"),
    (r"~\AppData\Local\GameViewer\webviewcache", "向日葵缓存"),
    (r"~\AppData\Local\NVIDIA\DXCache", "NVIDIA着色器缓存"),
    (r"~\AppData\Local\NVIDIA Corporation\NVIDIA App\CefCache", "NVIDIA App浏览器缓存"),
    (r"~\AppData\Local\NVIDIA Corporation\NVIDIA Overlay\CefCache", "NVIDIA覆盖层缓存"),
    (r"~\AppData\Local\Microsoft\Olk\EBWebView", "Outlook WebView缓存"),
    (r"~\AppData\Roaming\IQIYI Video\GeePlayer", "爱奇艺播放器缓存"),
    (r"~\AppData\Local\Unity\cache", "Unity包缓存"),
    (r"~\AppData\Local\Steam\htmlcache", "Steam商店缓存 (建议关Steam)"),
    (r"~\AppData\Roaming\Trae CN\logs", "Trae日志 (建议关Trae)"),
    (r"~\AppData\Roaming\Tencent\WeChat\radium\WmpfCache", "微信WmpfCache (建议关微信)"),
    (r"~\AppData\Roaming\Tencent\WeMeet\Global\Data\WebkitCacheData", "腾讯会议WebKit缓存"),
    (r"E:\<data_drive>:\<system_data_root>\QQMusicCache\downloadproxyNew\tp2p\.tpfs\duty", "QQ音乐播放缓存"),
]

# [2] 已卸载程序残留
UNINSTALLED_ITEMS = [
    (r"~\.lmstudio", "LM Studio (已卸载)"),
    (r"~\AppData\Local\lm-studio-updater", "LM Studio updater (已卸载)"),
    (r"~\AppData\Roaming\secoresdk", "360安全浏览器 (已卸载)"),
    (r"~\AppData\Roaming\GreenCore7z", "360安全浏览器-GreenCore7z (已卸载)"),
    (r"~\AppData\Roaming\greencore", "360安全浏览器-greencore (已卸载)"),
]

# [3] 软件更新器缓存
UPDATER_ITEMS = [
    (r"~\AppData\Local\obsidian-updater", "Obsidian更新器"),
    (r"~\AppData\Local\unityhub-updater", "Unity Hub更新器"),
    (r"~\AppData\Local\github-stars-manager-updater", "GitHub Stars Manager更新器"),
]


@dataclass
class CleanupItem:
    id: str
    path: Optional[str]  # None 表示命令型（pip/npm）
    label: str
    category: str
    command: Optional[List[str]] = None


def _resolve(template):
    """通用化路径：~ → 用户目录，%ProgramData% → 系统目录"""
    if template.startswith("~\\") or template.startswith("~/"):
        return os.path.join(os.path.expanduser("~"), template[2:])
    if template.startswith("%ProgramData%"):
        pd = os.environ.get("ProgramData", r"C:\ProgramData")
        return os.path.join(pd, template[len("%ProgramData%") + 1:])
    return template


def build_items():
    """构建全部清理项（带稳定 id）"""
    items = []
    for i, (tpl, label) in enumerate(CACHE_ITEMS):
        items.append(CleanupItem(f"cache-{i}", _resolve(tpl), label, "cache"))
    for i, (tpl, label) in enumerate(UNINSTALLED_ITEMS):
        items.append(CleanupItem(f"uninst-{i}", _resolve(tpl), label, "uninstalled"))
    for i, (tpl, label) in enumerate(UPDATER_ITEMS):
        items.append(CleanupItem(f"updater-{i}", _resolve(tpl), label, "updater"))
    items.append(CleanupItem("pip-cache", None, "pip cache purge", "special",
                             command=[sys.executable, "-m", "pip", "cache", "purge"]))
    items.append(CleanupItem("npm-cache", None, "npm cache clean", "special",
                             command=["npm", "cache", "clean", "--force"]))
    return items


def fmt_size(b):
    if b is None:
        return "command-based"
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}TB"


def _is_reparse(entry):
    """检测 symlink/junction，不跟随"""
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def get_size(path):
    """递归计算目录大小（只读 stat，reparse 不跟随）"""
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if _is_reparse(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total += get_size(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
            except OSError:
                pass
    except (PermissionError, OSError):
        pass
    return total


def _allowed_roots():
    """删除白名单根目录（路径安全：只允许这些根下的路径被删）"""
    return [
        os.path.realpath(os.path.expanduser("~")),
        os.path.realpath(os.environ.get("ProgramData", r"C:\ProgramData")),
        r"E:\<data_drive>:\<system_data_root>",
    ]


def _path_safe(path):
    """路径必须在白名单根下，防误删系统目录"""
    rp = os.path.normcase(os.path.realpath(path))
    for root in _allowed_roots():
        if rp.startswith(os.path.normcase(root) + os.sep) or rp == os.path.normcase(root):
            return True
    return False


def cmd_dry_run(items, as_json):
    """出逐项可回收字节回执，不删任何文件"""
    receipt = []
    total = 0
    for it in items:
        if it.path is not None:
            exists = os.path.exists(it.path)
            size = get_size(it.path) if exists else 0
            receipt.append({
                "id": it.id, "path": it.path, "label": it.label,
                "category": it.category, "exists": exists,
                "size_bytes": size, "size_human": fmt_size(size),
            })
            if exists:
                total += size
        else:
            receipt.append({
                "id": it.id, "path": None, "label": it.label,
                "category": it.category, "exists": None,
                "size_bytes": None, "size_human": "command-based",
                "reason": "运行命令清理（pip/npm cache）",
            })
    result = {
        "items": receipt,
        "total_reclaimable": total,
        "total_reclaimable_human": fmt_size(total),
        "note": "删除走回收站可恢复；微信/Trae/Steam 建议关闭后再清对应项",
    }
    print(json.dumps(result, ensure_ascii=False) if as_json
          else json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delete(items, ids, as_json):
    """只删确认项，走 ctypes 回收站，出回执"""
    id_set = set(ids)
    results = []
    freed = 0
    for it in items:
        if it.id not in id_set:
            continue
        if it.path is not None:
            if not os.path.exists(it.path):
                results.append({"id": it.id, "path": it.path, "status": "skip",
                                "size": 0, "reason": "不存在"})
                continue
            if not _path_safe(it.path):
                results.append({"id": it.id, "path": it.path, "status": "fail",
                                "size": 0, "reason": "路径不在白名单根下，拒绝删除"})
                continue
            size = get_size(it.path)
            ok, err = send_to_recycle(it.path)
            if ok:
                freed += size
                results.append({"id": it.id, "path": it.path, "status": "ok",
                                "size": size, "reason": ""})
            else:
                results.append({"id": it.id, "path": it.path, "status": "fail",
                                "size": size, "reason": err})
        else:
            # 命令型（pip/npm cache）
            try:
                r = subprocess.run(it.command, capture_output=True, text=True, timeout=30)
                results.append({
                    "id": it.id, "path": None,
                    "status": "ok" if r.returncode == 0 else "fail",
                    "size": 0, "reason": r.stderr.strip()[:80] if r.returncode else "",
                })
            except Exception as e:
                results.append({"id": it.id, "path": None, "status": "fail",
                                "size": 0, "reason": str(e)[:80]})
    result = {"deleted": results, "total_freed": freed, "total_freed_human": fmt_size(freed)}
    print(json.dumps(result, ensure_ascii=False) if as_json
          else json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="磁盘清理 - dry-run 回执 + 回收站删除")
    parser.add_argument("--dry-run", action="store_true", help="出逐项回执，不删（默认行为）")
    parser.add_argument("--delete", action="store_true", help="执行删除（走回收站）")
    parser.add_argument("--items", type=str, help="要删除的 item id，逗号分隔（--delete 时必填）")
    parser.add_argument("--json", action="store_true", help="JSON 直返 stdout")
    args = parser.parse_args()

    items = build_items()
    if args.delete:
        if not args.items:
            print(json.dumps({"error": "--delete 需要 --items <ids>（先 --dry-run 拿 id）"},
                             ensure_ascii=False), file=sys.stderr)
            sys.exit(2)
        cmd_delete(items, [s.strip() for s in args.items.split(",")], args.json)
    else:
        cmd_dry_run(items, args.json)


if __name__ == "__main__":
    main()
