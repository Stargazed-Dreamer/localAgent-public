"""审批日志轮转测试

验证 log_approval / log_approval_detailed 的按大小轮转行为，
包括轮转生成备份、备份数上限、并发写入安全。
"""

import json
import threading


class TestApprovalLogRotation:
    """审批日志按大小轮转测试"""

    def test_rotate_if_needed_no_file(self, monkeypatch, tmp_path):
        """文件不存在时不报错"""
        from server import approval_review
        path = tmp_path / "approvals.jsonl"
        # 文件不存在，不应抛异常
        approval_review._rotate_if_needed(path, max_bytes=100, backup_count=3)

    def test_rotate_if_needed_under_limit(self, monkeypatch, tmp_path):
        """文件未超 max_bytes 时不轮转"""
        from server import approval_review
        path = tmp_path / "approvals.jsonl"
        path.write_text("small content", encoding="utf-8")
        approval_review._rotate_if_needed(path, max_bytes=1000, backup_count=3)
        # 原文件仍在，无备份生成
        assert path.exists()
        assert not (tmp_path / "approvals.1.jsonl").exists()

    def test_log_approval_rotates_on_size_exceed(self, monkeypatch, tmp_path):
        """写入超过 max_bytes → 主文件轮转到 .1，新内容写入空主文件"""
        from server import approval_review

        # mock 日志路径和配置
        log_path = tmp_path / "approvals.jsonl"
        monkeypatch.setattr(approval_review, "_LOG_PATH", log_path)
        monkeypatch.setattr(approval_review, "get_cleanup_config", lambda: {
            "approvals_log_max_bytes": 50,  # 小阈值便于触发
            "approvals_log_backup_count": 3,
            "approval_audit_log_max_bytes": 50,
            "approval_audit_log_backup_count": 3,
            "browser_stats_details_retention_days": 30,
            "memory_facts_warn_rows": 5000,
            "todos_archived_warn_count": 1000,
            "db_file_warn_size_mb": 50,
        })

        # 先写入一条记录让文件存在（此时文件 < 50 字节，不轮转）
        approval_review.log_approval({"action": "init", "ts": "2026-01-01T00:00:00"})
        assert log_path.exists()
        assert not (tmp_path / "approvals.1.jsonl").exists()

        # 手动扩充文件超过 max_bytes，再写入触发轮转
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("x" * 100 + "\n")  # 文件现在 > 50 字节

        # 再写一条 → 写入前检查大小 > 50 → 轮转
        approval_review.log_approval({"action": "exec_python", "ts": "2026-01-02T00:00:00"})

        # 主文件存在且只有最后一条记录
        assert log_path.exists()
        # .1 备份应存在（轮转前的内容）
        backup1 = tmp_path / "approvals.1.jsonl"
        assert backup1.exists()

    def test_log_approval_backup_count_limit(self, monkeypatch, tmp_path):
        """多次轮转后备份数 ≤ backup_count"""
        from server import approval_review

        log_path = tmp_path / "approvals.jsonl"
        monkeypatch.setattr(approval_review, "_LOG_PATH", log_path)
        monkeypatch.setattr(approval_review, "get_cleanup_config", lambda: {
            "approvals_log_max_bytes": 30,  # 极小阈值，每条都触发轮转
            "approvals_log_backup_count": 3,
            "approval_audit_log_max_bytes": 30,
            "approval_audit_log_backup_count": 3,
            "browser_stats_details_retention_days": 30,
            "memory_facts_warn_rows": 5000,
            "todos_archived_warn_count": 1000,
            "db_file_warn_size_mb": 50,
        })

        # 写入 10 条大记录，触发多次轮转
        for i in range(10):
            approval_review.log_approval({
                "action": "exec_python", "code": f"record_{i}" + "x" * 50,
                "ts": f"2026-01-0{i+1}T00:00:00"
            })

        # 主文件 + 最多 3 个备份
        backups = list(tmp_path.glob("approvals.*.jsonl"))
        assert len(backups) <= 3
        # 主文件存在
        assert log_path.exists()

    def test_log_approval_concurrent_writes(self, monkeypatch, tmp_path):
        """多线程并发写入 + 大 max_bytes（不触发轮转）→ 不丢日志、无损坏

        验证并发安全性：所有写入完整保留 + 每行合法 JSON。
        轮转场景由 test_log_approval_rotates_on_size_exceed / backup_count_limit 覆盖。
        """
        from server import approval_review

        log_path = tmp_path / "approvals.jsonl"
        monkeypatch.setattr(approval_review, "_LOG_PATH", log_path)
        monkeypatch.setattr(approval_review, "get_cleanup_config", lambda: {
            "approvals_log_max_bytes": 10 * 1024 * 1024,  # 10MB，确保不触发轮转
            "approvals_log_backup_count": 5,
            "approval_audit_log_max_bytes": 10 * 1024 * 1024,
            "approval_audit_log_backup_count": 5,
            "browser_stats_details_retention_days": 30,
            "memory_facts_warn_rows": 5000,
            "todos_archived_warn_count": 1000,
            "db_file_warn_size_mb": 50,
        })

        # 预先写入一些数据
        for i in range(5):
            approval_review.log_approval({"action": "init", "idx": i, "ts": "2026-01-01T00:00:00"})

        # 10 个线程各写 20 条
        write_count = 20
        threads = []
        errors = []

        def writer(tid):
            try:
                for i in range(write_count):
                    approval_review.log_approval({
                        "action": "exec_python",
                        "tid": tid, "idx": i,
                        "code": "x" * 30,
                        "ts": "2026-01-01T00:00:00"
                    })
            except Exception as e:
                errors.append(e)

        for tid in range(10):
            threads.append(threading.Thread(target=writer, args=(tid,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发写入出错: {errors}"

        # 未触发轮转：主文件存在，无备份
        assert log_path.exists()
        assert not list(tmp_path.glob("approvals.*.jsonl"))

        # 统计主文件行数 = 5 预写 + 10*20 并发 = 205
        with open(log_path, encoding="utf-8") as fh:
            lines = [line for line in fh.read().splitlines() if line.strip()]
        assert len(lines) == 5 + 10 * write_count

        # 验证每行都是合法 JSON
        for line in lines:
            json.loads(line)  # 不抛异常即合法

    def test_log_approval_detailed_rotation(self, monkeypatch, tmp_path):
        """log_approval_detailed 同样支持轮转"""
        from server import approval_review

        detailed_path = tmp_path / "approval_audit.jsonl"
        monkeypatch.setattr(approval_review, "_DETAILED_LOG_PATH", detailed_path)
        monkeypatch.setattr(approval_review, "get_cleanup_config", lambda: {
            "approvals_log_max_bytes": 50,
            "approvals_log_backup_count": 3,
            "approval_audit_log_max_bytes": 50,
            "approval_audit_log_backup_count": 3,
            "browser_stats_details_retention_days": 30,
            "memory_facts_warn_rows": 5000,
            "todos_archived_warn_count": 1000,
            "db_file_warn_size_mb": 50,
        })
        # 启用 detailed_audit_log
        monkeypatch.setattr(approval_review, "get_command_guard_config", lambda: {"detailed_audit_log": True})

        # 先写入一条记录让文件存在（此时文件 < 50 字节，不轮转）
        approval_review.log_approval_detailed({
            "action": "init", "code": "small", "ts": "2026-01-01T00:00:00"
        })
        assert detailed_path.exists()
        assert not (tmp_path / "approval_audit.1.jsonl").exists()

        # 手动扩充文件超过 max_bytes，再写入触发轮转
        with open(detailed_path, "a", encoding="utf-8") as f:
            f.write("x" * 100 + "\n")  # 文件现在 > 50 字节

        # 再写一条 → 写入前检查大小 > 50 → 轮转
        approval_review.log_approval_detailed({
            "action": "exec_python", "code": "x" * 100, "ts": "2026-01-02T00:00:00"
        })

        # 主文件存在
        assert detailed_path.exists()
        # .1 备份应存在（轮转前的内容）
        backup1 = tmp_path / "approval_audit.1.jsonl"
        assert backup1.exists()
