---
name: wuwa_gacha
description: >
  鸣潮游戏抽卡记录采集与增量更新。支持三种URL提取方式：云游戏网页（推荐，无需游戏客户端/管理员权限）、
  日志解密（无需管理员权限）和内存扫描（需管理员权限），调用官方API获取结构化抽卡数据，增量合并到数据库。
  触发词：鸣潮抽卡、更新鸣潮卡池、鸣潮抽卡记录、鸣潮唤取记录。当用户提到
  鸣潮的抽卡/唤取/卡池记录时触发。
---

# 鸣潮抽卡记录统计 Skill

## 概述
通过三种方式提取鸣潮抽卡认证URL，调用官方API获取结构化抽卡数据，增量合并到本地数据库。无需OCR，数据100%准确。三种方式共用同一数据库（`wuwa_gacha_database.json`）和原始数据目录（`raw/`），可任意切换或互为回退。

## URL提取方式

### 方式1: 云游戏网页（推荐，无需游戏客户端）
通过库洛云游戏网页入口 `https://mc.kurogames.com/cloud/index.html#/tools` 获取认证 URL：
1. 启动调试浏览器（CDP 9222），在浏览器中登录库洛账号
2. 脚本自动加载云游戏工具页
3. 检查登录态（识别"通行证ID: xxx"）
4. 点击"唤取记录"卡片（通过 JS dispatch 事件到 SVG 父元素，绕过 SVG text 不能直接点击的限制）
5. 从 `iframe.webview-core.src` 提取完整认证 URL（与游戏内 URL 格式完全一致）

**优点**：
- 无需启动游戏客户端
- 无需管理员权限
- 无需解密日志文件
- API 端点/请求格式/响应格式与游戏内方式完全一致

**限制**：
- 依赖调试浏览器保持登录态
- record_id 有效期约1小时，过期重新运行脚本即可刷新（脚本会重新加载页面并点击唤取记录）

**前置条件**：
- 调试浏览器已启动：`uv run python tools/browser/start_debug_browser.py`
- 在调试浏览器中已登录库洛账号（首次需手动登录）

### 方式2: 日志解密（无需管理员权限）
新版本鸣潮(3.4+)的 `Client.log` 已被库洛加密，但加密方案已知：
- **Scheme A**（PC端常见）：前3字节为加密header，body使用交替LUT解密
  - 奇数位字节 XOR `0xA5`
  - 偶数位字节 XOR `0xEF`
  - 加密header特征：`?? F1 F5`（解密后为 `?? 54 50` 即 `??TP`）
- **Scheme B**：前3字节为 `00 4C 4F`，body使用 `0x55` XOR 解密

流程：读注册表获取安装路径 → 定位 `Client.log` → LUT解密 → 搜索 `aki-gm-resources` URL

### 方式3: 内存扫描（回退方案，需管理员权限）
扫描 `Client-Win64-Shipping.exe` 和 `KRWebView.exe` 进程内存，查找包含 `aki-gm-resources` 的URL。

### 优先级
- **wuwa_cloud_gacha.py** 仅使用云游戏方式（方式1）
- **wuwa_gacha.py** 优先尝试日志解密（方式2，更稳定、无需管理员权限），失败后回退到内存扫描（方式3）
- 两种方式共用数据库，可交替使用：平时用云游戏方式（无需启动游戏），游戏已启动时可直接用 wuwa_gacha.py

## 卡池类型（API cardPoolType → 游戏内名称）
| cardPoolType | 游戏内名称 | 说明 |
|---|---|---|
| 1 | 角色活动唤取 | 角色活动池 |
| 2 | 武器活动唤取 | 武器活动池 |
| 3 | 角色常驻唤取 | 常驻角色池 |
| 4 | 武器常驻唤取 | 常驻武器池 |
| 5 | 新手唤取 | 新手池 |
| 6 | 新手自选唤取 | 新手自选池 |
| 7 | 武器新旅唤取 | 新旅武器池（0记录待验证） |
| 8 | 角色新旅唤取 | 新旅角色池（守岸人+维里奈等） |
| 9 | 武器联动唤取 | 联动武器池（0记录待验证） |
| 10 | 角色联动唤取 | 联动角色池 |

**注意**：
- 7和9目前0条记录，映射基于配对规律推断，待有记录时验证
- 角色池也会出3星武器（保底机制），cardPoolType=10的9条记录全是3星武器是正常的
- 卡池类型可能随版本更新增加，脚本会自动获取所有有数据的卡池

## API 关键参数
- **端点**: `POST {api_base}/gacha/record/query`
- **方法**: 必须是 POST（GET返回405）
- **参数名**: `languageCode`（不是 languageType！这是最常见的坑）
- **请求体**: JSON格式
- **国服**: `https://gmserver-api.aki-game2.com`
- **国际服**: `https://gmserver-api.aki-game2.net`

## 记录格式
每条记录：
```json
{
  "cardPoolType": "1",
  "resourceId": 1505,
  "qualityLevel": 5,
  "resourceType": "角色",
  "name": "守岸人",
  "count": 1,
  "time": "2026-05-25 18:16:46"
}
```

## 工作流程

### 方式1：云游戏（推荐，无需游戏客户端）

#### 前置条件
1. 调试浏览器已启动：`uv run python tools/browser/start_debug_browser.py`
2. 在调试浏览器中打开 `https://mc.kurogames.com/cloud/index.html#/tools` 并登录库洛账号
3. 登录态由调试浏览器 user-data-dir 保留，后续无需重复登录

#### 采集命令
```bash
uv run python workspace/wuwa_gacha/wuwa_cloud_gacha.py
```

#### 采集步骤
1. 连接 CDP（端口 9222）
2. 加载云游戏工具页，检查登录态（识别 `通行证ID: xxx`）
3. 点击"唤取记录"卡片（JS dispatch 到 SVG 父元素）
4. 从 `iframe.webview-core.src` 提取认证 URL
5. 解析 URL 参数（与游戏内 URL 格式完全相同，复用 `parse_gacha_url`）
6. 按卡池类型逐一调用 API 获取记录
7. 保存原始数据到 `raw/` 目录
8. 增量合并到 `wuwa_gacha_database.json`

#### 失效处理
- **未登录**：脚本会提示并退出，需在调试浏览器手动登录
- **record_id 过期**（约1小时）：直接重跑脚本，会自动重新加载页面并点击唤取记录刷新参数
- **CDP 连接失败**：先运行 `start_debug_browser.py` 启动调试浏览器

### 方式2/3：游戏客户端（日志解密 + 内存扫描回退）

#### 前置条件
1. 鸣潮游戏已启动并登录
2. **已在游戏内打开"唤取记录"页面**（停留几秒让URL写入日志/内存）
3. URL有效期约1小时，过期需重新打开

#### 采集命令
```bash
# 直接运行
uv run python workspace/wuwa_gacha/wuwa_gacha.py
```

#### 采集步骤
1. 优先尝试日志解密：读注册表 → 定位Client.log → LUT解密 → 搜索URL
2. 日志方式失败则回退内存扫描：扫描进程内存 → 查找URL
3. 解析URL中的认证参数（svr_id, player_id, record_id, resources_id）
4. 按卡池类型逐一调用API获取记录
5. 保存原始数据到 `raw/` 目录
6. 增量合并到 `wuwa_gacha_database.json`

### 增量更新规则（两种方式共用）
- API 返回每个卡池的完整快照，不因少量重复跳过整池
- 以稳定内容字段构建多重集，按快照中的出现次数合并，保留同秒同名的合法重复记录
- 重复运行同一快照新增必须为 0
- 任一卡池请求失败时保存本次 raw 诊断数据，但不写合并数据库
- **原始数据永不删除**（raw/ 目录每次采集生成一个带时间戳的文件）

### 统计输出
采集完成后自动生成统计摘要，包括：
- 各卡池总抽数、星级分布
- 五星角色/武器列表
- 保底间隔分析

## 日志解密技术细节

### 注册表路径
`HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\KRInstall Wuthering Waves`
- 键名：`InstallPath`
- 示例值：`F:\<data_drive>:\<game_install_root>\Wuthering Waves`

### 日志文件路径
`{InstallPath}\Wuthering Waves Game\Client\Saved\Logs\Client.log`

### 加密方案
| 方案 | Header特征 | 解密方式 | 适用场景 |
|------|-----------|---------|---------|
| Scheme A | `?? 54 50`（加密后 `?? F1 F5`） | 奇数位 XOR 0xA5，偶数位 XOR 0xEF | PC端（当前版本） |
| Scheme B | `00 4C 4F`（加密后 `55 19 1A`） | 全部 XOR 0x55 | 移动端/旧版本 |
| 明文 | 可直接读取UTF-8 | 无需解密 | 2024年6月前的旧版本 |

解密时跳过前3字节header，对body逐字节查LUT表替换。

### 参考来源
- 解密方案来自 [arglax/Mobile-WuWa-Config](https://arglax.github.io/Mobile-WuWa-Config/tools/log-decryptor.html) 的 JS 实现
- ycTool（`com.youchuang.yclink`）是2024年的旧工具，当时日志未加密可直接读取

## 安全策略
- 每次API请求间隔 1 秒
- 最多重试 3 次，指数退避
- 内存扫描仅读取进程内存，不修改任何游戏数据
- 日志解密仅读取文件，无需管理员权限
- URL有效期约1小时，过期需重新打开唤取记录页面

## ⚠️ 原始数据保护规则（极其重要！）
- **采集完成后立即保存不可变原始数据**，无论数据质量好坏
- 原始数据位置：`workspace/wuwa_gacha/raw/wuwa_gacha_raw_YYYYMMDD_HHMMSS.json`
- 每次采集生成一个带时间戳的文件，**永不覆盖、永不删除**
- **合并/统计操作只作用于数据库和摘要文件**，绝不修改 raw/ 目录中的文件

## 关键文件
| 文件 | 用途 | 覆盖规则 |
|------|------|----------|
| `workspace/wuwa_gacha/wuwa_cloud_gacha.py` | 云游戏采集脚本（方式1，推荐） | - |
| `workspace/wuwa_gacha/wuwa_gacha.py` | 游戏客户端采集脚本v3（方式2/3：日志解密+内存扫描+API+增量更新） | - |
| `workspace/wuwa_gacha/raw/wuwa_gacha_raw_*.json` | 不可变原始数据（每次采集一个文件） | **永不覆盖、永不删除** |
| `workspace/wuwa_gacha/wuwa_gacha_database.json` | 合并数据库（增量追加，两种方式共用） | 增量合并 |
| `workspace/wuwa_gacha/wuwa_gacha_summary.json` | 统计摘要 | 每次重写 |

## 触发词
- "鸣潮抽卡"、"更新鸣潮卡池"、"鸣潮抽卡记录"、"鸣潮唤取记录"

## 已知问题
- 内存扫描需要管理员权限
- URL有效期约1小时，过期需重新打开唤取记录
- 卡池类型可能随版本更新增加，需定期检查并更新映射
- API一次返回所有记录（无需分页），但未来版本可能改变
- 日志解密方案依赖逆向工程结果，游戏更新可能更换加密方式
- **云游戏方式**：依赖调试浏览器登录态；如果库洛改了云游戏前端结构（iframe class 名、卡片 aria-label 等），点击/提取逻辑需相应更新
- **云游戏方式**：偶尔首次加载页面后 iframe 不出现，重跑脚本即可（脚本每次重新 goto 页面，会重置 Vue 组件状态）
