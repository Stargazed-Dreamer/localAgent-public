"""组件化 Manifest 系统 - 组件封面声明加载器（ADR-0027 共享层）

扫描 workspace/<module>/manifest.toml，解析为 Manifest 数据结构。
manifest 是组件的唯一封面声明，集中声明 4 个插入点入口位置：
  - loop_tasks: Loop 任务插入点
  - agent_guide: agent_guide 路由插入点
  - client_panel: client 面板插入点
  - skill: skill 文件插入点

主代码库不直接 import 任何 workspace 组件，删除 workspace/<module>/ 后
manifest 消失，4 个插入点自动注销。

设计原则：
- manifest 是封面/索引，不是实现：声明入口位置（file + entries_var），
  具体实现仍由各插入点文件承担（loop_actions.py 含 lambda trigger，TOML 无法表达）
- 双轨制：核心 skill 保留 .agents/skills/ 手工维护，组件化模块走 manifest
- 启动时扫描，结果缓存，不支持热重载（与现有 loop_actions.py 加载时机一致）

ADR-0027: 此模块从 server/component_manifest.py 迁移到 lib/，server 和 client
都从此模块加载 manifest。server/component_manifest.py 保留为兼容性重导出层。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

import toml

logger = logging.getLogger(__name__)

# 项目根：lib/component_manifest.py → parents[1] = localAgent/
# （原 server/component_manifest.py 用 parent.parent，lib 层等价为 parents[1]）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_WORKSPACE_DIR = _PROJECT_ROOT / "workspace"


# ========== 插入点入口 dataclass ==========

@dataclass
class LoopTasksEntry:
    """Loop 任务插入点入口"""
    file: str               # 入口文件（相对 workspace/<module>/）
    entries_var: str        # 入口变量名（如 LOOP_TASK_DEFS）


@dataclass
class AgentGuideEntry:
    """agent_guide 路由插入点入口"""
    file: str
    entries_var: str        # 如 GUIDE_REGISTRY_ENTRIES


@dataclass
class ClientPanelEntry:
    """client 面板插入点入口"""
    file: str
    class_name: str         # PanelBase 子类名
    category: str = "tools"
    order: int = 100


@dataclass
class SkillEntry:
    """skill 文件插入点入口"""
    file: str
    task_type: str          # 如 recurring.<component>
    index_section: str = "components"  # 程序化 _index.md 归类


@dataclass
class ConfigSchemaEntry:
    """配置 schema 插入点入口"""
    file: str               # 配置 schema 文件（相对 workspace/<module>/）


@dataclass
class WatchEntry:
    """数据文件监听"""
    paths: list[str] = field(default_factory=list)


@dataclass
class ReleaseEntry:
    """发布元数据（新 shape，spec-v2-compiler.md Q5 决策）

    组件声明中立事实（exports + release_facts），由 audience policy 决定交付内容。
    旧 shape（distribution / include_scripts / exclude_personal_data）已删除，big bang 一次性重写。

    spec-v4 G15：新增 exports_source_exclude / exports_runtime_exclude 字段，
    与 audience 层 core_files_exclude 对称设计。组件可通过 files_exclude 排除特定文件，
    避免在 audience 层用 core_files_exclude 处理组件内部文件（职责清晰）。
    """
    exports_runtime: list[str]            # exports.runtime.files（组件运行时所需文件 glob）
    exports_source: list[str]             # exports.source.files（组件源码交付文件 glob，含 tests/docs）
    contains_personal_data: bool          # release_facts.contains_personal_data
    requires_external_credentials: bool   # release_facts.requires_external_credentials
    license_class: str                    # release_facts.license_class
    # 组件专属依赖（不在 pyproject.toml import 扫描中体现，如未 git-tracked 的模块）
    extra_deps: list[str] = field(default_factory=list)
    # spec-v4 G15：组件级 exclude glob（与 audience 层 core_files_exclude 对称设计）
    # exports.source.files_exclude（可选，缺失返回空 list，向后兼容）
    exports_source_exclude: list[str] = field(default_factory=list)
    # spec-v4 G15：exports.runtime.files_exclude（对称字段，可选）
    exports_runtime_exclude: list[str] = field(default_factory=list)


@dataclass
class HealthCheckEntry:
    """健康检查插入点

    声明组件的健康检查入口，health.py 启动时动态调用。
    替代主代码库中硬编码的 _get_<module>_status() 函数。
    """
    file: str           # 健康检查文件（相对 workspace/<module>/）
    function: str       # 调用的函数名（返回 dict）


@dataclass
class LlmUseCaseEntry:
    """LLM use case 声明式注册条目"""
    name: str
    scope: str                  # "llm" | "vl" | "aigc_image" | "aigc_video"
    sensitive: bool
    default_tier: tuple[int, int]
    desc: str


@dataclass
class Manifest:
    """组件 Manifest 数据结构

    含 component 元信息 + 插入点入口 + 可选配置 schema/watch/release/health_check/llm_use_cases。
    所有插入点入口可选；某个入口为 None 表示该组件不接入该插入点。
    """
    name: str
    version: str
    description: str
    workspace_dir: Path
    project_tag: str | None = None
    enabled: bool = True
    loop_tasks: LoopTasksEntry | None = None
    agent_guide: AgentGuideEntry | None = None
    client_panel: ClientPanelEntry | None = None
    skill: SkillEntry | None = None
    config_schema: ConfigSchemaEntry | None = None
    watch: WatchEntry | None = None
    release: ReleaseEntry | None = None
    health_check: HealthCheckEntry | None = None
    llm_use_cases: list[LlmUseCaseEntry] = field(default_factory=list)


# ========== 加载器 ==========

_manifests_cache: dict[str, Manifest] | None = None
_cache_lock = threading.Lock()


def _parse_manifest(manifest_path: Path) -> Manifest | None:
    """解析单个 manifest.toml 文件，失败返回 None（静默跳过 + warning）"""
    try:
        data = toml.load(manifest_path)
    except Exception as e:
        logger.warning("manifest 解析失败 %s: %s", manifest_path, e)
        return None

    comp = data.get("component")
    if not comp or not isinstance(comp, dict):
        logger.warning("manifest 缺 [component] section: %s", manifest_path)
        return None

    name = comp.get("name")
    version = comp.get("version")
    description = comp.get("description")
    if not (name and version and description):
        logger.warning("manifest [component] 缺必填字段 name/version/description: %s", manifest_path)
        return None

    # 解析 4 个插入点入口
    loop_tasks = None
    if "loop_tasks" in data and isinstance(data["loop_tasks"], dict):
        d = data["loop_tasks"]
        if d.get("file") and d.get("entries_var"):
            loop_tasks = LoopTasksEntry(file=d["file"], entries_var=d["entries_var"])

    agent_guide = None
    if "agent_guide" in data and isinstance(data["agent_guide"], dict):
        d = data["agent_guide"]
        if d.get("file") and d.get("entries_var"):
            agent_guide = AgentGuideEntry(file=d["file"], entries_var=d["entries_var"])

    client_panel = None
    if "client_panel" in data and isinstance(data["client_panel"], dict):
        d = data["client_panel"]
        if d.get("file") and d.get("class"):
            client_panel = ClientPanelEntry(
                file=d["file"],
                class_name=d["class"],
                category=d.get("category", "tools"),
                order=int(d.get("order", 100)),
            )

    skill = None
    if "skill" in data and isinstance(data["skill"], dict):
        d = data["skill"]
        if d.get("file") and d.get("task_type"):
            skill = SkillEntry(
                file=d["file"],
                task_type=d["task_type"],
                index_section=d.get("index_section", "components"),
            )

    config_schema = None
    if "config_schema" in data and isinstance(data["config_schema"], dict):
        d = data["config_schema"]
        if d.get("file"):
            config_schema = ConfigSchemaEntry(file=d["file"])

    watch = None
    if "watch" in data and isinstance(data["watch"], dict):
        d = data["watch"]
        paths = d.get("paths", [])
        if isinstance(paths, list):
            watch = WatchEntry(paths=paths)

    release = None
    # 新 shape: [exports.runtime] / [exports.source] / [release_facts]（spec-v2-compiler.md Q5）
    # 旧 shape [release] 段（distribution / include_scripts / exclude_personal_data）→ fail closed
    if "release" in data:
        old_section = data["release"]
        if isinstance(old_section, dict) and any(
            k in old_section for k in ("distribution", "include_scripts", "exclude_personal_data")
        ):
            raise ValueError(
                f"manifest uses deprecated [release] shape (distribution/include_scripts/"
                f"exclude_personal_data). Big bang 迁移到 [exports.runtime]/[exports.source]/"
                f"[release_facts] required. File: {manifest_path}"
            )

    # 新 shape 解析：[exports] 段（runtime + source）+ [release_facts] 段
    exports_data = data.get("exports")
    release_facts_data = data.get("release_facts")

    # 三段都缺 → 视为该组件不参与发布（release=None）
    # 任一存在但缺其他 → fail closed
    has_exports = isinstance(exports_data, dict)
    has_release_facts = isinstance(release_facts_data, dict)

    if has_exports or has_release_facts:
        if not has_exports:
            raise ValueError(
                f"manifest has [release_facts] but missing [exports] section: {manifest_path}"
            )
        if not has_release_facts:
            raise ValueError(
                f"manifest has [exports] but missing [release_facts] section: {manifest_path}"
            )

        runtime_data = exports_data.get("runtime", {})
        source_data = exports_data.get("source", {})
        if not isinstance(runtime_data, dict) or not isinstance(source_data, dict):
            raise ValueError(
                f"manifest [exports] must contain [runtime] and [source] subtables: {manifest_path}"
            )

        release = ReleaseEntry(
            exports_runtime=list(runtime_data.get("files", [])),
            exports_source=list(source_data.get("files", [])),
            contains_personal_data=bool(release_facts_data.get("contains_personal_data", False)),
            requires_external_credentials=bool(release_facts_data.get("requires_external_credentials", False)),
            license_class=str(release_facts_data.get("license_class", "internal-review")),
            extra_deps=list(release_facts_data.get("extra_deps", [])),
            # spec-v4 G15：解析 [exports.source].files_exclude / [exports.runtime].files_exclude
            # 可选字段，缺失返回空 list（向后兼容，无 files_exclude 的组件不受影响）
            exports_source_exclude=list(source_data.get("files_exclude", [])),
            exports_runtime_exclude=list(runtime_data.get("files_exclude", [])),
        )

    health_check = None
    if "health_check" in data and isinstance(data["health_check"], dict):
        d = data["health_check"]
        if d.get("file") and d.get("function"):
            health_check = HealthCheckEntry(
                file=d["file"],
                function=d["function"],
            )

    llm_use_cases: list[LlmUseCaseEntry] = []
    if "llm_use_cases" in data:
        uc_list = data["llm_use_cases"]
        if isinstance(uc_list, list):
            for uc in uc_list:
                if not isinstance(uc, dict):
                    continue
                uc_name = uc.get("name")
                if not uc_name:
                    continue
                tier = uc.get("default_tier", [3, 5])
                tier = (int(tier[0]), int(tier[1])) if isinstance(tier, list) and len(tier) == 2 else (3, 5)
                llm_use_cases.append(LlmUseCaseEntry(
                    name=uc_name,
                    scope=uc.get("scope", "llm"),
                    sensitive=bool(uc.get("sensitive", False)),
                    default_tier=tier,
                    desc=uc.get("desc", ""),
                ))

    return Manifest(
        name=name,
        version=version,
        description=description,
        workspace_dir=manifest_path.parent,
        project_tag=comp.get("project_tag"),
        enabled=bool(comp.get("enabled", True)),
        loop_tasks=loop_tasks,
        agent_guide=agent_guide,
        client_panel=client_panel,
        skill=skill,
        config_schema=config_schema,
        watch=watch,
        release=release,
        health_check=health_check,
        llm_use_cases=llm_use_cases,
    )


def _scan_workspace() -> dict[str, Manifest]:
    """扫描 workspace/*/manifest.toml，返回 {name: Manifest}"""
    result: dict[str, Manifest] = {}
    if not _WORKSPACE_DIR.exists():
        return result

    for child in sorted(_WORKSPACE_DIR.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        manifest_path = child / "manifest.toml"
        if not manifest_path.exists():
            continue
        m = _parse_manifest(manifest_path)
        if m is None:
            continue
        # 重名检测：后加载的覆盖前者，但记录 warning
        if m.name in result:
            logger.warning("manifest 名称冲突 %s: %s vs %s", m.name, result[m.name].workspace_dir, m.workspace_dir)
        result[m.name] = m

    return result


def load_manifests() -> dict[str, Manifest]:
    """加载所有组件 manifest，结果缓存

    启动时调用一次，后续调用返回缓存。删除 workspace/<module>/ 后需调 reset_cache() 重扫。
    """
    global _manifests_cache
    if _manifests_cache is not None:
        return _manifests_cache
    with _cache_lock:
        if _manifests_cache is not None:
            return _manifests_cache
        _manifests_cache = _scan_workspace()
    return _manifests_cache


def get_component(name: str) -> Manifest | None:
    """按名查询组件 manifest"""
    return load_manifests().get(name)


def get_components_by_capability(capability: str) -> list[Manifest]:
    """按插入点能力查询组件（capability ∈ loop_tasks/agent_guide/client_panel/skill/config_schema）

    仅返回 enabled=True 且该能力入口已声明的组件。
    """
    manifests = load_manifests()
    result: list[Manifest] = []
    for m in manifests.values():
        if not m.enabled:
            continue
        attr = getattr(m, capability, None)
        if attr is not None:
            result.append(m)
    return result

def reset_cache() -> None:
    """重置 manifest 缓存（测试用，或强制重新扫描）"""
    global _manifests_cache
    with _cache_lock:
        _manifests_cache = None
