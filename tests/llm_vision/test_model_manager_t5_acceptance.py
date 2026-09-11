"""T5 验收测试：审批分类 / 端点契约 / health 指纹低频 / mlm_state 注入

设计真源：temp/sdd/model-lifecycle-manager/design.md §10/§14
- 审批回归（P0-1）：新 `/models/*` 端点经 classify_safety 判为 approval_required；
  fail-open 默认不回退（agent-callable=True，但安全靠 approval_required 把守）。
- 端点契约：/models（last_load_ms 可空）、/models/pause|resume（PAUSED 准入放行/逐出暂停）、
  /health.model_manager 低频枚举 + HealthResponse schema 显式声明。
- 兼容委托：/ocr/status、/health.ocr 追加 mlm_state 字段。
"""

from __future__ import annotations

# ==================== P0-1：审批分类 ====================

class TestModelsApprovalClassification:
    """design §10 P0：/models 前缀下变更类 POST 必须判为 approval_required。"""

    def test_models_load_unload_classified_approval_required(self, client):
        """POST /models/{id}/load 与 /models/{id}/unload 必须是 approval_required。"""
        from server.route_tags import _op_to_path, classify_safety

        for op in ("models_load", "models_unload"):
            method, path = _op_to_path[op]
            assert method == "POST", f"{op} 应为 POST，实际 {method}"
            assert classify_safety(method, path) == "approval_required", (
                f"{op} ({method} {path}) 必须判为 approval_required，"
                "否则 agent 可无审批卸载 OCR（瘫痪 Computer Use 文字定位主路径）"
            )

    def test_models_pause_resume_classified_approval_required(self, client):
        """POST /models/pause|resume 必须是 approval_required（手动超驰通道）。"""
        from server.route_tags import _op_to_path, classify_safety

        for op in ("models_pause", "models_resume"):
            method, path = _op_to_path[op]
            assert method == "POST"
            assert classify_safety(method, path) == "approval_required", (
                f"{op} ({method} {path}) 必须判为 approval_required，"
                "PAUSED 是手动超驰，不能被 agent 无审批触发"
            )

    def test_models_get_endpoints_classified_read_only(self, client):
        """GET /models 与 /models/pressure 是 read_only（agent 可直接查）。"""
        from server.route_tags import _op_to_path, classify_safety

        for op in ("models_list", "models_pressure"):
            method, path = _op_to_path[op]
            assert method == "GET"
            assert classify_safety(method, path) == "read_only", (
                f"{op} ({method} {path}) 应为 read_only"
            )


class TestFailOpenAudit:
    """design §10 P0：fail-open 默认不回退。

    x-agent-callable 是 fail-open（新端点默认 True 暴露给 agent），
    但安全防护靠 classify_safety → approval_required → HTTP 中间件 + MCP 网关审批。
    此测试确认 /models/* 端点确实在 safety_map 中且标记为 approval_required。
    """

    def test_models_ops_in_safety_map_as_approval_required(self, client):
        """所有 /models/* 变更类 operation_id 必须在 safety_map 中且为 approval_required。

        防止漏注册：若某端点未进 safety_map，会变成"未知 op"绕过审批。
        """
        from server.main import app
        from server.route_tags import build_operation_safety_map

        safety_map = build_operation_safety_map(app)
        for op in ("models_load", "models_unload", "models_pause", "models_resume"):
            assert op in safety_map, f"{op} 必须在 safety_map 中"
            assert safety_map[op] == "approval_required", (
                f"{op} 应为 approval_required，实际: {safety_map[op]}"
            )

    def test_models_get_ops_in_safety_map_as_read_only(self, client):
        """GET /models 与 /models/pressure 在 safety_map 中且为 read_only。"""
        from server.main import app
        from server.route_tags import build_operation_safety_map

        safety_map = build_operation_safety_map(app)
        for op in ("models_list", "models_pressure"):
            assert op in safety_map, f"{op} 必须在 safety_map 中"
            assert safety_map[op] == "read_only", (
                f"{op} 应为 read_only，实际: {safety_map[op]}"
            )


class TestModerateLevelExcludesModels:
    """design §10 P0：moderate 级别下 /models 视为低风险运维操作放行。

    与 /ocr/models/* 的 moderate 排除策略镜像（design §10 P0）。
    """

    def test_moderate_level_excludes_models_prefix(self, monkeypatch):
        """moderate 级别下 /models 前缀的所有 POST 端点应降级为 safe。"""
        from server.route_tags import (
            _APPROVAL_POST_PATTERNS,
            _MODERATE_EXCLUDE_PATTERNS,
            classify_safety_runtime,
        )

        # /models 必须在 moderate 排除清单中
        assert "/models" in _MODERATE_EXCLUDE_PATTERNS, (
            "/models 必须在 _MODERATE_EXCLUDE_PATTERNS 中（design §10 P0 moderate 镜像）"
        )
        # /models 必须在 strict 审批清单中（确认设计正确）
        assert "/models" in _APPROVAL_POST_PATTERNS

        # moderate 级别下，/models/ocr/load 应判为 safe
        monkeypatch.setattr(
            "server.route_tags.get_approval_level", lambda: "moderate"
        )
        result = classify_safety_runtime("POST", "/models/ocr/load")
        assert result == "safe", (
            f"moderate 级别下 /models/ocr/load 应为 safe，实际: {result}"
        )

    def test_strict_level_keeps_models_approval(self, monkeypatch):
        """strict 级别下 /models 前缀的所有 POST 端点必须保持 approval_required。"""
        from server.route_tags import classify_safety_runtime

        monkeypatch.setattr(
            "server.route_tags.get_approval_level", lambda: "strict"
        )
        for path in ("/models/ocr/load", "/models/ocr/unload",
                     "/models/pause", "/models/resume"):
            result = classify_safety_runtime("POST", path)
            assert result == "approval_required", (
                f"strict 级别下 {path} 应为 approval_required，实际: {result}"
            )


# ==================== 端点契约 ====================

class TestEndpointContract:
    """design §10/§14：端点契约与 schema 声明。"""

    def test_models_list_returns_array_with_full_contract(self, client, monkeypatch):
        """GET /models 返回 {models: [...]}, 每行含完整契约字段（last_load_ms 可空）。"""
        # 用 fake manager 避免触碰真实驱动
        fake_rows = [{
            "model_id": "ocr",
            "resource": "gpu",
            "loaded": True,
            "footprint_mb": 500,
            "priority": 90,
            "evictable": True,
            "in_flight": 0,
            "loaded_at": 1700000000.0,
            "last_load_ms": None,  # 可空契约
            "reload_degraded": False,
        }]

        class FakeManager:
            def models_report(self):
                return fake_rows

        monkeypatch.setattr(
            "server.model_manager.routes._get_manager", lambda: FakeManager()
        )

        r = client.get("/models")
        assert r.status_code == 200
        data = r.json()
        assert "models" in data
        assert len(data["models"]) == 1
        row = data["models"][0]
        # design §5.5 last_load_ms 可空（不是错误）
        for key in ("model_id", "resource", "loaded", "footprint_mb",
                    "priority", "evictable", "in_flight", "loaded_at",
                    "last_load_ms", "reload_degraded"):
            assert key in row, f"models_report 缺字段: {key}"
        assert row["last_load_ms"] is None

    def test_models_pressure_returns_gpu_cpu_states(self, client, monkeypatch):
        """GET /models/pressure 返回 gpu/cpu 各自的压力态。"""
        class FakeManager:
            def pressure_report(self):
                return {
                    "gpu": {"state": "normal", "ratio": 0.5,
                            "used": 4 * 1024**3, "total": 8 * 1024**3,
                            "recent_events": []},
                    "cpu": {"state": "disabled", "ratio": None,
                            "used": None, "total": None,
                            "recent_events": []},
                }

        monkeypatch.setattr(
            "server.model_manager.routes._get_manager", lambda: FakeManager()
        )

        r = client.get("/models/pressure")
        assert r.status_code == 200
        data = r.json()
        assert "gpu" in data and "cpu" in data
        assert data["gpu"]["state"] == "normal"

    def test_models_load_refused_returns_structured_response(self, client, monkeypatch):
        """POST /models/{id}/load 压力拒绝时返回 status=refused + reason（不抛 503）。"""
        class FakeManager:
            async def manual_load(self, model_id):
                return {"status": "refused", "reason": "pressure_refusing"}

        monkeypatch.setattr(
            "server.model_manager.routes._get_manager", lambda: FakeManager()
        )

        r = client.post("/models/ocr/load")
        assert r.status_code == 200  # 不抛 503，让调用方程序化区分
        data = r.json()
        assert data["status"] == "refused"
        assert data["reason"] == "pressure_refusing"
        assert data["model_id"] == "ocr"

    def test_models_unload_passes_through_model_id(self, client, monkeypatch):
        """POST /models/{id}/unload 调用 manual_unload 并回写 model_id。"""
        class FakeManager:
            async def manual_unload(self, model_id):
                return {"status": "ok", "detail": "unloaded"}

        monkeypatch.setattr(
            "server.model_manager.routes._get_manager", lambda: FakeManager()
        )

        r = client.post("/models/ocr/unload")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["model_id"] == "ocr"

    def test_models_pause_passes_resource_param(self, client, monkeypatch):
        """POST /models/pause 接收 resource 参数（缺省=全部）。"""
        captured = {}

        class FakeManager:
            def pause(self, resource=None):
                captured["resource"] = resource
                return [{"to": "paused", "resource": resource or "all"}]

        monkeypatch.setattr(
            "server.model_manager.routes._get_manager", lambda: FakeManager()
        )

        # 显式 resource=gpu
        r = client.post("/models/pause", json={"resource": "gpu"})
        assert r.status_code == 200
        assert r.json()["status"] == "paused"
        assert captured["resource"] == "gpu"

        # 缺省 resource=None
        r = client.post("/models/pause", json={})
        assert r.status_code == 200
        assert captured["resource"] is None


# ==================== HealthResponse schema 声明 ====================

class TestHealthResponseSchema:
    """design §10/§14：HealthResponse 必须显式声明 model_manager 字段。"""

    def test_health_response_has_model_manager_field(self):
        """HealthResponse schema 必须显式声明 model_manager 字段（extra='forbid'）。"""
        from server.core.health import HealthResponse

        assert "model_manager" in HealthResponse.model_fields, (
            "HealthResponse 必须显式声明 model_manager 字段（design §10 P2-6）"
        )
        # 默认空 dict（管理器未启动时）
        field = HealthResponse.model_fields["model_manager"]
        assert field.default == {} or field.default is None

    def test_health_endpoint_includes_model_manager(self, client, monkeypatch):
        """/health 响应必须包含 model_manager 字段。"""
        class FakeManager:
            def health_summary(self):
                return {
                    "enabled": True,
                    "gpu_state": "normal",
                    "cpu_state": "disabled",
                    "loaded_ids": ["ocr"],
                    "reload_degraded_ids": [],
                    "last_transition_ts": None,
                }

        # mock get_model_manager 入口（health.py 与 model_manager.routes 都用同一个）
        monkeypatch.setattr(
            "server.model_manager.get_model_manager", lambda: FakeManager()
        )

        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "model_manager" in data
        assert data["model_manager"]["enabled"] is True
        assert data["model_manager"]["gpu_state"] == "normal"

    def test_health_model_manager_low_freq_fields_only(self, monkeypatch):
        """health_summary() 只返回低频枚举字段（design §10：不放高频计数器）。"""
        from server.model_manager.manager import ModelLifecycleManager
        from server.model_manager.monitor import PressureMonitor, ResourceMonitorConfig

        # 构造一个最小 manager 实例（不启动监控循环）
        cfg = {
            "enabled": True, "poll_interval_sec": 5.0,
            "reload_fail_cooldown_sec": 30,
            "gpu": ResourceMonitorConfig(resource="gpu", enabled=True),
            "cpu": ResourceMonitorConfig(resource="cpu", enabled=False),
            "gpu_min_loaded_seconds": 60,
            "cpu_min_loaded_seconds": 60,
            "gpu_unload_timeout_sec": 15,
            "cpu_unload_timeout_sec": 15,
            "models": {},
        }
        monitors = {
            "gpu": PressureMonitor(cfg["gpu"]),
            "cpu": PressureMonitor(cfg["cpu"]),
        }
        m = ModelLifecycleManager(cfg=cfg, monitors=monitors)
        summary = m.health_summary()
        # design §10：只低频枚举字段，无高频计数器（避免 client 指纹漂移）
        expected_keys = {"enabled", "gpu_state", "cpu_state", "loaded_ids",
                         "reload_degraded_ids", "last_transition_ts"}
        assert set(summary.keys()) == expected_keys, (
            f"health_summary 字段集合应为 {expected_keys}，实际: {set(summary.keys())}"
        )


# ==================== 兼容委托：mlm_state 注入 ====================

class TestMlmStateInjection:
    """design §10：/ocr/status 与 /health.ocr 追加 mlm_state 字段。"""

    def test_ocr_status_includes_mlm_state(self, client, monkeypatch):
        """/ocr/status 响应必须包含 mlm_state 字段。"""
        # OCRStatusResponse 已声明 mlm_state: dict = {}，端点必须实际填值
        from server.ocr import OCRStatusResponse

        assert "mlm_state" in OCRStatusResponse.model_fields, (
            "OCRStatusResponse 必须声明 mlm_state 字段（design §10 兼容委托）"
        )

        # 用 fake manager 避免触发真实驱动初始化
        class FakeManager:
            def keep_models(self, model_id):
                return True

            def health_summary(self):
                return {"enabled": True, "gpu_state": "normal",
                        "cpu_state": "disabled", "loaded_ids": [],
                        "reload_degraded_ids": [], "last_transition_ts": None}

        # ocr._get_mlm_state 直接调 get_model_manager().health_summary()
        monkeypatch.setattr(
            "server.model_manager.get_model_manager", lambda: FakeManager()
        )

        r = client.get("/ocr/status")
        assert r.status_code == 200
        data = r.json()
        assert "mlm_state" in data, "/ocr/status 必须包含 mlm_state 字段"
        assert data["mlm_state"]["enabled"] is True
        assert "gpu_state" in data["mlm_state"]

    def test_health_ocr_includes_mlm_state(self, client, monkeypatch):
        """/health.ocr 子键必须包含 mlm_state 字段。"""
        class FakeManager:
            def keep_models(self, model_id):
                return True

            def health_summary(self):
                return {"enabled": True, "gpu_state": "normal",
                        "cpu_state": "disabled", "loaded_ids": ["ocr"],
                        "reload_degraded_ids": [], "last_transition_ts": None}

        monkeypatch.setattr(
            "server.model_manager.get_model_manager", lambda: FakeManager()
        )

        r = client.get("/health")
        assert r.status_code == 200
        ocr_status = r.json().get("ocr", {})
        assert "mlm_state" in ocr_status, "/health.ocr 必须包含 mlm_state 字段"
        assert ocr_status["mlm_state"]["enabled"] is True

    def test_mlm_state_safe_fallback_when_manager_unavailable(self, monkeypatch):
        """管理器未初始化时 mlm_state 返回安全降级（不抛异常）。"""
        from server.ocr import _get_mlm_state

        # 模拟管理器未注册（import 失败或单例未初始化）
        def _raise():
            raise RuntimeError("manager not initialized")

        monkeypatch.setattr(
            "server.model_manager.get_model_manager", _raise
        )

        result = _get_mlm_state()
        assert "enabled" in result
        assert result["enabled"] is False
        assert "error" in result  # 错误信息可见但截断


# ==================== 端点路由完整性 ====================

class TestRoutesRegistered:
    """design §10：所有 /models/* 端点必须在 app 中注册。"""

    def test_all_models_endpoints_registered(self, client):
        """5 个 /models/* 端点必须注册到 app（operation_id 反查）。"""
        from server.route_tags import _op_to_path

        expected_ops = {
            "models_list": ("GET", ""),
            "models_pressure": ("GET", "/pressure"),
            "models_load": ("POST", "/{model_id}/load"),
            "models_unload": ("POST", "/{model_id}/unload"),
            "models_pause": ("POST", "/pause"),
            "models_resume": ("POST", "/resume"),
        }
        for op, (expected_method, expected_suffix) in expected_ops.items():
            assert op in _op_to_path, (
                f"{op} 必须在 _op_to_path 中（已注册到 app）"
            )
            method, path = _op_to_path[op]
            assert method == expected_method, (
                f"{op} 方法应为 {expected_method}，实际 {method}"
            )
            assert path.endswith(expected_suffix), (
                f"{op} 路径应以 {expected_suffix} 结尾，实际 {path}"
            )
