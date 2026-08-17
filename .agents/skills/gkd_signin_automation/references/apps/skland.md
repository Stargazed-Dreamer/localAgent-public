# 森空岛

## 已确认流程

在明日方舟板块点击右上角检票，进入每日签到，执行现有的“签到福利 -> 领 -> 返回”规则。签到后入口图标从检票变为对勾。

## 2026-07-31 实测

- 包名：`com.hypergryph.skland`
- Activity：`com.hypergryph.skland.MainActivity`
- 入口节点：`android.widget.ImageView`
- `id=com.hypergryph.skland:id/check`
- `vid=check`
- `clickable=true`，`visibleToUser=true`
- 签到前后上述无障碍属性完全相同，仅 drawable 变化。

订阅 `17717` v1 同时使用完整 `id` 和短 `vid` 两条规则，日志证明两条都点击了同一节点。v2 只保留：

```text
@ImageView[vid="check"][clickable=true][visibleToUser=true]
```

v2 配置 `actionMaximum=1`、`resetMatch="app"`、`matchTime=30000`。2026-07-31 已签到状态回归只出现 `index:0` 一次，不再双击。

## 边界

`resetMatch="app"` 只控制 App 会话级执行，不等于自然日。由于入口节点在签到前后属性一致，当前规则不能从该节点判断“今天已签到”。如果要严格自然日去重，需要找到其他可访问状态信号或使用外部有状态编排。
