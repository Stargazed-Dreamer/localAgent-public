"""项目结构扫描与漂移检测

为 AGENTS.md 项目结构章节提供程序化支撑：
  - scan_project_structure() 扫描根目录一二级目录
  - load_baseline() / save_baseline() 维护 data/project_structure.json 映射表
  - diff_structure() 对比当前扫描与 baseline，找出未知/消失路径

agent 调 agent_guide(task_type='system.task_closure') 时，本模块自动扫描并对比 baseline，
若发现未知目录会在响应中标注，agent 在收尾报告中提示用户并补全 baseline。

baseline 文件：data/project_structure.json
首次运行时若 baseline 不存在，本模块会生成初始版本（仅记录当前扫描结果，description 留空待补）。
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("localagent.project_structure")

_PROJECT_ROOT = Path(__file__).parent.parent
BASELINE_FILE = _PROJECT_ROOT / "data" / "project_structure.json"

# 扫描时跳过的目录名（运行时缓存/虚拟环境等，不纳入结构映射）
_SKIP_DIRS = {
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".git",
    "data",
    "weights",
}
# 扫描时跳过的顶层条目（这些是 IDE/运行时产物，不属于项目结构）
_SKIP_TOP_LEVEL = {
    ".venv",
    ".pytest_cache",
    "__pycache__",
    ".git",
    ".ruff_cache",
}
# 二级扫描时跳过的（避免深入虚拟环境/缓存/运行时数据）
# chrome_debug = Chrome 调试实例用户数据（数百个缓存子目录，非项目结构）
_SKIP_SUBDIR_SCAN = {
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    ".ruff_cache",
    "weights",
    "envs",
    "chrome_debug",
    "data",
    "temp",
}
# Runtime/cache directories can appear below otherwise stable top-level modules.
# They are intentionally omitted from the two-level project map as well.


def scan_project_structure(root: Path = _PROJECT_ROOT) -> dict:
    """扫描项目根目录的一二级结构。

    返回:
      {
        "top_level": {"<name>/": {"type": "dir|file", "path": "<abs>"}},
        "subdirs": {"<parent>/": ["<child>/", ...]}
      }

    顶层包含目录和重要文件（.md/.toml/.json/.bat/.py 等配置/入口文件）。
    二级只扫描已知重要目录的子目录，避免深入 .venv/weights 等大目录。
    """
    top_level: dict = {}
    subdirs: dict = {}

    # 顶层条目：目录 + 重要文件
    important_exts = {".md", ".toml", ".json", ".bat", ".py", ".cfg", ".ini", ".txt", ".yml", ".yaml"}
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name in _SKIP_TOP_LEVEL:
            continue
        if entry.is_dir():
            top_level[f"{name}/"] = {"type": "dir", "path": str(entry)}
        elif entry.is_file() and (entry.suffix.lower() in important_exts or name.startswith(".")):
            # 配置/入口文件，记录但不深入
            top_level[name] = {"type": "file", "path": str(entry)}

    # 二级：扫描顶层目录的子目录（只列目录，不列文件，避免噪音）
    for name, info in top_level.items():
        if info["type"] != "dir" or not name.endswith("/"):
            continue
        parent_name = name[:-1]  # 去掉末尾斜杠
        if parent_name in _SKIP_SUBDIR_SCAN:
            continue
        dir_path = root / parent_name
        if not dir_path.is_dir():
            continue
        children: list = []
        try:
            for child in sorted(dir_path.iterdir()):
                if child.name in _SKIP_DIRS:
                    continue
                # 只记录子目录，不记录文件（用户要的是"目录映射"，文件太多噪音大）
                if child.is_dir():
                    children.append(f"{child.name}/")
        except (PermissionError, OSError) as e:
            logger.debug(f"扫描 {dir_path} 失败: {e}")
        if children:
            subdirs[name] = children

    return {"top_level": top_level, "subdirs": subdirs}


def load_baseline(path: Path = BASELINE_FILE) -> dict | None:
    """加载 baseline 映射表。不存在返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"加载 project_structure.json 失败: {e}")
        return None


def save_baseline(baseline: dict, path: Path = BASELINE_FILE) -> None:
    """保存 baseline 映射表。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(f"保存 project_structure.json 失败: {e}")


def diff_structure(baseline: dict, current: dict) -> dict:
    """对比 baseline 与当前扫描结果，找出漂移。

    返回:
      {
        "unknown_paths": [{"path": "...", "kind": "top|sub", "parent": "..."}],
        "missing_paths": [{"path": "...", "kind": "top|sub", "parent": "..."}],
        "summary": "一句话概述"
      }
    """
    unknown: list = []
    missing: list = []

    base_top = baseline.get("top_level", {})
    base_sub = baseline.get("subdirs", {})
    cur_top = current.get("top_level", {})
    cur_sub = current.get("subdirs", {})

    # 顶层对比
    for path in cur_top:
        if path not in base_top:
            unknown.append({"path": path, "kind": "top", "parent": ""})
    for path in base_top:
        if path not in cur_top:
            missing.append({"path": path, "kind": "top", "parent": ""})

    # 二级对比
    # planning_notes/ 下允许用户手动存放规划/路线图文档，不纳入漂移检测
    # （SDD 流程产物走 temp/sdd/，planning_notes/ 是用户手动管理区域）
    _IGNORE_SUBDIR_PARENTS = {"planning_notes/"}
    for parent, children in cur_sub.items():
        if parent in _IGNORE_SUBDIR_PARENTS:
            continue
        base_children = set(base_sub.get(parent, []))
        for child in children:
            if child not in base_children:
                unknown.append({"path": child, "kind": "sub", "parent": parent})
    for parent, children in base_sub.items():
        if parent in _IGNORE_SUBDIR_PARENTS:
            continue
        cur_children = set(cur_sub.get(parent, []))
        for child in children:
            if child not in cur_children:
                missing.append({"path": child, "kind": "sub", "parent": parent})

    parts = []
    if unknown:
        parts.append(f"{len(unknown)} 个未知新增路径")
    if missing:
        parts.append(f"{len(missing)} 个消失路径")
    summary = "、".join(parts) if parts else "无漂移"

    return {
        "unknown_paths": unknown,
        "missing_paths": missing,
        "summary": summary,
    }


def ensure_baseline() -> dict:
    """确保 baseline 存在。不存在则用当前扫描结果生成初始版本（description 留空）。

    返回 baseline dict。首次生成时会写入文件。
    """
    baseline = load_baseline()
    if baseline is None:
        current = scan_project_structure()
        baseline = {
            "version": "1.0",
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
            "note": "首次自动生成，description 待 agent 补全",
            "top_level": {
                path: {"type": info["type"], "description": ""}
                for path, info in current["top_level"].items()
            },
            "subdirs": current["subdirs"],
        }
        save_baseline(baseline)
        logger.info(f"首次生成 project_structure baseline: {BASELINE_FILE}")
    return baseline


def update_baseline_descriptions(updates: dict, path: Path = BASELINE_FILE) -> dict:
    """更新 baseline 中顶层路径的 description。

    Args:
      updates: {"<path>/": "<description>", ...}
    """
    baseline = load_baseline() or ensure_baseline()
    for p, desc in updates.items():
        if p in baseline.get("top_level", {}):
            baseline["top_level"][p]["description"] = desc
        else:
            # 新路径，添加到 baseline
            baseline.setdefault("top_level", {})[p] = {
                "type": "dir" if p.endswith("/") else "file",
                "description": desc,
            }
    baseline["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    save_baseline(baseline, path)
    return baseline


def sync_baseline(add_descriptions: dict | None = None) -> dict:
    """同步 baseline 与磁盘扫描结果（机械同步，不推断意图）。

    操作：
    - 磁盘上不存在的顶层路径（missing top）→ 从 baseline.top_level 移除
    - 磁盘上不存在的二级子目录（missing sub）→ 从 baseline.subdirs[parent] 移除
    - 磁盘新增的顶层路径（unknown top）→ 加入 baseline.top_level（description 取自 add_descriptions 或留空）
    - 磁盘新增的二级子目录（unknown sub）→ 加入 baseline.subdirs[parent]（parent 不存在则新建空数组）

    典型场景：发版前调用一次，把 task_closure 累积的 unknown/missing 全部同步，避免 baseline 漂移扩散。
    nested 路径误入 top_level 的情况（如 `private_vault/X/` 被旧 baseline 当成顶层条目）会被
    自动清理（missing top 删除），同时作为 sub 加入 subdirs["private_vault/"]，保持真实结构。

    Args:
        add_descriptions: {"path/": "description"} 给新加入的顶层路径提供描述（可选）。
                          二级子目录暂不支持描述（subdirs 只存路径列表）。

    Returns:
        {
            "added_top": ["..."],
            "added_sub": ["parent/child"],
            "removed_top": ["..."],
            "removed_sub": ["parent/child"],
            "last_updated": "YYYY-MM-DD",
        }
    """
    baseline = load_baseline() or ensure_baseline()
    current = scan_project_structure()
    diff = diff_structure(baseline, current)

    added_top: list = []
    added_sub: list = []
    removed_top: list = []
    removed_sub: list = []

    descriptions = add_descriptions or {}

    # missing：从 baseline 移除
    for m in diff["missing_paths"]:
        path = m["path"]
        if m["kind"] == "top":
            if path in baseline.get("top_level", {}):
                baseline["top_level"].pop(path, None)
                removed_top.append(path)
        elif m["kind"] == "sub":
            parent = m["parent"]
            if parent in baseline.get("subdirs", {}):
                if path in baseline["subdirs"][parent]:
                    baseline["subdirs"][parent].remove(path)
                    removed_sub.append(f"{parent}{path}")

    # unknown：添加到 baseline
    for u in diff["unknown_paths"]:
        path = u["path"]
        if u["kind"] == "top":
            if path not in baseline.get("top_level", {}):
                baseline.setdefault("top_level", {})[path] = {
                    "type": "dir" if path.endswith("/") else "file",
                    "description": descriptions.get(path, ""),
                }
                added_top.append(path)
        elif u["kind"] == "sub":
            parent = u["parent"]
            baseline.setdefault("subdirs", {})
            if parent not in baseline["subdirs"]:
                baseline["subdirs"][parent] = []
            if path not in baseline["subdirs"][parent]:
                baseline["subdirs"][parent].append(path)
                added_sub.append(f"{parent}{path}")

    baseline["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    save_baseline(baseline)

    return {
        "added_top": added_top,
        "added_sub": added_sub,
        "removed_top": removed_top,
        "removed_sub": removed_sub,
        "last_updated": baseline["last_updated"],
    }


def get_status() -> dict:
    """供 /health 聚合的状态。"""
    baseline = load_baseline()
    return {
        "available": True,
        "baseline_exists": baseline is not None,
        "baseline_entries": len(baseline.get("top_level", {})) if baseline else 0,
        "baseline_updated": baseline.get("last_updated") if baseline else None,
    }
