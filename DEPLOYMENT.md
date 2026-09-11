# LocalAgent 部署指南

> Profile: `public-full` | Audience: `public` | 构建时间: 2026-09-11T19:35:51.999160+00:00

## 项目简介

LocalAgent 是一个个人 AI Agent 项目，集合了电脑操控、Agent 工具和日常可自动化工作流。

本包为 **public 受众** 源码分发版，基于 git commit `e4ddebfcd94817ca531399aa6f93270d594025ec` 构建。

## 系统要求

- Python ≥ 3.12
- Windows 10/11（推荐；其他平台未测试）
- CUDA 12.6 兼容 GPU（可选，GPU 推理需要；CPU 模式可用但慢）

## 安装步骤

```bash
# 1. 解压本 ZIP 到目标目录
# 2. 进入项目根目录
cd localagent

# 3. 同步依赖（uv 会自动创建 .venv）
uv sync

# 4. 复制配置模板并填写必要字段
copy config.example.toml config.toml
# 编辑 config.toml 填入你的 API key（可选，无 key 后端仍可启动）
```

## 启动

```bash
# 后端（FastAPI + Uvicorn，端口 8766）
start.bat
# 或手动启动：
uv run python -m server.main

# GUI 客户端（PySide6）
start_client.bat
```

## 配置说明

详见 `config.example.toml` 中各配置项注释。关键配置：

- `[model]` LLM 提供商配置（DeepSeek/OpenAI/智谱等）
- `[ocr]` PaddleOCR 配置
- `[browser]` 调试浏览器配置
- `[memory]` 三层记忆系统配置

## 密钥期望

本包**不提供**任何 API key 或运行时凭据。后端在无 key 配置时应能启动，依赖 key 的功能（LLM 对话、OCR 等）会安全停止并报告缺失 key，不崩溃。

期望行为：`backend-starts-and-dependent-tasks-stop-safely`

## 包含组件

- **disk_manager** v0.0.0 — 
- **recorder** v0.0.0 — 
- **modelscope_model_update** v0.0.0 — 
- **accounting** v0.0.0 — 
- **dev_toolkit** v0.0.0 — 
- **life_design** v0.0.0 — 
- **arknights_gacha** v0.0.0 — 
- **wuwa_gacha** v0.0.0 — 
- **endfield_gacha** v0.0.0 — 
- **yihuan_gacha** v0.0.0 — 

## 文件统计

- 文件总数：1560
- 总大小：23187338 字节
- 源 git commit：`e4ddebfcd94817ca531399aa6f93270d594025ec`

## 审计追溯

- **plan_digest**: `142245832fb9b1a11f4a0e3655a8d52086520afe2636c014ec8156f861bdcf5e`
- **profile_id**: `public-full`
- **audience**: `public`
- **built_at**: 2026-09-11T19:35:51.999160+00:00

plan_digest 是本构建计划的 SHA-256 摘要，绑定所有输入（profile / components / scan / exemptions / source_commit / file_entries）。任何输入变化都会让 plan_digest 变化，使旧审批失效。

## 豁免声明

本包含以下敏感内容豁免（已审计，认为可安全分发）：

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
- `workspace/disk_manager/scripts/backup_env.py` — sensitive_line_skips
- `workspace/disk_manager/scripts/cleanup.py` — sensitive_line_skips
- `workspace/recorder/tools/editor/stt_runner.py` — sensitive_line_skips

## ZIP 校验

ZIP 文件的 SHA-256 校验和见同目录下的 `.zip.sha256` 文件。验证：

```bash
certutil -hashfile localagent-public-full.zip SHA256
# 比对 .zip.sha256 文件中的值
```

## 许可证

Apache License 2.0

- 允许商用、修改和再分发
- 版权所有 (c) 2026 <copyright_holder>

详见 `LICENSE` 文件。
