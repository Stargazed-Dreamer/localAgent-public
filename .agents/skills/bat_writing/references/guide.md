# 中文 Windows 批处理编写指南（详细参考）

> 本文件是 `bat_writing` skill 的详细参考；工作流和铁律速查见 [SKILL.md](../SKILL.md)。
> 适用范围：Windows 10/11 中文环境（简体），`cmd.exe` 批处理 `.bat` / `.cmd`。
> 核心结论：**GBK(CP936) 编码 + CRLF 行尾 + 无 BOM**。
> 实测环境：Windows 10 build 19045，ACP=936 / OEMCP=936。

## 目录

1. [先确认目标机器的代码页](#一先确认目标机器的代码页)
2. [最致命的坑：LF-only 行尾](#二最致命的坑lf-only-行尾)
3. [诊断：三步定位](#三诊断三步定位)
4. [修复](#四修复)
5. [各种组合的实测结论](#五各种组合的实测结论)
6. [编写时的规矩](#六编写时的规矩)
7. [自检清单](#七自检清单)
8. [最小可用模板](#八最小可用模板)

---

## 一、先确认目标机器的代码页

```powershell
$cp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\NLS\CodePage'
"ACP=$($cp.ACP)  OEMCP=$($cp.OEMCP)"
```

- `ACP=OEMCP=936`（中文 Windows 默认）→ 用 **GBK**，本文其余部分照做。
- `OEMCP=65001`（开了「Beta: 使用 Unicode UTF-8 提供全球语言支持」）→ 反过来用 **UTF-8 无 BOM + CRLF + 首行 `@echo off` + 第二行 `chcp 65001`**。
- 其他（如繁体 950、日文 932）→ 用对应的 ANSI 代码页，行尾和 BOM 规则不变。

## 二、最致命的坑：LF-only 行尾

### 现象

脚本跑起来满屏这种鬼东西：

```
'务开关' 不是内部或外部命令，也不是可运行的程序
或批处理文件。
'rlevel' 不是内部或外部命令，也不是可运行的程序
'ned'    不是内部或外部命令，也不是可运行的程序
'/b'     不是内部或外部命令，也不是可运行的程序
'[DONE]' 不是内部或外部命令，也不是可运行的程序
```

或者 `goto` / `call :label` 报「找不到批处理标签」，或者脚本被莫名截断。

### 根因

cmd.exe 每执行完一行，都要重新定位下一行的**字节偏移**。对 **LF-only** 文件，它按**字符数**而不是字节数来计算偏移。

GBK 中文是双字节，字符数 ≠ 字节数，于是每过一行偏移就错位一点，后面的行从汉字中间被劈开 —— 报错信息里就全是半个汉字。

**关键推论：纯 ASCII 的 LF-only 批处理往往还能正常跑。** 所以这个坑几乎只在中文脚本上爆，而且报错看起来像乱码，99% 会被误判成"编码问题"。

### 哪些操作会产出 LF

- VS Code / Notepad++ 默认保存为 LF（需显式改成 CRLF）
- Git Bash 里 `cat > x.bat`、`echo ... > x.bat`
- WSL 挂载目录里写文件
- 从 Linux/macOS 拷过来的脚本
- 某些 AI 助手 / 代码生成工具直接输出文件

## 三、诊断：三步定位

```bash
python - <<'EOF'
b = open(r'C:/path/to/script.bat','rb').read()
print('BOM   :', b[:3] == b'\xef\xbb\xbf')
print('CRLF=%d  LF=%d  孤立LF=%d' % (b.count(b'\r\n'), b.count(b'\n'),
                                     b.count(b'\n') - b.count(b'\r\n')))
for e in ('gbk', 'utf-8'):
    try:
        b.decode(e); print(e, 'OK')
    except Exception:
        print(e, 'FAIL')
EOF
```

（优先直接用 skill 自带脚本：`python .agents/skills/bat_writing/scripts/fix_bat_encoding.py <文件> --check`，等价且免写代码。）

| 看到 | 判定 |
|---|---|
| `孤立LF > 0` | 行尾问题 —— 最常见，占九成 |
| `gbk FAIL` + `utf-8 OK` | 编码不匹配（UTF-8 文件跑在 936 控制台上） |
| `BOM: True` | 首行会报 `'锘緻echo' 不是内部或外部命令` |
| 三项都正常还报错 | 脚本逻辑问题，不是编码问题 |

顺手也看一眼行尾是不是 CR-only（`CRLF=0` 且 `LF=0` 但有 `\r`）—— 那种会被拼成一整行。

## 四、修复

### 一行搞定（推荐）

```bash
python .agents/skills/bat_writing/scripts/fix_bat_encoding.py 你的脚本.bat --inplace
```

脚本行为：自动探测源编码（BOM → gb18030 → utf-8）→ 换行统一 CRLF → 补末尾换行 → 按 GBK 写出 → 打印校验信息。原地修改走「临时文件 + `os.replace`」，中途失败不会丢内容。

`--enc utf8` 切到 UTF-8 备选方案（仅在 OEMCP=65001 时才该用）。

### 不想带脚本，核心就三行

```python
raw  = open('in.bat', 'rb').read()
text = raw.decode('gb18030').replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
open('out.bat', 'wb').write(text.encode('gbk'))
```

### 用编辑器手动改

- **VS Code**：右下角状态栏，把 `LF` 点成 `CRLF`；再点编码（显示 `UTF-8`）→「通过编码保存」→ 选 `GBK` 或 `GB18030`。**顺序无所谓，两件都要做。**
- **记事本**：「另存为」→ 编码选 **ANSI** → 保存。注意记事本老版本会把 LF 当没有换行，建议先用别的工具转成 CRLF。
- **Notepad++**：编辑 → 文档格式转换 → 转为 Windows (CR LF)；编码 → 转为 GB2312/GBK。

## 五、各种组合的实测结论

同一份含 `call :label` / `goto :label` / 中文 `echo` 的脚本，只改编码和行尾，在 ACP=OEMCP=936 的机器上跑：

| 编码 | 行尾 | BOM | chcp | 结果 |
|---|---|---|---|---|
| **GBK** | **CRLF** | 无 | 无 | ✅ **推荐** |
| GBK | CRLF | 无 | 936 | ✅ 可用（冗余但无害） |
| GBK | LF | 无 | 无 | ❌ 崩坏（最常见的坏法） |
| GBK | CR | 无 | 无 | ❌ 全部拼成一行 |
| GBK | CRLF | 无 | 65001 | ❌ 锟斤拷乱码 |
| UTF-8 | CRLF | 无 | 65001 | ⚠️ 能跑但有隐患（见下） |
| UTF-8 | CRLF | 有 | 65001 | ⚠️ 侥幸可用，不推荐 |
| UTF-8 | CRLF | 无 | 936 | ❌ 部分字节变 `?`（如 `关`→`?`） |
| UTF-8 | CRLF | 有 | 无 | ❌ 首行 `'锘緻echo'` 报错 |
| UTF-8 | LF | 无 | 无 | ❌ 崩坏 |

### UTF-8 + chcp 65001 的具体隐患

实测抓到的真实故障：某脚本首行是 `::@echo off`（回声是**开**着的），转成 UTF-8 并加 `chcp 65001` 后，菜单里的这一行

```bat
echo   0  退出
```

被 cmd 误解析成去执行 `0  退出`，报 `'0' is not recognized as an internal or external command`。
同一行在 GBK 版本下解析完全正常。

**原因**：批处理运行过程中切换代码页，会让 cmd 缓存的字节↔字符映射失效，文件定位再次错位。

**规则：只要脚本里同时有中文和 `chcp`，就不要用 UTF-8。**

如果非用 UTF-8 不可，四个条件必须同时满足，缺一即炸：
1. 无 BOM
2. CRLF
3. 第一行是 `@echo off`（关掉回声，规避上面的错位）
4. 第二行是 `chcp 65001 >nul 2>&1`

即便如此仍不如直接用 GBK 稳。

## 六、编写时的规矩

1. **不要用会产出 LF 的编辑器直接写中文 bat。** 见第二节清单。
2. **写完必须确认三件事**：CRLF、GBK、无 BOM。用 skill 的 `--check` 一条命令搞定，别靠"看起来正常"判断。
3. **不要用 `chcp` 折腾。** 本机 ACP 已经是 936，GBK 文件直接跑就对了。加 `chcp 936` 无害但也没必要。
4. **文件末尾留一个空行。** 某些情况下最后一行没有换行符会被吞掉。
5. **中文标签名（`goto` / `call` 的目标）尽量用英文。** 即使编码正确，`goto 中文标签` 在部分系统上仍有坑。
6. **路径里有中文时，`%~f0` 之类的变量要注意引号。** 例：`powershell -Command "Start-Process '%~f0' -Verb RunAs"`。
7. **提权重启后工作目录会变成 `C:\Windows\System32`。** 脚本里用相对路径会找不到文件，一律用 `%~dp0` 拼接。

## 七、自检清单

交付/运行一个含中文的 bat 之前，过一遍：

- [ ] `--check` 全 OK（等价于以下四条）
- [ ] `CRLF` 数量 = 总行数，`孤立LF = 0`
- [ ] 用 GBK 能正确解码；文件头三个字节不是 `EF BB BF`
- [ ] 最后一行以换行结尾
- [ ] 没有在脚本中途切 `chcp`（除非同时是 UTF-8 方案）
- [ ] 实际双击运行过一次，不是只在 IDE 里看过源码

## 八、最小可用模板

保存时务必选 **GBK + CRLF + 无 BOM**（写完跑 `--check` 确认）。

```bat
@echo off
chcp 936 >nul 2>&1
setlocal enabledelayedexpansion
title 示例脚本

REM 用 %~dp0 拼路径，提权后工作目录会变
set "ROOT=%~dp0"

echo ============================
echo   1  执行操作
echo   0  退出
echo ============================

:menu
set /p "CHOICE=请输入选项: "
if "%CHOICE%"=="1" call :do_work
if "%CHOICE%"=="0" exit /b 0
echo 无效输入，请重新选择。
goto menu

:do_work
echo [执行] 正在处理...
REM 具体逻辑
exit /b 0
```
