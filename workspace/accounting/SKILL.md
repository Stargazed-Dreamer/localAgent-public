---
name: accounting
description: >
  从微信/支付宝账单自动识别交易记录，按映射表分类并通过 PySide6 GUI 面板审核后导出 Obsidian 记账模板。
  触发词：记账、帮我记账、识别账单、整理账单。当用户提到账单、记账、微信支付账单、
  支付宝账单时触发，即使没有明确说"记账"。
task_type: recurring.accounting
---

# 记账 Skill

## 概述
从微信/支付宝官方导出的账单文件（xlsx/csv/zip）自动识别交易记录，按映射表分类，通过 PySide6 GUI 面板审核后导出 Obsidian 记账模板。数据存本地 SQLite（`data/accounting.db`），不走后端 HTTP。

### Obsidian 兼容约定

旧版账单仓库默认位于 `E:\<data_drive>:\<system_data_root>\Obsidian\Task\Task\main\记账`（可在 `config.toml` 的 `[accounting].accounting_dir` 覆盖）。其中 `YYYY-M.md` 是月度账单，`收支总览.md` 保留两段 DataviewJS：第一段按年份生成月度总览表，第二段按月份生成可展开的支出/收入/投资明细。GUI 只追加到已有月度 md，不改写总览文件，也不会覆盖旧条目。

> **Legacy Web 服务**：`workspace/accounting/accounting_server.py`（端口 8780）仍可用但不再推荐，仅作备用。新流程统一走 GUI 面板。

## 触发词
- "帮我记账"
- "记账"
- "识别账单"
- "整理账单"

## 工作流程

### 1. 准备账单文件
- 用户将微信/支付宝导出的账单文件放入 `workspace/accounting/账单/` 目录
- 支持格式：
  - 微信支付账单流水文件（.xlsx）
  - 支付宝交易明细（.csv，或包含 csv 的 .zip 加密压缩包）
- 导出方式：微信/支付宝 APP → 账单 → 导出

### 2. 打开 GUI 面板
- 启动客户端：`python -m client.main`
- 左侧导航点击 **💰 记账** tab
- 面板自动发现（PanelRegistry），无需后端 HTTP

### 3. 📥 导入账单
- 在 **📥 导入** tab 中点击"选择文件"，选中账单文件（xlsx/csv/zip）
- 支付宝 zip 文件需在密码框输入导出时设置的身份证后6位
- 点击"预览"查看解析结果（交易时间/对方/金额/原始名称）
- 点击"导入"写入 SQLite（`data/accounting.db`），自动分类并标记

### 4. ✏️ 审核分类
- 切到 **✏️ 审核** tab，使用过滤栏筛选（月份/标记/来源）
- 每行显示：日期 | 原始名称 | 金额 → 板块 | 大类 | 说明文本
- 交互方式（QComboBox 委托，可直接输入或下拉选）：
  - **板块**：下拉选择（出/进/理财）
  - **大类**：下拉选已有值或自由输入新值
  - **说明文本**：下拉选已有值或自由输入新值
- 标记颜色：
  - `✓` 绿色 — 自动映射
  - `!` 橙色 — 需确认（可变映射）
  - `?` 红色 — 未映射
  - `⊘` 灰色 — 跳过项（不计入导出）
- 修改后条目高亮黄色，板块/大类/说明改变后自动标记 `✓` 并写入 SQLite

### 5. 📤 导出 Obsidian
- 切到 **📤 导出** tab，选择月份
- 预览已确认交易列表
- 点击"导出"：追加写入 Obsidian 记账 md 文件（append 模式，不覆盖已有数据）
- 可选勾选"git commit"自动提交

### 6. 核对结果
- 检查 Obsidian 记账 md 文件中的数据是否正确
- 如有错误，可在 GUI 中修改后重新导出，或手动修改 md 后 git commit

## 其他 tab

- **📅 月度**：按实际存在的年月查看可折叠明细 + 支出/收入/净额统计
- **📊 汇总**：起止年月 + 可选详细日期 + 来源筛选，行=月份列=大类的交叉表
- **🔧 映射表**：展示固定/可变/跳过/退款 4 类映射、触发次数和搜索补全；顶部可新增、重命名或删除板块内的大类

大类重命名会同步历史交易、固定映射、可变映射和说明文本；删除只移除可选配置及依赖映射，历史交易仍保留原分类和板块，后续相同流水会重新进入审核。

## 板块与分类

### 出（支出）
- **吃**：餐饮、零食、水果、超市食品、外卖
- **学习资料**：书籍、课程
- **娱乐**：游戏、电影、KTV、聚餐、群红包
- **生活**：话费、校园网、饮用水、宿舍水电费、日用品
- **交通**：出行费用
- **电动车**：普通大类示例；与其他大类一样允许收入/支出同时存在

### 进（收入）
- **商业副业**：副业收入
- **活动返现**：红包、返现
- **宿舍共享**：群收款收入、转账收入
- **妈妈**：来自妈妈的转账
- **工资**：工资收入

### 理财
- **零钱通**、**余额宝**、**CS市场**、**基金**

## 存储层

数据存入 SQLite（`data/accounting.db`，路径由 `config.toml [accounting] db_path` 配置）：

| 表 | 用途 |
|------|------|
| `transactions` | 每条交易一行（原始字段 + 分类字段 + 确认状态） |
| `mapping_fixed` | 固定映射（map_key → name + category） |
| `mapping_variable` | 可变映射（需确认） |
| `skip_patterns` | 跳过模式 |
| `refund_keywords` | 退款关键词 |
| `config_sectors` | 板块→大类列表 |
| `config_descriptions` | 大类→说明文本列表 |
| `import_batches` | 导入批次记录 |
| `mapping_events` | 每次导入的映射命中/未命中记录，用于诊断 |

首次启动时自动从旧 `name_mapping.json` / `review_config.json` 迁移数据到 SQLite。

## 映射表
- `fixed`：固定映射（名称固定，每次都是同一种东西）
- `variable`：需确认映射（每次内容不同，如超市、外卖平台）
- `skip_patterns`：跳过项（零钱通转出等资金流转）

确认导出后：
- 未映射(`?`)和自动映射修正(`✓`)的映射会添加到 fixed 中
- 可变映射(`!`)的修改不写入 fixed，因为每次内容不同

## 记账模板格式
```
## 支出
###### 吃：金额1名称1+金额2名称2+...
###### 学习资料：
###### 娱乐：
###### 生活：金额名称+...
###### 交通：
###### 电动车：金额名称+...

## 收入
###### 商业副业：
###### 活动返现：+金额1名称1+金额2名称2+...
###### 宿舍共享：+金额1名称1+...
###### 妈妈：金额
###### 工资：0

## 投资
###### 零钱通：
###### 余额宝：
###### CS市场：
###### 基金：
```

## 关键规则
- 退款、AA 回流等流水保留在原大类中，以方向形成正负号，不再配对或创建“退款/待对冲”伪大类
- 已有数据按截止时间追加，不覆盖
- 群收款/转账按方向区分（收入→宿舍共享，支出→需确认事由）
- 更新记账文件后必须 git commit
- 收入行格式：+金额名称（不加额外+号）
- GUI 面板不走后端 HTTP，直读 SQLite + 直读 config.toml

## 依赖
- 客户端 GUI（`python -m client.main`，PySide6）
- openpyxl（读取 xlsx）
- config.toml `[accounting]` 段配置 `accounting_dir`（Obsidian 路径）和 `db_path`（SQLite 路径）

## 文件结构
```
F:\<project_root>\
├── workspace/accounting/              # 记账任务工作区（组件化模块）
│   ├── panel.py                       # GUI 面板（6 tab，PanelRegistry 自动发现）
│   ├── panel_widgets.py               # GUI 辅助组件（_AccountingCalendar / _MappingDelegate 等）
│   ├── manifest.toml                  # 组件 manifest（声明 client_panel/agent_guide/skill 插入点）
│   ├── 账单/                          # 用户放入账单文件
│   ├── store.py                       # SQLite 存储层（8 张表）
│   ├── parser.py                      # 账单解析层（微信 xlsx + 支付宝 zip）
│   ├── exporter.py                    # Obsidian md 导出层
│   ├── bill_converter.py              # 旧脚本（纯函数被 parser/exporter 复用，OUTPUT_DIR 指向 private_vault/accounting/）
│   ├── name_mapping.json              # 旧映射表（首次启动迁移到 SQLite）
│   └── review_config.json             # 旧审核配置（首次启动迁移到 SQLite）
├── private_vault/accounting/          # 账单核对私有产出（obsidian vault，不进 release）
│   ├── bill_review.txt                # legacy 文本核对（含个人消费记录，bill_converter.OUTPUT_DIR 写入）
│   └── bill_review_data.json          # legacy JSON 账单数据（accounting_server / health 读取）
├── data/accounting.db                 # SQLite 数据库（GUI 存储层）
└── server/component_config.py         # get_component_config("accounting") 读组件配置
```

## Legacy Web 服务（不推荐，仅备用）

`workspace/accounting/accounting_server.py`（端口 8780）仍可独立启动，提供 Web 审核页面 + 6 个 `/accounting/*` 端点。新流程统一走 GUI 面板，此服务仅供特殊场景备用。

| 路径 | 方法 | 说明 |
|------|------|------|
| `/accounting/status` | GET | 审核模块状态 |
| `/accounting/review-data` | GET | 获取审核数据 |
| `/accounting/config` | GET | 获取审核配置 |
| `/accounting/config` | POST | 更新审核配置 |
| `/accounting/generate` | POST | 生成审核数据 |
| `/accounting/apply` | POST | 确认并应用审核结果 |
