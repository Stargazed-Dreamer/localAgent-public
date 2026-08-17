"""配置管理端点测试

测试 /config 接口的读取和更新功能，验证敏感字段脱敏。
"""



class TestConfig:
    """配置管理测试"""

    def test_get_config(self, client):
        """GET /config 应返回脱敏配置"""
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        # 敏感字段应该被脱敏
        config = data["config"]
        llm = config.get("llm", {})
        # 如果有api_key，应该是脱敏的
        if "api_key" in llm:
            val = llm["api_key"]
            if val and val != "":
                assert "****" in val or len(val) < 8

    def test_update_config(self, client):
        """POST /config 应能更新配置字段"""
        # 更新一个安全的非敏感字段
        resp = client.post("/config", json={
            "path": "server.port",
            "value": 8766,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data

    def test_update_config_invalid_path(self, client, monkeypatch):
        """POST /config 自动创建嵌套路径结构（server/config.py:74-89 update_config）

        mock save_config 防止测试副作用污染生产 config.toml
        （历史教训：曾因未 mock 导致 [nonexistent.deep.path] 段被写入 config.toml）
        """
        from server import config as config_module
        monkeypatch.setattr(config_module, "save_config", lambda cfg: None)

        resp = client.post("/config", json={
            "path": "nonexistent.deep.path.test",
            "value": "test",
        })
        # update_config 对 nonexistent.deep.path.test 会逐级创建嵌套 dict，
        # 最终设置 obj["test"] = "test"，save_config（已 mock），返回 200。
        # 修复（2026-08-06 测试修复铁律）：原 `in (200, 400)` 放宽断言，读被测代码后
        # 确认预期是 200（自动嵌套创建），改为精确断言。
        assert resp.status_code == 200
        data = resp.json()
        # ConfigResponse 包装：{"config": _mask_config(config)}，嵌套结构在 data["config"] 下
        assert "nonexistent" in data["config"]


class TestCleanupConfig:
    """[cleanup] 配置段测试"""

    def test_get_cleanup_config_defaults(self, monkeypatch):
        """config 缺失 [cleanup] 段时返回所有默认值"""
        from server import config as config_module
        monkeypatch.setattr(config_module, "load_config", lambda: {})

        cfg = config_module.get_cleanup_config()
        assert cfg["browser_stats_details_retention_days"] == 30
        assert cfg["approvals_log_max_bytes"] == 10485760
        assert cfg["approvals_log_backup_count"] == 5
        assert cfg["approval_audit_log_max_bytes"] == 10485760
        assert cfg["approval_audit_log_backup_count"] == 5
        assert cfg["memory_facts_warn_rows"] == 5000
        assert cfg["todos_archived_warn_count"] == 1000
        assert cfg["db_file_warn_size_mb"] == 50

    def test_get_cleanup_config_custom(self, monkeypatch):
        """config 自定义 [cleanup] 段时返回自定义值"""
        from server import config as config_module
        monkeypatch.setattr(config_module, "load_config", lambda: {
            "cleanup": {
                "browser_stats_details_retention_days": 7,
                "approvals_log_max_bytes": 5242880,
                "approvals_log_backup_count": 3,
                "memory_facts_warn_rows": 2000,
                "db_file_warn_size_mb": 20,
            }
        })

        cfg = config_module.get_cleanup_config()
        assert cfg["browser_stats_details_retention_days"] == 7
        assert cfg["approvals_log_max_bytes"] == 5242880
        assert cfg["approvals_log_backup_count"] == 3
        # 未自定义的仍返回默认值
        assert cfg["approval_audit_log_max_bytes"] == 10485760
        assert cfg["approval_audit_log_backup_count"] == 5
        assert cfg["memory_facts_warn_rows"] == 2000
        assert cfg["todos_archived_warn_count"] == 1000
        assert cfg["db_file_warn_size_mb"] == 20

    def test_get_cleanup_config_invalid_type(self, monkeypatch):
        """[cleanup] 段为非 dict 类型时 fallback 到默认值"""
        from server import config as config_module
        monkeypatch.setattr(config_module, "load_config", lambda: {"cleanup": "invalid"})

        cfg = config_module.get_cleanup_config()
        assert cfg["browser_stats_details_retention_days"] == 30
        assert cfg["approvals_log_max_bytes"] == 10485760
