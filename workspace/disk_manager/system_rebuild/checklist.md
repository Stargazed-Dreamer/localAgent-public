# 重装系统人工检查清单

> 使用方法：按阶段完成。每个勾选项都要填写“证据/时间/备注”；只安装软件但没有登录、配置或验证，不算完成。
> 凭据规则：密码、私钥、Cookie、恢复码和激活码只在本机密码本或加密备份中处理，不写进此文件。

## 0. 任务登记

- [ ] 重装原因、目标 Windows 版本和预计日期已记录
- [ ] 外置备份盘/第二份备份位置可用，剩余空间足够
- [ ] `manifest.json` 的版本、路径、优先级和状态已更新
- [ ] 本次不恢复的内容已明确列出，避免重装后反复猜测
- [ ] 已确认 E:、F: 等数据盘是否保留、盘符是否会变化

## 1. 重装前：身份、激活和密码本（P0）

### Windows 与设备

- [ ] Microsoft 账户可登录，Windows 数字许可证/产品密钥来源已确认
- [ ] BitLocker 是否启用已确认，恢复密钥已在密码本和离线位置各保存一份
- [ ] 主板/网卡/NVIDIA/AMD/外设驱动包已离线准备
- [ ] BIOS/UEFI 设置、Secure Boot、虚拟化、TPM 状态已记录

### 密码本与 MFA

- [ ] 已明确使用的密码管理器（名称、版本、同步账户）
- [ ] 主密码在本机可用；没有把主密码写在清单或对话中
- [ ] 已导出加密密码库，并在另一设备/浏览器配置中成功试解锁
- [ ] 邮箱、Microsoft、GitHub、ModelScope、LLM、游戏平台、微信/QQ 等 MFA 恢复码已导出
- [ ] 硬件安全密钥/Passkey/手机验证设备已盘点，至少有一种备用登录方式
- [ ] 重要 API token、SSH/GPG 私钥、npm/PyPI/容器仓库凭据已列入密码本，而不是散落在配置文件

### 激活与许可证

- [ ] Office 2016：账号/产品密钥/组织 KMS 来源已确认，重装后按合法方式激活
- [ ] Adobe Acrobat/Photoshop/Premiere/After Effects/Audition：账号、订阅或许可证来源已确认
- [ ] JetBrains IDEA/PyCharm/Rider：合法许可证/账号已确认；旧的破解文件不自动恢复
- [ ] VMware、Proxifier、NetLimiter、Beyond Compare、WinRAR 等商业软件的许可证来源已记录
- [ ] 游戏平台和加速器账号可用；不把平台 Cookie 当作备份

## 2. 重装前：数据和配置备份（P0）

### 必备数据

- [ ] 项目源码、Git 仓库、未提交改动、Issue/笔记、文档和桌面文件已同步
- [ ] LocalAgent：`config.toml`（脱敏副本）、`.agents/`、`server/`、`tools/`、`workspace/`、必要的 `data/` 已备份
- [ ] 记忆数据库、WIP、loop/todo 配置已备份；密钥文件单独加密，不放入普通压缩包
- [ ] Chrome/Edge 书签和扩展列表已导出；密码使用浏览器同步或密码本官方导入
- [ ] 微信/QQ/企业微信/腾讯会议需要保留的聊天记录已按官方方式备份
- [ ] Obsidian vault、calibre 图书库、TeX 文档、音乐/视频工程和 OBS 场景已备份

### 游戏和本地应用数据

- [ ] Steam/Epic 云同步状态已确认，Steam `userdata` 已有本地副本
- [ ] Minecraft、BakaXL、StardewValley、Duck Game、Kingdom Rush 等非纯云存档已定位并备份
- [ ] 鸣潮、异环、明日方舟/MuMu、MAA 的本地配置/截图/任务数据已定位并备份
- [ ] Wallpaper Engine、Godot、Unity 项目和资源位置已记录
- [ ] 游戏安装盘与存档盘的盘符、安装路径、启动器账号已记录

### 配置快照

- [ ] 用户/系统环境变量、PATH、Git、Conda、SSH、GPG、VSCode 配置已导出
- [ ] 服务、计划任务、HKCU/HKLM Run、启动文件夹、右键菜单、文件关联已生成只读快照
- [ ] VMware VMnet1/2/8、Radmin/ZeroTier、代理规则、Clash 配置和订阅来源已记录
- [ ] VoiceMeeter XML、Razer/HECATE、OBS、TrafficMonitor、DesktopOK 配置已备份
- [ ] `DesktopOK.ini`、任务栏固定项和开始菜单布局状态已记录；不要假设旧 XML 仍有效

## 3. 重装后：基础系统（P0）

- [ ] Windows 更新完成，设备管理器无未知设备
- [ ] 激活、BitLocker、时区、输入法、显示缩放、深色/浅色模式和电源计划已确认
- [ ] AMD 芯片组、NVIDIA、Realtek 2.5GbE、Razer/HECATE、音频驱动已安装
- [ ] `ipconfig`、浏览器访问、DNS、代理开关和校园认证可用
- [ ] 重要盘符和数据盘挂载正确；没有把旧路径直接写入新系统而未核对

## 4. 重装后：身份与登录（P0）

- [ ] 密码管理器已安装并从加密导出恢复，随机抽查 3 条记录可用
- [ ] Microsoft/邮箱/手机/MFA/Passkey 恢复成功
- [ ] Chrome/Edge 已登录并完成同步；书签、扩展和默认搜索引擎正确
- [ ] GitHub/GitLab/ModelScope/LLM 提供商账号恢复，token 只从密码本注入
- [ ] 微信、QQ、企业微信、腾讯会议登录并按需恢复聊天数据
- [ ] Steam、Epic、鸣潮、异环、明日方舟/MuMu、Minecraft 等账号登录

## 5. 重装后：开发与 AI 工具链（P0/P1）

### 基础运行时

- [ ] Git、Git LFS、SSH、GPG、Conda、uv、Python、Node、Java 8/17、.NET、C++/Windows SDK、texlive
- [ ] `git --version`、`git lfs version`、`python --version`、`uv --version`、`conda --version`
- [ ] `node --version`、`java -version`、`dotnet --list-sdks`、`where git` 输出符合 manifest
- [ ] PATH 采用合并方式恢复，重复项和失效路径已清理，未覆盖系统默认项

### IDE 与项目

- [ ] Visual Studio 工作负载、VSCode、IntelliJ IDEA、PyCharm、Rider、Sublime 已安装
- [ ] VSCode `settings.json`、扩展列表、Remote-SSH、Java/Python 解释器和终端配置已恢复
- [ ] Trae CN、CodeBuddy CN、CatPawAI、Cursor、ima.copilot 等按账号重新登录
- [ ] JetBrains 使用合法授权完成登录/激活；旧 `*_VM_OPTIONS` 仅在确认来源合法且路径有效后处理
- [ ] LocalAgent 依赖安装完成，`uv sync`、后端启动、MCP、GUI 和 Chrome CDP 验证通过

### GPU/数据库/虚拟化

- [ ] `nvidia-smi` 正常；`nvcc --version` 与 CUDA 12.9 默认路径一致
- [ ] CUDA 12.9、cuDNN、Nsight、Paddle/PyTorch 版本与项目要求一致
- [ ] SQL Server 2019、SSMS、ODBC/OLE DB 驱动、MongoDB 服务和连接字符串验证通过
- [ ] VMware Workstation、VMnet1/2/8、虚拟机文件和 USB/网络设置验证通过
- [ ] Ollama/LM Studio 安装，模型目录真实存在；`ollama list` 和一个模型调用成功

## 6. 重装后：网络、办公、媒体和外设（P1）

- [ ] Clash Verge、Geph、iKuuu、ZeroTier、Radmin、Oray、UU/雷神、校园认证按需恢复
- [ ] Office、Adobe、LibreOffice、Obsidian、calibre、TeXstudio 完成激活/登录/配置
- [ ] OBS 场景/插件、VLC、QQMusic、LosslessCut、UVR、Umi-OCR、PixPin、LocalSend 配置可用
- [ ] VoiceMeeter 音频路由导入，默认输入/输出和麦克风测试通过
- [ ] Razer Synapse/HECATE 登录同步，宏、灯光、耳机和降噪功能测试通过
- [ ] Everything、Sandboxie、NetLimiter、TrafficMonitor、Tai、DesktopOK、DeskPins 等按需恢复

## 7. 重装后：启动项、桌面和游戏（P1/P2）

- [ ] 只恢复确认过的 HKCU/HKLM Run、Startup 文件夹和计划任务
- [ ] AutoStartManager、MAA、Mem Reduct、AB Download Manager、KMCounter 路径逐项验证
- [ ] 任务栏固定 Chrome/画图，DesktopOK 布局恢复；开始菜单仅在新系统支持时导入
- [ ] Steam/Epic 游戏库重新识别，云存档与本地存档冲突时先保留两份再选择
- [ ] MAA 能连接 MuMu；鸣潮/异环/明日方舟和 Minecraft 实际启动并检查存档

## 8. 最终验收

- [ ] 冷启动后网络、代理、音频、外设、启动项符合预期
- [ ] 打开一个 Python/Java/.NET/C++ 项目并完成构建或测试
- [ ] LocalAgent 后端、MCP、GUI、浏览器调试实例和 OCR/VL 关键链路可用
- [ ] Git SSH 推送/拉取、远程 SSH、GitHub Desktop 工作流可用
- [ ] 浏览器、聊天、游戏平台、密码本均可重新登录，不依赖旧系统 Cookie
- [ ] 备份盘上的加密凭据包、项目和存档均可读取
- [ ] `manifest.json` 中所有 P0 为 `verified`；P1/P2 的未完成项已写明原因
- [ ] 生成一次“恢复后快照”，注明 Windows 版本、驱动、关键工具版本和剩余风险
