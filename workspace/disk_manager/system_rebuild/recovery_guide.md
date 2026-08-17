# 重装系统恢复指南（历史参考）

> **重要：本文件是 2026-06-23 的旧扫描产物，不是当前恢复入口。**
> 当前方法论、条目模型和人工检查表见同目录 `README.md`、`manifest.json`、`checklist.md`。
> 本文件中的旧路径、旧扫描结论和激活绕过内容不得自动执行；激活必须使用合法许可证/账号，登录态必须走官方同步或重新登录。
> `restore_env.ps1` 也只是实验性历史脚本，存在整体覆盖 PATH 等风险，运行前必须重新审查。

> 扫描时间：2026-06-23 | 用户：admin | 系统：Windows 11 AMD64
> 当前完整清单见同目录 `manifest.json`，人工清单见 `checklist.md`。
> 原始扫描数据见 `../scan/`；本文件中的旧路径只保留作历史对照。

---

## 目录
1. [CUDA + cuDNN 双版本](#1-cuda--cudnn-双版本)
2. [开发环境全套 + Path](#2-开发环境全套--path)
3. [VSCode 扩展 + 配置](#3-vscode-扩展--配置)
4. [Ollama + LM Studio](#4-ollama--lm-studio)
5. [桌面/任务栏/开始菜单布局](#5-桌面任务栏开始菜单布局)
6. [启动项配置](#6-启动项配置)
7. [系统工具配置](#7-系统工具配置)
8. [快速恢复脚本](#8-快速恢复脚本)

> JetBrains IDE 激活请走官方合法许可证/账号流程，本指南不再包含破解内容（2026-07-31 T05a 移除，IP 侵权与 Apache-2.0 不兼容）。

---

## 1. CUDA + cuDNN（单版本 12.9）

> 2026-07-27 更新：CUDA 11.8 已移除。ark 环境（`E:\<data_drive>:\<projects_root>\舟\combine`）升级为 CPU 版 `paddlepaddle 2.6.2` + `paddleocr 2.10.0`，不再依赖 11.8；LocalAgent 活跃栈使用 paddle/torch cu126，由 v12.9 工具包 + cuDNN 9.x 支撑。清理脚本：`F:\<project_root>\temp\cleanup_cuda118_and_conda_backups.ps1`。

### 当前配置
- **CUDA 12.9**：`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9`
- **cuDNN 9.14**：已集成在 `CUDA\v12.9\bin\`（cudnn64_9.dll, cudnn_cnn64_9.dll 等 9 个 dll）
- **Nsight Compute 2025.2.1**：`C:\Program Files\NVIDIA Corporation\Nsight Compute 2025.2.1\`
- **Nsight Systems**：2022.4.2 / 2024.5.1 / 2025.1.3（多版本）
- **显卡驱动**：NVIDIA 610.74（支持 CUDA 至 13.3，向下兼容 12.x）

### 环境变量
```
CUDA_PATH = C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9
CUDA_PATH_V12_9 = C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9
NVTOOLSEXT_PATH = C:\Program Files\NVIDIA Corporation\NvToolsExt\
```
Path 包含：`CUDA\v12.9\bin`、`CUDA\v12.9\libnvvp`、`Nsight Compute 2025.2.1\`

### 重装步骤
1. **装显卡驱动**：NVIDIA 610.74 或更新版
2. **装 CUDA 12.9**：从 NVIDIA 官网下载 `cuda_12.9.x_windows.exe`，自定义安装（不装驱动）
3. **装 cuDNN 9.14**：下载 cuDNN 9.14，解压后将 `bin/*.dll` 复制到 `CUDA\v12.9\bin\`
4. **装 Nsight Compute**：随 CUDA 安装或单独下载
5. **设置环境变量**：见第 9 节脚本
6. **验证**：`nvcc --version` 应显示 12.9；`nvidia-smi` 正常

### 可脚本化
⚠️ CUDA 安装需手动（安装器交互），但环境变量可脚本化

---

## 2. 开发环境全套 + Path

### 当前版本
| 工具 | 版本 | 路径 |
|------|------|------|
| Java JDK8 | Eclipse Temurin 8u452 | `E:\System_Programes\Java_JDK8\` |
| Java JDK17 | — | `E:\System_Programes\Java_JDK17\` |
| Node.js | 24.11.0 | `C:\Program Files\nodejs\` |
| Miniconda3 | py311_24.3.0 (Python 3.11.8) | `E:\<data_drive>:\<miniconda_root>\` |
| uv | — | `E:\System_Programes\uv\` |
| Git | 2.46.0 | `E:\System_Programes\Git\` |
| .NET | 3.1/6.0/8.0/9.0/10.0.1 + SDK 9.0.315 | `C:\Program Files\dotnet\` |
| texlive | 2025 | `E:\System_Programes\texlive\2025\` |
| MinGW | — | `E:\System_Programes\MinGW\` |
| MongoDB | — | `E:\System_Programes\MongoDB\` |

### 系统环境变量 Path（30 项，按顺序）
```
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\libnvvp
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin
E:\System_Programes\Java_JDK8\bin
E:\System_Programes\NVIDIA\bin
C:\Program Files (x86)\Common Files\Intel\Shared Libraries\redist\intel64\compiler
C:\Windows\system32
C:\Windows
E:\System_Programes\uv
E:\<data_drive>:\<miniconda_root>\
E:\<data_drive>:\<miniconda_root>\Scripts
E:\<data_drive>:\<miniconda_root>\Library\bin
C:\Windows\System32\Wbem
C:\Windows\System32\WindowsPowerShell\v1.0\
C:\Windows\System32\OpenSSH\
C:\Program Files\dotnet\
C:\Program Files\NVIDIA Corporation\NVIDIA app\NvDLISR
C:\Program Files (x86)\NVIDIA Corporation\PhysX\Common
E:\System_Programes\Git\cmd
E:\System_Programes\Microsoft VS Code\bin
E:\System_Programes\calibre\
c:\<user_home>\AppData\Local\Programs\cursor\resources\app\bin
E:\System_Programes\texlive\2025\bin\windows
C:\Program Files\NVIDIA Corporation\Nsight Compute 2025.2.1\
C:\Program Files\nodejs\
F:\rubbish\Microsoft SQL Server\150\Tools\Binn\
C:\Program Files\Microsoft SQL Server\150\Tools\Binn\
F:\rubbish\Microsoft SQL Server\150\DTS\Binn\
C:\Program Files\Microsoft SQL Server\150\DTS\Binn\
C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\
C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit\
```

### 用户环境变量 Path（13 项）
```
C:\<user_home>\.local\bin
C:\<user_home>\AppData\Local\Microsoft\WindowsApps
C:\<user_home>\.dotnet\tools
E:\System_Programes\JetBrains\PyCharm 2024.1\bin
C:\<user_home>\AppData\Local\Programs\Ollama
C:\<user_home>\.lmstudio\bin
E:\System_Programes\texlive\2025\bin\windows
E:\System_Programes\JetBrains\IntelliJ IDEA Community Edition 2025.1.3\bin
E:\System_Programes\Fiddler
C:\<user_home>\AppData\Local\GitHubDesktop\bin
C:\<user_home>\AppData\Roaming\npm
```
（注：`.dotnet\tools` 重复 3 次，可去重）

### 其他环境变量
```
JAVA_HOME = E:\System_Programes\Java_JDK8\
OLLAMA_HOST = :11434
OLLAMA_MODELS = E:\Ollama\models
INTEL_DEV_REDIST = C:\Program Files (x86)\Common Files\Intel\Shared Libraries\
```

### 关键配置文件
- `.gitconfig`：用户 <copyright_holder>，email 101247328+<copyright_holder>@users.noreply.github.com，lfs 启用，sslVerify=false，safe.directory=*
- `.condarc`：envs_dirs=E:\<data_drive>:\<miniconda_root>\envs，pkgs_dirs=E:\<data_drive>:\<miniconda_root>\pkgs
- `.ssh`：id_ed25519 + id_rsa，config 配置 192.168.10.128 (ubuntu_admin)

### 重装步骤
1. 按上表安装各 SDK（建议顺序：Git → Java → Node → Conda → uv → .NET → texlive）
2. 运行第 9 节脚本设置环境变量
3. 恢复 `.gitconfig`、`.condarc`、`.ssh` 目录

### 可脚本化
✅ 环境变量 Path 设置
✅ .gitconfig / .condarc 恢复
⚠️ SDK 安装需手动（安装器交互）

---

## 3. VSCode 扩展 + 配置

### settings.json（完整）
```json
{
    "workbench.colorTheme": "Monokai Dimmed",
    "editor.maxTokenizationLineLength": 2000000,
    "[python]": {
        "diffEditor.ignoreTrimWhitespace": false,
        "editor.formatOnType": true,
        "editor.wordBasedSuggestions": "off"
    },
    "python.createEnvironment.contentButton": "show",
    "roo-cline.allowedCommands": ["npm test","npm install","tsc","git log","git diff","git show"],
    "security.workspace.trust.untrustedFiles": "open",
    "python.defaultInterpreterPath": "<按需修改>",
    "redhat.telemetry.enabled": false,
    "java.jdt.ls.java.home": "E:\\System_Programes\\Java_JDK17",
    "terminal.integrated.profiles.windows": {
        "PowerShell": {"source": "PowerShell","icon": "terminal-powershell"},
        "Command Prompt": {"path": "C:\\Windows\\System32\\cmd.exe","args": []},
        "Git Bash": {"source": "Git Bash","icon": "terminal-git-bash"}
    },
    "terminal.integrated.defaultProfile.windows": "Command Prompt",
    "github.copilot.chat.useResponsesApi": false,
    "musicScore.enginePath": "E:\\<data_drive>:\<system_data_root>\\Desktop\\music-score-vscode\\engine\\main.py",
    "musicScore.wavPlayer": "E:\\System_Programes_x86\\VLC\\vlc.exe",
    "remote.SSH.remotePlatform": {"192.168.10.128": "linux"},
    "makefile.configureOnOpen": true,
    "chat.tools.terminal.autoApprove": {"printf": true,"true": true}
}
```

### 扩展列表（去重后）
```
adpyke.codesnap
alibaba-cloud.tongyi-lingma
anthropic.claude-code
geequlim.godot-tools
james-yu.latex-workshop
ms-azuretools.vscode-containers
ms-ceintl.vscode-language-pack-zh-hans
ms-dotnettools.vscode-dotnet-modernize
ms-dotnettools.vscode-dotnet-runtime
ms-python.debugpy
ms-python.python
ms-python.vscode-pylance
ms-vscode-remote.remote-containers
ms-vscode-remote.remote-ssh
ms-vscode-remote.remote-ssh-edit
ms-vscode.cmake-tools
ms-vscode.cpp-devtools
ms-vscode.cpptools
ms-vscode.cpptools-extension-pack
ms-vscode.cpptools-themes
ms-vscode.remote-explorer
redhat.java
saoudrizwan.claude-dev
vscjava.vscode-gradle
vscjava.vscode-java-debug
vscjava.vscode-java-dependency
vscjava.vscode-java-pack
vscjava.vscode-java-test
vscjava.vscode-java-upgrade
vscjava.vscode-maven
vue.volar
```

### 重装步骤
1. 安装 VSCode 到 `E:\System_Programes\Microsoft VS Code\`
2. 恢复 settings.json 到 `%APPDATA%\Code\User\settings.json`
3. 批量装扩展：`code --install-extension <name>`（见第 9 节脚本）

### 可脚本化
✅ 扩展批量安装
✅ settings.json 恢复

---

## 4. Ollama + LM Studio

### 当前配置
- **Ollama**：
  - 环境变量 `OLLAMA_HOST=:11434`，`OLLAMA_MODELS=E:\Ollama\models`
  - ⚠️ `E:\Ollama\models` 目录当前不存在（可能模型未下载或路径已改）
  - 安装位置：`C:\<user_home>\AppData\Local\Programs\Ollama`
- **LM Studio**：
  - 配置：`%APPDATA%\LM Studio\`（settings.json, Local State, Preferences）
  - `.lmstudio` 目录在用户目录下

### 重装步骤
1. 从 ollama.com 下载安装
2. 设置环境变量 `OLLAMA_HOST=:11434`、`OLLAMA_MODELS=E:\Ollama\models`
3. 创建模型目录 `mkdir E:\Ollama\models`
4. 重新下载所需模型（`ollama pull <model>`）
5. LM Studio 从 lmstudio.ai 下载，恢复 `%APPDATA%\LM Studio\` 配置

### 可脚本化
✅ 环境变量 + 目录创建
⚠️ 模型下载需手动（体积大）

---

## 5. 桌面/任务栏/开始菜单布局

### 当前配置
- **用户桌面**：空（无图标）
- **公共桌面快捷方式**（14 个）：Dadroit JSON Viewer, Epic Games Launcher, GitHub Stars Manager, Handy, iKuuuVPN, Radmin VPN, TraceNG, Unity Hub, UU加速器, VMware Workstation Pro, 校园网上网认证客户端, 蛟龙游戏控制中心, 迷雾通, 鸣潮启动器
- **任务栏固定**：Google Chrome, 画图 (Paint)
- **DesktopOK**：已安装，配置 `DesktopOK.ini`（11550 bytes）保存了桌面布局
- **开始菜单布局**：旧记录声称已导出；当前证据应以 `../scan/start_layout.xml` 是否存在为准

### 重装步骤
1. **桌面快捷方式**：各软件安装后会自动创建，或手动从 `../scan/A_desktop_full.txt` 恢复
2. **任务栏固定**：手动右键固定 Chrome 和 画图
3. **开始菜单**：管理员 PowerShell 运行 `Import-StartLayout -LayoutPath start_layout.xml -MountPath C:\`
4. **DesktopOK**：安装后恢复 `DesktopOK.ini` 到 `%APPDATA%\DesktopOK\`，用 DesktopOK 恢复布局

### 可脚本化
✅ 开始菜单导入（Import-StartLayout）
⚠️ 任务栏固定需手动（注册表二进制，难以脚本化）

---

## 6. 启动项配置

### 当前启动项（7 项）

| 名称 | 类型 | 命令 |
|------|------|------|
| ctfmon | HKCU Run | `C:\Windows\system32\ctfmon.exe` |
| AMDNoiseSuppression | HKCU Run | `C:\Windows\system32\AMD\ANR\AMDNoiseSuppression.exe` |
| AB Download Manager | HKCU Run | `E:\GreenSoftware_Workspace\ABDownloadManager\ABDownloadManager.exe --background` |
| AutoStartManager | HKCU Run | `E:\<data_drive>:\<miniconda_root>\pythonw.exe E:\<data_drive>:\<system_data_root>\Desktop\main.py --run-startup` |
| Mem Reduct | HKCU Run | `E:\GreenSoftware_Workspace\mem64\memreduct.exe -minimized` |
| MAA_578A4D8B | HKCU Run | `E:\GreenSoftware_Workspace\MAA-v5.17.2-win-x64\MAA.exe` |
| EDG2 | HKLM Run | `C:\Program Files\HECATE GAMING HEADSET\CPL\Hecate Gaming Center_x64.exe /h /d` |
| KMCounter | Startup 文件夹 | `E:\GreenSoftware_Workspace\KMCounter\KMCounter-键鼠使用统计-会误报-在github开源.exe` |

### AutoStartManager 说明
`E:\<data_drive>:\<system_data_root>\Desktop\main.py` 是一个 Python 自启动管理器，读取同目录 `programs.json` 管理多个程序的自启动。重装后需恢复整个 `E:\<data_drive>:\<system_data_root>\Desktop\` 目录。

### 重装步骤
1. 各软件安装到位后，运行第 9 节脚本恢复注册表启动项
2. KMCounter：创建快捷方式到 `shell:startup`
3. AutoStartManager：恢复 `E:\<data_drive>:\<system_data_root>\Desktop\` 目录

### 可脚本化
✅ HKCU Run 注册表项（见第 9 节脚本）
✅ Startup 文件夹快捷方式

---

## 7. 系统工具配置

### TrafficMonitor
- 配置目录：`%APPDATA%\TrafficMonitor\`（当前为空，用默认配置）
- 开机自启：通过计划任务 `\TrafficMonitor\Autorun for admin`
- 重装：安装后配置显示项（CPU/内存/网速/天气）

### Tai（应用管理）
- 配置目录：`%APPDATA%\Tai1.5.0.6\`（当前为空）
- 路径：`E:\GreenSoftware_Workspace\Tai1.5.0.6\`

### MAA（明日方舟助手）
- 路径：`E:\GreenSoftware_Workspace\MAA-v5.17.2-win-x64\`
- 配置：目录内有 `Achievement_*.json`（成就记录）、任务配置
- 开机自启：HKCU Run `MAA_578A4D8B`
- 重装：恢复整个目录，配置连接 MuMu 模拟器

### Razer Synapse
- 配置：`%APPDATA%\Razer\` + `%LOCALAPPDATA%\Razer\`
- 服务：Razer Synapse Service + Razer Game Manager Service（自动启动）
- 重装：安装 Synapse，登录 Razer 账号同步外设配置（键鼠宏/灯光）

### VoiceMeeter Banana
- 配置：`%APPDATA%\VoiceMeeterDefault.xml` + `VoiceMeeterBananaDefault.xml`
- 重装：安装后导入 XML 配置恢复音频路由

### 其他工具（安装即用，配置较少）
- Everything、7-Zip、WinRAR、explorerpp、DeskPins、OnTopReplica、Handy、ProjectEye、mem reduct、ABDownloadManager、PixPin、LocalSend

### 可脚本化
⚠️ 大部分工具配置需 GUI 操作，难以脚本化
✅ 配置文件备份/恢复（复制 XML/ini）

---

## 8. 快速恢复脚本

见同目录 `restore_env.ps1`，可脚本化的部分：
- ✅ CUDA 环境变量
- ✅ 系统/用户 Path
- ✅ Ollama 环境变量 + 目录
- ✅ JAVA_HOME
- ✅ HKCU Run 启动项注册表
- ✅ VSCode 扩展批量安装

需手动操作的部分：
- ⚠️ CUDA/Java/Node/Conda/.NET 等 SDK 安装
- ⚠️ JetBrains IDE 合法许可证/账号激活（不走破解）
- ⚠️ 任务栏固定
- ⚠️ Razer Synapse 登录同步
- ⚠️ 模型下载

---

## 备份清单（重装前必做）

| 备份项 | 路径 | 说明 |
|--------|------|------|
| AutoStartManager | `E:\<data_drive>:\<system_data_root>\Desktop\` | main.py + programs.json |
| .gitconfig | `~\.gitconfig` | Git 配置 |
| .ssh | `~\.ssh\` | SSH 密钥（敏感） |
| .condarc | `~\.condarc` | Conda 配置 |
| VSCode 配置 | `%APPDATA%\Code\User\` | settings.json |
| VSCode 扩展 | `~\.vscode\extensions\` | 可选，重装会重新下载 |
| DesktopOK | `%APPDATA%\DesktopOK\` | 桌面布局 |
| 开始菜单 | `../scan/start_layout.xml` | 旧记录，恢复前重新确认 |
| MAA 配置 | `E:\GreenSoftware_Workspace\MAA-v5.17.2-win-x64\` | 任务配置 |
| VoiceMeeter | `%APPDATA%\VoiceMeeter*.xml` | 音频路由 |
| LM Studio | `%APPDATA%\LM Studio\` | 配置 |
| Razer | `%APPDATA%\Razer\` | 外设配置 |
| 浏览器数据 | Chrome/Edge 登录态 | 用浏览器同步 |
| 游戏存档 | Steam 云 + 各游戏目录 | Steam 自动云同步 |
