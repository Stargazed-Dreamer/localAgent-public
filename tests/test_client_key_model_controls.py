
from PySide6.QtWidgets import QCheckBox, QComboBox, QScrollArea

from client.panels.keys import KeysPanel
from client.widgets.key_form import _BatchEditModelsDialog
from client.widgets.tool_runner import CategoryPageWidget
from server.apikey import _record_to_dict
from server.llm_pool import key_store
from server.llm_pool.key_store import KeyRecord


def _record(models: list[dict]) -> KeyRecord:
    return KeyRecord(
        id="key-1",
        label="test",
        key="secret",
        base_url="https://example.test/v1",
        models=models,
        status={"works": True},
    )


def test_model_tier_and_enabled_survive_all_serializers():
    raw = {
        "id": "key-1",
        "label": "test",
        "key": "secret",
        "base_url": "https://example.test/v1",
        "models": [
            {
                "name": "model-a",
                "scope": ["llm"],
                "tier": 4,
                "enabled": False,
            }
        ],
    }

    record = key_store._to_record(raw)
    assert record.models[0]["tier"] == 4
    assert record.models[0]["enabled"] is False

    stored = key_store._to_dict(record)
    assert stored["models"][0]["tier"] == 4
    assert stored["models"][0]["enabled"] is False

    response = _record_to_dict(record, mask=False)
    assert response["models"][0]["tier"] == 4
    assert response["models"][0]["enabled"] is False


def test_disabled_model_is_excluded_from_resolution(monkeypatch):
    record = _record([
        {
            "name": "model-a",
            "scope": ["llm"],
            "tier": 3,
            "enabled": False,
        }
    ])
    monkeypatch.setattr(key_store, "_load_keys_cached", lambda: [record])
    assert key_store.resolve_keys("agent_chat") == []

    record.models[0]["enabled"] = True
    resolved = key_store.resolve_keys("agent_chat")
    assert [item.model for item in resolved] == ["model-a"]


def test_model_toggle_updates_only_target_model(qapp):
    """T08: _on_toggle_model_enabled 异步化后，本地状态更新发生在 done 回调中。

    流程：start worker → worker.run() 在子线程调 FakeHttp.put → emit done（Queued）
    → 主线程 processEvents 派发到 _on_toggle_model_done → 更新 key_record["models"]。

    测试必须：worker.wait() 等子线程结束 + processEvents() 派发队列信号，再做断言。
    """
    class FakeHttp:
        def __init__(self):
            self.calls = []

        def put(self, path, json):
            self.calls.append((path, json))
            return {"ok": True}

    panel = KeysPanel()
    panel._http = FakeHttp()
    panel._keys = [{
        "id": "key-1",
        "enabled": True,
        "models": [
            {"name": "model-a", "scope": ["llm"], "tier": 3, "enabled": True},
            {"name": "model-b", "scope": ["llm"], "tier": 4, "enabled": True},
        ],
    }]

    panel._on_toggle_model_enabled("key-1", "model-a", False)

    # 异步 HttpWorker：_make_worker 把 worker 加入 panel._http_workers
    assert panel._http_workers, "worker 应被 _http_workers 跟踪"
    worker = next(iter(panel._http_workers))
    # 等待 worker.run() 完成（FakeHttp.put 同步返回，子线程很快结束）
    assert worker.wait(2000) is True, "worker 应在 2s 内完成"
    # 派发 done 信号（QueuedConnection）到主线程槽 _on_toggle_model_done
    qapp.processEvents()

    assert panel._keys[0]["enabled"] is True
    assert panel._keys[0]["models"][0]["enabled"] is False
    assert panel._keys[0]["models"][1]["enabled"] is True
    assert panel._http.calls[0][1]["models"][0]["enabled"] is False
    panel.close()


def test_batch_editor_reads_each_tier_without_apply_all(qapp):
    dialog = _BatchEditModelsDialog(
        existing_models=[
            {"name": "model-a", "scope": ["llm"], "tier": 2, "enabled": True},
            {"name": "model-b", "scope": ["llm"], "tier": 4, "enabled": False},
        ]
    )
    dialog._tier_combos[0].setCurrentText("5")
    dialog._on_accept()
    result = dialog.get_result()
    assert result[0]["tier"] == 5
    assert result[1]["tier"] == 4
    assert result[0]["enabled"] is True
    assert result[1]["enabled"] is False
    dialog.close()


def test_tool_page_scrolls_instead_of_collapsing_controls(qapp):
    options = [
        {"name": f"--option-{index}", "description": "option", "default": False}
        for index in range(18)
    ]
    page = CategoryPageWidget(
        [{
            "id": "tool-a",
            "name": "Tool A",
            "command": "python tool.py",
            "description": "A tool with enough options to require scrolling.",
            "options": options,
        }],
        "Tests",
    )
    page.resize(520, 360)
    page.show()
    qapp.processEvents()

    scroll = page.findChild(QScrollArea)
    assert scroll is not None
    detail = scroll.widget()
    checkboxes = detail.findChildren(QCheckBox)
    combos = detail.findChildren(QComboBox)
    assert checkboxes
    assert all(widget.height() >= widget.sizeHint().height() for widget in checkboxes)
    assert all(widget.height() >= widget.sizeHint().height() for widget in combos)
    page.close()
