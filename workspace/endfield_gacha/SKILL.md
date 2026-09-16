---
name: endfield_gacha
description: >
  终末地寻访记录采集与统计。触发词：终末地抽卡、终末地寻访、endfield抽卡、终末地抽卡记录、
  终末地寻访记录、更新终末地记录。当用户要求获取或更新终末地（Endfield）的寻访（抽卡）记录时触发。
  ⚠️ 本地日志路径读取功能未经测试，使用时必须提醒用户并全程跟进。
task_type: recurring.endfield_gacha
---

# 终末地寻访记录 (endfield_gacha)

## 触发词
终末地抽卡、终末地寻访、endfield抽卡、终末地抽卡记录、终末地寻访记录、更新终末地记录

## 概述
终末地寻访记录的采集与统计。支持三种 token 获取方式（优先级从高到低）：
1. **读取游戏日志**：从 `HGWebview.log` 中提取 u8_token（需先在游戏内打开寻访记录页）
2. **浏览器登录**：打开鹰角通行证，用户手动登录，拦截认证 token
3. **命令行传入**：`--token` 直接使用已有 token

> **⚠️ 未测试提醒：本地日志路径读取功能（`read_token_from_log()`）尚未经过实际测试。**
> 当使用日志方式获取 token 时，必须明确告知此功能未测试，执行过程中需全程跟进并记录问题。

**与明日方舟的关键区别**：终末地拿到 token 后可直接调 API，无需模拟翻页操作，流程更简单。

## 前置条件
- **游戏日志方式**：已安装终末地游戏，且在游戏内打开过寻访记录页面
- **浏览器登录方式**：Chrome 调试浏览器已启动（CDP 端口 9222）
- `config.toml` 配置 `[endfield]` provider（默认 `hypergryph` 国服）
- 用户有鹰角通行证账号

## 工作流

### E0：获取认证信息（三种方式，自动选择）

**方式 A：读取游戏日志（优先，无需浏览器）**
1. 读取游戏日志文件：
   - 国服：`%USERPROFILE%/AppData/LocalLow/Hypergryph/Endfield/sdklogs/HGWebview.log`
   - 国际服：`%USERPROFILE%/AppData/LocalLow/Gryphline/Endfield/sdklogs/HGWebview.log`
2. 搜索包含 `ef-webview.{provider}.com/page/gacha_` 的 URL
3. 解析 URL 参数提取 `u8_token`、`server` 等（兼容早期 `token`、`server_id` 命名）
4. 前置条件：用户需先在游戏内打开寻访记录页面，让 URL 写入日志
5. 日志方式直接拿到 u8_token，**无需 token 交换**，跳到 E4

**方式 B：浏览器登录（日志失败时回退）**
1. 连接调试浏览器（CDP 端口 9222）
2. 打开鹰角用户中心：
   - 国服：`https://user.hypergryph.com/`
   - 国际服：`https://user.gryphline.com/`
3. 注入 token 拦截脚本（XHR 拦截 + Fetch 拦截 + Cookie 轮询）
4. **提示用户在浏览器中登录**（手机号+验证码/密码/扫码均可）
5. 脚本自动拦截登录成功后的认证 token（最多等 6 分钟）

**方式 C：命令行传入（`--token`）**
1. 直接使用已有的认证 token
2. 需要后续 token 交换步骤

### E1：Token 交换（仅浏览器/命令行方式需要）
1. 用认证 token 调用 `oauth2/v2/grant`（appCode: `be36d44aa36bfb5b`）换取 grant token
2. 用 grant token + uid 调用 `u8_token_by_uid` 换取 u8_token
3. 用 u8_token 查询角色信息（昵称、服务器等）

### E2：采集角色寻访记录
角色池有 4 种类型，逐池采集：

| pool_type | 名称 |
|-----------|------|
| `E_CharacterGachaPoolType_Special` | 特许寻访 |
| `E_CharacterGachaPoolType_Joint` | 辉光庆典 |
| `E_CharacterGachaPoolType_Standard` | 基础寻访 |
| `E_CharacterGachaPoolType_Beginner` | 启程寻访 |

API 端点：`GET https://ef-webview.hypergryph.com/api/record/char`

参数：`lang`, `token`, `server_id`, `pool_type`, `seq_id`（游标分页）

### E3：采集武器寻访记录
1. 先获取武器池列表：`GET /api/record/weapon/pool`
2. 逐池采集记录：`GET /api/record/weapon`（参数：`pool_id`, `seq_id`）

### E4：处理与保存
1. 转换原始记录为统一格式（毫秒时间戳→秒，稀有度映射 4-6→★4-★6）
2. 增量合并到已有数据（按 seqId 去重）
3. 保存原始数据到 `workspace/endfield_gacha/raw/`
4. 保存角色记录到 `workspace/endfield_gacha/char_records.json`
5. 保存武器记录到 `workspace/endfield_gacha/weapon_records.json`
6. 生成统计摘要到 `workspace/endfield_gacha/summary.json`

## 增量同步机制

基于 **seqId 游标**，比明日方舟的 timestamp 去重更精确：

1. 首次采集：全量拉取所有记录
2. 后续采集：读取已有记录中每个池子的最大 seqId
3. API 请求时从最大 seqId 开始拉取，遇到已存在的 seqId 即停止
4. 新记录按“池标识 + seqId”增量合并到已有数据

## 数据格式

### 角色记录 (`char_records.json`)

```json
{
  "type": "char",
  "timestamp": 1749900000,
  "time": "2025-06-14 12:00:00",
  "poolType": "E_CharacterGachaPoolType_Special",
  "poolId": "pool_001",
  "pool": "特许寻访",
  "charId": "char_1001",
  "name": "角色名",
  "rarity": 6,
  "rarity_label": "★6",
  "is_new": true,
  "is_free": false,
  "seqId": "12345"
}
```

### 武器记录 (`weapon_records.json`)

```json
{
  "type": "weapon",
  "timestamp": 1749900000,
  "time": "2025-06-14 12:00:00",
  "poolId": "weapon_pool_001",
  "pool": "武器池名",
  "weaponId": "weapon_2001",
  "name": "武器名",
  "rarity": 5,
  "rarity_label": "★5",
  "is_new": false,
  "seqId": "12340"
}
```

### 统计摘要 (`summary.json`)

```json
{
  "total": 500,
  "char_count": 400,
  "weapon_count": 100,
  "rarity_count": {"★6": 8, "★5": 45, "★4": 447},
  "six_star_count": 8,
  "six_star_names": ["角色A(特许寻访)", "角色B(辉光庆典)"],
  "char_pool_count": {"特许寻访": 120, "基础寻访": 200},
  "weapon_pool_count": {"武器池A": 60},
  "user": {
    "uid": "12345678",
    "roleId": "87654321",
    "roleName": "玩家昵称",
    "serverName": "CN-1",
    "server_id": "1",
    "provider": "hypergryph"
  },
  "updated_at": "2026-06-14 21:00:00"
}
```

## 服务器配置

| 服务器 | provider | 登录页 | API 基础 URL | appCode |
|--------|----------|--------|-------------|---------|
| 国服 | `hypergryph` | `user.hypergryph.com` | `ef-webview.hypergryph.com` | `be36d44aa36bfb5b` |
| 国际服 | `gryphline` | `user.gryphline.com` | `ef-webview.gryphline.com` | `3dacefa138426cfe` |

## 关键规则

- **原始数据永不覆盖、永不删除**：每次采集保存独立的 raw 文件
- **增量更新只增不删**：新记录合并到已有数据，seqId 去重
- **不需要模拟翻页**：拿到 u8_token 后直接调 API，比明日方舟简单
- **优先读日志**：v2 优先从游戏日志获取 u8_token，无需浏览器和 token 交换
- **日志方式直接拿到 u8_token**：跳过 auth_token → grant → u8_token 交换流程
- **用户手动登录**：浏览器方式下，AI 负责打开页面和拦截 token，用户在浏览器中输入手机号+验证码
- **seqId 游标分页**：API 返回 `{code, data: {list, hasMore}}`，用最后一条的 seqId 翻页
- **分页完整性校验**：seqId 必须严格递减、游标必须前进；HTTP/API/页数异常时不写数据库
- **强制模式不替换历史**：`--force` 重新采集全量后仍与已有数据合并
- **国服 serverId 固定为 "1"**：国际服从角色列表中获取
- **稀有度 4-6**：对应★4-★6（终末地没有★1-★3）

## 日志路径

| 服务器 | 日志路径 |
|--------|---------|
| 国服 | `%USERPROFILE%/AppData/LocalLow/Hypergryph/Endfield/sdklogs/HGWebview.log` |
| 国际服 | `%USERPROFILE%/AppData/LocalLow/Gryphline/Endfield/sdklogs/HGWebview.log` |

日志中搜索的 URL 模式：`https://ef-webview.{provider}.com/page/gacha_...`

当前国服 URL 使用 `u8_token` 和 `server` 参数；脚本同时兼容早期的 `token` 和 `server_id`。

前置条件：用户需先在游戏内打开寻访记录页面，让 URL 写入日志。

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 采集脚本 | `workspace/endfield_gacha/endfield_gacha.py` | 终末地寻访记录采集 v2 |
| 配置 | `config.toml [endfield]` | provider (hypergryph/gryphline) |
| 游戏日志 | `HGWebview.log` | 优先 token 来源（无需浏览器） |
| 浏览器 | CDP 端口 9222 | Chrome 调试实例（日志失败时回退） |
| HTTP | httpx | API 请求（采集数据用） |

## 输入/输出

- **输入**：游戏日志（优先）、鹰角通行证登录（浏览器回退）或 `--token` 直接传入认证 token
- **原始数据**：`workspace/endfield_gacha/raw/raw_{timestamp}.json`
- **角色记录**：`workspace/endfield_gacha/char_records.json`
- **武器记录**：`workspace/endfield_gacha/weapon_records.json`
- **统计摘要**：`workspace/endfield_gacha/summary.json`

## 命令行用法

```bash
# 默认：优先读游戏日志，失败回退浏览器登录
python workspace/endfield_gacha/endfield_gacha.py                # 国服，增量采集
python workspace/endfield_gacha/endfield_gacha.py --force        # 重新采集全量并与历史合并
python workspace/endfield_gacha/endfield_gacha.py --global       # 国际服
python workspace/endfield_gacha/endfield_gacha.py --log          # 仅从日志获取 token（不回退浏览器）

# 直接使用已有 token（跳过浏览器登录）
python workspace/endfield_gacha/endfield_gacha.py --token YOUR_AUTH_TOKEN
```

## 与其他抽卡 Skill 对比

| 特性 | 终末地 | 明日方舟 | 鸣潮 |
|------|--------|---------|------|
| 认证方式 | 游戏日志/浏览器登录 | 浏览器登录→模拟翻页 | 日志解密/内存扫描 |
| 数据获取 | 直接 API 调用 | 模拟翻页+拦截响应 | 直接 API 调用 |
| 增量同步 | seqId 游标 | timestamp 去重 | name\|time 去重 |
| 需要安装游戏 | 日志方式需要 | 否 | 是（日志/内存） |
| 服务器 | 国服+国际服 | 仅国服 | 仅国服 |
| 卡池类型 | 4种角色池+武器池 | 6种寻访分类 | 10种卡池类型 |
