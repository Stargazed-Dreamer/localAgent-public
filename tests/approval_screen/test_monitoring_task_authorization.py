"""Monitoring panel visibility for current-task Computer Use authorization."""

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
    """autouse 安全网：mock offscreen 环境下会 crash 的 Qt 调用。

    教训（2026-08-03）：offscreen 平台 + WindowStaysOnTopHint/Dialog flag +
    processEvents / QDialog.exec 组合触发 access violation（Windows fatal exception）。
    之前只逐个测试 monkeypatch（头痛医头），同文件其他测试仍 crash。

    正确做法：autouse fixture 在模块级别统一 mock，所有测试都不调真实 processEvents/exec：
    - QApplication.processEvents → no-op（测试断言不依赖事件循环处理，show() 后
      isVisible/height 已更新）
    - QDialog.exec → 返回 Accepted（不弹真实模态对话框，测试验证业务逻辑而非 UI 交互）

    符合"危险操作测试铁律"的 autouse 安全网原则：从机制上兜底，防任何测试意外触发 crash。
    """
    from PySide6.QtWidgets import QApplication, QDialog

    monkeypatch.setattr(QApplication, "processEvents", lambda *a, **kw: None)
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)


def test_monitoring_shows_inactive_available_state(qapp):
    from client.panels.monitoring import MonitoringPanel

    panel = MonitoringPanel()
    panel._health = {
        "screen": {
            "takeover_persistent": {
                "configured": True,
                "active": False,
                "enabled": False,
            }
        }
    }
    panel._update_task_authorization_controls()

    assert panel._task_authorization_status_label.text() == "当前任务未授权"
    assert panel._permission_check.isEnabled() is True
    assert panel._permission_check.isChecked() is False


def test_monitoring_shows_task_and_remaining_time(qapp):
    from client.panels.monitoring import MonitoringPanel

    panel = MonitoringPanel()
    panel._health = {
        "screen": {
            "takeover_persistent": {
                "configured": True,
                "active": True,
                "enabled": True,
                "mode": "normal",
                "phase": "normal",
                "task_description": "整理窗口",
                "remaining_seconds": 299,
                "source": "agent",
                "shutdown_permitted": False,
            }
        }
    }
    panel._update_task_authorization_controls()

    text = panel._task_authorization_status_label.text()
    # 新文案：普通执行模式 · {task} · 来源 {source} · 剩余 MM:SS
    assert "普通执行模式" in text
    assert "整理窗口" in text
    assert "04:59" in text
    assert panel._permission_check.isEnabled() is True
    assert panel._permission_check.isChecked() is True


def test_monitoring_shows_normal_warning_phase(qapp):
    """normal 告警阶段（10-30 分钟）显示"即将降级"文案。"""
    from client.panels.monitoring import MonitoringPanel

    panel = MonitoringPanel()
    panel._health = {
        "screen": {
            "takeover_persistent": {
                "configured": True,
                "active": True,
                "enabled": True,
                "mode": "normal",
                "phase": "warning",
                "task_description": "整理窗口",
                "remaining_seconds": 1190,
                "source": "agent",
                "shutdown_permitted": False,
            }
        }
    }
    panel._update_task_authorization_controls()

    text = panel._task_authorization_status_label.text()
    assert "即将降级" in text
    assert "整理窗口" in text
    assert "19:50" in text


def test_monitoring_shows_watchdog_with_shutdown_permission(qapp):
    """watchdog 模式显示 HH:MM:SS 倒计时 + 关机权限状态。"""
    from client.panels.monitoring import MonitoringPanel

    panel = MonitoringPanel()
    panel._health = {
        "screen": {
            "takeover_persistent": {
                "configured": True,
                "active": True,
                "enabled": True,
                "mode": "watchdog",
                "phase": "watchdog",
                "task_description": "长时间任务",
                "remaining_seconds": 36000,
                "source": "agent",
                "shutdown_permitted": True,
            }
        }
    }
    panel._update_task_authorization_controls()

    text = panel._task_authorization_status_label.text()
    assert "看门狗模式" in text
    assert "长时间任务" in text
    assert "10:00:00" in text
    assert "允许关机" in text


def test_start_confirm_dialog_normal_mode_hides_watchdog_controls(qapp):
    """normal 模式弹窗：watchdog_group 不可见，无时长输入/关机复选框。"""
    from lib.ui.theme import apply_theme
    from server.overlay_client import _create_start_confirm_dialog

    apply_theme(qapp)
    dialog = _create_start_confirm_dialog()
    dialog.setup("整理窗口", "Ctrl+`", requested_mode="normal")
    dialog.show()
    qapp.processEvents()

    # normal 模式：watchdog_group 应隐藏
    assert dialog.watchdog_group.isVisible() is False
    # duration_spinbox 和 shutdown_checkbox 仍存在但不可见
    assert dialog.duration_spinbox.value() == 10  # 默认值
    assert dialog.shutdown_checkbox.isChecked() is True  # 默认勾选

    dialog.hide()


def test_start_confirm_dialog_watchdog_mode_shows_controls(qapp):
    """watchdog 模式弹窗：watchdog_group 可见，时长输入 1-999 默认 10，允许关机默认勾选。"""
    from lib.ui.theme import apply_theme
    from server.overlay_client import _create_start_confirm_dialog

    apply_theme(qapp)
    dialog = _create_start_confirm_dialog()
    dialog.setup("长时间任务", "Ctrl+`", requested_mode="watchdog")
    dialog.show()
    qapp.processEvents()

    # watchdog 模式：watchdog_group 应可见
    assert dialog.watchdog_group.isVisible() is True
    # duration_spinbox 默认 10，范围 1-999
    assert dialog.duration_spinbox.value() == 10
    assert dialog.duration_spinbox.minimum() == 1
    assert dialog.duration_spinbox.maximum() == 999
    # shutdown_checkbox 默认勾选（spec 决策 19）
    assert dialog.shutdown_checkbox.isChecked() is True

    dialog.hide()


def test_start_confirm_dialog_watchdog_finish_returns_user_choices(qapp):
    """watchdog 模式用户确认后：result_data 含 max_duration_hours + shutdown_permitted + authorized_mode。

    回归断言 authorized_mode（2026-08-06 bug：dialog _finish 从不设置 authorized_mode，
    导致 routes.py 读取默认值 "normal"，watchdog 授权被静默降级）。
    """
    from lib.ui.theme import apply_theme
    from server.overlay_client import _create_start_confirm_dialog

    apply_theme(qapp)
    dialog = _create_start_confirm_dialog()
    dialog.setup("长时间任务", "Ctrl+`", requested_mode="watchdog")
    dialog.show()
    qapp.processEvents()

    # 用户修改时长为 5 小时，取消勾选允许关机
    dialog.duration_spinbox.setValue(5)
    dialog.shutdown_checkbox.setChecked(False)
    dialog._finish("confirmed")

    assert dialog.result_data["status"] == "confirmed"
    assert dialog.result_data["task_authorization"] is True
    assert dialog.result_data["requested_mode"] == "watchdog"
    assert dialog.result_data["authorized_mode"] == "watchdog"
    assert dialog.result_data["max_duration_hours"] == 5
    assert dialog.result_data["shutdown_permitted"] is False

    dialog.hide()


def test_start_confirm_dialog_normal_mode_finish_no_watchdog_fields(qapp):
    """normal 模式用户确认后：max_duration_hours=None, shutdown_permitted=False, authorized_mode="normal"。"""
    from lib.ui.theme import apply_theme
    from server.overlay_client import _create_start_confirm_dialog

    apply_theme(qapp)
    dialog = _create_start_confirm_dialog()
    dialog.setup("整理窗口", "Ctrl+`", requested_mode="normal")
    dialog.show()
    qapp.processEvents()

    dialog._finish("confirmed")

    assert dialog.result_data["status"] == "confirmed"
    assert dialog.result_data["task_authorization"] is True
    assert dialog.result_data["requested_mode"] == "normal"
    assert dialog.result_data["authorized_mode"] == "normal"
    assert dialog.result_data["max_duration_hours"] is None
    assert dialog.result_data["shutdown_permitted"] is False

    dialog.hide()


def test_start_confirm_dialog_cancelled_returns_normal_authorized_mode(qapp):
    """用户拒绝时：authorized_mode="normal"（无授权），即便 requested_mode="watchdog"。

    回归断言（2026-08-06 bug）：cancelled 路径也必须返回 authorized_mode，
    避免 routes.py 读取默认值 "normal" 时与 task_authorization=False 不一致。
    """
    from lib.ui.theme import apply_theme
    from server.overlay_client import _create_start_confirm_dialog

    apply_theme(qapp)
    dialog = _create_start_confirm_dialog()
    dialog.setup("长时间任务", "Ctrl+`", requested_mode="watchdog")
    dialog.show()
    qapp.processEvents()

    dialog._finish("cancelled")

    assert dialog.result_data["status"] == "cancelled"
    assert dialog.result_data["task_authorization"] is False
    assert dialog.result_data["requested_mode"] == "watchdog"
    assert dialog.result_data["authorized_mode"] == "normal"
    assert dialog.result_data["max_duration_hours"] is None
    assert dialog.result_data["shutdown_permitted"] is False

    dialog.hide()


def test_monitoring_permission_toggle_requests_watchdog(qapp):
    """测试电脑操作许可开关勾选 → 发出授权请求。

    开关勾选直接发 POST /screen/control/request（mode=watchdog, source=gui_panel），
    由后端 overlay 确认窗完成用户确认（安全闸门保留）；不再弹本地任务描述对话框。
    """
    from client.panels.monitoring import MonitoringPanel

    captured = {}

    class FakeHttp:
        def post(self, path, json, timeout):
            captured.update(path=path, json=json, timeout=timeout)
            return {"success": True, "status": "ok"}

    panel = MonitoringPanel()
    panel._http = FakeHttp()
    panel._health = {
        "screen": {
            "takeover_confirm": {"timeout_seconds": 30},
            "takeover_persistent": {"configured": True, "active": False},
        }
    }

    # 勾选开关（setChecked 触发 toggled → _on_permission_toggled）
    panel._permission_check.setChecked(True)
    assert panel._persistent_thread.wait(2000) is True
    assert captured["path"] == "/screen/control/request"
    assert captured["json"]["source"] == "gui_panel"
    assert captured["json"]["mode"] == "watchdog"
    assert captured["json"]["task_description"] == "用户面板手动授权"
    assert captured["timeout"] == 35.0


def test_monitoring_permission_uncheck_releases(qapp):
    """测试电脑操作许可开关取消勾选 → 立即发出收回请求。"""
    from client.panels.monitoring import MonitoringPanel

    captured = {}

    class FakeHttp:
        def post(self, path, json, timeout):
            captured.update(path=path)
            return {"success": True, "status": "released"}

    panel = MonitoringPanel()
    panel._http = FakeHttp()
    panel._health = {
        "screen": {
            "takeover_persistent": {
                "configured": True,
                "active": True,
                "enabled": True,
                "mode": "normal",
            }
        }
    }
    # 先让开关与 health 同步（blockSignals 内部回写，不触发请求）
    panel._update_task_authorization_controls()
    assert panel._permission_check.isChecked() is True

    # 用户取消勾选 → release
    panel._permission_check.setChecked(False)
    assert panel._persistent_thread.wait(2000) is True
    assert captured["path"] == "/screen/control/release"


def test_monitoring_permission_health_resync_unchecks(qapp):
    """agent 侧 release / idle 撤销后 /health 回写开关自动回弹（blockSignals 防循环）。"""
    from client.panels.monitoring import MonitoringPanel

    panel = MonitoringPanel()
    panel._health = {
        "screen": {
            "takeover_persistent": {"configured": True, "active": False, "enabled": False}
        }
    }
    # 模拟开关此前处于勾选态（用户视角），health 刷新发现已撤销
    panel._permission_check.blockSignals(True)
    panel._permission_check.setChecked(True)
    panel._permission_check.blockSignals(False)

    panel._update_task_authorization_controls()
    assert panel._permission_check.isChecked() is False
    assert panel._permission_check.isEnabled() is True
