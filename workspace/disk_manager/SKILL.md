---
name: disk_manager
description: >
  磁盘空间分析与清理建议、系统环境全面备份（浏览器/游戏存档/桌面/系统设置）。
  触发词：清理硬盘、磁盘清理、空间不足、备份系统、环境备份、游戏存档备份。
  当用户提到磁盘空间、清理垃圾文件、备份电脑环境、系统迁移时触发。
  ⚠️ 全面备份功能未经测试，执行时必须提醒用户并全程跟进。
---

# 磁盘管理 Skill

## 概述

协助清理硬盘空间和全面备份系统，所有操作必须经过用户审核确认，**绝不擅自删除或移动任何文件**。

> **⚠️ 未测试提醒：全面备份功能（`backup_env.py` 的全量备份模式）尚未经过实际测试。**
> 当用户要求执行全面备份时，必须明确告知此功能未测试，执行过程中需全程跟进并记录问题。

---

## 软件名称对应关系
```
.lmstudio / lm-studio-updater     → LM Studio（已卸载）
.paddlex                           → PaddleX
.lingma                            → 通义灵码
.catpawai / CatPawAI               → CatPawAI
.codex                             → OpenAI Codex
.qq-chat-exporter                  → QQ聊天导出工具
.abdm                              → ABDownloader（在用）
.trae-cn / .trae                   → Trae AI（在用）
.secoresdk / 360se6               → 360安全浏览器（已卸载）
.GreenCore7z / greencore           → 360安全浏览器（已卸载）
.360safe                           → 360安全卫士
.KRLauncher                        → 鸣潮启动器（在用，不删）
.ima.copilot / ima.copilot         → 知识库工具（在用）
.sankuai                           → 美团内部工具
HT                                 → 异环游戏（在用，不删）
GeePlayer                          → 爱奇艺播放器
EBWebView                          → Outlook内嵌浏览器
.modelscope                        → ModelScope模型
.cache\huggingface                 → HuggingFace模型
.cache\puppeteer                   → Chromium/Puppeteer
.cache\modelscope                  → ModelScope模型缓存
QQMusicCache                       → QQ音乐播放缓存（可达10GB+）
```

---

## 一、文件清理方案

### 铁律
1. **所有操作必须用户审核确认** - 脚本只生成报告和建议，不执行任何删除/移动
2. **尽量减少磁盘扫描** - 使用轻量级扫描策略，避免全盘遍历
3. **分类清晰** - 每个建议都标注原因和风险等级

### 空间分析工作流（scan → drill → find → dry-run → delete）

用户说"清理磁盘/空间不足"时，走以下闭环：

**Step 1: scan — 空间概览**
```
scan_disk.py scan <path> --json -d 4 -n 10
```
拿到紧凑 tree JSON（每层 top 10 目录 + other 折叠），快速定位大目录。

**Step 2: drill — 下钻定位**
```
scan_disk.py drill <path>/<大目录> --json -d 5
```
对 scan 发现的大目录下钻，收窄路径看更细的 tree。

**Step 3: find — 按目录名定位（可选）**
```
scan_disk.py find <path> node_modules .venv __pycache__ --json
```
全树搜指定目录名，返回路径+大小，适合批量定位已知费空间目录。

**Step 4: dry-run — 出清理回执**
```
cleanup.py --dry-run --json
```
拿到逐项可回收字节回执（id/path/size_bytes/size_human），不删任何文件。

**Step 5: 用户勾选 → delete**
```
cleanup.py --delete --items cache-0,cache-3,cache-6 --json
```
用 AskUserQuestion 把 dry-run 回执交给用户勾选，只删确认项，走 ctypes 回收站（可恢复）。

**补充：**
- Chrome缓存提醒用户用 `chrome://settings/clearBrowserData` 清理
- E/F盘用户自行管理，不干预
- 需要找最大单个文件时用 `scan_disk.py files <path> -n 10 --json`

### 定期清理清单（每次清理检查这些位置）

**第一梯队：安全删除，自动重建**
```
%LOCALAPPDATA%\NVIDIA\DXCache                                               # DX着色器缓存（可达15GB+）
%LOCALAPPDATA%\pip\cache                                                    # pip缓存（可达9GB+）
%LOCALAPPDATA%\npm-cache                                                    # npm缓存
%LOCALAPPDATA%\CrashDumps                                                   # 崩溃转储
%LOCALAPPDATA%\Temp                                                         # 临时文件
%APPDATA%\Tencent\WeChat\log                                                # 微信日志
%LOCALAPPDATA%\GameViewer\webviewcache                                      # 向日葵缓存
%LOCALAPPDATA%\Steam\htmlcache                                              # Steam商店缓存
%APPDATA%\Trae CN\logs                                                      # Trae日志
%APPDATA%\Tencent\WeChat\radium\WmpfCache                                   # 微信WmpfCache
%APPDATA%\Tencent\WeMeet\Global\Data\WebkitCacheData                        # 腾讯会议WebKit缓存
C:\ProgramData\NVIDIA Corporation\NVIDIA app\UpdateFramework\ota-artifacts  # NVIDIA驱动安装包（可达7GB+）
%LOCALAPPDATA%\NVIDIA Corporation\NVIDIA App\CefCache                       # NVIDIA App浏览器缓存
%LOCALAPPDATA%\NVIDIA Corporation\NVIDIA Overlay\CefCache                   # NVIDIA覆盖层缓存
%LOCALAPPDATA%\Microsoft\Windows\Explorer                                   # 缩略图缓存
<data_drive>:\<system_data_root>\QQMusicCache\downloadproxyNew\tp2p\.tpfs\duty     # QQ音乐播放缓存（可达10GB+）
```

**第二梯队：需关闭对应程序**
```
Chrome缓存 → chrome://settings/clearBrowserData（勾选缓存，所有时间）
```

**第三梯队：软件更新器（安装后可删）**
```
%LOCALAPPDATA%\*-updater  # 所有updater目录
```

**第四梯队：可选清理（删了会重建，但重建可能较慢）**
```
%LOCALAPPDATA%\Unity\cache                    # Unity包缓存（2.6GB+）
%LOCALAPPDATA%\Unity\npm                      # Unity包管理器缓存（921MB+）
%LOCALAPPDATA%\Microsoft\Olk\EBWebView        # Outlook WebView缓存（737MB+）
%APPDATA%\IQIYI Video\GeePlayer               # 爱奇艺播放器缓存（458MB+）
%USERPROFILE%\.cache\modelscope               # ModelScope模型缓存（2.2GB+）
%USERPROFILE%\.cache\huggingface              # HF模型缓存（1.5GB+）
%USERPROFILE%\.cache\puppeteer                # Chromium缓存（583MB+）
%LOCALAPPDATA%\Google\Chrome\User Data\Default\Service Worker\CacheStorage  # 网站缓存（3GB+）
%APPDATA%\Code\CachedExtensionVSIXs           # VS Code扩展缓存（648MB+）
```

**Windows系统清理命令**
```
dism /online /cleanup-image /startcomponentcleanup /resetbase  # 组件存储清理（释放assembly/Installer/LCU空间）
cleanmgr /d C                                                   # 磁盘清理工具
```

---

## 二、全面备份方案

### 设计理念

备份不仅仅是复制文件，而是**完整迁移一个工作环境**。目标是：换一台电脑，运行恢复脚本后，能快速恢复到熟悉的工作状态。

### 备份层次

```
┌─────────────────────────────────────────┐
│  L1: 文件同步（已有filesync）             │  ← 你已有
├─────────────────────────────────────────┤
│  L2: 系统环境备份（新增）                  │  ← 本次设计
│  - 浏览器数据（书签/扩展/密码/设置）        │
│  - 游戏存档                              │
│  - 桌面布局                              │
│  - 系统设置/注册表                        │
│  - 已安装软件列表                         │
├─────────────────────────────────────────┤
│  L3: 一键恢复脚本（新增）                  │  ← 本次设计
│  - 自动恢复L2备份项                       │
│  - 环境调整清单（需手动确认的项）           │
└─────────────────────────────────────────┘
```

### L2: 系统环境备份详情

#### 1. 浏览器数据

| 数据 | Chrome路径 | 备份方式 |
|------|-----------|----------|
| 书签 | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\Bookmarks` | 直接复制 |
| 扩展列表 | `chrome://extensions` 导出ID列表 | 脚本提取 |
| 密码 | Chrome不提供明文导出，需用户手动导出CSV | 提示用户 |
| 搜索引擎 | `Preferences` JSON中的`search_engines` | 直接复制 |
| 油猴脚本 | `Extensions\*tampermonkey*\` | 直接复制 |

#### 2. 游戏存档

| 游戏/平台 | 存档位置 |
|-----------|----------|
| Steam云存档 | 自动同步，无需备份 |
| Steam本地存档 | `C:\Program Files (x86)\Steam\userdata\` |
| Epic | `%LOCALAPPDATA%\Epic Games\*` |
| 单机游戏常见路径 | `%<data_drive>:\Documents%\My Games\`, `%APPDATA%\*`, `%LOCALAPPDATA%\*` |
| 特殊游戏 | 需用户补充列表 |

#### 3. 桌面布局

| 数据 | 方式 |
|------|------|
| 桌面图标位置 | 注册表 `HKCU\Software\Microsoft\Windows\Shell\Bags` |
| 任务栏固定 | 注册表 + 快捷方式文件 |
| 开始菜单布局 | PowerShell导出 `Export-StartLayout` |

#### 4. 系统设置

| 数据 | 方式 |
|------|------|
| 环境变量 | `reg export "HKCU\Environment"` + `reg export "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"` |
| 右键菜单 | `HKCR\Directory\Background\shell`, `HKCR\*\shell` |
| 文件关联 | `HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts` |
| 输入法设置 | 注册表导出 |
| 电源计划 | `powercfg /export` |
| Windows Terminal配置 | `%LOCALAPPDATA%\Packages\Microsoft.WindowsTerminal_*/LocalState/settings.json` |
| hosts文件 | `C:\Windows\System32\drivers\etc\hosts` |
| PATH变量 | 环境变量备份中已包含 |

#### 5. 已安装软件列表

| 方式 | 命令 |
|------|------|
| 已安装程序 | `wmic product get name,version` 或 PowerShell `Get-ItemProperty HKLM:\Software\*` |
| Chocolatey | `choco list --local-only` |
| Scoop | `scoop list` |
| pip | `pip list --format=freeze` |
| npm全局 | `npm list -g --depth=0` |
| uv | `uv tool list` |

### L3: 一键恢复脚本设计

```
恢复流程：
1. 运行 restore_env.bat
2. 自动恢复：
   - 注册表项（环境变量、右键菜单、文件关联）
   - 桌面图标位置
   - hosts文件
   - Windows Terminal配置
   - 浏览器书签
3. 半自动恢复（需确认）：
   - 安装软件（从列表生成安装命令）
   - 恢复游戏存档（确认游戏已安装后复制）
   - 恢复浏览器扩展（逐个确认安装）
4. 手动恢复（生成清单）：
   - 密码导入
   - 需要重新配置的软件设置
   - 系统偏好设置（深色模式、缩放比例等）
```

### 备份文件组织

```
E:\<data_drive>:\<backup_root>\SystemBackup\{日期}\
├── registry\              # 注册表导出
│   ├── env_user.reg
│   ├── env_system.reg
│   ├── context_menu.reg
│   └── desktop_layout.reg
├── browser\               # 浏览器数据
│   ├── bookmarks.json
│   ├── extensions_list.txt
│   └── preferences.json
├── game_saves\            # 游戏存档
│   └── {game_name}\
├── system\                # 系统配置
│   ├── power_plan.pow
│   ├── terminal_settings.json
│   ├── hosts
│   └── start_layout.xml
├── software\              # 软件列表
│   ├── installed_programs.txt
│   ├── choco_list.txt
│   ├── pip_requirements.txt
│   └── npm_global.txt
├── desktop\               # 桌面文件和快捷方式
│   └── *.lnk
├── restore_env.bat        # 一键恢复脚本
└── manual_checklist.md    # 需手动确认的清单
```

---

## 三、与filesync的协作

你的filesync工具处理文件同步，新增的系统环境备份需要加入filesync配置：

```
E:\<data_drive>:\<backup_root>\SystemBackup → <backup_drive> |重复文件:检查日期和大小 |增删文件处理:增量更新 |状态:启用
```

备份脚本执行后，filesync会自动同步到移动硬盘。

---

## 四、扫描策略（保护磁盘寿命）

1. **紧凑 tree**：`scan` 默认深度 4、每层 top 10，`other` 折叠小目录，JSON 通常几 KB
2. **下钻收窄**：`drill` 对大目录逐层下钻，不做一次性全盘深扫
3. **按名定位**：`find` 全树搜指定目录名（如 node_modules），匹配后计算大小即停（不重复下钻）
4. **硬上限保护**：`--max-files`（默认10万）、`--max-dirs`（默认2万）、`-d` 深度上限，达到即停报告 `stop_reason`
5. **symlink/junction 不跟随**：用 `st_file_attributes & 0x400` 检测 reparse point，跳过不递归
6. **增量备份**：只备份变更的文件，不全量复制

---

## 触发词
- 清理硬盘、磁盘清理、空间不足、清理文件
- 备份系统、全面备份、系统迁移、环境备份
- 游戏存档备份、浏览器备份

## 工具脚本
- `scan_disk.py` - 磁盘空间扫描（子命令：scan/drill/find/files，--json 直返上下文，symlink/junction 不跟随，硬上限保护）
- `cleanup.py` - 磁盘清理（--dry-run 出逐项回执 → --delete --items <ids> 走 ctypes 回收站，路径白名单保护）
- `backup_env.py` - 系统环境备份（浏览器数据/游戏存档/桌面布局/系统设置/软件列表）