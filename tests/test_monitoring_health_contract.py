"""Monitoring ↔ Health 字段对齐 Contract Test

防止 monitoring.py 与 health.py 字段漂移。基于 spec.md + field-mapping.md 冻结规格。

测试策略（按 spec Anti-Cheat 段）：
- 不 mock 被测对象：monitoring._update_all_cards() 必须真实执行
- mock 上游：构造 health dict（不调真实后端）
- 反向验证：临时把字段改回错误名，确认测试失败 → 还原

测试覆盖：
1. _module_status_text 显式映射表（无状态模块返回 "·"）
2. maintainer 卡片嵌套路径 h["memory"]["maintainer"]
3. 删除 alias fallback 后字段仍能读到值
4. 所有卡片字段（除显式标记可选）都有非默认值
5. 4 个删除字段不再被读取
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# ========== Fixtures ==========


@pytest.fixture(scope="module")
def qapp():
    """模块级 QApplication（offscreen 平台，不弹真实窗口）"""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def health_response_full() -> dict:
    """构造完整 health dict，含所有 12 个模块的真实字段。

    字段名严格按 server/core/health.py 各 _get_xxx_status() 返回值结构
    （参考 temp/sdd/monitoring-health-alignment/field-mapping.md 冻结规格）。
    """
    return {
        # 顶层字段
        "version": "0.16.0-test",
        "uptime_seconds": 3600,
        # backend.modules 用的 12 个 health 模块
        "ocr": {
            "ocr_loaded": True,
            "keep_models": True,
            "vl_model_enabled": True,
            "vl_provider": "modelscope",
            "vl_model": "Qwen3-VL-235B",
            "vl_available": True,
            "vl_usable_for_sensitive": False,
            "model_ready": True,
            "cold_start": False,
            "loading": False,
            "failed": False,
            "load_elapsed_ms": 1500,
            "last_inference_ms": 80,
            "inference_count": 42,
            "last_error": "",
        },
        "vision": {
            "vl_model_enabled": True,
            "vl_provider": "modelscope",
            "vl_model": "Qwen3-VL-235B",
            "vl_available": True,
            "vl_usable_for_sensitive": False,
            "allow_privacy_warning_for_sensitive": False,
            "providers": [],
            "provider_exhausted": {},
            "daily_exhausted": False,
            "daily_exhausted_at": "",
            "daily_exhausted_reason": "",
        },
        "memory": {
            "available": True,
            "messages": 128,
            "facts": 32,
            "summaries": 8,
            "embedding_ready": True,
            "db_size_mb": 12.5,
            "maintainer": {
                "last_run": "2026-08-07T10:30:00",
                "stale_threshold_days": 7,
                "interval_hours": 24,
                "validate_enabled": True,
                "model_tier": "cheap",
                "status_counts": {"keep": 20, "update": 8, "archive": 4},
                "stale_total": 12,
            },
        },
        "browser": {
            "connected": True,
            "debug_port": 9222,
            "tab_count": 3,
            "target_count": 5,
            "page_count": 3,
            "sessions_count": 2,
            "session_idle_timeout_secs": 900,
            "session_max_count": 20,
        },
        "exec": {
            "temp_dir": "/tmp/localagent_exec",
            "history_count": 56,
            "terminals_total": 4,
            "terminals_running": 1,
            "terminals": [],
        },
        "screen": {
            "capture_available": True,
            "emergency_stopped": False,
            "auto_skip_cache_size": 15,
            "require_confirm_default": True,
            "admin_privileges": False,
            "focus_protection": {"enabled": True},
            "canonical_token": {"configured": True},
            "uia": {"available": True},
            "window_lifecycle": {"tracked": True},
            "takeover_confirm": {"enabled": True},
            "takeover_persistent": {
                "configured": True,
                "active": False,
                "enabled": False,
                "configured_idle_timeout_seconds": 300,
            },
        },
        "mindforge": {
            "available": True,
            "has_index": True,
            "search_engine_loaded": True,
            "converter_daemon_running": True,
        },
        "loops": {
            "available": True,
            "active_count": 0,
        },
        "apikey": {
            "supported_vendors": 6,
            "last_test": "2026-08-07T09:00:00",
        },
        "agent_guide": {
            "available": True,
            "registered_tasks": 12,
        },
        "inbox": {
            "available": True,
            "pending_count": 2,
            "pending_messages": [],
        },
        "user_message": {
            "pending_count": 0,
            "pending_messages": [],
        },
        # 其他卡片
        "docviewer": {
            "available": True,
            "supported_formats": [".pdf", ".docx", ".txt"],
        },
        "accounting": {
            "review_data_available": True,
            "item_count": 5,
        },
        "llm_pool": {
            "initialized": True,
            "version": "v14",
            "total_keys": 8,
            "active_keys": 6,
            "current_active": 2,
            "free_keys": 4,
            "paid_keys": 4,
            "total_models": 10,
            "free_models": 5,
            "recent_calls_count": 120,
            "needs_health_check": False,
            "last_health_check": "2026-08-07T08:00:00",
            "circuit_breaker_open_count": 0,
        },
        "todos": {
            "available": True,
            "todos_total": 15,
            "todos_due": 3,
            "todos_by_type": {"daily": 5, "adhoc": 10},
            "todos_archived": 8,
            "wip_total": 2,
            "wip_active": 1,
        },
        "mcp": {
            "direct_tools_count": 12,
            "direct_tools_limit": 50,
            "gateway_tools_count": 35,
            "template_count": 8,
            "gateway_categories": ["fs", "web", "sys"],
            "template_categories": ["dev", "daily"],
            "image_content_patch": True,
        },
    }


@pytest.fixture
def monitoring_panel(qapp, health_response_full):
    """创建 MonitoringPanel 实例，注入完整 health dict，调 _update_all_cards。

    按 Anti-Cheat：不 mock _update_all_cards，真实执行字段渲染。
    """
    from client.panels.monitoring import MonitoringPanel

    panel = MonitoringPanel()
    panel._health = health_response_full
    panel._fake_stats = None  # FakeProxy 卡片不在 contract test 范围
    panel._update_all_cards()
    return panel


# ========== 测试用例 ==========


def test_module_status_text_explicit_mapping():
    """显式映射表：exec/user_message/apikey 返回 "·"，ocr/vision 等返回 "✓"/"✗"。

    反向验证无状态模块不再误报 "✓"（spec Anti-Cheat: 禁止 fallback "✓"）。
    """
    from client.panels.monitoring import _module_status_text

    # 无状态模块（spec _MODULE_STATUS_FIELD 中 field=None）
    assert _module_status_text("exec", {"temp_dir": "/tmp", "history_count": 5}) == "·"
    assert _module_status_text("user_message", {"pending_count": 0}) == "·"
    assert _module_status_text("apikey", {"supported_vendors": 5}) == "·"

    # 有状态模块：field 值为 True → "✓"
    assert _module_status_text("ocr", {"model_ready": True}) == "✓"
    assert _module_status_text("vision", {"vl_available": True}) == "✓"
    assert _module_status_text("memory", {"available": True}) == "✓"
    assert _module_status_text("browser", {"connected": True}) == "✓"
    assert _module_status_text("screen", {"capture_available": True}) == "✓"
    assert _module_status_text("mindforge", {"available": True}) == "✓"

    # 有状态模块：field 值为 False → "✗"
    assert _module_status_text("ocr", {"model_ready": False}) == "✗"
    assert _module_status_text("memory", {"available": False}) == "✗"

    # 空 dict / 非 dict → "—"
    assert _module_status_text("ocr", {}) == "—"
    assert _module_status_text("ocr", None) == "—"


def test_maintainer_card_reads_nested_memory_maintainer(monitoring_panel):
    """反向验证：maintainer 卡片必须读 h["memory"]["maintainer"] 嵌套路径。

    spec 核心修复（field-mapping.md L100-110）：原代码 h.get("memory_maintainer", ...)
    永远返回 None → 3 字段全部显示 "—"；修复后读 h["memory"]["maintainer"]。
    """
    maintainer_card = monitoring_panel._cards["maintainer"]
    last_run_label = maintainer_card._field_labels["last_run"].text()
    stale_count_label = maintainer_card._field_labels["stale_count"].text()
    validate_dist_label = maintainer_card._field_labels["validate_dist"].text()

    # 三字段都必须非 "—"（health_response_full["memory"]["maintainer"] 有真实值）
    assert last_run_label != "—", f"maintainer.last_run 仍为 '—'，嵌套路径未生效: {last_run_label!r}"
    assert stale_count_label != "—", f"maintainer.stale_count 仍为 '—': {stale_count_label!r}"
    assert validate_dist_label != "—", f"maintainer.validate_dist 仍为 '—': {validate_dist_label!r}"

    # 验证具体值
    assert "2026-08-07T10:30:00" in last_run_label
    assert "12" in stale_count_label  # stale_total=12
    # validate_dist 格式化为 "keep=N update=N archive=N"
    assert "keep=20" in validate_dist_label
    assert "update=8" in validate_dist_label
    assert "archive=4" in validate_dist_label


def test_no_alias_fallback_in_field_reads(monitoring_panel, health_response_full):
    """反向验证：删除 alias fallback 后字段仍能读到值。

    health_response_full 不含旧字段名（如 emergency_stop / confirm_mode / db_size /
    v2 / ocr_model / cooldown_keys），仅含 health 真源字段名。
    若 monitoring 仍依赖 alias fallback，这些字段会显示 "—"。
    """
    # 验证 health_response_full 不含旧字段名（fixture 自检）
    screen = health_response_full["screen"]
    assert "emergency_stop" not in screen, "fixture 不应含旧字段 emergency_stop"
    assert "confirm_mode" not in screen, "fixture 不应含旧字段 confirm_mode"
    assert "skip_cache" not in screen, "fixture 不应含旧字段 skip_cache"

    memory = health_response_full["memory"]
    assert "db_size" not in memory, "fixture 不应含旧字段 db_size"
    assert "v2" not in memory and "v2_enabled" not in memory, "fixture 不应含旧字段 v2"

    ocr = health_response_full["ocr"]
    assert "ocr_model" not in ocr, "fixture 不应含已删除字段 ocr_model"

    llm_pool = health_response_full["llm_pool"]
    assert "cooldown_keys" not in llm_pool and "cooldown" not in llm_pool, "fixture 不应含已删除字段 cooldown_keys"

    # monitoring 字段必须读到真实值（非 "—"）
    screen_card = monitoring_panel._cards["screen"]
    assert screen_card._field_labels["emergency"].text() != "—", "emergency_stopped 字段未读到值"
    assert screen_card._field_labels["confirm"].text() != "—", "require_confirm_default 字段未读到值"
    assert screen_card._field_labels["skip_cache"].text() != "—", "auto_skip_cache_size 字段未读到值"

    memory_card = monitoring_panel._cards["memory"]
    assert memory_card._field_labels["db_size"].text() != "—", "db_size_mb 字段未读到值"
    assert "MB" in memory_card._field_labels["db_size"].text(), "db_size 应附 MB 单位"
    assert memory_card._field_labels["embedding"].text() != "—", "embedding_ready 字段未读到值"

    # llm_pool.keys 不再含 cooldown 段
    llm_pool_card = monitoring_panel._cards["llm_pool"]
    keys_text = llm_pool_card._field_labels["keys"].text()
    assert "冷却" not in keys_text, f"keys 字段仍含 '冷却' 段（应已删除）: {keys_text!r}"


def test_all_monitoring_fields_have_non_default_value(monitoring_panel):
    """主验收：所有卡片字段（除 fake_proxy 和显式标记"允许空"外）都有非默认值（非 "—"）。

    遍历所有卡片的 _field_labels，断言每个 QLabel 文本不为 "—"。
    fake_proxy 卡片不在 contract test 范围（数据源是独立端点，fixture 中 _fake_stats=None）。
    ocr.last_error 在无错误时（last_error=""）显示 "—" 是字段语义（表示无错误），
    非字段对齐失败，因此显式允许。
    """
    skip_cards = {"fake_proxy"}  # fake_proxy 数据源是独立端点，fixture 未注入数据
    # 允许显示 "—" 的字段（字段语义：值为空时 "—" 是预期，非对齐失败）
    allow_empty_fields = {
        ("ocr", "last_error"),  # last_error="" 时显示 "—" 表示无错误
    }

    empty_fields: list[tuple[str, str]] = []
    for card_id, card in monitoring_panel._cards.items():
        if card_id in skip_cards:
            continue
        for field_key, label in card._field_labels.items():
            text = label.text()
            if text == "—" and (card_id, field_key) not in allow_empty_fields:
                empty_fields.append((card_id, field_key))

    assert not empty_fields, (
        f"以下 {len(empty_fields)} 个字段仍显示 '—'（字段对齐失败或 health 缺字段）:\n"
        + "\n".join(f"  - {card}.{field}" for card, field in empty_fields)
    )


def test_deleted_fields_not_present(monitoring_panel):
    """反向验证：4 个删除字段不再被 monitoring 读取（_init_card_fields 不再注册）。

    spec 冻结清单（field-mapping.md L232-237）：
    - ocr.ocr_model
    - memory.v2
    - screen.windows
    - llm_pool.cooldown_keys（在 keys 字段拼接中，已删除该段）
    """
    # ocr 卡片不应有 ocr_model field
    ocr_fields = monitoring_panel._cards["ocr"]._field_labels.keys()
    assert "ocr_model" not in ocr_fields, "ocr.ocr_model 字段应已删除"

    # memory 卡片不应有 v2 field
    memory_fields = monitoring_panel._cards["memory"]._field_labels.keys()
    assert "v2" not in memory_fields, "memory.v2 字段应已删除"

    # screen 卡片不应有 windows field
    screen_fields = monitoring_panel._cards["screen"]._field_labels.keys()
    assert "windows" not in screen_fields, "screen.windows 字段应已删除"


def test_field_name_alignment_specific_cards(monitoring_panel):
    """针对 spec 中点名的 15+ 字段名不匹配问题做逐一验证。

    覆盖 field-mapping.md 中标记 "改：字段名" 的所有字段。
    """
    # screen 卡片
    screen_card = monitoring_panel._cards["screen"]
    assert screen_card._field_labels["emergency"].text() == "否", (
        f"emergency_stopped=False 应显示 '否'，实际: {screen_card._field_labels['emergency'].text()!r}"
    )
    assert screen_card._field_labels["confirm"].text() == "是", (
        f"require_confirm_default=True 应显示 '是'，实际: {screen_card._field_labels['confirm'].text()!r}"
    )
    assert screen_card._field_labels["skip_cache"].text() == "15", (
        f"auto_skip_cache_size=15 应显示 '15'，实际: {screen_card._field_labels['skip_cache'].text()!r}"
    )

    # memory 卡片
    memory_card = monitoring_panel._cards["memory"]
    assert memory_card._field_labels["messages"].text() == "128"
    assert memory_card._field_labels["facts"].text() == "32"
    assert memory_card._field_labels["summaries"].text() == "8"
    assert memory_card._field_labels["embedding"].text() == "是", (
        f"embedding_ready=True 应显示 '是'，实际: {memory_card._field_labels['embedding'].text()!r}"
    )

    # accounting 卡片
    accounting_card = monitoring_panel._cards["accounting"]
    assert accounting_card._field_labels["available"].text() == "是", (
        f"review_data_available=True 应显示 '是'，实际: {accounting_card._field_labels['available'].text()!r}"
    )
    assert accounting_card._field_labels["items"].text() == "5"

    # todos 卡片
    todos_card = monitoring_panel._cards["todos"]
    assert todos_card._field_labels["total"].text() == "15", (
        f"todos_total=15 应显示 '15'，实际: {todos_card._field_labels['total'].text()!r}"
    )
    assert todos_card._field_labels["due_count"].text() == "3", (
        f"todos_due=3 应显示 '3'，实际: {todos_card._field_labels['due_count'].text()!r}"
    )

    # apikey 卡片
    apikey_card = monitoring_panel._cards["apikey"]
    assert apikey_card._field_labels["vendors"].text() == "6", (
        f"supported_vendors=6 应显示 '6'，实际: {apikey_card._field_labels['vendors'].text()!r}"
    )

    # mindforge 卡片
    mindforge_card = monitoring_panel._cards["mindforge"]
    assert mindforge_card._field_labels["index"].text() == "是", (
        f"has_index=True 应显示 '是'，实际: {mindforge_card._field_labels['index'].text()!r}"
    )

    # mcp 卡片
    mcp_card = monitoring_panel._cards["mcp"]
    assert mcp_card._field_labels["image_patch"].text() == "是", (
        f"image_content_patch=True 应显示 '是'，实际: {mcp_card._field_labels['image_patch'].text()!r}"
    )
    assert mcp_card._field_labels["categories"].text() != "—", (
        "gateway_categories 字段未读到值（应显示 ['fs', 'web', 'sys'] 的 str 形式）"
    )


def test_module_status_in_backend_card(monitoring_panel):
    """backend 卡片 'modules' 行：exec/user_message/apikey 显示 "·"，ocr/memory 显示 "✓"。

    spec 核心修复（field-mapping.md L192-220）：扫描法 fallback "✓" 改为显式映射表，
    无状态模块显示 "·"。
    """
    modules_text = monitoring_panel._cards["backend"]._field_labels["modules"].text()
    # 无状态模块应显示 "·"
    assert "Exec ·" in modules_text or "·" in modules_text.split("Exec")[1][:5] if "Exec" in modules_text else False, (
        f"Exec 模块应显示 '·'（无状态），实际 modules 文本: {modules_text!r}"
    )
    assert "UserMsg ·" in modules_text or "·" in modules_text.split("UserMsg")[1][:5] if "UserMsg" in modules_text else False, (
        f"UserMsg 模块应显示 '·'（无状态），实际 modules 文本: {modules_text!r}"
    )
    # 有状态模块应显示 "✓"（health_response_full 中 ocr.model_ready=True）
    assert "OCR ✓" in modules_text, f"OCR 模块应显示 '✓'，实际: {modules_text!r}"
    assert "Memory ✓" in modules_text, f"Memory 模块应显示 '✓'，实际: {modules_text!r}"
