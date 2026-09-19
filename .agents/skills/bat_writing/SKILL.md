---
name: bat_writing
description: >
  编写和修复能在中文 Windows 上跑起来的批处理（.bat/.cmd）。任何要新建或修改 bat/cmd 脚本、
  或排查"bat 跑不了 / 双击没反应 / 满屏乱码 / 'XX' 不是内部或外部命令 / 找不到批处理标签"的任务，
  都必须先读本 skill。核心铁律：GBK(CP936) 编码 + CRLF 行尾 + 无 BOM。
  触发词：写 bat、bat 脚本、批处理、.bat、.cmd、启动脚本、bat 乱码、bat 跑不了、
  批处理报错、不是内部或外部命令、找不到批处理标签、批处理编码、batch script、cmd 脚本。
task_type: adhoc.bat_writing
trust: native
---

# 批处理编写与修复 (bat_writing)

## 触发词
写bat、bat脚本、批处理、.bat、.cmd、启动脚本、bat乱码、bat跑不了、批处理报错、不是内部或外部命令、找不到批处理标签、batch script、cmd脚本

## 概述

中文 Windows（ACP=OEMCP=936）上 cmd.exe 解析批处理的稳定格式是：**GBK 编码 + CRLF 行尾 + 无 BOM**。三条任意一条破坏，脚本就会以各种看似"编码问题"的方式跑不起来——这是 bat 反复"写了跑不了"的根因，且报错外观（乱码、半截汉字）几乎必然误导排查方向。本 skill 把编写规范、写完自检、坏了修复收敛成一条固定流程。

## 何时必须遵循

- **新建任何 .bat/.cmd**（含纯英文的也建议走自检，成本一行命令）
- **修改现有 bat**（⚠️ 不是"可能丢行尾"那么轻：Edit/Write 按 UTF-8 读写，改一份 GBK bat 会把中文**永久冲成 U+FFFD**，必须走下面 2b 的回路）
- **排查 bat 运行报错**（先跑自检排除编码/行尾问题，再查脚本逻辑——九成"乱码报错"是行尾问题）

## 工作流

### 1. 写之前：确认目标机代码页

本机已实测 ACP=OEMCP=936（中文 Windows 默认），直接用 GBK。仅当目标机开启了"Beta: UTF-8 全球语言支持"（OEMCP=65001）时才切换 UTF-8 方案，且四个条件缺一即炸：无 BOM + CRLF + 首行 `@echo off` + 第二行 `chcp 65001 >nul 2>&1`。查询方法见 references/guide.md 第二节。

### 2. 编写

- 内容含中文时，用 references/guide.md 附录的最小模板起手（`%~dp0` 拼路径、菜单循环、提权注意事项都已就位）
- `goto` / `call :label` 的标签名用英文
- 脚本中途**绝不**切 `chcp`（含中文时切代码页会让 cmd 缓存的字节↔字符映射失效，同一行被误解析成命令）
- 文件末尾留一个换行（最后一行无换行符会被吞）
- 提权重启后工作目录变成 `C:\Windows\System32`，路径一律用 `%~dp0` 拼接

### 2b. 改一份已转 GBK 的旧 bat：先解成 UTF-8 工作稿

IDE 的 Edit/Write 工具按 UTF-8 读写文件，直接改 GBK bat 会把中文字节当成非法 UTF-8，替换成 U+FFFD 后写回——**中文不可逆丢失**（2026-09-18 实测踩过一次，`--check` 从 OK 变成 `GBK可解码=✗`，文件里出现 `EF BF BD`）。所以改旧 bat 必须走"解出 UTF-8 工作稿 → 改 → 正向转回 GBK"的回路：

```bash
# 1) GBK → UTF-8 工作稿（不要用 --enc utf8：那个模式会往文件里插 chcp 65001，是给 UTF-8 方案用的，不是转码工具）
uv run python -c "src='<旧bat路径>'; open('temp/work.bat','wb').write(open(src,'rb').read().decode('gbk').encode('utf-8'))"
# 2) 用 Edit 工具改 temp/work.bat（此刻是 UTF-8，安全）
# 3) 正向转回 GBK + 自检，全绿再回写各副本
uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py temp/work.bat --inplace
uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py temp/work.bat --check
cp temp/work.bat <旧bat路径>
```

已经在 UTF-8 主稿上写完的内容，也可以整份用 Write 重写（Write 是覆盖写，不解码旧内容），然后只跑一次正向转码——比逐行 Edit 更稳。

### 3. 写完必须自检（completion criterion）

写完/改完立刻跑（不是"交付前有空再跑"）：

```bash
uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件.bat> --check
```

**全部输出 OK 才算写完**；任何一项 FAIL，进入第 4 步修复后重新自检。别靠"看起来正常"判断——GBK 和 UTF-8 在乱码爆发前肉眼无法分辨。

### 4. 修复

```bash
uv run python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件.bat> --inplace
```

脚本自动探测源编码（BOM → gb18030 → utf-8）→ 统一 CRLF → 补末尾换行 → 按 GBK 写出。原地修改走临时文件 + `os.replace`，中途失败不会丢内容。

### 5. 运行验证

自检通过后在真实环境跑一次（命令行或双击），确认功能行为正确。只在 IDE 里看过源码不算验证。

## 坑点清单（Gotchas）

- **LF-only 是最常见死法（占九成）**：cmd 每执行完一行按字节偏移找下一行，对 LF-only 文件却按字符数计算；GBK 中文双字节导致偏移逐行错位，后面的行从汉字中间劈开——报错全是 `'务开关' 不是内部或外部命令` 这类半截汉字。**纯 ASCII 的 LF-only 往往还能正常跑**，所以这个坑几乎只在中文脚本上爆
- 产出 LF 的常见途径：VS Code/Notepad++ 默认 LF、Git Bash 里 `echo ... > x.bat`、WSL 写文件、从 Linux/macOS 拷贝、AI 工具直接生成文件。所以**写完必须自检，不信任任何写入工具的行尾**
- UTF-8 BOM 的症状：首行报 `'锘緻echo' 不是内部或外部命令`（BOM 三个字节被当成命令的一部分）
- `chcp 65001` + UTF-8 看似现代方案，实测有隐蔽故障：回声未关时，`echo   0  退出` 会被误解析为执行 `0  退出`。含中文就别用 UTF-8 方案
- GBK 与 UTF-8 大多数 ASCII 段完全相同，**编码错在运行前不可见**——这是"写完看一眼没问题，一跑就炸"的原因
- 三项自检全过还报错 → 是脚本逻辑问题，不是编码问题（诊断分流见 references/guide.md 第四节）
- **`%VAR%` 展开后 cmd 会再扫一遍特殊字符**：值里带 `&` 时（典型如 PnP 设备实例 ID `PCI\VEN_8086&DEV_2723&SUBSYS_...`），`echo 目标设备: %DEVID%` 会劈成 6 条命令，报一串 `'DEV_2723' 不是内部或外部命令` / `'REV_1A\4'` 变成"系统找不到指定的路径"，而功能却"看起来跑成了"。解法二选一：`setlocal EnableDelayedExpansion` + `!DEVID!`（延迟展开发生在解析之后，特殊字符安全），或把展开包进双引号（`pnputil /restart-device "%DEVID%"` 实测安全）。⚠️ 开了延迟展开就要注意值里不能出现裸 `!`
- **`rem` 注释行不屏蔽重定向**：注释里写 `rem 第 1 步 -> 第 2 步` 会被 cmd 当成 `>` 重定向，在项目目录凭空拉出一个名为 `第` 的文件（`<`、`|` 同理）。注释里的箭头一律写 `=>` 或全角，或直接避免
- **入库的 bat 存的是 LF**：blob 里永远是 LF（`git cat-file` 实测现有 bat blob `CRLF=0`）。2026-09-18 已在 `.gitattributes` 加 `*.bat text eol=crlf` / `*.cmd text eol=crlf`，所以 **clone 与 checkout 会还原成 CRLF**，不再依赖本机 `core.autocrlf=true`。但直接读 blob 的路径不受该规则保护：`git show HEAD:x.bat > x.bat`、`git cat-file`、任何绕过 checkout 的打包/复制链路拿到的仍是 LF-only 中文脚本——这类取出的 bat 一律先过一遍 `--inplace` 再交付（`--check` 会立刻报 `孤立LF≠0`）
- **IDE 采集层会二次解码子进程输出**：agent 侧看到的中文乱码（`锟斤拷`）是 MCP/终端把 GBK 字节按 UTF-8 解码的产物，不代表双击时显示错。要判定真实渲染，让 Python 自己 `subprocess` 捕获后 `.decode('gbk', errors='strict')`——严格解码不抛错即证明输出字节合法（对照：直接把 bat 输出重定向到文件再读，会被采集层污染，不能当证据）

## 关键规则

- 写完/改完必须跑 `--check` 自检，全 OK 才能声称完成
- 含中文的 bat 禁止用 UTF-8（无论带不带 BOM）交付到 ACP=936 机器
- 禁止在脚本中途 `chcp` 切换代码页
- 修复必须用 `scripts/fix_bat_encoding.py`，不要手写 sed/PowerShell 重定向转码（PS 5.1 重定向有 BOM 累积前科）
- **改已转 GBK 的旧 bat 禁止用 Edit 直接改**，必须走 2b 的"UTF-8 工作稿 → 改 → 正向转回 GBK"回路；整份重写用 Write 覆盖 + 一次转码
- 注释和 `echo` 里**禁止裸放**可能含 `&`/`>`/`|` 的 `%VAR%` 展开——要么 `EnableDelayedExpansion` + `!VAR!`，要么包在双引号里

## 依赖

| 依赖 | 路径 | 说明 |
|------|------|------|
| 自检/修复脚本 | `.agents/skills/bat_writing/scripts/fix_bat_encoding.py` | `--check` 只检不改；`--inplace` 修复；`--enc utf8` 仅 OEMCP=65001 时用 |
| 详细参考 | `references/guide.md` | 代码页查询、LF 根因解析、10 种编码×行尾×BOM 组合实测结论表、编辑器手动修法、最小模板 |

## 输入/输出

- 输入：任意路径的 .bat/.cmd 文件
- 输出：原地修复（`--inplace`）或 `<名字>.fixed.bat`；`--check` 只打印判定不修改
