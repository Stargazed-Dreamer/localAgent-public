# _vendor/ — 第三方 Skill 来源档案

**状态**：本目录已于 **2026-07-21 完成融合改造**，所有 vendor 副本已删除。所有 vendor 均已按 [ADR-0003](../../../docs/adr/0003-vendor-skill-absorption-criteria.md) 的 4 项标准评估并完成融合：

- **cangjie-skill** → 融合到 `.agents/skills/daily/cangjie_extraction/`（含 `methodology/` / `extractors/` / `templates/` / `scripts/` / `upstream/`）
- **impeccable** → 融合到 `.agents/skills/dev/impeccable/`（含 `reference/` 30 文件 + `scripts/` + `upstream/`）
- **hallmark** → 融合到 `.agents/skills/dev/hallmark/`（含 `references/` 25+ 主题 + `upstream/`）
- **mattpocock-zh** → `engineering/` 和 `productivity/` 桶已分别被 `dev/` 和 `daily/` 改造接入（见 `dev/README.md`、`daily/README.md`），`workspace/dev_toolkit/` 同步保留

本文件作为**唯一来源档案**保留，记录各 vendor 的上游 URL / 同步 commit / 融合状态，便于将来"想起来更新时"做大动作同步。

## Vendor 来源清单

| Vendor | 上游 URL | 上游 commit | License | 同步时间 | 融合状态 |
|---|---|---|---|---|---|
| **cangjie-skill** | https://github.com/kangarooking/cangjie-skill | （depth=1 浅克隆，commit 未记录） | MIT | 2026-07-20 同步，2026-07-21 融合 | ✅ 已融入 `.agents/skills/daily/cangjie_extraction/` |
| **impeccable** | https://github.com/pbakaus/impeccable | 84135db（上游同步到 2026-05-23） | Apache-2.0 | 2026-07-21 同步并融合 | ✅ 已融入 `.agents/skills/dev/impeccable/`（含 reference/ 30 命令规范 + scripts/ 11 类方法论 + upstream/ 原版档案） |
| **hallmark** | https://github.com/nutlope/hallmark | aeb42fb（上游同步到 2026-06-05） | MIT | 2026-07-21 同步并融合 | ✅ 已融入 `.agents/skills/dev/hallmark/`（含 references/ 25+ 主题 + upstream/ 原版档案） |
| **mattpocock-zh** | https://github.com/vinvcn/mattpocock-skills-zh-CN | 391a270（上游同步到 2026-07-12） | MIT | 2026-07-20 同步，2026-07-21 删除 | ✅ 已通过 `workspace/dev_toolkit/` 间接吸收（`engineering/` 和 `productivity/` 桶已分别被 `dev/` 和 `daily/` 改造接入） |

## 安全审查结论（历史快照）

所有 vendor 在同步时已做安全审查，**风险等级均为低**。原审查详情见各 vendor 同步时的 `GITHUB_REPO.md`（已随融合一并消失，但融合后的 `upstream/GITHUB_REPO.md` 保留关键元数据），关键结论：

- **cangjie-skill**：纯方法论 + Python 脚本（generate_star_history.py），无网络请求，无 install 钩子
- **impeccable**：无 install 钩子，所有网络访问仅指向官方 `impeccable.style`，主动硬跳过 `.env*`/`.git/`/`id_rsa*`/`*.pem` 等敏感文件，zip 解包有 zip-slip 防护
- **hallmark**：无 npm 依赖、无 install/build 钩子、无后端代码，JS 不读环境变量不调子进程；唯一第三方脚本是 demo 站点的 Plausible Analytics
- **mattpocock-zh**：纯 md skill + Node.js 自检脚本（check-translation.mjs），无运行时副作用

## 吸收判断标准（ADR-0003）

详见 [docs/adr/0003-vendor-skill-absorption-criteria.md](../../../docs/adr/0003-vendor-skill-absorption-criteria.md)。4 项标准全部成立才吸收：

1. **方法论普适性**：skill 包含可在多个项目复用的方法论，而非项目特定代码或一次性脚本
2. **未来场景预期**：用户原话确认未来场景可能用到，或 skill 主题与项目方向（前端 / 数据 / 自动化 / 文档等）有重合可能
3. **dev_toolkit 适配性**：skill 值得纳入 `workspace/dev_toolkit/` 作为新项目启动模板的一部分
4. **融合成本可控**：能按 "Vendor 克隆 + 适配 wrapper" 模式融合（`upstream/` 原版档案 + wrapper SKILL.md 路径适配），不破坏项目本体

**明确反对**的判断模式：因当前项目前端联系不紧密就放弃吸收方法论 skill。

## 未来更新流程

**触发条件**：用户主动说"跟进 cangjie-skill 上游" / "看看 impeccable 有没有新版本" / "重新同步 vendor" 等大动作更新意图。

**操作步骤**：

```powershell
# 1. 浅克隆最新版本到临时目录（按需选 vendor）
git clone --depth 1 https://github.com/kangarooking/cangjie-skill.git temp/cangjie-skill-clone
git clone --depth 1 https://github.com/pbakaus/impeccable.git temp/impeccable-clone
git clone --depth 1 https://github.com/nutlope/hallmark.git temp/hallmark-clone
git clone --depth 1 https://github.com/vinvcn/mattpocock-skills-zh-CN.git temp/mattpocock-zh-clone

# 2. 对比新旧版本的 package.json scripts / 网络访问模式 / 新增依赖（重点）
#    - 重新做安全审查，更新本文件 "安全审查结论" 段
# 3a. 对 cangjie-skill：将新版 methodology/ extractors/ templates/ scripts/ SKILL.md 复制覆盖
#     `.agents/skills/daily/cangjie_extraction/{methodology,extractors,templates,scripts,upstream/SKILL.md}`
#     然后重新审视 daily/cangjie_extraction/SKILL.md wrapper 的适配点是否需要调整
# 3b. 对 impeccable：将新版 .pi/skills/impeccable/{SKILL.md,reference,scripts} 复制覆盖
#     `.agents/skills/dev/impeccable/{SKILL.md,reference,scripts}` + 更新 upstream/ 原版档案
#     然后重新审视 dev/impeccable/SKILL.md wrapper 的适配点是否需要调整
# 3c. 对 hallmark：将新版 skills/hallmark/{SKILL.md,references} 复制覆盖
#     `.agents/skills/dev/hallmark/{SKILL.md,references}` + 更新 upstream/ 原版档案
#     然后重新审视 dev/hallmark/SKILL.md wrapper 的适配点是否需要调整
# 4. 更新本文件的"同步时间"和"上游 commit"
# 5. 同步更新本体 + workspace/dev_toolkit/ 副本（4 处同步，按 ADR-0003 吸收后必做）
# 6. 提交 commit：chore(vendor): 跟进 <vendor> 上游到 <commit>
```

## 与 _index.md / agent_guide 的关系

- 本目录**不进** `agent_guide` 路由，**不进** `_index.md` skill 列表
- 已融合的 vendor 在 `_index.md` 中以独立节出现：
  - `daily/cangjie_extraction`：第 53 节
  - `dev/impeccable`：第 57 节
  - `dev/hallmark`：第 58 节
- 已融合的 vendor 在 `server/agent_guide.py` 的 `GUIDE_REGISTRY` 中均有对应条目（`daily.cangjie_extraction` / `dev.impeccable` / `dev.hallmark`）
- `mattpocock-zh` 不出现在 `_index.md` 中，但其 `engineering/` 和 `productivity/` 桶已分别被 `dev/` 和 `daily/` 桶改造吸收
