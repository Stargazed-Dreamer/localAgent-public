"""test_parse_sns.py — parse_sns.py 快照导入 + scan_disk 互操作的单元测试

用 struct 按逆向出的格式规范合成微型 .sns 快照（含 GBK 中文文件名、空目录、
伪记录、栈弹出标记），验证：
- parse_sns.py 导入 → scan_disk 缓存格式的结构与计数
- scan_disk.py 的 files/find/tree 子命令直接消费导入缓存（互操作）
- 损坏快照 → rc=1 + error JSON
"""

import base64
import json
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
PARSE_SNS = _SCRIPTS_DIR / "parse_sns.py"
SCAN_DISK = _SCRIPTS_DIR / "scan_disk.py"

# 2025-06-01 12:00:00 UTC 的 FILETIME；期望值在测试里用同一公式算，避免时区脆弱
_FT = 1748779200 * 10_000_000 + 116444736000000000


def rec(tt: int, name: bytes, size: int, mtime_ft: int = 0, pops: int = 0) -> bytes:
    """合成一条 .sns 记录"""
    b64 = base64.b64encode(name)
    return (
        b"\x02" + bytes([tt]) + struct.pack("<I", len(b64)) + b64
        + struct.pack("<q", size)                # logical
        + struct.pack("<I", 0)                   # 簇尾浪费
        + struct.pack("<I", 0x20)                # attrs
        + struct.pack("<qqq", 0, 0, mtime_ft)    # 3×FILETIME
        + b"\x00\x00" + b"\x01\x00" * pops
    )


def build_sns(path: Path):
    """合成快照：
    C: (卷根, 5150)
    ├── Users (150)
    │   └── admin (150)
    │       ├── 文件.txt (100, mtime=FT)
    │       └── b.log (50)
    ├── pf (0, 空目录)
    ├── pagefile.sys (5000)
    ├── [可用空间 12345]
    └── [未知 0]
    """
    cn = "文件.txt".encode("gbk")
    blob = b"".join([
        rec(1, b"C:\\", 5150, pops=0),                                   # 卷，保持开
        rec(3, b"Users", 150, pops=0),                                   # 开
        rec(3, b"admin", 150, pops=0),                                   # 开
        rec(2, cn, 100, mtime_ft=_FT, pops=1),                           # 关文件
        rec(2, b"b.log", 50, pops=3),                                    # 关 b.log+admin+Users
        rec(3, b"pf", 0, pops=1),                                        # 空目录：开+关
        rec(2, b"pagefile.sys", 5000, pops=1),
        rec(4, "可用空间".encode("gbk"), 12345, pops=1),
        rec(5, "未知 (尚未扫描) 空间".encode("gbk"), 0, pops=2),          # 关伪记录+卷
    ])
    path.write_bytes(blob)
    return path


def _run(script: Path, *args):
    r = subprocess.run([sys.executable, str(script)] + list(args),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        out = json.loads(r.stdout) if r.stdout else None
    except json.JSONDecodeError:
        out = None
    return r.returncode, out, r.stderr


def _parse(tmp_path):
    sns = build_sns(tmp_path / "snap.sns")
    out_file = tmp_path / "cache.json"
    rc, out, err = _run(PARSE_SNS, "parse", str(sns), "--json", "--out", str(out_file))
    assert rc == 0, f"parse failed: {err}\n{out}"
    assert out is not None
    return out, json.loads(out_file.read_text(encoding="utf-8"))


# ========== 导入正确性 ==========

def test_parse_basic_structure(tmp_path):
    """快照导入：树结构、子树计数、卷信息全部正确"""
    out, cache = _parse(tmp_path)
    tree, meta = cache["tree"], cache["scan_meta"]

    assert meta["source"] == "spacesniffer_sns"
    assert meta["path"] == "C:"
    assert meta["volume"] == {"used_bytes": 5150, "free_bytes": 12345, "unknown_bytes": 0}
    assert meta["files"] == 3 and meta["dirs"] == 3

    assert tree["name"] == "C:\\"
    assert tree["size"] == 5150
    assert tree["files"] == 3 and tree["dirs"] == 3
    assert tree["complete"] is True

    by_name = {c["name"]: c for c in tree["children"]}
    assert set(by_name) == {"Users", "pf"}
    users = by_name["Users"]
    assert users["size"] == 150 and users["files"] == 2 and users["dirs"] == 1
    assert users["children"][0]["name"] == "admin"
    admin = users["children"][0]
    assert admin["files"] == 2 and admin["dirs"] == 0
    # files_top 按 size 降序；GBK 中文名解码正确；mtime 格式与 scan_disk 一致
    names = [f["name"] for f in admin["files_top"]]
    assert names == ["文件.txt", "b.log"]
    expected_mtime = datetime.fromtimestamp(1748779200).strftime("%Y-%m-%d %H:%M")
    assert admin["files_top"][0]["mtime"] == expected_mtime
    assert admin["files_top"][1]["mtime"] is None
    # 根级文件进根的 files_top
    assert [f["name"] for f in tree["files_top"]] == ["pagefile.sys"]


def test_parse_top_dirs_summary(tmp_path):
    """parse --json 输出附 top_dirs 摘要（agent 首屏）"""
    out, _ = _parse(tmp_path)
    top = out["top_dirs"]
    assert top[0]["name"] == "Users" and top[0]["size"] == 150
    assert top[1]["name"] == "pf" and top[1]["size"] == 0


# ========== 与 scan_disk 互操作 ==========

def test_interop_files(tmp_path):
    """scan_disk files --cache 直接消费导入缓存，路径以 C:/ 拼接"""
    _, cache = _parse(tmp_path)
    cp = str(tmp_path / "cache.json")
    rc, out, err = _run(SCAN_DISK, "files", "--cache", cp, "-n", "3")
    assert rc == 0, err
    assert out is not None
    largest = out["largest"]
    assert largest[0]["path"] == "C:/pagefile.sys" and largest[0]["size"] == 5000
    paths = {f["path"] for f in largest}
    assert "C:/<user_home>/文件.txt" in paths
    assert "C:/<user_home>/b.log" in paths


def test_interop_find_and_tree(tmp_path):
    """scan_disk find 按目录名定位 + tree 紧凑视图"""
    _, cache = _parse(tmp_path)
    cp = str(tmp_path / "cache.json")

    rc, out, _ = _run(SCAN_DISK, "find", "--cache", cp, "--names", "admin", "pf")
    assert rc == 0
    assert out is not None
    matches = {m["name"]: m for m in out["results"]}
    assert matches["admin"]["size"] == 150 and matches["admin"]["files"] == 2
    assert matches["pf"]["size"] == 0

    rc, out, _ = _run(SCAN_DISK, "tree", "--cache", cp, "--depth", "2", "-n", "5")
    assert rc == 0
    assert out is not None
    children = {c["name"] for c in out["tree"]["children"]}
    assert children == {"Users", "pf"}


def test_interop_caches_builtin_list(tmp_path):
    """caches 内置清单：快照里放一个 node_modules 命中"""
    blob = b"".join([
        rec(1, b"C:\\", 300, pops=0),
        rec(3, b"node_modules", 300, pops=1),  # 空目录：开+关（卷保持开）
        rec(5, "未知 (尚未扫描) 空间".encode("gbk"), 0, pops=2),
    ])
    sns = tmp_path / "snap2.sns"
    sns.write_bytes(blob)
    out_file = tmp_path / "cache2.json"
    rc, out, err = _run(PARSE_SNS, "parse", str(sns), "--json", "--out", str(out_file))
    assert rc == 0, err
    rc, out, _ = _run(SCAN_DISK, "caches", "--cache", str(out_file))
    assert rc == 0
    assert out is not None
    assert out["matches"] == 1
    assert out["results"][0]["name"] == "node_modules"
    assert out["results"][0]["category"] == "build_artifact"


# ========== 健壮性 ==========

def test_truncated_sns_errors(tmp_path):
    """截断的快照：rc=1 + error JSON（可信度门槛：pos==n）"""
    sns = build_sns(tmp_path / "snap.sns")
    good = sns.read_bytes()
    sns.write_bytes(good[: len(good) - 6])  # 掐掉最后的关栈标记
    rc, out, _ = _run(PARSE_SNS, "parse", str(sns), "--json", "--out", str(tmp_path / "c.json"))
    assert rc == 1
    assert out and "error" in out


def test_keep_files_trim(tmp_path):
    """--keep-files 截断 files_top，但 files 计数仍是全量"""
    blob = b"".join(
        [rec(1, b"C:\\", 1000, pops=0)]
        + [rec(2, b"f%03d" % i, 100, pops=1) for i in range(10)]
        + [rec(5, "未知 (尚未扫描) 空间".encode("gbk"), 0, pops=2)]
    )
    sns = tmp_path / "snap3.sns"
    sns.write_bytes(blob)
    out_file = tmp_path / "cache3.json"
    rc, out, err = _run(PARSE_SNS, "parse", str(sns), "--json", "--keep-files", "3", "--out", str(out_file))
    assert rc == 0, err
    cache = json.loads(out_file.read_text(encoding="utf-8"))
    tree = cache["tree"]
    assert tree["files"] == 10                    # 计数全量
    assert len(tree["files_top"]) == 3            # 节点截断
    assert tree["files_top"][0]["name"] == "f000"  # size 相同按原顺序稳定
