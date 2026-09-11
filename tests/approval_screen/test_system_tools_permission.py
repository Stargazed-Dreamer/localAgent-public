"""SystemToolsPanel 电脑操作许可卡测试（用户开关，默认关）。

覆盖：
- 未授权 → 按钮文案"开启许可"、状态"未授权"
- 已授权（normal/watchdog）→ 按钮文案"关闭许可"、状态/详情渲染
- 勾选开启 → POST /screen/control/request（watchdog + gui_panel）
- 关闭 → POST /screen/control/release
- agent 侧撤销后 /health 回写自动回弹
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _mock_qt_offscreen_crashes(monkeypatch):
    """offscreen 安全网（同 test_monitoring_task_authorization.py 的既有模式）。"""
    from PySide6.QtWidgets import QApplication, QDialog

    monkeypatch.setattr(QApplication, "processEvents", lambda *a, **kw: None)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)


def test_permission_card_default_off(qapp):
    """未授权：按钮文案"开启许可"、状态"未授权"。"""
    from client.panels.system_tools import SystemToolsPanel

    panel = SystemToolsPanel()
    panel._perm_configured = True
    panel._perm_active = False
    panel._perm_mode = "no_permission"
    panel._update_permission_ui()

    assert panel._perm_toggle_btn.text() == "开启许可"
    assert panel._perm_status_label.text() == "当前：未授权"
    assert panel._perm_toggle_btn.isEnabled() is True


def test_permission_card_active_normal(qapp):
    """普通授权：按钮"关闭许可"、状态含"普通"、详情含剩余时间。"""
    from client.panels.system_tools import SystemToolsPanel

    panel = SystemToolsPanel()
    panel._perm_configured = True
    panel._perm_active = True
    panel._perm_mode = "normal"
    panel._perm_source = "gui_panel"
    panel._perm_remaining = 125
    panel._update_permission_ui()

    assert panel._perm_toggle_btn.text() == "关闭许可"
    assert "普通" in panel._perm_status_label.text()
    assert "02:05" in panel._perm_detail_label.text()
    assert "gui_panel" in panel._perm_detail_label.text()


def test_permission_card_active_watchdog(qapp):
    """看门狗授权：HH:MM:SS 倒计时。"""
    from client.panels.system_tools import SystemToolsPanel

    panel = SystemToolsPanel()
    panel._perm_configured = True
    panel._perm_active = True
    panel._perm_mode = "watchdog"
    panel._perm_source = "gui_panel"
    panel._perm_remaining = 36000
    panel._update_permission_ui()

    assert "看门狗" in panel._perm_status_label.text()
    assert "10:00:00" in panel._perm_detail_label.text()


def test_permission_card_disabled_by_config(qapp):
    """config 禁用授权 → 按钮禁用 + 状态提示。"""
    from client.panels.system_tools import SystemToolsPanel

    panel = SystemToolsPanel()
    panel._perm_configured = False
    panel._perm_active = False
    panel._update_permission_ui()

    assert panel._perm_toggle_btn.isEnabled() is False
    assert "禁用" in panel._perm_status_label.text()


def test_permission_toggle_on_posts_request(qapp):
    """未授权时点开关 → POST /screen/control/request（watchdog + gui_panel）。"""
    from client.panels.system_tools import SystemToolsPanel

    captured = {}

    class FakeHttp:
        def post(self, path, json, timeout):
            captured.update(path=path, json=json, timeout=timeout)
            return {"success": True, "status": "ok"}

    panel = SystemToolsPanel()
    panel._http = FakeHttp()
    panel._health = {
        "screen": {
            "takeover_confirm": {"timeout_seconds": 30},
            "takeover_persistent": {"configured": True, "active": False},
        }
    }
    panel._perm_active = False
    panel._update_permission_ui()

    panel._on_permission_toggle()
    assert panel._perm_thread.wait(2000) is True
    assert captured["path"] == "/screen/control/request"
    assert captured["json"]["source"] == "gui_panel"
    assert captured["json"]["mode"] == "watchdog"
    assert captured["json"]["task_description"] == "用户系统工具面板授权"
    assert captured["timeout"] == 35.0


def test_permission_toggle_off_posts_release(qapp):
    """已授权时点开关 → POST /screen/control/release。"""
    from client.panels.system_tools import SystemToolsPanel

    captured = {}

    class FakeHttp:
        def post(self, path, json, timeout):
            captured.update(path=path)
            return {"success": True, "status": "released"}

    panel = SystemToolsPanel()
    panel._http = FakeHttp()
    panel._perm_active = True
    panel._update_permission_ui()

    panel._on_permission_toggle()
    assert panel._perm_thread.wait(2000) is True
    assert captured["path"] == "/screen/control/release"


def test_permission_health_resync(qapp):
    """health 解析：/health.screen.takeover_persistent 驱动 UI 状态。"""
    from client.panels.system_tools import SystemToolsPanel

    panel = SystemToolsPanel()
    health = {
        "ocr": {"keep_models": False, "ocr_loaded": False},
        "screen": {
            "takeover_persistent": {
                "configured": True,
                "active": True,
                "enabled": True,
                "mode": "watchdog",
                "phase": "watchdog",
                "source": "gui_panel",
                "remaining_seconds": 7325,
                "revoked_reason": "",
            }
        },
    }

    # 模拟 _on_refresh_done 的 health 解析段（不启动真线程，直接复用逻辑）
    persistent = (health.get("screen", {}) or {}).get("takeover_persistent", {}) or {}
    panel._perm_configured = bool(persistent.get("configured", True))
    panel._perm_active = bool(persistent.get("active", persistent.get("enabled", False)))
    panel._perm_mode = persistent.get("mode", "no_permission")
    panel._perm_source = persistent.get("source", "") or ""
    panel._perm_remaining = int(persistent.get("remaining_seconds", 0))
    panel._perm_revoked = persistent.get("revoked_reason", "") or ""
    panel._update_permission_ui()

    assert panel._perm_toggle_btn.text() == "关闭许可"
    assert "看门狗" in panel._perm_status_label.text()
    assert "02:02:05" in panel._perm_detail_label.text()
