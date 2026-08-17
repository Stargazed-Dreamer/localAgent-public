"""PreparedRelease 不可变数据模型（spec-v2-compiler.md Q3 决策）。

13 字段精简集，全部 frozen dataclass + tuple 替代 list 保证 hashable。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileEntry:
    """文件清单条目（tracked 源文件或 generated 产物）"""
    rel_path: str
    sha256: str
    size: int
    source: str  # "tracked" | "generated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "size": self.size,
            "source": self.source,
        }


@dataclass(frozen=True)
class Exemption:
    """敏感内容豁免条目（绑定 path + reason + fingerprint + expires_at）"""
    path: str
    reason: str
    fingerprint: str  # sha256 of exempted content
    expires_at: str   # ISO date

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class GateResult:
    """Gate 执行结果"""
    name: str
    passed: bool
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "details": self.details,
        }


@dataclass(frozen=True)
class PreparedRelease:
    """不可变 release 计划（spec Q3 决策：13 字段精简集）

    prepare_release() 产出，build_release() 消费。
    plan_digest 绑定所有输入字段；任何输入变化让 plan_digest 变化，使旧 approval 失效。
    """
    plan_digest: str
    profile_id: str
    profile_digest: str        # SHA-256 of profile 文件 bytes
    source_commit: str         # git commit sha
    components: tuple[str, ...]
    components_digest: str     # SHA-256 of components selection
    audience: str              # "friend" | "public"
    file_entries: tuple[FileEntry, ...]
    exemptions: tuple[Exemption, ...]
    scan_digest: str           # SHA-256 of scan results
    gates_required: tuple[str, ...]         # 全部 gates 清单（静态 + 构建期）
    gates_results: tuple[GateResult, ...]   # prepare 阶段跑的静态 gates 结果
    created_at: str            # ISO datetime

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的 dict（tuple → list 转换）"""
        return {
            "plan_digest": self.plan_digest,
            "profile_id": self.profile_id,
            "profile_digest": self.profile_digest,
            "source_commit": self.source_commit,
            "components": list(self.components),
            "components_digest": self.components_digest,
            "audience": self.audience,
            "file_entries": [e.to_dict() for e in self.file_entries],
            "exemptions": [e.to_dict() for e in self.exemptions],
            "scan_digest": self.scan_digest,
            "gates_required": list(self.gates_required),
            "gates_results": [g.to_dict() for g in self.gates_results],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PreparedRelease":
        """从 dict 反序列化（list → tuple 转换）"""
        return cls(
            plan_digest=d["plan_digest"],
            profile_id=d["profile_id"],
            profile_digest=d["profile_digest"],
            source_commit=d["source_commit"],
            components=tuple(d["components"]),
            components_digest=d["components_digest"],
            audience=d["audience"],
            file_entries=tuple(FileEntry(**e) for e in d["file_entries"]),
            exemptions=tuple(Exemption(**e) for e in d["exemptions"]),
            scan_digest=d["scan_digest"],
            gates_required=tuple(d["gates_required"]),
            gates_results=tuple(GateResult(**g) for g in d["gates_results"]),
            created_at=d["created_at"],
        )


@dataclass(frozen=True)
class Approval:
    """审批凭据（plan_digest 必须与 PreparedRelease.plan_digest 匹配）"""
    plan_digest: str
    approved_at: str
    approved_by: str
    signature: str | None = None  # phase 2 不实现签名验证，保留字段

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_digest": self.plan_digest,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Approval":
        return cls(
            plan_digest=d["plan_digest"],
            approved_at=d["approved_at"],
            approved_by=d["approved_by"],
            signature=d.get("signature"),
        )


@dataclass(frozen=True)
class Artifact:
    """构建产物元信息（build_release 返回）"""
    plan_digest: str
    zip_path: Path
    zip_sha256: str
    manifest_path: Path
    archive_verification_passed: bool
    built_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_digest": self.plan_digest,
            "zip_path": str(self.zip_path),
            "zip_sha256": self.zip_sha256,
            "manifest_path": str(self.manifest_path),
            "archive_verification_passed": self.archive_verification_passed,
            "built_at": self.built_at,
        }
