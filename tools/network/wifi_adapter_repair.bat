@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem =====================================================================
rem  WiFi 无线网卡一键修复
rem  适用: 无线网卡突然掉线/消失、托盘 WiFi 图标不见、netsh 报"没有无线接口"
rem  本机实测根因: Intel(R) Wi-Fi 6 AX200 的 miniport 报 fatal internal error,
rem                设备状态 CM_PROB_FAILED_POST_START, 而 WlanSvc 服务仍在运行
rem                所以必须重启"设备", 光重启服务或改 IP 栈没用
rem  分级: [1] 重启设备  [2] 禁用+启用+重扫  [3] 重启 WLAN 服务  [4] 提示重启电脑
rem  用法: 双击本文件; 或在命令行执行  <本文件名> fix   (非交互, 只跑修复)
rem  仓库主稿: tools/network/wifi_adapter_repair.bat (GBK+CRLF+无BOM, 桌面副本内容相同)
rem            改这份 bat 前先看 .agents/skills/bat_writing/SKILL.md 的编辑回路
rem  还原加固: reg delete "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /f
rem            reg add   "HKLM\SYSTEM\CurrentControlSet\Enum\[对应设备实例ID]\Device Parameters" /v PnPCapabilities /t REG_DWORD /d 0 /f
rem =====================================================================

set "ADAPTER=WLAN"
set "DEVICE_LIKE=Intel*Wi-Fi*"
set "FALLBACK_DEVID=PCI\VEN_8086&DEV_2723&SUBSYS_00848086&REV_1A\4&333C8B5&0&0012"
set "TMPF=%TEMP%\wifidoctor_id.txt"
set "AUTO=0"
set "TRY=0"
if /i "%~1"=="fix" set "AUTO=1"

net session >nul 2>&1
if not errorlevel 1 goto have_admin
echo 需要管理员权限, 正在申请提权(请在弹窗点是)...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%~1' -Verb RunAs"
exit /b

:have_admin
if "%AUTO%"=="1" goto fix

:menu
set /a TRY+=1
if %TRY% GTR 3 goto no_console
echo.
echo   ================= WiFi 网卡修复 =================
echo     [1] 一键修复    设备 - 禁用启用 - 服务 逐级尝试
echo     [2] 查看状态    只读, 不动任何东西
echo     [3] 一次性加固  关网卡节能 + 关快速启动(可逆, 先打印当前值)
echo     [0] 退出
echo   只操作无线网卡, 不影响有线网/VPN/虚拟网卡
echo   =================================================
set "CHOICE="
set /p CHOICE=  请输入序号: 
if "%CHOICE%"=="1" goto fix
if "%CHOICE%"=="2" goto status
if "%CHOICE%"=="3" goto harden
if "%CHOICE%"=="0" exit /b 0
echo 无效输入, 请重来
goto menu

:no_console
rem 被管道或无人值守拉起时 set /p 永远读不到内容, 不能死循环刷菜单
echo 连续 3 次没有有效输入, 判定为无交互控制台, 退出。非交互修复请跑: 本文件名 fix
exit /b 1

:fix
call :resolve_id
echo.
echo 目标设备实例: !DEVID!
call :check
if "!HEALTHY!"=="1" (
    echo 体检: 网卡当前正常, 不需要修复。
    call :show_state
    goto finish
)
echo 体检: 网卡异常, 当前状态如下
call :show_state

echo.
echo --- 第 1 级: 重启网卡设备 ---
pnputil /restart-device "!DEVID!"
call :wait_healthy 30
if "!HEALTHY!"=="1" goto recovered

echo.
echo --- 第 2 级: 禁用 - 启用 - 重扫硬件 ---
pnputil /disable-device "!DEVID!"
call :wait 3
pnputil /enable-device "!DEVID!"
pnputil /scan-devices >nul 2>&1
call :wait_healthy 45
if "!HEALTHY!"=="1" goto recovered

echo.
echo --- 第 3 级: 重启 WLAN AutoConfig 服务 ---
net stop WlanSvc >nul
call :wait 2
net start WlanSvc >nul
call :wait_healthy 30
if "!HEALTHY!"=="1" goto recovered

echo.
echo 三级都没救回来 = 驱动或 PCIe 层已崩, 需要重启电脑重新枚举设备。
if "%AUTO%"=="1" (
    echo 非交互模式: 不自动重启, 请自行重启电脑。
    goto finish
)
set "R="
set /p R=  现在重启电脑吗? (y/N): 
if /i "!R!"=="y" shutdown /r /t 5 /c "WiFi网卡修复: 5 秒后重启"
goto finish

:recovered
echo.
echo [OK] 网卡已恢复, 当前状态:
call :show_state
powershell -NoProfile -Command "$a = Get-NetAdapter -Name '%ADAPTER%' -ErrorAction SilentlyContinue; if ($a.Status -ne 'Up') { Write-Host '提示: 接口已复活但还没连上路由器, AutoConfig 通常几秒内自动重连; 没连上就在任务栏 WiFi 列表点一下。' }"
goto finish

:status
call :resolve_id
echo.
echo 体检结果:
call :check
if "!HEALTHY!"=="1" (echo   判定 = 正常) else (echo   判定 = 异常)
call :show_state
goto finish

:harden
call :resolve_id
set "PKEY=HKLM\SYSTEM\CurrentControlSet\Enum\!DEVID!\Device Parameters"
echo.
echo 加固改两处, 都是可逆的系统设置(重启电脑或重启网卡后生效):
echo   A. 网卡"允许计算机关闭此设备以节约电源" - 写 PnPCapabilities=24 屏蔽掉
echo   B. Windows 快速启动 - HiberbootEnabled=0 (开机略慢一点, 但避免唤醒把驱动带崩)
echo.
echo 当前值:
powershell -NoProfile -Command "$v=(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -ErrorAction SilentlyContinue).HiberbootEnabled; Write-Host ('  快速启动 HiberbootEnabled = ' + $v); $k=Get-ItemProperty '%PKEY%' -Name PnPCapabilities -ErrorAction SilentlyContinue; Write-Host ('  网卡 PnPCapabilities = ' + $k.PnPCapabilities)"
if "%AUTO%"=="1" goto harden_go
set "R="
set /p R=  确认执行加固? (y/N): 
if /i not "!R!"=="y" (
    echo 已取消, 未改动任何设置。
    goto finish
)
:harden_go
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f >nul
reg add "!PKEY!" /v PnPCapabilities /t REG_DWORD /d 24 /f >nul
echo 加固完成。还原方法见脚本开头注释。
goto finish

:resolve_id
set "DEVID="
del "%TMPF%" >nul 2>&1
powershell -NoProfile -Command "$d = Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -like '%DEVICE_LIKE%' } | Select-Object -First 1; if ($d) { $d.InstanceId | Out-File -FilePath '%TMPF%' -Encoding ascii }"
if exist "%TMPF%" set /p DEVID=<"%TMPF%"
if not defined DEVID set "DEVID=%FALLBACK_DEVID%"
goto :eof

:check
powershell -NoProfile -Command "$a = Get-NetAdapter -Name '%ADAPTER%' -ErrorAction SilentlyContinue; $d = Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -eq '%DEVID%' } | Select-Object -First 1; if ($a -and $a.Status -and $d -and $d.Status -eq 'OK') { exit 0 }; exit 1"
if errorlevel 1 (set "HEALTHY=0") else (set "HEALTHY=1")
goto :eof

:wait_healthy
rem %~1 = 最多等多少秒, 每 5 秒体检一次, 好了就提前返回
set /a "WLIMIT=%~1"
set /a "WUSED=0"
:wait_healthy_loop
call :check
if "%HEALTHY%"=="1" goto :eof
if %WUSED% GEQ %WLIMIT% goto :eof
call :wait 5
set /a WUSED+=5
goto wait_healthy_loop

:show_state
echo   --- 无线接口 ---
netsh wlan show interfaces
echo   --- 适配器 / 设备 / 服务 ---
powershell -NoProfile -Command "Get-NetAdapter -Name '%ADAPTER%' -ErrorAction SilentlyContinue | Format-List Name, InterfaceDescription, Status, LinkSpeed | Out-String -Width 200; Get-PnpDevice -Class Net -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -eq '%DEVID%' } | Format-List FriendlyName, Status, Problem | Out-String -Width 200; (Get-Service WlanSvc | Format-List Name, Status | Out-String -Width 200)"
goto :eof

:wait
powershell -NoProfile -Command "Start-Sleep -Seconds %~1"
goto :eof

:finish
echo.
if "%AUTO%"=="1" exit /b 0
pause
exit /b 0
