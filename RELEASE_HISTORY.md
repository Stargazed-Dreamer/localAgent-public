# 发布历史

本文件由发布流程自动生成,记录每个公开版本的演进脉络:
版本间包含的私有仓库 commit 数量、开发时间跨度与主要更改摘要
(摘自 CHANGELOG,仅取条目标题)。

<!-- 由 tools/release/engine/history.py 维护,请勿手工编辑 -->

## 0.48.0
- 自上次发布以来:私有仓库 **42 个 commit**(起点 2026-09-12)
- 主要更改:
  - 新增 docs/project-intro.md 深度项目导览（第一批：第 0-5 章 + 附录 D 25 问，方案与进度见 temp/sdd/project-intro/plan.md）
  - project-intro 第二、三批完成（第 6-15 章九个新章，文档现 1312 行）
  - project-intro 第四批完成，全文交付（第 16-20 章 + 附录 A/B，定稿 1776 行）
  - 新增 x.com(Twitter) 视频下载工具 `tools/x_video_dl.py`（savetwitter.net 免梯子链路，`tools_manifest.json` 新增 `media_dl` 媒体下载分类）
  - AGENTS.md 新增「文档防过时规范」+ project_rules.md 新增「文档与注释变更」检查清单（双镜像同步）
  - 屏幕授权 watchdog 升级请求不再被已有授权短路（`server/overlay_client.py`）
  - 文档与代码一致性集中整改（对照外部实测文档 project-intro.md 的"文档与代码不一致清单"逐条在当前仓库核实后修正）
  - 清理 zcode_plugins/watchdog ZCode 插件的全部文档引用（用户确认插件已废弃移除、完全不可用）
  - 删除 4 个零引用 REST 端点（端点冗余清理 wip_1f161bdc 收尾决策）
