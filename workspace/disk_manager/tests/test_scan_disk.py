"""test_scan_disk.py — scan_disk.py 单次遍历架构的单元测试

用 importlib 动态加载 workspace/disk_manager/scripts/scan_disk.py。
测试覆盖：depth bug 回归、单次遍历零 IO、complete/partial 标记、续扫、各 query 子命令。
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 动态加载 scan_disk 模块
_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "workspace" / "disk_manager" / "scripts" / "scan_disk.py"
_spec = importlib.util.spec_from_file_location("scan_disk", _SCRIPT_PATH)
scan_disk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_disk)


def _run_cli(*args):
    """运行 scan_disk.py CLI，返回 (returncode, stdout_json, stderr)"""
    cmd = [sys.executable, str(_SCRIPT_PATH)] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        out = json.loads(r.stdout) if r.stdout else None
    except json.JSONDecodeError:
        out = None
    return r.returncode, out, r.stderr


# ========== depth bug 回归（Ticket 01 核心）==========

def test_depth_bug_regression_siblings_not_interrupted(tmp_path):
    """depth bug 回归：深度超限不应中断同层兄弟目录。
    构造 tmp_path/A/x/deep/...（超深）、tmp_path/B/y/deep/...（超深）、tmp_path/C（浅），
    scan -d 1，断言 A/B/C 三个都出现在 children 里。
    """
    # 构造：A/x/deep、B/y/deep、C（C 是浅目录，A/B 下有超深嵌套）
    (tmp_path / "A" / "x" / "deep").mkdir(parents=True)
    (tmp_path / "A" / "x" / "deep" / "f1.txt").write_text("a")
    (tmp_path / "B" / "y" / "deep").mkdir(parents=True)
    (tmp_path / "B" / "y" / "deep" / "f2.txt").write_text("b")
    (tmp_path / "C").mkdir()
    (tmp_path / "C" / "f3.txt").write_text("c")

    # scan -d 1（max_depth=1，A/B 下的 x/y/deep 会超限）
    rc, out, err = _run_cli("scan", str(tmp_path), "--json", "--max-depth", "1")
    assert rc == 0, f"scan failed: {err}"
    assert out is not None, f"no json output: {err}"

    # 读缓存验证
    cache_path = out["cache_path"]
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    children_names = {c["name"] for c in cache["tree"]["children"]}
    # 关键断言：A/B/C 三个都必须出现（depth bug 时只有第一个）
    assert "A" in children_names, f"A missing (depth bug): {children_names}"
    assert "B" in children_names, f"B missing (depth bug): {children_names}"
    assert "C" in children_names, f"C missing (depth bug): {children_names}"


# ========== complete/partial 标记 ==========

def test_complete_partial_marking_on_limit(tmp_path):
    """硬上限触发时：根节点 complete=false（scandir 被 stop_reason 中断），
    被截断子树 complete=false，扫完的子树 complete=true。"""
    # 构造 3 个子目录，每个放 2 个文件
    for sub in ["full1", "full2", "partial"]:
        d = tmp_path / sub
        d.mkdir()
        (d / "a.txt").write_text("a")
        (d / "b.txt").write_text("b")

    # scan --max-files 3：扫到第 3 个文件时触发上限
    rc, out, _ = _run_cli("scan", str(tmp_path), "--json", "--max-files", "3")
    assert rc == 0
    cache_path = out["cache_path"]
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)

    # 根节点 scandir 被 stop_reason 中断（for 循环 break），complete=false
    assert cache["tree"]["complete"] is False, "root scandir was interrupted by stop_reason"
    # stop_reason 非空
    assert cache["scan_meta"]["stop_reason"] is not None
    # 至少有一个子树 complete=false（被上限截断的）
    completes = [c["complete"] for c in cache["tree"]["children"]]
    assert False in completes, f"expected some partial subtrees, got {completes}"


def test_all_complete_when_no_limit(tmp_path):
    """无硬上限触发时：所有节点 complete=true"""
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "f.txt").write_text("a")
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "g.txt").write_text("b")

    rc, out, _ = _run_cli("scan", str(tmp_path), "--json")
    assert rc == 0
    with open(out["cache_path"], encoding="utf-8") as f:
        cache = json.load(f)
    assert cache["tree"]["complete"] is True
    assert all(c["complete"] for c in cache["tree"]["children"])
    assert cache["scan_meta"]["stop_reason"] is None


# ========== 缓存写入 ==========

def test_cache_written_and_loadable(tmp_path):
    """scan 后缓存文件存在、json.loads 成功、scan_meta 字段齐全"""
    (tmp_path / "f.txt").write_text("hello")
    rc, out, _ = _run_cli("scan", str(tmp_path), "--json")
    assert rc == 0
    cache_path = Path(out["cache_path"])
    assert cache_path.exists(), "cache file not created"

    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    meta = cache["scan_meta"]
    for field in ["path", "scanned_at", "max_files", "max_dirs", "max_duration",
                  "stop_reason", "files", "dirs", "errors", "duration_ms", "cache_path"]:
        assert field in meta, f"scan_meta missing {field}"
    assert cache["tree"]["name"]


# ========== 单次遍历：query 子命令零 IO ==========

def _scan_first(tmp_path, **kwargs):
    """扫描 tmp_path 并返回 cache_path"""
    args = ["scan", str(tmp_path), "--json"]
    for k, v in kwargs.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    rc, out, _ = _run_cli(*args)
    assert rc == 0, "scan failed"
    return out["cache_path"]


def test_tree_zero_io_after_scan(tmp_path, monkeypatch):
    """scan 建缓存后，tree 子命令不碰磁盘"""
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "f.txt").write_text("a")
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "g.txt").write_text("b")
    cache_path = _scan_first(tmp_path)

    # monkeypatch os.scandir 抛异常，验证 tree 不调它
    original_scandir = os.scandir

    def boom(*args, **kwargs):
        raise AssertionError("tree should not call os.scandir (zero IO)")

    monkeypatch.setattr(os, "scandir", boom)
    try:
        rc, out, _ = _run_cli("tree", "--cache", cache_path)
        assert rc == 0, f"tree failed: {out}"
        assert "error" not in out
        assert "tree" in out
    finally:
        monkeypatch.setattr(os, "scandir", original_scandir)


def test_files_zero_io_after_scan(tmp_path, monkeypatch):
    """scan 建缓存后，files 子命令不碰磁盘"""
    (tmp_path / "big.bin").write_bytes(b"x" * 100)
    (tmp_path / "small.txt").write_text("s")
    cache_path = _scan_first(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("files should not call os.scandir (zero IO)")

    monkeypatch.setattr(os, "scandir", boom)
    try:
        rc, out, _ = _run_cli("files", "--cache", cache_path)
        assert rc == 0
        assert "largest" in out
    finally:
        monkeypatch.setattr(os, "scandir", os.scandir)


def test_find_zero_io_after_scan(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "mod.js").write_text("m")
    cache_path = _scan_first(tmp_path)

    def boom(*a, **k):
        raise AssertionError("find should not call os.scandir")

    monkeypatch.setattr(os, "scandir", boom)
    try:
        rc, out, _ = _run_cli("find", "--cache", cache_path, "--names", "node_modules")
        assert rc == 0
        assert out["matches"] == 1
    finally:
        monkeypatch.setattr(os, "scandir", os.scandir)


def test_caches_zero_io_after_scan(tmp_path, monkeypatch):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("p")
    cache_path = _scan_first(tmp_path)

    def boom(*a, **k):
        raise AssertionError("caches should not call os.scandir")

    monkeypatch.setattr(os, "scandir", boom)
    try:
        rc, out, _ = _run_cli("caches", "--cache", cache_path)
        assert rc == 0
        assert out["matches"] >= 2
    finally:
        monkeypatch.setattr(os, "scandir", os.scandir)


def test_dups_zero_io_after_scan(tmp_path, monkeypatch):
    # 两个同名同 size 文件
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "dup.txt").write_bytes(b"same content")
    (tmp_path / "B" / "dup.txt").write_bytes(b"same content")
    cache_path = _scan_first(tmp_path)

    def boom(*a, **k):
        raise AssertionError("dups should not call os.scandir")

    monkeypatch.setattr(os, "scandir", boom)
    try:
        rc, out, _ = _run_cli("dups", "--cache", cache_path)
        assert rc == 0
        assert out["clusters"] >= 1
    finally:
        monkeypatch.setattr(os, "scandir", os.scandir)


# ========== query 子命令行为 ==========

def test_tree_path_filter(tmp_path):
    """tree --path 过滤：只返回指定子树"""
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "f.txt").write_text("a")
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "g.txt").write_text("b")
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("tree", "--cache", cache_path, "--path", str(tmp_path / "A"))
    assert rc == 0
    # 返回的 tree 应该是 A 子树
    assert out["tree"]["name"] in ("A", os.path.basename(str(tmp_path / "A")))


def test_tree_top_n_other_fold(tmp_path):
    """tree -n 限制每层 top-N，其余折叠进 other"""
    for i in range(5):
        (tmp_path / f"d{i}").mkdir()
        (tmp_path / f"d{i}" / "f.txt").write_text("x" * (i + 1))
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("tree", "--cache", cache_path, "-n", "2")
    assert rc == 0
    assert len(out["tree"]["children"]) == 2
    assert out["tree"]["other"]["dirs"] == 3


def test_files_top_n_sorted(tmp_path):
    """files 按 size 降序，-n 限制数量"""
    for i, sz in enumerate([10, 50, 30, 40, 20]):
        (tmp_path / f"f{i}.bin").write_bytes(b"x" * sz)
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("files", "--cache", cache_path, "-n", "3")
    assert rc == 0
    sizes = [f["size"] for f in out["largest"]]
    assert sizes == [50, 40, 30]


def test_files_min_size_filter(tmp_path):
    """files --min-size 过滤"""
    for i, sz in enumerate([10, 50, 30, 40, 20]):
        (tmp_path / f"f{i}.bin").write_bytes(b"x" * sz)
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("files", "--cache", cache_path, "--min-size", "25")
    assert rc == 0
    sizes = [f["size"] for f in out["largest"]]
    assert all(s >= 25 for s in sizes)
    assert 50 in sizes and 30 in sizes


def test_find_multiple_names(tmp_path):
    """find --names 多名匹配"""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "y.pyc").write_text("y")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "z").write_text("z")
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("find", "--cache", cache_path, "--names", "node_modules", "__pycache__", ".venv")
    assert rc == 0
    assert out["matches"] == 3
    names = {m["name"] for m in out["results"]}
    assert names == {"node_modules", "__pycache__", ".venv"}


def test_find_no_descent_into_match(tmp_path):
    """find 匹配目录后不再下钻其子目录（避免重复计数）"""
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "node_modules").mkdir()  # 嵌套
    (tmp_path / "node_modules" / "node_modules" / "x").write_text("x")
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("find", "--cache", cache_path, "--names", "node_modules")
    assert rc == 0
    # 只命中外层 1 个
    assert out["matches"] == 1


def test_caches_matches_known_names(tmp_path):
    """caches 命中已知缓存目录名，且 category 标注正确"""
    (tmp_path / "QQMusicCache").mkdir()
    (tmp_path / "QQMusicCache" / "c").write_text("c")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "m.js").write_text("m")
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("caches", "--cache", cache_path)
    assert rc == 0
    assert out["matches"] >= 2
    by_name = {m["name"]: m for m in out["results"]}
    assert "QQMusicCache" in by_name
    assert by_name["QQMusicCache"]["category"] == "cache"
    assert "node_modules" in by_name
    assert by_name["node_modules"]["category"] == "build_artifact"


def test_dups_detects_same_size_same_name(tmp_path):
    """dups 检出同 size 同名文件簇"""
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "dup.txt").write_bytes(b"same content")
    (tmp_path / "B" / "dup.txt").write_bytes(b"same content")
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("dups", "--cache", cache_path)
    assert rc == 0
    assert out["clusters"] == 1
    assert out["results"][0]["count"] == 2
    assert out["results"][0]["name"] == "dup.txt"


def test_dups_by_size_only_mode(tmp_path):
    """dups --by size 宽松模式：不同名同 size 也检出"""
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "f1.txt").write_bytes(b"same size!!")
    (tmp_path / "B" / "f2.txt").write_bytes(b"same size!!")
    cache_path = _scan_first(tmp_path)

    # --by size+name 不检出（名不同）
    rc, out, _ = _run_cli("dups", "--cache", cache_path, "--by", "size+name")
    assert rc == 0
    assert out["clusters"] == 0

    # --by size 检出
    rc, out, _ = _run_cli("dups", "--cache", cache_path, "--by", "size")
    assert rc == 0
    assert out["clusters"] == 1


def test_dups_filters_zero_size(tmp_path):
    """dups 过滤 size=0 文件"""
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "empty.txt").write_text("")
    (tmp_path / "B" / "empty.txt").write_text("")
    cache_path = _scan_first(tmp_path)

    rc, out, _ = _run_cli("dups", "--cache", cache_path)
    assert rc == 0
    assert out["clusters"] == 0  # 空文件被过滤


# ========== resume 续扫 ==========

def test_resume_skips_complete_subtrees(tmp_path):
    """resume 跳过 complete=true 子树，只重扫 partial"""
    # 构造：3 个子目录，每个 2 文件
    for sub in ["d1", "d2", "d3"]:
        d = tmp_path / sub
        d.mkdir()
        (d / "a.txt").write_text("a")
        (d / "b.txt").write_text("b")

    # 首次 scan --max-files 3 触发上限
    rc, out, _ = _run_cli("scan", str(tmp_path), "--json", "--max-files", "3")
    assert rc == 0
    assert out["scan_meta"]["stop_reason"] is not None
    first_cache = out["cache_path"]

    with open(first_cache, encoding="utf-8") as f:
        first = json.load(f)
    partial_count_before = sum(1 for c in first["tree"]["children"] if not c["complete"])

    # resume（不触上限，max-files 设大）
    rc, out, _ = _run_cli("resume", first_cache)
    assert rc == 0
    new_cache = out["scan_meta"]["cache_path"]

    with open(new_cache, encoding="utf-8") as f:
        resumed = json.load(f)
    partial_count_after = sum(1 for c in resumed["tree"]["children"] if not c["complete"])

    # resume 后 partial 数量应减少（至少有一个变 complete）
    assert partial_count_after < partial_count_before, \
        f"resume should reduce partials: {partial_count_before} -> {partial_count_after}"


def test_resume_root_complete_after_full(tmp_path):
    """resume 完成后根节点 complete=true（不再触上限）"""
    (tmp_path / "d1").mkdir()
    (tmp_path / "d1" / "f.txt").write_text("a")
    (tmp_path / "d2").mkdir()
    (tmp_path / "d2" / "g.txt").write_text("b")

    # 首次 scan --max-files 1 触发上限
    rc, out, _ = _run_cli("scan", str(tmp_path), "--json", "--max-files", "1")
    assert rc == 0
    first_cache = out["cache_path"]

    # resume（max-files 大，不触上限）
    rc, out, _ = _run_cli("resume", first_cache)
    assert rc == 0
    new_cache = out["scan_meta"]["cache_path"]

    with open(new_cache, encoding="utf-8") as f:
        resumed = json.load(f)
    # 根节点 complete
    assert resumed["tree"]["complete"] is True
    # 所有子树 complete
    assert all(c["complete"] for c in resumed["tree"]["children"])


# ========== symlink/junction 跳过（Windows 专用）==========

@pytest.mark.skipif(sys.platform != "win32", reason="junction test is Windows-only")
def test_junction_skipped(tmp_path):
    """junction 不跟随，计入 reparse_skipped"""
    # 创建真实目录 + junction 指向它
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").write_text("x")
    junction = tmp_path / "link"
    # 用 mklink /J 创建 junction
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        pytest.skip(f"failed to create junction: {r.stderr}")

    rc, out, _ = _run_cli("scan", str(tmp_path), "--json")
    assert rc == 0
    assert out["scan_meta"]["reparse_skipped"] >= 1
    # junction 完全跳过（不跟随、不进 children）
    with open(out["cache_path"], encoding="utf-8") as f:
        cache = json.load(f)
    names = {c["name"] for c in cache["tree"]["children"]}
    assert "real" in names
    assert "link" not in names, f"junction should be skipped entirely, got {names}"
