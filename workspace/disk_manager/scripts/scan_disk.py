"""磁盘空间扫描工具 - 单次遍历 + 多视图查询架构

第一性原理：每次完整清理评估最多一次物理遍历。
scan 子命令一次遍历建缓存，后续 tree/files/find/caches/dups 全部从缓存读、零磁盘 IO。
resume 子命令跳过 complete=true 子树，只重扫 partial 子树。

子命令：
  scan   <path>           单次物理遍历，建缓存 JSON（唯一碰磁盘的命令）
  tree   [--path X]       从缓存读，top-N 紧凑 tree（--path 前缀过滤）
  files  [--path X]       从缓存读，最大文件 top-N
  find   [--path X] --names N1 N2 ..  按目录名定位
  caches [--path X]       按内置缓存清单匹配
  dups   [--path X]       疑似重复文件簇（按 size+name 聚类）
  resume <cache-path>     续扫：跳过 complete，重扫 partial

所有命令支持 --json 直返 stdout。symlink/junction 不跟随，多维硬上限保护。
缓存路径：temp/disk_scan_cache/<slug>_<timestamp>.json
"""

import os
import sys
import time
import json
import argparse
import re
from pathlib import Path
from datetime import datetime

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# 硬上限默认值
DEFAULT_MAX_FILES = 1_000_000
DEFAULT_MAX_DIRS = 500_000
DEFAULT_MAX_DURATION = 300  # 秒
DEFAULT_MAX_DEPTH = 1_000_000  # 实质不限，靠 files/dirs/duration 兜底
DEFAULT_TOP_N = 10
DEFAULT_KEEP_FILES = 1000  # scan 时每个目录保留的文件节点数（按 size 降序）

# 缓存目录
CACHE_DIR = Path(__file__).resolve().parents[3] / "temp" / "disk_scan_cache"

# 内置缓存目录清单（源自 SKILL.md，caches 子命令用）
# (names, category) — names 是目录名集合，category 是分类标签
CACHE_NAMES = {
    # 缓存类（安全删，自动重建）
    "QQMusicCache": "cache",
    "node_modules": "build_artifact",
    "__pycache__": "build_artifact",
    ".venv": "build_artifact",
    "venv": "build_artifact",
    ".cache": "cache",
    "pip-cache": "cache",
    "npm-cache": "cache",
    "CrashDumps": "cache",
    "DXCache": "cache",
    "CefCache": "cache",
    "htmlcache": "cache",
    "webviewcache": "cache",
    "WmpfCache": "cache",
    "CacheStorage": "cache",
    "CachedExtensionVSIXs": "cache",
    "EBWebView": "cache",
    # 临时文件类
    "Temp": "temp",
    "tmp": "temp",
    # 更新器类（安装后可删）
    "ota-artifacts": "updater",
    "lm-studio-updater": "updater",
    # 构建产物类
    ".gradle": "build_artifact",
    "build": "build_artifact",
    "dist": "build_artifact",
    "target": "build_artifact",
    ".next": "build_artifact",
    ".nuxt": "build_artifact",
    ".rustc": "build_artifact",
    ".rust-analyzer": "build_artifact",
}


def human(n):
    """格式化文件大小"""
    f = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if f < 1024:
            return f"{f:.1f}{u}"
        f /= 1024
    return f"{f:.1f}PB"


def _entry_stat(entry):
    """获取 entry 的 stat（follow_symlinks=False，用 DirEntry 缓存）"""
    try:
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def is_reparse_stat(st):
    """判断 stat 是否为 reparse point（symlink/junction）"""
    return st is not None and hasattr(st, "st_file_attributes") and bool(st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def slugify(path):
    """目标路径派生 slug：去分隔符，截断 30 字符"""
    s = re.sub(r"[/:\\]+", "_", str(path)).strip("_")
    s = re.sub(r"[^A-Za-z0-9_-]", "", s)
    return s[:30] if s else "root"


def norm_path(p):
    """路径归一化：统一分隔符为 /，小写（Windows 大小写不敏感）"""
    return str(p).replace("\\", "/").lower().rstrip("/")


# ========== BoundedScanner：单次遍历，带 complete/partial 标记 ==========

class BoundedScanner:
    """带多维硬上限的只读扫描器。symlink/junction 一律不跟随。

    关键修复：深度超限在 build_tree 入口单独判断（不设 stop_reason），
    不再中断同层兄弟目录迭代。files/dirs/duration 超限才设 stop_reason。
    """

    def __init__(self, max_files=DEFAULT_MAX_FILES, max_dirs=DEFAULT_MAX_DIRS,
                 max_duration=DEFAULT_MAX_DURATION, max_depth=DEFAULT_MAX_DEPTH,
                 keep_files=DEFAULT_KEEP_FILES, exclude=None):
        self.max_files = max_files
        self.max_dirs = max_dirs
        self.max_duration = max_duration
        self.max_depth = max_depth
        self.keep_files = keep_files
        self.exclude = set(exclude or [])
        self.files = 0
        self.dirs = 0
        self.errors = 0
        self.reparse_skipped = 0
        self.stop_reason = None
        self._t0 = None

    def _start_timer(self):
        self._t0 = time.perf_counter()

    def _check_global_stop(self):
        """检查全局硬上限（files/dirs/duration）。深度超限不在此处判断。"""
        if self.stop_reason:
            return True
        if self.files >= self.max_files:
            self.stop_reason = f"files>={self.max_files}"
            return True
        if self.dirs >= self.max_dirs:
            self.stop_reason = f"dirs>={self.max_dirs}"
            return True
        if self._t0 is not None and (time.perf_counter() - self._t0) > self.max_duration:
            self.stop_reason = f"duration>{self.max_duration}s"
            return True
        return False

    def build_tree(self, path, depth):
        """构建树到 max_depth。深度超限返回空节点（不设 stop_reason），
        全局上限超限设 stop_reason。每个节点带 complete 标记。

        complete 语义：
        - True = 本节点 scandir 跑完了（children 列表完整），但其后代可能因上限未完整
        - False = 本节点 scandir 被 stop_reason 中断（children 列表可能不全）
        深度超限返回的空节点 complete=False（未扫描）。
        """
        # 深度超限：返回空节点，不设 stop_reason（不中断同层兄弟）
        if depth > self.max_depth:
            return {"name": os.path.basename(path) or path, "size": 0, "files": 0,
                    "dirs": 0, "complete": False, "children": [], "files_top": []}

        node = {
            "name": os.path.basename(path) or str(path),
            "size": 0, "files": 0, "dirs": 0,
            "complete": False,  # 默认 False，scandir 跑完才置 True
            "children": [],
            "files_top": [],  # 本层文件节点（top-N by size）
            "mtime": None,
        }
        # 全局上限已在入口外检查，这里若已停则直接返回空节点
        if self._check_global_stop():
            return node

        files_here = []  # 本层文件节点（用于 top-N 裁剪）
        scandir_completed = False
        try:
            for entry in os.scandir(path):
                if self.stop_reason:
                    break
                try:
                    if entry.name in self.exclude:
                        continue
                    st = _entry_stat(entry)
                    if st is None:
                        continue
                    if is_reparse_stat(st):
                        self.reparse_skipped += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        self.dirs += 1
                        child = self.build_tree(entry.path, depth + 1)
                        node["children"].append(child)
                        node["size"] += child["size"]
                        node["files"] += child["files"]
                        node["dirs"] += child["dirs"] + 1
                    elif entry.is_file(follow_symlinks=False):
                        self.files += 1
                        fsize = st.st_size
                        node["size"] += fsize
                        node["files"] += 1
                        files_here.append({
                            "name": entry.name,
                            "size": fsize,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                        })
                except OSError:
                    self.errors += 1
                # 每个文件后检查全局上限（高频检查，避免单目录爆上限）
                if self._check_global_stop():
                    break
            else:
                # for 循环正常结束（未 break），说明 scandir 跑完了
                scandir_completed = True
        except (PermissionError, OSError):
            self.errors += 1

        # 文件节点裁剪：按 size 降序保留 top-N
        files_here.sort(key=lambda x: x["size"], reverse=True)
        node["files_top"] = files_here[:self.keep_files]

        # complete 标记：本节点 scandir 跑完（无论全局是否触上限）
        node["complete"] = scandir_completed
        return node


def _prune_tree(node, top_n, depth, max_depth):
    """裁剪 tree 用于输出：每层保留 top-N 子目录，其余折叠进 other。
    限制输出深度。文件节点保留 top-N（已在 build_tree 时裁剪）。"""
    children = node.get("children", [])
    kept = sorted(children, key=lambda c: c.get("size", 0), reverse=True)[:top_n]
    rest = children[len(kept):]
    node["children"] = [_prune_tree(c, top_n, depth + 1, max_depth) for c in kept] if depth < max_depth else []
    node["other"] = {"dirs": len(rest), "size": sum(c.get("size", 0) for c in rest)}
    return node


def _collect_files(node, root_abs, prefix_path=""):
    """从树收集所有文件节点（含路径拼接）。

    路径拼接规则（与 cmd_find/cmd_caches 一致）：
    - 根节点：cur_path = root_abs（绝对路径，不拼 node["name"]，避免重复）
    - 子节点：cur_path = prefix_path + "/" + node["name"]
    """
    cur_path = root_abs if not prefix_path else prefix_path + "/" + node["name"]
    for f in node.get("files_top", []):
        yield {"path": cur_path + "/" + f["name"], "size": f["size"], "modified": f["mtime"]}
    for c in node.get("children", []):
        yield from _collect_files(c, root_abs, cur_path)


def _find_node_by_path(node, target_norm, root_abs_norm, prefix_path=""):
    """在树中按路径前缀查找子节点。target_norm 是归一化后的目标路径，
    root_abs_norm 是 scan_meta.path 的归一化绝对路径（用于根节点匹配）。
    返回匹配节点或 None。"""
    # 根节点：用 root_abs_norm 匹配
    if not prefix_path:
        cur_path = root_abs_norm
    else:
        cur_path = prefix_path + "/" + node["name"]
    cur_norm = norm_path(cur_path)
    target = target_norm.rstrip("/")
    # 完全匹配
    if cur_norm == target:
        return node
    # target 是 cur 的子路径：往 children 找
    if target.startswith(cur_norm + "/"):
        for c in node.get("children", []):
            found = _find_node_by_path(c, target_norm, root_abs_norm, cur_path)
            if found:
                return found
    return None


# ========== 缓存读写 ==========

def _cache_path_for(target_path):
    """生成缓存文件路径：temp/disk_scan_cache/<slug>_<timestamp>.json"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(target_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return CACHE_DIR / f"{slug}_{ts}.json"


def _find_latest_cache(slug):
    """找同 slug 最新缓存文件"""
    if not CACHE_DIR.exists():
        return None
    pattern = f"{slug}_*.json"
    candidates = sorted(CACHE_DIR.glob(pattern))
    return candidates[-1] if candidates else None


def _write_cache(cache_path, scan_meta, tree):
    """写缓存 JSON"""
    payload = {"scan_meta": scan_meta, "tree": tree}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _load_cache(cache_path):
    """读缓存 JSON，返回 (scan_meta, tree)"""
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("scan_meta", {}), data.get("tree", {})


# ========== 子命令实现 ==========

def cmd_scan(args):
    """scan: 单次物理遍历，建缓存"""
    target = args.path
    if not os.path.exists(target):
        print(json.dumps({"error": f"path not found: {target}"}, ensure_ascii=False))
        sys.exit(1)

    scanner = BoundedScanner(
        max_files=args.max_files, max_dirs=args.max_dirs,
        max_duration=args.max_duration, max_depth=args.max_depth,
        keep_files=args.keep_files, exclude=args.exclude,
    )
    scanner._start_timer()
    t0 = time.perf_counter()
    tree = scanner.build_tree(target, 0)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    cache_path = _cache_path_for(target)
    scan_meta = {
        "path": os.path.abspath(target),
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "max_files": args.max_files,
        "max_dirs": args.max_dirs,
        "max_duration": args.max_duration,
        "max_depth": args.max_depth,
        "keep_files": args.keep_files,
        "stop_reason": scanner.stop_reason,
        "files": scanner.files,
        "dirs": scanner.dirs,
        "errors": scanner.errors,
        "reparse_skipped": scanner.reparse_skipped,
        "duration_ms": duration_ms,
        "cache_path": str(cache_path),
    }
    _write_cache(cache_path, scan_meta, tree)

    result = {
        "scan_meta": scan_meta,
        "tree_root": {k: v for k, v in tree.items() if k != "children"},
        "cache_path": str(cache_path),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"扫描完成: {scan_meta['path']}")
        print(f"  文件: {scanner.files:,}  目录: {scanner.dirs:,}  大小: {human(tree['size'])}")
        print(f"  耗时: {duration_ms}ms  stop_reason: {scanner.stop_reason or 'none'}")
        print(f"  缓存: {cache_path}")


def _resolve_cache(args):
    """解析缓存路径：--cache 显式 > 同 slug 最新"""
    if getattr(args, "cache", None):
        return Path(args.cache)
    slug = slugify(args.path) if hasattr(args, "path") else None
    if slug:
        latest = _find_latest_cache(slug)
        if latest:
            return latest
    print(json.dumps({"error": "no cache found, run scan first or specify --cache"}, ensure_ascii=False))
    sys.exit(1)


def cmd_tree(args):
    """tree: 从缓存读，--path 前缀过滤，top-N + other 折叠"""
    cache_path = _resolve_cache(args)
    scan_meta, tree = _load_cache(cache_path)

    # --path 过滤
    if args.path:
        target_norm = norm_path(args.path)
        root_abs_norm = norm_path(scan_meta.get("path", ""))
        if target_norm == root_abs_norm:
            sub = tree
        else:
            sub = _find_node_by_path(tree, target_norm, root_abs_norm)
            if sub is None:
                print(json.dumps({"error": f"path not in cache: {args.path}"}, ensure_ascii=False))
                sys.exit(1)
    else:
        sub = tree

    # 裁剪输出
    pruned = _prune_tree(sub, args.top_n, 0, args.max_depth if args.max_depth is not None else 3)

    result = {
        "cache_path": str(cache_path),
        "scan_meta": {"path": scan_meta.get("path"), "scanned_at": scan_meta.get("scanned_at"),
                      "stop_reason": scan_meta.get("stop_reason")},
        "tree": pruned,
    }
    print(json.dumps(result, ensure_ascii=False))


def _resolve_search_root(args, scan_meta, tree):
    """公共辅助：解析 --path 过滤后的搜索根节点。无 --path 返回整棵 tree。"""
    if not args.path:
        return tree
    target_norm = norm_path(args.path)
    root_abs_norm = norm_path(scan_meta.get("path", ""))
    if target_norm == root_abs_norm:
        return tree
    sub = _find_node_by_path(tree, target_norm, root_abs_norm)
    if sub is None:
        print(json.dumps({"error": f"path not in cache: {args.path}"}, ensure_ascii=False))
        sys.exit(1)
    return sub


def cmd_files(args):
    """files: 从缓存读，按 size 降序输出 top-N 文件"""
    cache_path = _resolve_cache(args)
    scan_meta, tree = _load_cache(cache_path)

    search_root = _resolve_search_root(args, scan_meta, tree)
    root_abs = scan_meta.get("path", "")

    files = list(_collect_files(search_root, root_abs))
    if args.min_size:
        files = [f for f in files if f["size"] >= args.min_size]
    files.sort(key=lambda x: x["size"], reverse=True)
    top = files[:args.top_n]

    result = {
        "cache_path": str(cache_path),
        "total_files_in_cache": len(files),
        "largest": top,
    }
    print(json.dumps(result, ensure_ascii=False))


def cmd_find(args):
    """find: 从缓存读，按 --names 目录名匹配"""
    cache_path = _resolve_cache(args)
    scan_meta, tree = _load_cache(cache_path)

    search_root = _resolve_search_root(args, scan_meta, tree)
    root_abs = scan_meta.get("path", "")

    names_set = set(args.names)
    matches = []

    def walk(node, prefix_path=""):
        # 根节点用绝对路径，子节点拼接
        cur_path = root_abs if not prefix_path else prefix_path + "/" + node["name"]
        for c in node.get("children", []):
            child_path = cur_path + "/" + c["name"]
            if c["name"] in names_set:
                matches.append({"name": c["name"], "path": child_path,
                                "size": c.get("size", 0), "files": c.get("files", 0),
                                "complete": c.get("complete", False)})
            else:
                walk(c, cur_path)

    walk(search_root)
    matches.sort(key=lambda x: x["size"], reverse=True)

    result = {
        "cache_path": str(cache_path),
        "names": args.names,
        "matches": len(matches),
        "total_size": sum(m["size"] for m in matches),
        "results": matches,
    }
    print(json.dumps(result, ensure_ascii=False))


def cmd_caches(args):
    """caches: 从缓存读，按内置缓存清单匹配"""
    cache_path = _resolve_cache(args)
    scan_meta, tree = _load_cache(cache_path)

    search_root = _resolve_search_root(args, scan_meta, tree)
    root_abs = scan_meta.get("path", "")

    matches = []

    def walk(node, prefix_path=""):
        cur_path = root_abs if not prefix_path else prefix_path + "/" + node["name"]
        for c in node.get("children", []):
            child_path = cur_path + "/" + c["name"]
            if c["name"] in CACHE_NAMES:
                matches.append({"name": c["name"], "path": child_path,
                                "size": c.get("size", 0), "files": c.get("files", 0),
                                "category": CACHE_NAMES[c["name"]],
                                "complete": c.get("complete", False)})
            walk(c, cur_path)

    walk(search_root)
    matches.sort(key=lambda x: x["size"], reverse=True)

    result = {
        "cache_path": str(cache_path),
        "matches": len(matches),
        "total_size": sum(m["size"] for m in matches),
        "results": matches,
    }
    print(json.dumps(result, ensure_ascii=False))


def cmd_dups(args):
    """dups: 从缓存读，按 size+name 聚类疑似重复文件"""
    cache_path = _resolve_cache(args)
    scan_meta, tree = _load_cache(cache_path)

    search_root = _resolve_search_root(args, scan_meta, tree)
    root_abs = scan_meta.get("path", "")

    files = list(_collect_files(search_root, root_abs))
    # 过滤 size=0
    files = [f for f in files if f["size"] > 0]

    # 聚类
    clusters = {}
    for f in files:
        if args.by == "size":
            key = f["size"]
        else:  # size+name
            key = (f["size"], os.path.basename(f["path"]))
        clusters.setdefault(key, []).append(f["path"])

    # 只保留 ≥2 文件的簇
    dups = []
    for key, paths in clusters.items():
        if len(paths) >= 2:
            size = key[0] if isinstance(key, tuple) else key
            name = key[1] if isinstance(key, tuple) else os.path.basename(paths[0])
            dups.append({"size": size, "name": name, "files": paths,
                         "count": len(paths), "total_waste": size * (len(paths) - 1)})
    dups.sort(key=lambda x: x["total_waste"], reverse=True)

    result = {
        "cache_path": str(cache_path),
        "by": args.by,
        "clusters": len(dups),
        "total_waste": sum(d["total_waste"] for d in dups),
        "results": dups,
    }
    print(json.dumps(result, ensure_ascii=False))


def cmd_resume(args):
    """resume: 读旧缓存，跳过 complete=true 子树，重扫 partial。

    设计要点：
    - 默认用 DEFAULT_MAX_FILES 等大上限（旧配置可能过小，正是首次扫描 partial 的原因）
    - --max-files 等参数允许覆盖默认值
    - 递归重扫所有 complete=False 的子树（不只根下第一层）
    - 根节点本身不重扫（它是入口，scandir 状态继承旧缓存）
    """
    old_cache = Path(args.cache_path)
    if not old_cache.exists():
        print(json.dumps({"error": f"cache not found: {old_cache}"}, ensure_ascii=False))
        sys.exit(1)

    scan_meta, tree = _load_cache(old_cache)
    root_path = scan_meta.get("path", "")
    if not root_path:
        print(json.dumps({"error": "old cache missing scan_meta.path"}, ensure_ascii=False))
        sys.exit(1)

    # resume 默认用大上限（旧配置可能过小），CLI 参数可覆盖
    scanner = BoundedScanner(
        max_files=args.max_files if args.max_files is not None else DEFAULT_MAX_FILES,
        max_dirs=args.max_dirs if args.max_dirs is not None else DEFAULT_MAX_DIRS,
        max_duration=args.max_duration if args.max_duration is not None else DEFAULT_MAX_DURATION,
        max_depth=args.max_depth if args.max_depth is not None else DEFAULT_MAX_DEPTH,
        keep_files=scan_meta.get("keep_files", DEFAULT_KEEP_FILES),
    )
    scanner._start_timer()
    t0 = time.perf_counter()

    resumed_count = [0]  # 用 list 闭包可变

    def rescan_node(node, abs_path):
        """对 node 递归重扫：遇到 complete=False 的子节点就 build_tree 重扫并替换；
        complete=True 的子节点递归下去看有没有更深的 partial。"""
        for i, c in enumerate(node.get("children", [])):
            if scanner.stop_reason:
                break
            child_abs = os.path.join(abs_path, c["name"])
            if not c.get("complete", False):
                # 重扫整个子树（深度从 0 开始，build_tree 会按 max_depth 控制）
                if os.path.exists(child_abs):
                    new_child = scanner.build_tree(child_abs, 0)
                    new_child["name"] = c["name"]
                    node["children"][i] = new_child
                    resumed_count[0] += 1
                    # 新子树内部若仍有 partial（本轮又触上限），不再递归（避免无限循环）
            else:
                # complete=True 的子节点：递归看后代是否有 partial
                rescan_node(c, child_abs)

    rescan_node(tree, root_path)

    duration_ms = int((time.perf_counter() - t0) * 1000)

    # 重新计算根节点聚合（root 自己的 files_top 不变，只重聚 children）
    children = tree.get("children", [])
    tree["size"] = sum(c.get("size", 0) for c in children) + sum(f["size"] for f in tree.get("files_top", []))
    tree["files"] = sum(c.get("files", 0) for c in children) + len(tree.get("files_top", []))
    tree["dirs"] = sum(c.get("dirs", 0) for c in children) + len(children)
    tree["complete"] = scanner.stop_reason is None

    new_cache = _cache_path_for(root_path)
    new_meta = {
        "path": root_path,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "resumed_from": str(old_cache),
        "max_files": scanner.max_files, "max_dirs": scanner.max_dirs,
        "max_duration": scanner.max_duration, "max_depth": scanner.max_depth,
        "keep_files": scanner.keep_files,
        "stop_reason": scanner.stop_reason,
        "files": scanner.files, "dirs": scanner.dirs,
        "errors": scanner.errors, "reparse_skipped": scanner.reparse_skipped,
        "duration_ms": duration_ms,
        "resumed_count": resumed_count,
        "cache_path": str(new_cache),
    }
    _write_cache(new_cache, new_meta, tree)

    result = {"scan_meta": new_meta, "cache_path": str(new_cache)}
    print(json.dumps(result, ensure_ascii=False))


# ========== argparse ==========

def build_parser():
    parser = argparse.ArgumentParser(
        description="磁盘空间扫描工具 - 单次遍历 + 多视图查询"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_cache_opts(p):
        p.add_argument("--cache", default=None, help="显式指定缓存文件路径（否则读同 slug 最新）")

    def add_path_filter(p):
        p.add_argument("--path", default=None, help="路径前缀过滤（从缓存中筛该子树）")

    # scan
    p = sub.add_parser("scan", help="单次物理遍历，建缓存")
    p.add_argument("path", help="目标路径")
    p.add_argument("--json", action="store_true")
    p.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    p.add_argument("--max-dirs", type=int, default=DEFAULT_MAX_DIRS)
    p.add_argument("--max-duration", type=int, default=DEFAULT_MAX_DURATION)
    p.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="深度上限（默认不限）")
    p.add_argument("--keep-files", type=int, default=DEFAULT_KEEP_FILES, help="每目录保留文件节点数（按 size 降序）")
    p.add_argument("--exclude", nargs="*", default=[])
    p.set_defaults(func=cmd_scan)

    # tree
    p = sub.add_parser("tree", help="从缓存读，top-N 紧凑 tree")
    add_path_filter(p)
    add_cache_opts(p)
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("-n", "--top-n", type=int, default=DEFAULT_TOP_N)
    p.add_argument("--depth", dest="max_depth", type=int, default=3, help="输出深度（默认 3）")
    p.set_defaults(func=cmd_tree)

    # files
    p = sub.add_parser("files", help="从缓存读，最大文件 top-N")
    add_path_filter(p)
    add_cache_opts(p)
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("-n", "--top-n", type=int, default=20)
    p.add_argument("--min-size", type=int, default=0)
    p.set_defaults(func=cmd_files)

    # find
    p = sub.add_parser("find", help="按目录名定位")
    add_path_filter(p)
    add_cache_opts(p)
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--names", nargs="+", required=True, help="要查找的目录名")
    p.set_defaults(func=cmd_find)

    # caches
    p = sub.add_parser("caches", help="按内置缓存清单匹配")
    add_path_filter(p)
    add_cache_opts(p)
    p.add_argument("--json", action="store_true", default=True)
    p.set_defaults(func=cmd_caches)

    # dups
    p = sub.add_parser("dups", help="疑似重复文件簇")
    add_path_filter(p)
    add_cache_opts(p)
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--by", choices=["size+name", "size"], default="size+name")
    p.set_defaults(func=cmd_dups)

    # resume
    p = sub.add_parser("resume", help="续扫：跳过 complete，重扫 partial")
    p.add_argument("cache_path", help="旧缓存文件路径")
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--max-files", type=int, default=None,
                   help=f"覆盖 max_files（默认 {DEFAULT_MAX_FILES}，不用旧缓存配置）")
    p.add_argument("--max-dirs", type=int, default=None,
                   help=f"覆盖 max_dirs（默认 {DEFAULT_MAX_DIRS}）")
    p.add_argument("--max-duration", type=int, default=None,
                   help=f"覆盖 max_duration（默认 {DEFAULT_MAX_DURATION}s）")
    p.add_argument("--max-depth", type=int, default=None,
                   help=f"覆盖 max_depth（默认 {DEFAULT_MAX_DEPTH}）")
    p.set_defaults(func=cmd_resume)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
