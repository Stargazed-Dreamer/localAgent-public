# 重装系统环境恢复方法论

这是“重装系统环境清单”的唯一入口。它面向两个读者：未来协助重装的 Agent，以及需要自己逐项确认的用户。

## 核心结论

重装不是“重新安装软件”，而是恢复五种状态：

1. **可启动状态**：Windows、驱动、网络、显示、音频和外设正常。
2. **可验证身份**：Microsoft、密码本、邮箱/手机、MFA、开发平台和游戏平台都能重新登录。
3. **可复现工具链**：Java、Python/Conda、uv、Node、.NET、Git、CUDA/cuDNN、SQL、VMware 等版本和环境变量可复现。
4. **可恢复数据**：项目、浏览器书签、聊天记录、游戏存档、应用配置和本地模型可恢复。
5. **可继续工作**：LocalAgent、IDE、代理、AI 工具和常用工作流通过验收，而不是只显示“已安装”。

## 不变的规则

- **清单和密码分离**：仓库只记录“需要什么”和“去哪里取”，不保存密码、Cookie、私钥明文、API token、订阅链接或激活码。
- **登录态不等于复制目录**：浏览器 Cookie、微信/QQ 会话、游戏平台 token 只通过官方同步、重新登录或受控导出恢复；不直接复制未知的登录数据库。
- **激活只记录合法路径**：Windows、Office、Adobe、JetBrains、VMware、Proxifier、NetLimiter 等记录许可证来源、账号和激活步骤；不自动恢复破解或绕过授权的文件。
- **只读先行**：重装前先生成快照和备份清单；重装后每阶段先验证再进入下一阶段。
- **证据驱动**：每个条目都有来源、恢复方式、验证方法和状态；扫描旧数据与当前机器冲突时，以当前只读扫描为准并标记漂移。
- **不覆盖 PATH**：恢复环境变量必须按键合并、去重、确认后写入，禁止用旧脚本整体覆盖系统或用户 PATH。

## 条目模型

机器可读条目见 `manifest.json`，人工勾选见 `checklist.md`。每个条目使用以下字段：

| 字段 | 含义 |
|---|---|
| `id` | 稳定标识，供 Agent 汇报和恢复脚本引用 |
| `domain` | 所属领域，如 identity/dev/game |
| `priority` | `P0` 必须恢复、`P1` 工作需要、`P2` 便利项、`P3` 可放弃 |
| `recovery` | `install` 安装、`copy` 复制文件、`reauth` 重新登录、`manual` 手动设置、`verify` 仅验收、`skip` 跳过 |
| `evidence` | 当前扫描或备份中证明它存在的路径/命令 |
| `pre_action` | 重装前必须完成的动作 |
| `post_action` | 重装后的恢复动作 |
| `activation_or_login` | 激活、登录、MFA、恢复码要求 |
| `verification` | 完成判定；没有验证方法就不算完成 |
| `status` | `unknown`、`ready`、`backed_up`、`restored`、`verified`、`blocked` |

## 阶段化流程

### A. 现在不重装时：维护基线

每次大版本升级、换账号、改路径或增加软件时，只做以下轻量维护：

- 更新 `manifest.json` 中的版本、路径、账号类型和验证命令。
- 把新增的配置文件加入备份白名单；把凭据加入“密码本/恢复码”清单，不把秘密写入仓库。
- 每季度做一次离线备份恢复演练：随机恢复一个配置文件、一个项目和一个游戏存档。
- 运行只读盘点，比较 `workspace/disk_manager/scan/` 与当前结果，记录路径或版本漂移。

### B. 重装前（P0/P1）

1. **冻结变更**：停止同步、下载、数据库写入和游戏更新，记录时间点。
2. **身份与密码本**：确认密码管理器名称、主密码可用、加密导出可解锁；导出 MFA 恢复码、SSH/GPG/安全密钥清单。
3. **备份数据**：项目和文档、浏览器书签、聊天记录、游戏存档、LocalAgent 数据、IDE 配置、代理配置、音频/外设配置、本地模型索引。
4. **备份软件证据**：已安装程序、版本、安装路径、环境变量、服务、计划任务、启动项、网络适配器、文件关联、桌面布局。
5. **离线验证**：在另一台设备或备份盘上检查文件数量、大小和 SHA-256；至少试开一个项目和一个加密凭据包。
6. **硬件前置**：准备网卡/芯片组/NVIDIA/AMD/外设驱动包和 Windows 安装介质，确认 BitLocker 恢复密钥。

### C. 重装后恢复顺序

1. **OS 基础**：Windows 更新、管理员账户、激活、BitLocker、时区/输入法/显示缩放、电源计划。
2. **驱动与网络**：AMD 芯片组、NVIDIA、Realtek、Razer/HECATE、音频驱动；再恢复 VMware VMnet、Radmin/ZeroTier、代理和校园认证。
3. **密码本与身份**：先恢复密码本和 MFA，再登录浏览器、邮箱、GitHub/ModelScope/LLM、通讯和游戏平台。
4. **基础工具链**：7-Zip、Git、PowerShell/终端、Python/Conda、uv、Node、Java 8/17、.NET、C++/Windows SDK、texlive。
5. **开发/AI 栈**：VS/VSCode/JetBrains、CUDA 12.9、cuDNN、Paddle/PyTorch、SQL/MongoDB、Ollama/LM Studio、LocalAgent。
6. **应用与工作流**：Office、Adobe、Obsidian、微信/QQ/腾讯会议、OBS/VLC、MAA、VoiceMeeter、Razer、下载/同步工具。
7. **游戏与数据**：Steam/Epic、模拟器、鸣潮/异环/明日方舟、Minecraft、独立游戏；最后恢复本地存档和桌面布局。
8. **验收与收尾**：按 `checklist.md` 全部验收；保留“未恢复/暂不恢复”列表，不用“差不多能用”结束任务。

## Agent 协作协议

Agent 接手任务时必须按下面顺序工作：

1. 读取本文件、`manifest.json`、`checklist.md`，再读取 `workspace/disk_manager/scan/` 的相关证据。
2. 先生成当前机器的只读差异报告，不直接安装、删除、导入注册表或覆盖配置。
3. 按阶段推进，每次只处理一个阶段；阶段内先恢复后验证，失败就记录 `blocked` 和证据。
4. 需要用户输入时，用“账号/密码本/恢复码/许可证/是否恢复本地存档”等最小问题，不要求用户把秘密发到对话中。
5. 遇到激活、MFA、登录态、私钥、Cookie、加密数据库时，只提供官方恢复路径并等待用户在本机完成。
6. 每完成一个条目，回写 `manifest.json` 的状态和验证时间；不要修改旧扫描文件来伪造完成。

## 当前已知的环境画像

基线来自 2026-06-23 扫描和 LocalAgent 当前项目：Windows 11 AMD64；双 CUDA（11.8/12.9）、cuDNN、Java 8/17、Miniconda、uv、Node、.NET 多版本、Git、texlive、SQL Server、VMware、Unity、Keil、Android SDK、GNS3、Wireshark、Fiddler；VS/VSCode/JetBrains/Trae/CodeBuddy/CatPawAI/Cursor；Chrome/Edge；微信/QQ/腾讯会议/企业微信；Office/Adobe；Steam/Epic/鸣潮/异环/明日方舟-MuMu/Minecraft；Clash/Geph/iKuuu/ZeroTier/Radmin/校园认证；OBS/VLC/VoiceMeeter/Razer；Ollama/LM Studio；LocalAgent 自身配置、记忆数据库和模型缓存。

这些是“待核实基线”，不是永久事实。尤其需要在下一次维护时确认：密码管理器、Windows/Office/Adobe/JetBrains 的合法授权方式、BitLocker、Chrome 日常配置与调试 Chrome 的边界、E/F 盘是否保留、Ollama 模型目录是否实际存在、MAA/MuMu 实例位置、开始菜单布局导出状态。

## 文件说明

- `manifest.json`：不含秘密的结构化资产/恢复条目；`software_catalog` 列出当前扫描到的 162 个软件/组件，`items` 提供 17 个可恢复域。
- `checklist.md`：重装前、重装后和验收阶段的人工检查表。
- `recovery_guide.md`：2026-06-23 旧指南，仅作历史参考；不要把其中的旧路径或激活绕过步骤当作现行方案。
- `restore_env.ps1`：旧的实验性脚本，当前不作为自动恢复入口；它存在整体覆盖 PATH 等风险，运行前必须重新审查。
- `../scan/`：原始只读扫描证据，不直接作为恢复脚本输入。
