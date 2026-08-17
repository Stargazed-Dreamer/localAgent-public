# LocalAgent 部署指南

> Profile: `public-full` | Audience: `public` | 构建时间: 2026-08-17T18:04:21.149632+00:00

## 项目简介

LocalAgent 是一个个人 AI Agent 项目，集合了电脑操控、Agent 工具和日常可自动化工作流。

本包为 **public 受众** 源码分发版，基于 git commit `191b379d754143b7a75e2d9c9866dcd53a50a913` 构建。

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

- 文件总数：1462
- 总大小：22221804 字节
- 源 git commit：`191b379d754143b7a75e2d9c9866dcd53a50a913`

## 审计追溯

- **plan_digest**: `a9d545e2131b357f79e6f0ffe41dbbff60c79a9272a939ca2320656e92a439c7`
- **profile_id**: `public-full`
- **audience**: `public`
- **built_at**: 2026-08-17T18:04:21.149632+00:00

plan_digest 是本构建计划的 SHA-256 摘要，绑定所有输入（profile / components / scan / exemptions / source_commit / file_entries）。任何输入变化都会让 plan_digest 变化，使旧审批失效。

## 豁免声明

本包含以下敏感内容豁免（已审计，认为可安全分发）：

- `docs/changelog-archive.md` — post_process_remove_lines
- `docs/changelog-archive.md` — sensitive_line_skips
- `tools/release/engine/prepare.py` — sensitive_line_skips

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
