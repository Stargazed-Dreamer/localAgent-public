"""独立审批面板进程（lightweight，不依赖 client/core 重量级模块）。

启动方式：
    uv run python -m client.approval_panel
    或 tools/approval_panel.bat

职责：
- 长期挂在任务栏/托盘，展示待审批事项和过期倒计时
- 心跳上报让 server 感知面板在线（在线时审批走面板不弹窗）
- 轮询 pending 列表渲染卡片（Ticket 05 实现）
- 托盘模式 + 任务栏闪烁提醒（Ticket 06 实现）
"""
