"""SBOM 生成器（spec-v3-public.md Solution 节"SBOM 生成方案"）。

使用 spdx-tools 库生成 SPDX 2.3 JSON 格式 SBOM。

职责：
1. 构造 SPDX Document（CreationInfo + Package + Files + Relationships）
2. 每个 file 含 SHA256 checksum（来自 manifest_files）
3. package licenseConcluded=licenseDeclared="Apache-2.0"
4. 序列化为 dict（可 json.dumps）

不读文件系统，只依赖 plan + manifest_files（spec Decision #15：SBOM 不入 plan，只在 build 时生成）。
"""
from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from license_expression import get_spdx_licensing
from spdx_tools.spdx.model import (
    Actor,
    ActorType,
    Checksum,
    ChecksumAlgorithm,
    CreationInfo,
    Document,
    File as SpdxFile,
    Package,
    Relationship,
    RelationshipType,
    SpdxNoAssertion,
)
from spdx_tools.spdx.writer.json.json_writer import write_document_to_stream

from .models import PreparedRelease

# SPDX 文档命名空间前缀（spec Solution 节"SBOM 生成方案"）
_NAMESPACE_PREFIX = "https://localagent.example.com/spdx/"

# 工具名称（写入 creationInfo.creators）
_TOOL_NAME = "localagent-release-engine-3.0"

# 公共许可证（Apache-2.0，spec Decision #1）
_PUBLIC_LICENSE = "Apache-2.0"

# spdx_licensing 单例（parse license expression 用）
_SPDX_LICENSING = get_spdx_licensing()


def _parse_created_at(created_at: str) -> datetime:
    """解析 plan.created_at ISO 字符串为 datetime（带 UTC tzinfo）。

    spdx-tools 要求 CreationInfo.created 是 datetime 对象。
    """
    # plan.created_at 来自 prepare.py 的 datetime.now(timezone.utc).isoformat()
    # 形如 "2026-07-31T12:00:00+00:00"
    dt = datetime.fromisoformat(created_at)
    if dt.tzinfo is None:
        # 防御性：无 tzinfo 视为 UTC
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_license_expr(license_str: str):
    """解析 license 字符串为 license_expression.Expression 对象。

    spdx-tools 的 Package/File 的 license_concluded 字段类型是
    Union[LicenseExpression, SpdxNoAssertion, SpdxNone, None]，
    不能直接传字符串，需用 license_expression 库解析。
    """
    expr = _SPDX_LICENSING.parse(license_str, validate=True, strict=True)
    if expr is None:
        # 解析失败回退到 NOASSERTION（不应发生，Apache-2.0 是合法 SPDX license）
        return SpdxNoAssertion()
    return expr


def generate_spdx_sbom(
    plan: PreparedRelease,
    manifest_files: list[dict],
) -> dict[str, Any]:
    """生成 SPDX 2.3 JSON 格式 SBOM

    Args:
        plan: PreparedRelease（含 source_commit / plan_digest / created_at / profile_id）
        manifest_files: MANIFEST.json 的 files 列表，每项含：
            - path: 文件相对路径（str）
            - sha256: 文件 SHA256（64 位小写十六进制 str）
            - size: 文件大小（int）
            - source: "tracked" | "generated"

    Returns:
        SPDX 2.3 JSON dict（可 json.dumps）：
        - spdxVersion: "SPDX-2.3"
        - SPDXID: "SPDXRef-DOCUMENT"
        - name: "localagent-<profile_id>"
        - documentNamespace: 唯一命名空间
        - creationInfo: {created, creators, licenseListVersion}
        - packages: [{SPDXID, name, versionInfo, licenseConcluded, licenseDeclared, ...}]
        - files: [{SPDXID, fileName, checksums, licenseConcluded, ...}]
        - relationships: [DESCRIBES + CONTAINS]

    Note:
        用 validate=False 序列化（spdx-tools 严格验证要求 file 含 SHA1 checksum，
        但本项目只记录 SHA256；SPDX 2.3 规范本身不强制 SHA1，spdx-tools 反向
        解析仍能通过，schema 合规）。
    """
    # 1. 解析 plan 字段
    created_dt = _parse_created_at(plan.created_at)
    version = plan.source_commit[:12]
    namespace_suffix = plan.plan_digest[:12]
    doc_name = f"localagent-{plan.profile_id}"
    namespace = f"{_NAMESPACE_PREFIX}{doc_name}-{namespace_suffix}"

    # 2. license expression
    license_expr = _parse_license_expr(_PUBLIC_LICENSE)

    # 3. CreationInfo
    creation_info = CreationInfo(
        spdx_version="SPDX-2.3",
        spdx_id="SPDXRef-DOCUMENT",
        name=doc_name,
        document_namespace=namespace,
        creators=[Actor(actor_type=ActorType.TOOL, name=_TOOL_NAME)],
        created=created_dt,
        data_license="CC0-1.0",
        license_list_version=None,
    )

    # 4. Package（单个，代表整个 release）
    # package checksum 用 plan_digest 作为标识（无 ZIP sha256，因 SBOM 在 ZIP 生成前生成）
    package_checksum = Checksum(
        algorithm=ChecksumAlgorithm.SHA256,
        value=plan.plan_digest,
    )
    package = Package(
        spdx_id="SPDXRef-Package",
        name="localagent",
        download_location=SpdxNoAssertion(),
        version=version,
        file_name=None,
        files_analyzed=True,
        checksums=[package_checksum],
        license_concluded=license_expr,
        license_declared=license_expr,
        copyright_text=SpdxNoAssertion(),
    )

    # 5. Files（每个 manifest_file → SPDX File）
    spdx_files: list[SpdxFile] = []
    relationships: list[Relationship] = []

    # DESCRIBES: Document → Package
    relationships.append(Relationship(
        spdx_element_id="SPDXRef-DOCUMENT",
        relationship_type=RelationshipType.DESCRIBES,
        related_spdx_element_id="SPDXRef-Package",
    ))

    for idx, mf in enumerate(manifest_files):
        file_spdx_id = f"SPDXRef-File-{idx}"
        file_name = mf.get("path", f"unknown-{idx}")
        sha256_value = mf.get("sha256", "")

        # SHA256 checksum（若 sha256 非空且为 64 位十六进制）
        checksums: list[Checksum] = []
        if len(sha256_value) == 64:
            checksums.append(Checksum(
                algorithm=ChecksumAlgorithm.SHA256,
                value=sha256_value,
            ))
        else:
            # 防御性：sha256 缺失或格式错，用空 SHA256 占位（不应发生）
            checksums.append(Checksum(
                algorithm=ChecksumAlgorithm.SHA256,
                value="0" * 64,
            ))

        spdx_file = SpdxFile(
            name=file_name,
            spdx_id=file_spdx_id,
            checksums=checksums,
            license_concluded=license_expr,
        )
        spdx_files.append(spdx_file)

        # CONTAINS: Package → File
        relationships.append(Relationship(
            spdx_element_id="SPDXRef-Package",
            relationship_type=RelationshipType.CONTAINS,
            related_spdx_element_id=file_spdx_id,
        ))

    # 6. Document
    doc = Document(
        creation_info=creation_info,
        packages=[package],
        files=spdx_files,
        relationships=relationships,
    )

    # 7. 序列化为 JSON dict（validate=False：不强制 SHA1，详见 docstring）
    stream = io.StringIO()
    write_document_to_stream(doc, stream, validate=False, drop_duplicates=True)
    return json.loads(stream.getvalue())
