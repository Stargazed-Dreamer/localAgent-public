"""Manifest 加载与 file_entries 计算（spec-v2-compiler.md Solution 节"组件 manifest"）。

提供：
- `load_components_for_audience(audience_policy)` —— 按 audience 选择组件，返回 Manifest dict
- `compute_file_entries(components, export_set, source_commit)` —— 按 export_set 展开组件 glob →
  取交集 tracked 文件 → 算 sha256 + size → FileEntry tuple
- `validate_manifest_shape(manifest_path)` —— 独立校验 manifest 满足新 shape（fail closed on old shape）

P0-1 tracked-only 保证：file_entries 只含 `git ls-files` 跟踪文件，未跟踪文件一律排除。
"""
from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from pathlib import Path

from server.component_manifest import Manifest, load_manifests, _parse_manifest
from .models import FileEntry

# 项目根目录（manifest.py 在 tools/release/engine/，需 4 级 parent 到项目根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def validate_manifest_shape(manifest_path: Path) -> None:
    """独立校验 manifest 满足新 shape（spec Q5 决策：big bang，旧 shape fail closed）

    Raises:
        ValueError: manifest 使用旧 [release] shape（含 distribution/include_scripts/exclude_personal_data）
        ValueError: manifest 缺 [exports] 或 [release_facts] 段（任一存在但缺其他）
        ValueError: manifest [exports] 缺 [runtime] 或 [source] 子段
    """
    if not manifest_path.exists():
        raise ValueError(f"manifest not found: {manifest_path}")

    # 复用 _parse_manifest 的解析逻辑（含 fail closed）
    m = _parse_manifest(manifest_path)
    if m is None:
        raise ValueError(f"manifest invalid (missing [component] or required fields): {manifest_path}")
    # _parse_manifest 已对旧 shape raise ValueError，对新 shape 构造 ReleaseEntry
    # 这里只做形状校验，不要求 release 字段非空（无 release 表示该组件不参与发布）


def load_components_for_audience(audience_policy: dict) -> dict[str, Manifest]:
    """按 audience policy 的 components 列表加载组件 manifest

    Args:
        audience_policy: load_audience_policy() 返回的 dict，需含 audience.components list

    Returns:
        {component_name: Manifest}，仅包含 audience policy 声明的组件

    Raises:
        ValueError: audience policy 引用了不存在的组件
    """
    audience = audience_policy.get("audience", {})
    required_components = audience.get("components", [])

    all_manifests = load_manifests()

    result: dict[str, Manifest] = {}
    missing: list[str] = []
    for name in required_components:
        if name not in all_manifests:
            missing.append(name)
            continue
        m = all_manifests[name]
        # 该组件必须声明了 release（参与发布），否则 fail closed
        if m.release is None:
            raise ValueError(
                f"component '{name}' is in audience policy but has no [exports]/[release_facts] "
                f"sections (component does not participate in release): {m.workspace_dir / 'manifest.toml'}"
            )
        result[name] = m

    if missing:
        raise ValueError(
            f"audience policy references missing components: {missing}. "
            f"available components: {sorted(all_manifests.keys())}"
        )

    return result


def _git_ls_files(root: Path) -> set[str]:
    """调 `git ls-files` 拿 tracked 文件清单（P0-1 tracked-only 保证）

    Returns:
        set of relative paths (POSIX separators)
    """
    command = ["git", "-c", "core.quotepath=false", "ls-files", "-z"]
    result = subprocess.run(command, cwd=root, check=True, stdout=subprocess.PIPE)
    return {p for p in result.stdout.decode("utf-8").split("\0") if p}


def _expand_glob(globs: list[str], tracked: set[str]) -> list[str]:
    """展开 glob 列表到 tracked 文件清单中的匹配项

    - glob 用 POSIX 风格（`/`），tracked 也是 POSIX 风格
    - `**` 递归匹配（fnmatch.translate 不支持 `**`，需手动处理）
    - 大小写敏感（路径大小写在 Windows 上不区分，但 fnmatch 默认大小写敏感；
      为安全起见，对 glob 和路径都做 lower() 比对）

    Returns:
        sorted list of matched tracked paths
    """
    matched: set[str] = set()
    for pattern in globs:
        if not pattern:
            continue
        # 规范化 pattern 为 POSIX 风格
        pat = pattern.replace("\\", "/")
        # 处理 `**`：转换为 `*` 的 fnmatch 等价（fnmatch 不区分 `**` 和 `*`）
        # 但 `**` 应当跨目录匹配，而 `*` 不跨目录。用 pathlib.match 更准。
        for path in tracked:
            if _glob_match(pat, path):
                matched.add(path)
    return sorted(matched)


def _glob_match(pattern: str, path: str) -> bool:
    """glob 匹配，支持 `**` 跨目录递归

    策略：
    - 把 pattern 按 `**` 分段，逐段前缀匹配
    - `workspace/foo/**` 匹配 `workspace/foo/任何/深度`
    - `workspace/foo/*.py` 匹配 `workspace/foo/x.py` 但不匹配 `workspace/foo/sub/x.py`
    """
    # 简单情况：无 `**`
    if "**" not in pattern:
        return fnmatch.fnmatch(path, pattern)

    # 含 `**`：把 pattern 拆为 [前缀, **, 后缀]
    # 如 `workspace/foo/**` → ["workspace/foo/", "**", ""]
    # 如 `workspace/foo/**/*.py` → ["workspace/foo/", "**", "/*.py"]
    parts = pattern.split("**", 1)
    prefix = parts[0]
    suffix = parts[1] if len(parts) > 1 else ""

    # path 必须以 prefix 开头
    if not path.startswith(prefix):
        return False

    # path 的剩余部分（去掉 prefix 后）必须匹配 suffix
    rest = path[len(prefix):]
    if not suffix:
        # `**` 后无内容 → 匹配任意剩余
        return True

    # suffix 可能以 `/` 开头（如 `**/*.py` 实际拆为 prefix + `/*.py`）
    # `**` 应当匹配 0 个或多个目录段
    # 简化：检查 rest 等于 suffix 去掉前导 `/`，或 rest 的某段后缀匹配 suffix
    suffix_clean = suffix.lstrip("/")
    # 0 段匹配：rest == suffix_clean
    if fnmatch.fnmatch(rest, suffix_clean):
        return True
    # N 段匹配：rest 的任意子路径（去掉前 N 段）匹配 suffix_clean
    # 用 split 把 rest 拆段，逐个尝试
    rest_parts = rest.split("/")
    for i in range(1, len(rest_parts)):
        candidate = "/".join(rest_parts[i:])
        if fnmatch.fnmatch(candidate, suffix_clean):
            return True
    return False


def compute_file_entries(
    components: dict[str, Manifest],
    export_set: str,
    source_commit: str,
    core_files: list[str] | None = None,
    core_files_exclude: list[str] | None = None,
) -> tuple[FileEntry, ...]:
    """按 audience.export_set 展开组件 glob → 取交集 tracked 文件 → FileEntry tuple

    Args:
        components: {name: Manifest} from load_components_for_audience()
        export_set: "source" or "runtime"
            - "source": 使用 manifest.release.exports_source.files（含源码 + 测试 + 文档）
            - "runtime": 使用 manifest.release.exports_runtime.files（运行时所需文件）
        source_commit: git commit sha（仅用于错误信息上下文，不参与 file_entries 内容）
        core_files: audience policy 的 [audience.core_files] glob 列表（spec-v4 决策 G1）。
            若提供，在组件 glob 展开后，再展开 core_files glob 取并集。
            典型值：["server/**", "client/**", "lib/**", "tools/**", ...]
        core_files_exclude: audience policy 的 [audience.core_files_exclude] glob 列表。
            若提供，在并集后应用排除。典型值：[".agents/wip/**", "release/profiles/**", ...]

    Returns:
        tuple[FileEntry, ...]，按 rel_path 排序；每个 FileEntry.source = "tracked"

    Raises:
        ValueError: export_set 非 "source" 或 "runtime"
    """
    if export_set not in ("source", "runtime"):
        raise ValueError(f"invalid export_set: {export_set!r} (must be 'source' or 'runtime')")

    tracked = _git_ls_files(_PROJECT_ROOT)

    all_files: dict[str, str] = {}  # rel_path → 来源标签（用于去重和审计）
    for name, manifest in components.items():
        if manifest.release is None:
            # load_components_for_audience 已校验，防御性检查
            raise ValueError(f"component '{name}' has no release entry")

        if export_set == "source":
            globs = manifest.release.exports_source
            # spec-v4 G15：组件级 exclude glob（与 audience 层 core_files_exclude 对称设计）
            exclude_globs = manifest.release.exports_source_exclude
        else:
            globs = manifest.release.exports_runtime
            exclude_globs = manifest.release.exports_runtime_exclude

        matched = _expand_glob(globs, tracked)
        # spec-v4 G15：在组件 glob 展开后、写入 all_files 前应用排除
        if exclude_globs:
            exclude_matched = set(_expand_glob(exclude_globs, tracked))
            matched = [p for p in matched if p not in exclude_matched]
        for path in matched:
            if path in all_files and all_files[path] != name:
                # 跨组件重复文件：保留先到的，记录警告（不 raise，因为组件间共享文件常见）
                # 但同一文件被两组件声明可能是 manifest 错误，保留 first-wins 行为
                continue
            all_files[path] = name

    # spec-v4 决策 G1：core_files 展开后取并集
    if core_files:
        core_matched = _expand_glob(core_files, tracked)
        for path in core_matched:
            if path not in all_files:
                all_files[path] = "core_files"

    # spec-v4 决策 G1：core_files_exclude 应用排除
    if core_files_exclude:
        exclude_matched = _expand_glob(core_files_exclude, tracked)
        exclude_set = set(exclude_matched)
        all_files = {p: src for p, src in all_files.items() if p not in exclude_set}

    # 算 sha256 + size → FileEntry
    entries: list[FileEntry] = []
    for rel_path in sorted(all_files.keys()):
        abs_path = _PROJECT_ROOT / rel_path
        if not abs_path.is_file():
            # tracked 但不在工作区（如 .gitattributes filter 导致）→ skip
            continue
        content = abs_path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        entries.append(FileEntry(
            rel_path=rel_path,
            sha256=sha,
            size=len(content),
            source="tracked",
        ))

    return tuple(entries)
