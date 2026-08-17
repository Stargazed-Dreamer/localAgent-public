"""活动追踪闭环包

归集活动追踪相关的所有模块：
- loop_manager: Loop 调度器
- loop_actions: Action 实现（ScreenVL / HourlySummarize / CollectWindows 等）
- activity: daily 报告 CRUD API
- vl_quota: VL 配额管理器
- activity_signal: 活动信号检测（idle/focus_changed）
"""
