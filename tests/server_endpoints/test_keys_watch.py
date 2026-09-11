"""keys.json 热重载守护单测（server/core/keys_watch.py）

只测决策函数 `check_keys_changed_and_reload` 的分支逻辑：未变不重载、首次只立基准、
变更即重载、在途让位、超最长让位后强制、空 keys 保旧池、mtime 取不到不动作。
底层 IO（读文件 mtime / 加载 keys / 重建池 / 查在途）全部 monkeypatch，不触真实池。
"""

import server.core.keys_watch as kw


def test_no_reload_when_mtime_unchanged(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: 100.0)
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(keys))
    last, reloaded, defer = kw.check_keys_changed_and_reload(100.0)
    assert (last, reloaded, defer) == (100.0, False, 0)
    assert inited == []


def test_first_call_only_sets_baseline(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: 100.0)
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(keys))
    # last_mtime=None 是监听启动首帧：只采基准，绝不因"看似变更"重建池
    last, reloaded, defer = kw.check_keys_changed_and_reload(None)
    assert (last, reloaded, defer) == (100.0, False, 0)
    assert inited == []


def test_reload_on_mtime_change(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: 200.0)
    monkeypatch.setattr(kw, "_has_inflight_calls", lambda: False)
    monkeypatch.setattr(kw, "_load_keys", lambda: [object(), object()])
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(len(keys)))
    monkeypatch.setattr(kw, "warn_use_case_coverage", lambda: {})
    last, reloaded, defer = kw.check_keys_changed_and_reload(100.0)
    assert (last, reloaded, defer) == (200.0, True, 0)
    assert inited == [2]


def test_defer_when_inflight(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: 200.0)
    monkeypatch.setattr(kw, "_has_inflight_calls", lambda: True)
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(keys))
    last, reloaded, defer = kw.check_keys_changed_and_reload(100.0, defer=0, max_defer=3)
    assert (last, reloaded, defer) == (100.0, False, 1)  # 保留旧基准，下帧再试
    assert inited == []


def test_force_reload_after_max_defer(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: 200.0)
    monkeypatch.setattr(kw, "_has_inflight_calls", lambda: True)  # 一直有在途
    monkeypatch.setattr(kw, "_load_keys", lambda: [object()])
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(len(keys)))
    monkeypatch.setattr(kw, "warn_use_case_coverage", lambda: {})
    last, reloaded, defer = kw.check_keys_changed_and_reload(100.0, defer=3, max_defer=3)
    assert (last, reloaded, defer) == (200.0, True, 0)  # 不让配置更新被永不结束的流饿死
    assert inited == [1]


def test_empty_keys_keeps_old_pool(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: 200.0)
    monkeypatch.setattr(kw, "_has_inflight_calls", lambda: False)
    monkeypatch.setattr(kw, "_load_keys", lambda: [])  # 解析失败/空
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(keys))
    last, reloaded, defer = kw.check_keys_changed_and_reload(100.0)
    # 不重建旧池，但推进基准：避免每 tick 拿坏文件刷屏重试
    assert (last, reloaded, defer) == (200.0, False, 0)
    assert inited == []


def test_mtime_unavailable_is_noop(monkeypatch):
    monkeypatch.setattr(kw, "_keys_file_mtime", lambda: None)
    inited: list = []
    monkeypatch.setattr(kw, "_init_pool_with", lambda keys: inited.append(keys))
    last, reloaded, defer = kw.check_keys_changed_and_reload(50.0)
    assert (last, reloaded, defer) == (50.0, False, 0)
    assert inited == []


def test_keys_file_mtime_reads_real_file(tmp_path, monkeypatch):
    import lib.secret
    f = tmp_path / "keys.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(lib.secret, "get_llm_keys_path", lambda: f, raising=False)
    assert kw._keys_file_mtime() == f.stat().st_mtime
    # 文件不存在 → None（不抛）
    f.unlink()
    assert kw._keys_file_mtime() is None
