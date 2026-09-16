"""SpaceSniffer .sns 快照导入工具 — 二进制快照 → scan_disk.py 缓存格式

SpaceSniffer（GUI 磁盘分析器）可把全盘扫描结果导出为 .sns 二进制快照。
本工具解析快照并转换成 scan_disk.py 的缓存 JSON，之后 tree/files/find/caches/dups
查询子命令直接以 --cache 复用，等效一次全盘 Python scan，但：

- 零磁盘 IO（GUI 已扫完，导入是纯内存转换，秒级）
- 无截断（快照是全量扫描结果；unknown=0 即完整）
- 可离线反复分析同一时点，适合清理前后对比留档

格式逆向笔记见 ../references/sns_format.md。

用法：
  parse_sns.py parse <快照.sns> --json
  scan_disk.py tree --cache <cache_path> -n 10     # 后续查询全部复用 scan_disk
"""

import argparse
import base64
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

sys.setrecursionlimit(100_000)  # finalize 递归；真实快照层级可达数十层

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from scan_disk import CACHE_DIR, DEFAULT_KEEP_FILES, human, slugify  # noqa: E402

SNS_MAGIC = 0x02
TYPE_VOLUME = 1
TYPE_FILE = 2
TYPE_DIR = 3
TYPE_FREE = 4
TYPE_UNKNOWN = 5
LEAF_TYPES = (TYPE_FILE, TYPE_FREE, TYPE_UNKNOWN)

# FILETIME(100ns, 1601 纪元) → Unix(秒, 1970 纪元)
_FT_TO_UNIX = 116444736000000000


def dec_name(raw: bytes) -> str:
    """Base64 名称解码。b64decode(validate=False) 自动丢弃 MIME 换行 \\r\\n。
    中文文件名实测为 GBK，utf-8 优先兜底 Windows 早期导出。"""
    b = base64.b64decode(raw)
    for enc in ("utf-8", "gbk", "utf-16-le"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return b.decode("latin-1", "replace")


def ft2str(ft: int) -> "str | None":
    """FILETIME → 'YYYY-MM-DD HH:MM'（与 scan_disk 文件节点格式一致）。目录时间戳全 0 → None。"""
    if ft <= 0:
        return None
    return datetime.fromtimestamp((ft - _FT_TO_UNIX) / 1e7).strftime("%Y-%m-%d %H:%M")


def parse_sns(data: bytes) -> "tuple[dict, dict]":
    """解析 .sns 字节流。返回 (root_node, meta)；任何结构违例 raise ValueError。

    栈语义：每条记录逻辑入栈；记录尾部的 01 00×k 从栈顶弹出 k 层。
    目录有子项 → 自身 pops=0（由最后一个子项记录的额外 pop 关闭）；
    空目录 → 自身 pops=1；文件/伪记录 → 第 1 个 pop 关闭自己，多余的关祖先。
    """
    n = len(data)
    root = None
    stack: "list[dict]" = []
    pos = 0
    nfiles = 0
    ndirs = 0
    free_bytes = 0
    unknown_bytes = 0

    while pos < n:
        if pos + 6 > n or data[pos] != SNS_MAGIC or data[pos + 1] not in (1, 2, 3, 4, 5):
            raise ValueError(f"bad record header at byte {pos}")
        tt = data[pos + 1]
        (ln,) = struct.unpack_from("<I", data, pos + 2)
        ne = pos + 6 + ln
        if ne + 42 > n:
            raise ValueError(f"truncated record at byte {pos}")
        name = dec_name(data[pos + 6:ne])
        (size,) = struct.unpack_from("<q", data, ne)
        (mtime_ft,) = struct.unpack_from("<q", data, ne + 32)
        if data[ne + 40:ne + 42] != b"\x00\x00":
            raise ValueError(f"missing end marker at byte {pos} (name={name!r})")
        p = ne + 42
        pops = 0
        while p + 2 <= n and data[p:p + 2] == b"\x01\x00":
            pops += 1
            p += 2

        if tt == TYPE_FILE:
            if not stack:
                raise ValueError(f"file record outside any dir at byte {pos} (name={name!r})")
            stack[-1]["files_top"].append({"name": name, "size": size, "mtime": ft2str(mtime_ft)})
            nfiles += 1
        elif tt in (TYPE_FREE, TYPE_UNKNOWN):
            if tt == TYPE_FREE:
                free_bytes = size
            else:
                unknown_bytes = size
        else:  # volume / dir
            node = {
                "name": name, "size": size,
                "files": 0, "dirs": 0,
                "complete": True,  # 快照是全量结果
                "children": [], "files_top": [],
                "mtime": None,
            }
            if stack:
                stack[-1]["children"].append(node)
            elif root is None:
                root = node
            else:
                raise ValueError(f"second root-level dir record at byte {pos} (name={name!r})")
            stack.append(node)
            if tt == TYPE_DIR:
                ndirs += 1

        # 弹出：叶子记录第 1 个 pop 关闭自己（不入栈），目录记录的 pop 从自己起关
        ancestor_pops = pops - 1 if tt in LEAF_TYPES else pops
        if ancestor_pops > len(stack):
            raise ValueError(f"pop on empty stack at byte {pos} (name={name!r}, pops={pops})")
        for _ in range(ancestor_pops):
            stack.pop()
        pos = p

    if root is None:
        raise ValueError("no volume record found")
    if stack:
        raise ValueError(f"unclosed records at EOF (stack depth={len(stack)})")
    if pos != n:
        raise ValueError(f"stopped at byte {pos} of {n}")

    meta = {
        "files": nfiles, "dirs": ndirs,
        "free_bytes": free_bytes, "unknown_bytes": unknown_bytes,
    }
    return root, meta


def finalize(node: dict, keep_files: int):
    """后序遍历：填 files/dirs 子树计数（与 scan_disk 语义一致），files_top 按 size 截断。"""
    files_here = node["files_top"]
    total_files = len(files_here)
    total_dirs = 0
    for c in node["children"]:
        f, d = finalize(c, keep_files)
        total_files += f
        total_dirs += d + 1
    node["files"] = total_files
    node["dirs"] = total_dirs
    files_here.sort(key=lambda x: x["size"], reverse=True)
    if keep_files > 0 and len(files_here) > keep_files:
        node["files_top"] = files_here[:keep_files]
    return total_files, total_dirs


def cmd_parse(args):
    sns_path = Path(args.sns)
    if not sns_path.exists():
        print(json.dumps({"error": f"sns file not found: {sns_path}"}, ensure_ascii=False))
        sys.exit(1)
    data = sns_path.read_bytes()

    try:
        root, meta = parse_sns(data)
    except ValueError as e:
        print(json.dumps({"error": f"sns parse failed: {e}"}, ensure_ascii=False))
        sys.exit(1)

    finalize(root, args.keep_files)

    # 卷路径归一化："C:\" → "C:"（scan_disk 的 --path 过滤与 files 路径拼接都用这个约定）
    volume_path = root["name"].replace("\\", "/").rstrip("/") or root["name"]

    if args.out:
        cache_path = Path(args.out)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        cache_path = CACHE_DIR / f"sns_{slugify(volume_path)}_{ts}.json"

    scan_meta = {
        "path": volume_path,
        "scanned_at": datetime.fromtimestamp(sns_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "spacesniffer_sns",
        "sns_file": str(sns_path),
        "volume": {
            "used_bytes": root["size"],
            "free_bytes": meta["free_bytes"],
            "unknown_bytes": meta["unknown_bytes"],
        },
        "keep_files": args.keep_files,
        "stop_reason": None,
        "files": meta["files"],
        "dirs": meta["dirs"],
        "errors": 0,
        "cache_path": str(cache_path),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"scan_meta": scan_meta, "tree": root}, f, ensure_ascii=False)

    top_dirs = sorted(root["children"], key=lambda c: c["size"], reverse=True)[:10]
    result = {
        "cache_path": str(cache_path),
        "scan_meta": scan_meta,
        "top_dirs": [{"name": c["name"], "size": c["size"], "size_human": human(c["size"])} for c in top_dirs],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        v = scan_meta["volume"]
        print(f"导入完成: {scan_meta['path']}  (快照 {scan_meta['scanned_at']})")
        print(f"  已用 {human(v['used_bytes'])}  可用 {human(v['free_bytes'])}  "
              f"未知 {human(v['unknown_bytes'])}  文件 {meta['files']:,}  目录 {meta['dirs']:,}")
        for d in result["top_dirs"][:5]:
            print(f"  {d['size_human']:>10}  {d['name']}")
        print(f"  缓存: {cache_path}")


def build_parser():
    parser = argparse.ArgumentParser(description="SpaceSniffer .sns 快照导入（→ scan_disk 缓存格式）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("parse", help="解析快照，写 scan_disk 兼容缓存")
    p.add_argument("sns", help=".sns 快照文件路径")
    p.add_argument("--json", action="store_true")
    p.add_argument("--keep-files", type=int, default=DEFAULT_KEEP_FILES,
                   help="每目录保留文件节点数（按 size 降序，0=全保留）")
    p.add_argument("--out", default=None, help="显式指定缓存输出路径（默认 temp/disk_scan_cache/sns_*.json）")
    p.set_defaults(func=cmd_parse)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
