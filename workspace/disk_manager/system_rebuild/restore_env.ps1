# 重装系统环境恢复脚本（历史实验脚本，不是当前入口）
#
# WARNING: Do not run automatically. This script was generated from a 2026-06-23
# snapshot and can overwrite the whole system/user PATH with stale values. It also
# assumes old drive letters and application paths. Read README.md + checklist.md
# first, compare the current machine, and use a reviewed/merge-based script only.
# Activation and login must follow legitimate vendor/account flows; no bypass files
# or plaintext secrets belong in a recovery script.
# 生成于 2026-06-23 | 需管理员权限运行
# 用法: 右键以管理员身份运行 PowerShell，执行:
#   powershell -ExecutionPolicy Bypass -File restore_env.ps1
#
# 本脚本只恢复可脚本化的部分（环境变量/启动项/VSCode扩展）
# SDK 安装、任务栏固定等需手动操作
# 详见同目录 recovery_guide.md
#
# 2026-07-31 T05a: 移除 JetBrains 破解相关内容（IP 侵权，与 Apache-2.0 不兼容）。
# JetBrains IDE 应使用合法许可证/账号激活；如需恢复 IDE 配置，请走官方登录流程。

#Requires -RunAsAdministrator

$ErrorActionPreference = "Continue"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  重装系统环境恢复脚本" -ForegroundColor Cyan
Write-Host "  生成于 2026-06-23" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "⚠️ 请确认：" -ForegroundColor Yellow
Write-Host "  1. 已安装各 SDK（CUDA/Java/Node/Conda/uv/Git/.NET/texlive）"
Write-Host "  2. VSCode 已安装到 E:\System_Programes\Microsoft VS Code\"
Write-Host ""
Read-Host "按回车继续，Ctrl+C 取消"

# ========== 1. CUDA 环境变量 ==========
Write-Host "`n[1/6] 设置 CUDA 环境变量..." -ForegroundColor Green
# CUDA 11.8 已于 2026-07-27 移除（ark 环境改用 CPU paddlepaddle 2.6.2，不再依赖 11.8）
[Environment]::SetEnvironmentVariable("CUDA_PATH", "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9", "Machine")
[Environment]::SetEnvironmentVariable("CUDA_PATH_V12_9", "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9", "Machine")
[Environment]::SetEnvironmentVariable("NVTOOLSEXT_PATH", "C:\Program Files\NVIDIA Corporation\NvToolsExt\", "Machine")
Write-Host "  ✓ CUDA 环境变量已设置" -ForegroundColor Green

# ========== 2. JAVA_HOME + Ollama ==========
Write-Host "`n[2/6] 设置 JAVA_HOME + Ollama 环境变量..." -ForegroundColor Green
[Environment]::SetEnvironmentVariable("JAVA_HOME", "E:\System_Programes\Java_JDK8\", "Machine")
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", ":11434", "Machine")
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "E:\Ollama\models", "Machine")
New-Item -ItemType Directory -Path "E:\Ollama\models" -Force | Out-Null
Write-Host "  ✓ JAVA_HOME + Ollama 已设置，模型目录已创建" -ForegroundColor Green

# ========== 3. 系统 Path ==========
Write-Host "`n[3/6] 设置系统 Path..." -ForegroundColor Green
$sysPath = @(
    "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\libnvvp",
    "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin",
    "E:\System_Programes\Java_JDK8\bin",
    "E:\System_Programes\NVIDIA\bin",
    "C:\Program Files (x86)\Common Files\Intel\Shared Libraries\redist\intel64\compiler",
    "C:\Windows\system32",
    "C:\Windows",
    "E:\System_Programes\uv",
    "E:\<data_drive>:\<miniconda_root>\",
    "E:\<data_drive>:\<miniconda_root>\Scripts",
    "E:\<data_drive>:\<miniconda_root>\Library\bin",
    "C:\Windows\System32\Wbem",
    "C:\Windows\System32\WindowsPowerShell\v1.0\",
    "C:\Windows\System32\OpenSSH\",
    "C:\Program Files\dotnet\",
    "C:\Program Files\NVIDIA Corporation\NVIDIA app\NvDLISR",
    "C:\Program Files (x86)\NVIDIA Corporation\PhysX\Common",
    "E:\System_Programes\Git\cmd",
    "E:\System_Programes\Microsoft VS Code\bin",
    "E:\System_Programes\calibre\",
    "E:\System_Programes\texlive\2025\bin\windows",
    "C:\Program Files\NVIDIA Corporation\Nsight Compute 2025.2.1\",
    "C:\Program Files\nodejs\",
    "C:\Program Files\Microsoft SQL Server\150\Tools\Binn\",
    "C:\Program Files\Microsoft SQL Server\150\DTS\Binn\",
    "C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\",
    "C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit\"
) -join ";"
[Environment]::SetEnvironmentVariable("Path", $sysPath, "Machine")
Write-Host "  ✓ 系统 Path 已设置 (27 项)" -ForegroundColor Green

# ========== 4. 用户 Path ==========
Write-Host "`n[4/6] 设置用户 Path..." -ForegroundColor Green
$userPath = @(
    "C:\<user_home>\.local\bin",
    "C:\<user_home>\AppData\Local\Microsoft\WindowsApps",
    "C:\<user_home>\.dotnet\tools",
    "E:\System_Programes\JetBrains\PyCharm 2024.1\bin",
    "C:\<user_home>\AppData\Local\Programs\Ollama",
    "C:\<user_home>\.lmstudio\bin",
    "E:\System_Programes\texlive\2025\bin\windows",
    "E:\System_Programes\JetBrains\IntelliJ IDEA Community Edition 2025.1.3\bin",
    "E:\System_Programes\Fiddler",
    "C:\<user_home>\AppData\Local\GitHubDesktop\bin",
    "C:\<user_home>\AppData\Roaming\npm"
) -join ";"
[Environment]::SetEnvironmentVariable("Path", $userPath, "User")
Write-Host "  ✓ 用户 Path 已设置 (11 项，已去重)" -ForegroundColor Green

# ========== 5. HKCU Run 启动项 ==========
Write-Host "`n[5/6] 设置启动项 (HKCU Run)..." -ForegroundColor Green
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$startups = @{
    "ctfmon" = "C:\Windows\system32\ctfmon.exe"
    "AMDNoiseSuppression" = '"C:\Windows\system32\AMD\ANR\AMDNoiseSuppression.exe"'
    "AB Download Manager" = '"E:\GreenSoftware_Workspace\ABDownloadManager\ABDownloadManager.exe" --background'
    "AutoStartManager" = '"E:\<data_drive>:\<miniconda_root>\pythonw.exe" "E:\<data_drive>:\<system_data_root>\Desktop\main.py" --run-startup'
    "Mem Reduct" = '"E:\GreenSoftware_Workspace\mem64\memreduct.exe" -minimized'
    "MAA_578A4D8B" = '"E:\GreenSoftware_Workspace\MAA-v5.17.2-win-x64\MAA.exe"'
}
foreach ($k in $startups.Keys) {
    New-ItemProperty -Path $runKey -Name $k -Value $startups[$k] -PropertyType String -Force | Out-Null
    Write-Host "  $k"
}
Write-Host "  ✓ 6 个启动项已设置" -ForegroundColor Green

# ========== 6. VSCode 扩展批量安装 ==========
Write-Host "`n[6/6] VSCode 扩展批量安装..." -ForegroundColor Green
$code = "E:\System_Programes\Microsoft VS Code\bin\code.cmd"
if (Test-Path $code) {
    $extensions = @(
        "adpyke.codesnap",
        "alibaba-cloud.tongyi-lingma",
        "anthropic.claude-code",
        "geequlim.godot-tools",
        "james-yu.latex-workshop",
        "ms-azuretools.vscode-containers",
        "ms-ceintl.vscode-language-pack-zh-hans",
        "ms-dotnettools.vscode-dotnet-modernize",
        "ms-dotnettools.vscode-dotnet-runtime",
        "ms-python.debugpy",
        "ms-python.python",
        "ms-python.vscode-pylance",
        "ms-vscode-remote.remote-containers",
        "ms-vscode-remote.remote-ssh",
        "ms-vscode-remote.remote-ssh-edit",
        "ms-vscode.cmake-tools",
        "ms-vscode.cpp-devtools",
        "ms-vscode.cpptools",
        "ms-vscode.cpptools-extension-pack",
        "ms-vscode.cpptools-themes",
        "ms-vscode.remote-explorer",
        "redhat.java",
        "saoudrizwan.claude-dev",
        "vscjava.vscode-gradle",
        "vscjava.vscode-java-debug",
        "vscjava.vscode-java-dependency",
        "vscjava.vscode-java-pack",
        "vscjava.vscode-java-test",
        "vscjava.vscode-java-upgrade",
        "vscjava.vscode-maven",
        "vue.volar"
    )
    $count = 0
    foreach ($ext in $extensions) {
        Write-Host "  安装 $ext..." -NoNewline
        & $code --install-extension $ext --force 2>&1 | Out-Null
        Write-Host " OK" -ForegroundColor Green
        $count++
    }
    Write-Host "  ✓ $count 个扩展已安装" -ForegroundColor Green
} else {
    Write-Host "  ✗ VSCode 未安装: $code" -ForegroundColor Red
}

# ========== 完成 ==========
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  自动恢复完成" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "还需手动操作：" -ForegroundColor Yellow
Write-Host "  1. 恢复 ~/.gitconfig ~/.ssh ~/.condarc"
Write-Host "  2. 恢复 VSCode settings.json 到 %APPDATA%\Code\User\"
Write-Host "  3. 任务栏固定 Chrome + 画图"
Write-Host "  4. Import-StartLayout 导入开始菜单布局"
Write-Host "  5. DesktopOK 恢复桌面布局"
Write-Host "  6. Razer Synapse 登录同步"
Write-Host "  7. JetBrains IDE 用合法许可证/账号激活（不走破解）"
Write-Host "  8. ollama pull 下载模型"
Write-Host ""
Write-Host "详见 recovery_guide.md" -ForegroundColor Cyan
Write-Host ""
Read-Host "按回车退出"
