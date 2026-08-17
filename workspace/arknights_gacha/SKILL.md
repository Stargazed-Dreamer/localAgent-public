---
name: arknights_gacha
description: >
  明日方舟寻访记录采集与统计。触发词：明日方舟抽卡、方舟寻访、更新方舟记录、arknights抽卡、
  方舟抽卡统计、导入小黑盒数据、方舟寻访记录。当用户要求获取或更新明日方舟的寻访（抽卡）记录，
  或导入小黑盒导出数据时触发。
---

# 明日方舟寻访记录 (arknights_gacha)

## 触发词
明日方舟抽卡、方舟寻访、更新方舟记录、arknights抽卡、方舟抽卡统计、方舟寻访记录、导入小黑盒数据、小黑盒导入

## 概述
明日方舟寻访记录的采集、导入与统计。支持两种数据来源：
1. **官网采集**：Playwright + CDP 操控浏览器，模拟翻页点击+拦截API响应
2. **小黑盒导入**：将小黑盒导出的历史全量数据增量合并到数据库

## 前置条件
- **官网采集**：Chrome 调试浏览器已启动（CDP 端口 9222）
- **小黑盒导入**：将导出的 JSON 文件放入 `workspace/arknights_gacha/` 目录

## 工作流

### 流程 A：官网采集（90天内数据）

#### A1：检查登录状态
1. 连接调试浏览器，查找 `hypergryph.com` 页面
2. 若无明日方舟页面，打开 `https://ak.hypergryph.com/user/home`
3. 检查页面是否包含"前往登录"文字
4. 若已登录，跳到 A3

#### A2：浏览器登录
1. 在浏览器中打开鹰角通行证登录页
2. **用户手动完成登录**（手机号+验证码/密码/扫码均可）
3. 脚本通过 XHR/Fetch 拦截 + Cookie 轮询自动获取认证 token
4. 等待登录成功（最多3分钟）

#### A3：采集寻访记录（v4 优化版）
1. 用 `page.route` 将 API 请求的 `size=10` 改为 `size=50`（翻页次数减少5倍）
2. 导航到寻访记录页面 `https://ak.hypergryph.com/user/headhunting?menu=1`
3. 刷新页面，拦截API获取初始数据
4. 获取页面上的卡池分类列表（`div.LbzcSW`）
5. 逐分类采集：
   a. 点击分类 → 拦截API响应（`/user/api/inquiry/gacha/history`）
   b. 检查"暂无数据"→ 有数据则继续
   c. 循环点击"下一页"按钮，拦截每页响应（间隔0.5秒）
   d. 当 `hasMore=False` 或无下一页按钮时停止
6. 去重（按 `gachaTs + charId + pos`）

#### A4：处理与保存
1. 转换原始API记录为统一格式（毫秒时间戳→秒，稀有度映射2-5→★3-★6）
2. 增量合并到已有数据（按秒级时间戳 + 卡池 + 干员 + 十连位置 `pos` + 稀有度 + `is_new` 去重）
3. 保存原始数据到 `workspace/arknights_gacha/raw/`
4. 保存完整数据到 `workspace/arknights_gacha/gacha_records.json`
5. 生成统计摘要到 `workspace/arknights_gacha/summary.json`
6. 更新卡池注册表到 `workspace/arknights_gacha/pool_registry.json`

### 流程 B：小黑盒数据导入（历史全量）

#### B1：准备数据
1. 用户从小黑盒导出寻访记录 JSON 文件
2. 将文件放入 `workspace/arknights_gacha/` 目录（文件名通常为 `{uid}.json`）

#### B2：运行导入
1. 运行 `python workspace/arknights_gacha/arknights_import.py`
2. 脚本自动查找 `workspace/arknights_gacha/` 下的 JSON 文件
3. 解析小黑盒数据格式：`{timestamp: {c: [[name, rarity, isNew], ...], p: "pool_name"}}`
4. 增量合并到 `gacha_records.json`（使用与官网相同的跨来源唯一键）
5. 生成/更新卡池注册表和统计摘要

#### B3：指定文件或预览
```bash
python workspace/arknights_gacha/arknights_import.py --file path/to/file.json  # 指定文件
python workspace/arknights_gacha/arknights_import.py --dry-run                  # 只预览不写入
```

## 卡池数据架构

### 卡池注册表 (`pool_registry.json`)

每个卡池一条记录，字段设计：

```json
{
  "name": "承诺",
  "start_time": "2026-05-01",
  "end_time": "2026-05-14",
  "category": "活动寻访",
  "pull_count": 75,
  "up_operators": [],
  "six_star_pulled": ["凯尔希·思衡托", "乌尔比安"]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 卡池名称（来自数据源） |
| start_time | string | 最早抽卡日期 |
| end_time | string | 最晚抽卡日期 |
| category | string | 分类推断（限定寻访/标准寻访/中坚寻访/联合行动/定向甄选/活动寻访/特殊寻访） |
| pull_count | int | 总抽数 |
| up_operators | list | UP干员列表（**留空待补充**） |
| six_star_pulled | list | 实际出金列表（自动统计） |

**分类推断规则**：
- 名称含"限定"/"庆典" → 限定寻访
- 名称含"中坚甄选" → 中坚甄选
- 名称含"中坚" → 中坚寻访
- 名称含"标准"/"常驻" → 标准寻访
- 名称含"联合" → 联合行动
- 名称含"甄选" → 定向甄选
- 名称含"跨年" → 特殊寻访
- 其他 → 活动寻访

**UP干员补充方式**（后续探索）：
- 手动编辑 `pool_registry.json`
- 从 PRTS Wiki 抓取卡池信息
- 用户口述补充

## 关键规则

- **原始数据永不覆盖、永不删除**：每次采集保存独立的 raw 文件
- **增量更新不删原始数据**：合并新记录到已有数据，只增不删
- **不直接调用API**：浏览器内 fetch/XHR 不带认证，必须通过模拟页面操作触发请求
- **page.route 修改请求参数**：v4 用 `page.route` 将 `size=10` 改为 `size=50`，减少翻页次数
- **用户手动登录**：v4 不再自动填写密码/验证码，由用户在浏览器中手动登录（更安全，不怕人机验证）
- **登录态联合验证**：只检查可见登录提示，并以寻访分类成功加载和 history API 响应作为最终证据
- **跨来源去重**：官网毫秒时间戳与小黑盒秒时间戳归一到秒，`pos` 保留同秒同名的合法十连结果
- **强制模式不替换历史**：`--force` 只重新采集官网可见全量，仍与历史数据库合并
- **翻页间隔0.5秒**：v4 优化后 API 直接返回数据，无需等待页面渲染
- **API返回的稀有度是2-5**：对应★3-★6（明日方舟没有★1★2）
- **小黑盒数据优先用于历史**：官网只有90天数据，小黑盒可提供全量历史

## 数据源对比

| 特性 | 官网采集 | 小黑盒导入 |
|------|---------|-----------|
| 数据范围 | 90天内 | 全量历史 |
| 卡池信息 | poolId + poolName | 仅 poolName |
| 干员ID | charId | 无 |
| isNew | 有 | 有 |
| 认证 | 需登录 | 无需 |
| 风控 | 需模拟操作 | 无 |

## 依赖

| 依赖 | 路径/接口 | 说明 |
|------|----------|------|
| 采集脚本 | `workspace/arknights_gacha/arknights_gacha.py` | 官网采集 v4 |
| 导入脚本 | `workspace/arknights_gacha/arknights_import.py` | 小黑盒数据导入 |
| 浏览器 | CDP 端口 9222 | Chrome 调试实例（仅官网采集需要） |

## 输入/输出

- **小黑盒输入**：`workspace/arknights_gacha/{uid}.json`
- **官网输入**：明日方舟官网寻访记录页面（浏览器自动访问）
- **原始数据**：`workspace/arknights_gacha/raw/raw_{timestamp}.json`
- **完整数据**：`workspace/arknights_gacha/gacha_records.json`
- **卡池注册表**：`workspace/arknights_gacha/pool_registry.json`
- **统计摘要**：`workspace/arknights_gacha/summary.json`

## 命令行用法

```bash
# 官网采集
python workspace/arknights_gacha/arknights_gacha.py                # 采集所有记录（默认增量）
python workspace/arknights_gacha/arknights_gacha.py --force        # 重新采集官网全量并与历史合并

# 小黑盒导入
python workspace/arknights_gacha/arknights_import.py               # 从 workspace/arknights_gacha/ 导入
python workspace/arknights_gacha/arknights_import.py --file xxx.json  # 指定文件
python workspace/arknights_gacha/arknights_import.py --dry-run     # 只预览不写入
```
