---
aliases: ['qBittorrent', 'qbittorrent']
---

# qBittorrent (qbittorrent.exe)

## 元信息

| 属性 | 值 |
|------|------|
| **process_name** | qbittorrent.exe |
| **class_name** | Qt5152QWindowIcon（Qt 应用，随版本变化） |
| **UIA 友好度** | 部分（Qt 应用，UIA 树不完整） |
| **首次接触日期** | 2026-07-14 |
| **相关 task_type** | adhoc.byr_pt |
| **文件创建日期** | 2026-08-08 |
| **最后更新** | 2026-08-08 |

## 软件概况

BT/PT 下载客户端。agent 任务主要是配置代理（IPv6-only PT 站点场景）、调整下载设置、排查 tracker 连接问题。界面基于 Qt，UIA 支持不完整，设置对话框部分控件可走 UIA，下载列表表格需 OCR。

## 软件识别

| 标识 | 值 | 备注 |
|------|------|------|
| process_name | qbittorrent.exe | 主键 |
| class_name | Qt5152QWindowIcon | 辅助消歧（Qt 版本相关） |
| 窗口标题模式 | "qBittorrent v5.2.2" | 含版本号，版本升级后标题变化 |
| UWP/WinUI 多层 HWND | 否 | 标准 Win32 窗口 |

## UIA 友好度与定位策略

| 控件类型 | UIA 支持 | 推荐 locator | 备注 |
|----------|---------|-------------|------|
| 菜单栏 | 部分 | UIA + OCR | Qt 菜单 UIA 不完整，部分需 OCR |
| 设置对话框 | 是 | UIA invoke | 标准对话框控件 UIA 友好 |
| 下载列表表格 | 否 | OCR bbox | Qt 表格 UIA 不暴露行/列 |
| 工具栏按钮 | 部分 | UIA invoke / OCR | 主要操作按钮 UIA 可用 |

## 常用快捷键

| 操作 | 快捷键 | 备注 |
|------|--------|------|
| 添加种子 | Ctrl+D | 打开"添加种子"对话框 |
| 暂停所有 | Ctrl+Alt+P | |
| 恢复所有 | Ctrl+Alt+R | |
| 打开设置 | Alt+O | 或菜单"工具→选项" |
| 搜索 | Ctrl+F | |

## 菜单路径（常见任务）

| 任务 | 路径 | 备注 |
|------|------|------|
| 打开设置 | 工具 → 选项 | 或 Alt+O |
| 代理配置 | 工具 → 选项 → BitTorrent → 代理 | v4.1.8+ 路径 |
| 下载路径 | 工具 → 选项 → 下载 | |
| Web UI | 工具 → 选项 → Web UI | |
| 添加种子 | 文件 → 添加种子文件 | 或 Ctrl+D |

## 对话框处理

| 对话框 | 处理方式 | 备注 |
|--------|---------|------|
| "添加种子" | type 路径 + Enter | 文件选择器标准处理 |
| "选项" 设置 | UIA invoke 应用 | close force=false 自动处理模态 |
| "确认删除" | UIA 找按钮 invoke | 删除任务时弹出 |

## 已知坑

| 问题 | 错误做法 | 正确做法 | 发现日期 | 最近验证日期 |
|------|---------|---------|---------|------------|
| 配置改 ini 被界面覆盖 | 直接改 qbittorrent.ini | 通过界面操作保存（界面保存会覆盖 ini 修改） | 2026-07-14 | |
| v4.1.8 双配置段冲突 | 只改 [Preferences] | 同步改 [Preferences] 和 [Network] | 2026-07-14 | |
| 代理选项命名不直观 | 凭直觉勾选 | "通过代理查找主机名"不勾选=让代理服务器解析 DNS（IPv6-only 站点的正确做法） | 2026-07-14 | |
| Web UI 密码格式 | 明文密码 | PBKDF2 哈希格式，明文不被接受 | 2026-07-14 | |
| Qt 表格 UIA 不可用 | 用 UIA 读下载列表 | 用 OCR bbox 定位行/列 | 2026-07-14 | |
| 窗口标题含版本号 | 硬编码标题匹配 | 用 process_name 过滤，标题只做辅助 | 2026-07-14 | |

## 遗留问题

- **PT 站点特殊网络环境**（byr.pt 实测）：IPv6-only + TLS 版本限制 + 客户端封禁可能多重叠加，根因难快速定位。详见 docs/computer-use-reference.md 历史经验留档（byr.pt 任务实测章节）。
- **tracker 测试 UA**：必须传正确 User-Agent（如 `qBittorrent/5.2.2`），curl 默认 UA 会被 PT 站点封禁。

## 修改历史

| 日期 | 变更 |
|------|------|
| 2026-08-08 | 初始创建，迁移自 docs/computer-use-reference.md byr.pt 历史经验留档 |
