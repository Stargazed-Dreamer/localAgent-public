# Release Notes — public-full

**Audience**: public
**Source commit**: `191b379d754143b7a75e2d9c9866dcd53a50a913`
**Plan digest**: `a9d545e2131b357f79e6f0ffe41dbbff60c79a9272a939ca2320656e92a439c7`
**Built at**: 2026-08-17T18:04:21.149632+00:00

## 组件清单

### disk_manager v0.0.0


### recorder v0.0.0


### modelscope_model_update v0.0.0


### accounting v0.0.0


### dev_toolkit v0.0.0


### life_design v0.0.0


### arknights_gacha v0.0.0


### wuwa_gacha v0.0.0


### endfield_gacha v0.0.0


### yihuan_gacha v0.0.0



## 文件统计

- 文件总数：1462
- 总大小：22221804 字节

## ZIP 校验

ZIP 文件的 SHA-256 校验和见同目录下的 `localagent-public-full.zip.sha256` 文件。

验证方法：

```bash
certutil -hashfile localagent-public-full.zip SHA256
# 比对 .zip.sha256 文件中的值
```

## 已知豁免

本包含以下敏感内容豁免（已审计，认为可安全分发）：

- `docs/changelog-archive.md` — post_process_remove_lines
- `docs/changelog-archive.md` — sensitive_line_skips
- `tools/release/engine/prepare.py` — sensitive_line_skips

## 审计追溯

本构建计划由 `prepare_release()` 产出不可变 `PreparedRelease`，经用户审批后由 `build_release()` 构建产物。plan_digest 绑定所有输入字段，任何输入变化都会让 plan_digest 变化，使旧审批失效。

- **plan_digest**: `a9d545e2131b357f79e6f0ffe41dbbff60c79a9272a939ca2320656e92a439c7`
- **profile_id**: `public-full`
- **audience**: `public`
- **source_commit**: `191b379d754143b7a75e2d9c9866dcd53a50a913`
- **built_at**: 2026-08-17T18:04:21.149632+00:00

## 许可证

Apache License 2.0 — 版权所有 (c) 2026 <copyright_holder>

详见 `LICENSE` 文件。
