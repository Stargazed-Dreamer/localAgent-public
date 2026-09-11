# 发布历史

本文件由发布流程自动生成,记录每个公开版本的演进脉络:
版本间包含的私有仓库 commit 数量、开发时间跨度与主要更改摘要
(摘自 CHANGELOG,仅取条目标题)。

<!-- 由 tools/release/engine/history.py 维护,请勿手工编辑 -->

## 0.46.0
- 首次发布,无区间统计
- 主要更改:
  - release 一键编排 + publish 持久副本发布（append-only 公开历史）
  - pre-commit 钩子会重写 `uv.lock` 的 registry 源，静默撤销「lock 用官方源」的决策
  - gh_mirror 清库守护第二层（自动禁用镜像 workflow）上线以来从未生效
