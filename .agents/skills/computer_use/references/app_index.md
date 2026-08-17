# 软件经验索引（computer_use/apps/）

> 人类可读汇总。agent 查询走 `screen_match_app(process_name=...)` MCP 工具，不依赖此文件。

## 已记录软件

| process_name | 软件名 | UIA 友好度 | 最后更新 | 简介 |
|--------------|--------|-----------|---------|------|
| qbittorrent | qBittorrent | 部分 | 2026-08-08 | BT/PT 下载客户端，配置代理/下载设置 |

## 命名规范

- 文件名 = process_name 去扩展名小写（如 `qbittorrent.exe` → `qbittorrent.md`）
- frontmatter `aliases` 放友好名 + 常见标题片段
- 匹配优先级：文件名精确 > aliases 子串

## 知识老化规则

- 文件"最后更新"日期超过 90 天 → `staleness_level=warn`，提示 agent 验证后再用
- 超过 180 天 → `staleness_level=critical`，高度可能过时
- "已知坑"表格含"最近验证日期"列，agent 验证某条坑仍适用时用 `screen_write_lesson` 更新该列
- 不自动删除（保留历史），仅提示

## 新增软件经验

agent 在某软件踩坑或摸索出新方法后，调 `screen_write_lesson` 写入：

```
screen_write_lesson(
  process_name="xxx.exe",
  section="已知坑",
  content="| 问题 | 错误做法 | 正确做法 | 2026-08-08 | |",
  source_window_title="窗口标题"
)
```

下次操作该软件时，`list_windows`/`screen_app_list` 响应会自动注入 `app_lessons_hint`。
