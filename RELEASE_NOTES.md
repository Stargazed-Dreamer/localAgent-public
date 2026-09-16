# Release Notes — public-full

**Audience**: public
**Source commit**: `29b3adc7dfc6a50db982b47324fe23155f6b3cac`
**Plan digest**: `09a4f385534aff5295c0c3fcd32bd8a4df03d34aa1c85bc1d5d542046ec7ff4b`
**Built at**: 2026-09-16T16:11:47.364595+00:00

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

- 文件总数：1578
- 总大小：23778035 字节

## ZIP 校验

ZIP 文件的 SHA-256 校验和见同目录下的 `localagent-public-full.zip.sha256` 文件。

验证方法：

```bash
certutil -hashfile localagent-public-full.zip SHA256
# 比对 .zip.sha256 文件中的值
```

## 已知豁免

本包含以下敏感内容豁免（已审计，认为可安全分发）：

- `.agents/skills/bat_writing/SKILL.md` — sensitive_line_skips
- `.agents/skills/bat_writing/references/guide.md` — sensitive_line_skips
- `.agents/skills/deep_research/references/pdf_report_style.md` — sensitive_line_skips
- `.agents/skills/office_docs/references/pdf_basic.md` — sensitive_line_skips
- `client/core/agent/builtin_tools/base.py` — sensitive_line_skips
- `docs/adr/0007-omniparser-removal.md` — sensitive_line_skips
- `docs/changelog-archive.md` — post_process_remove_lines
- `docs/changelog-archive.md` — sensitive_line_skips
- `docs/deployment.md` — sensitive_line_skips
- `docs/dev-workflow.md` — sensitive_line_skips
- `docs/environment-constraints.md` — sensitive_line_skips
- `docs/tools-guide.md` — sensitive_line_skips
- `lib/recorder/processor/stt_engine.py` — sensitive_line_skips
- `server/agent_guide_data.py` — sensitive_line_skips
- `server/approval_review.py` — sensitive_line_skips
- `server/browser/wait_endpoints.py` — sensitive_line_skips
- `tests/approval_screen/test_approval_low_risk.py` — sensitive_line_skips
- `tests/guide_loops/test_inbox.py` — sensitive_line_skips
- `tests/llm_vision/test_model_manager_mindforge.py` — sensitive_line_skips
- `tests/release_ci/test_release_orchestrator.py` — sensitive_line_skips
- `tests/server_endpoints/test_todos.py` — sensitive_line_skips
- `tools/browser/start_debug_browser.py` — sensitive_line_skips
- `tools/deploy/apply_placeholders.py` — sensitive_line_skips
- `tools/file_classifier/file_classifier_guide.md` — sensitive_line_skips
- `tools/image_organizer/prompt_old.txt` — sensitive_line_skips
- `tools/llm/mimo_batch_comment.py` — sensitive_line_skips
- `tools/media_classifier/mimo_lrc_analyze.py` — sensitive_line_skips
- `tools/release/engine/prepare.py` — sensitive_line_skips
- `workspace/disk_manager/SKILL.md` — sensitive_line_skips
- `workspace/disk_manager/references/sns_format.md` — sensitive_line_skips
- `workspace/disk_manager/scripts/backup_env.py` — sensitive_line_skips
- `workspace/disk_manager/scripts/cleanup.py` — sensitive_line_skips
- `workspace/disk_manager/tests/test_parse_sns.py` — sensitive_line_skips
- `workspace/recorder/tools/editor/stt_runner.py` — sensitive_line_skips

## 审计追溯

本构建计划由 `prepare_release()` 产出不可变 `PreparedRelease`，经用户审批后由 `build_release()` 构建产物。plan_digest 绑定所有输入字段，任何输入变化都会让 plan_digest 变化，使旧审批失效。

- **plan_digest**: `09a4f385534aff5295c0c3fcd32bd8a4df03d34aa1c85bc1d5d542046ec7ff4b`
- **profile_id**: `public-full`
- **audience**: `public`
- **source_commit**: `29b3adc7dfc6a50db982b47324fe23155f6b3cac`
- **built_at**: 2026-09-16T16:11:47.364595+00:00

## 许可证

Apache License 2.0 — 版权所有 (c) 2026 <copyright_holder>

详见 `LICENSE` 文件。
