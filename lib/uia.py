"""L0 公用 UIA helper：取焦点控件 ValuePattern 快照

设计（D019 / D021 / D022）：
- 提供最小化的 UIA 能力：取当前焦点控件的 ValuePattern.Value + IsPassword 标志
- 给 L0 M1 KeyboardSensor 做"中文输入法三层检测"的 Strategy 2（UIA ValuePattern 差值）用
- 不依赖 server/screen/uia.py（lib/ 必须独立于 server/，D019）
- uiautomation 库 lazy import：测试环境无桌面时调用方注入 fake snapshot_fn，不真调系统 API
- 后端未来迁移到 lib/uia 时（独立工作，不在 L0 范围内），可在此扩展更多能力

返回结构：
    {
        "value": str,           # 焦点控件 ValuePattern.Value（无 ValuePattern 时为 ""）
        "is_password": bool,    # 是否密码框（IsPassword=True）
    }

失败时返回 None（UIA 不可用 / 无前台窗口 / 无焦点控件）。
"""

from typing import Any


def take_focused_value_snapshot() -> dict[str, Any] | None:
    """取当前焦点控件的 ValuePattern 快照。

    Returns:
        {"value": str, "is_password": bool} 或 None（UIA 不可用）
    """
    try:
        import uiautomation as ua
    except ImportError:
        return None

    try:
        # GetForegroundWindow 返回前台窗口的 Control
        fg_window = ua.GetForegroundWindow()
        if fg_window is None:
            return None
        # GetFocusControl 返回窗口内当前焦点控件
        _fg_dynamic: Any = fg_window  # ua 桩把 GetForegroundWindow 标为 int，运行时是 Control
        focused = _fg_dynamic.GetFocusControl()
        if focused is None:
            return None
        # 取 ValuePattern（不是所有控件都支持，不支持时 value=""）
        # 注：uiautomation 2.0.29 无 GetValuePattern 便捷方法，用通用 GetPattern；
        # PatternId 不存在或 pattern 不支持时由外层 except 降级为空值
        try:
            vp = focused.GetPattern(ua.PatternId.ValuePattern)
            value = vp.Value if vp is not None else ""
            is_password = bool(getattr(vp, "IsPassword", False)) if vp is not None else False
        except Exception:
            value = ""
            is_password = False
        return {"value": value, "is_password": is_password}
    except Exception:
        return None
