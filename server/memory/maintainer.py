"""记忆维护器 — 自动老化 + LLM 验证

定期检查 facts 表中超过 stale_days 未更新的记忆，标记为 stale，
然后用 cheap 模型验证是否仍准确，返回建议动作 (keep/update/archive)。

设计原则（来自 Claude Code memory docs）：
  - 结构化而非自由文本：验证结果强制 JSON
  - cheap 模型做选择题，不做摘要：只返回 is_accurate + suggested_action
  - 时间感知注入：>7天 = stale
  - 不确定时保留 (when in doubt, keep)
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from server.memory.config import MemoryConfig
from server.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryMaintainer:
    """记忆维护器：老化评分 + LLM 验证"""

    def __init__(self, store: MemoryStore, config: MemoryConfig):
        self.store = store
        self.config = config

    def should_run(self) -> bool:
        """检查是否应该运行维护（距上次运行 > interval_hours）"""
        cur = self.store.conn.execute(
            "SELECT value FROM schema_info WHERE key = 'maintainer_last_run'"
        )
        row = cur.fetchone()
        if not row:
            return True
        try:
            last_run = datetime.fromisoformat(row[0])
            elapsed = datetime.now() - last_run
            return elapsed > timedelta(hours=self.config.maintain_interval_hours)
        except (ValueError, TypeError):
            return True

    async def run(self) -> dict:
        """执行完整维护流程：垃圾预过滤 → 老化 → 验证 → 清理过期 trace，返回统计"""
        prefilter_result = self._prefilter_garbage()
        aging_result = self.run_aging()
        validate_result = {"validated": 0, "keep": 0, "update": 0, "archive": 0}
        if self.config.maintain_enable_validate:
            validate_result = await self.run_validate()

        # v3: 清理过期 search_traces（如果 SearchTracer 已注入）
        trace_cleanup = {"deleted": 0, "cutoff": None, "retention_days": 0}
        # maintainer 持有 store 引用，store.config.search_tracer_retention_days 不存在
        # 我们通过 manager 注入 search_tracer（见 manager.py）
        # maintainer 自己不直接持有 tracer，但可以通过 self.config 计算
        try:
            # 尝试调用注入的 _search_tracer（由 MemoryManager.set_search_tracer 设置）
            tracer = getattr(self, "_search_tracer", None)
            if tracer is not None:
                trace_cleanup = tracer.cleanup_old()
        except Exception as e:
            logger.warning(f"清理 search_traces 失败: {e}")

        # v3: 清理过期 evidence_ledger（如果 EvidenceLedger 已注入）
        evidence_cleanup = {"deleted": 0, "cutoff": None, "retention_days": 0}
        try:
            evidence_ledger = getattr(self, "_evidence_ledger", None)
            if evidence_ledger is not None:
                evidence_cleanup = evidence_ledger.cleanup_old()
        except Exception as e:
            logger.warning(f"清理 evidence_ledger 失败: {e}")

        now_str = datetime.now().isoformat()
        # T06：INSERT + commit 包入 _write_lock
        with self.store._write_lock:
            self.store.conn.execute(
                "INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)",
                ("maintainer_last_run", now_str),
            )
            self.store.conn.commit()
        return {
            "prefilter": prefilter_result,
            "aging": aging_result,
            "validate": validate_result,
            "trace_cleanup": trace_cleanup,
            "evidence_cleanup": evidence_cleanup,
            "last_run": now_str,
        }

    def _prefilter_garbage(self) -> dict:
        """预过滤明显垃圾，直接删除，不浪费 LLM 调用。

        规则（确定性，无 LLM 参与）：
        1. test_ 前缀：测试数据泄漏到生产库
        2. 孤儿子字段：key 含 "." 且父 key 不存在于 facts 表（父 key 已被删除的残留）
        3. 保留带 "." 的合法主体 key（如 reference.vision_503_cooldown）
        """
        stats = {"deleted_test": 0, "deleted_orphan_subfield": 0, "skipped": 0}

        cur = self.store.conn.execute("SELECT key FROM facts")
        all_keys = {row[0] for row in cur.fetchall()}

        to_delete: list[str] = []
        for key in all_keys:
            # 规则 1：test_ 前缀
            if key.startswith("test_"):
                to_delete.append(key)
                stats["deleted_test"] += 1
                continue
            # 规则 2：孤儿子字段（含 "." 且父 key 不存在）
            # 但跳过合法的带点主体：父 key 本身不存在但 key 是通过 manual 写入的主体
            # 判断方式：带 "." 的 key，如果去掉最后一段后的父 key 不在 facts 表中，
            # 且该 key 自身不是 manual source 的主体（即 value 不是完整 JSON dict），则视为孤儿
            if "." in key:
                parent = key.rsplit(".", 1)[0]
                if parent not in all_keys:
                    # 父 key 不在 facts 表，检查是否是 manual source 主体
                    cur2 = self.store.conn.execute(
                        "SELECT source FROM facts WHERE key = ?", (key,)
                    )
                    row = cur2.fetchone()
                    is_manual_main = row and row[0] == "manual"
                    if not is_manual_main:
                        to_delete.append(key)
                        stats["deleted_orphan_subfield"] += 1

        # T06：循环 DELETE (facts + memory_meta) + commit 整体包入 _write_lock
        # 不调 self.store.delete_fact（它内部也获取 _write_lock，会死锁），直接 execute
        if to_delete:
            with self.store._write_lock:
                for key in to_delete:
                    self.store.conn.execute(
                        "DELETE FROM facts WHERE key = ?", (key,)
                    )
                    self.store.conn.execute(
                        "DELETE FROM memory_meta WHERE key = ?", (key,)
                    )
                self.store.conn.commit()
            logger.info(
                f"memory prefilter: 删除 {len(to_delete)} 条垃圾 "
                f"(test={stats['deleted_test']}, orphan_subfield={stats['deleted_orphan_subfield']})"
            )

        return stats

    def run_aging(self) -> dict:
        """扫描所有 facts，计算 stale_score，标记过期的记忆为 pending。

        对已标记为 keep/update 的记忆，如果重新变 stale 则重置为 pending 等待再验证。

        T06：循环 INSERT + commit 整体包入 _write_lock，避免并发写交叉。
        """
        stale_days = self.config.maintain_stale_days
        cutoff_dt = datetime.now() - timedelta(days=stale_days)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

        cur = self.store.conn.execute(
            "SELECT key, value, updated_at FROM facts WHERE updated_at < ? ORDER BY updated_at ASC",
            (cutoff_str,),
        )
        rows = cur.fetchall()
        stale_keys = []
        # T06：循环 INSERT + commit 整体包入 _write_lock
        with self.store._write_lock:
            for row in rows:
                key, value, updated_at = row
                try:
                    updated_dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    days_since = (datetime.now() - updated_dt).days
                except (ValueError, TypeError):
                    days_since = stale_days

                self.store.conn.execute(
                    """INSERT INTO memory_meta (key, stale_score, validation_status)
                       VALUES (?, ?, 'pending')
                       ON CONFLICT(key) DO UPDATE SET
                           stale_score = excluded.stale_score,
                           validation_status = CASE WHEN validation_status IN ('keep','update')
                                                    THEN 'pending' ELSE validation_status END""",
                    (key, float(days_since)),
                )
                stale_keys.append({"key": key, "days_since_update": days_since})
            self.store.conn.commit()
        return {
            "stale_count": len(stale_keys),
            "stale_threshold_days": stale_days,
            "stale_keys": [k["key"] for k in stale_keys[:20]],
        }

    async def run_validate(self) -> dict:
        """对 pending 的记忆调用 LLM 验证，写入建议动作

        v15：统一走 pool.call(use_case="memory_validate")，不再 OpenAI SDK 直连。
        429 熔断：pool 内部已对 429 做 model_cooldown，但 memory_validate 走 retries=1
        单条调用，失败后立即返回。这里仍保留 consecutive_429 计数做批量熔断，
        防止 quota 耗尽时连发数十次失败调用透支 quota。
        跳过的 pending 条目下次 maintain 周期再处理。
        """
        cur = self.store.conn.execute(
            """SELECT m.key, f.value FROM memory_meta m
               JOIN facts f ON m.key = f.key
               WHERE m.validation_status = 'pending'"""
        )
        rows = cur.fetchall()
        stats = {"validated": 0, "keep": 0, "update": 0, "archive": 0,
                 "skipped_429": 0, "broke_on_429": False}

        consecutive_429 = 0
        _RATE_LIMIT_BREAK_THRESHOLD = 3
        for key, value in rows:
            result, is_rate_limited = await asyncio.to_thread(
                self._call_llm_validate_with_429_flag, key, value
            )
            if is_rate_limited:
                consecutive_429 += 1
                stats["skipped_429"] += 1
                if consecutive_429 >= _RATE_LIMIT_BREAK_THRESHOLD:
                    remaining = len(rows) - stats["validated"] - stats["skipped_429"]
                    logger.warning(
                        f"memory.maintain 连续 {consecutive_429} 次 429，"
                        f"跳过本次剩余 {remaining} 条 pending 验证（下次 maintain 周期再处理）"
                    )
                    stats["broke_on_429"] = True
                    break
                continue
            # 非 429 的失败（None）或成功都重置计数
            consecutive_429 = 0
            if result is None:
                continue

            action = result.get("suggested_action", "keep")
            if action not in ("keep", "update", "archive"):
                action = "keep"

            now_str = datetime.now().isoformat()
            self.store.conn.execute(
                """UPDATE memory_meta SET
                       last_validated_at = ?,
                       validation_status = ?,
                       validation_note = ?
                   WHERE key = ?""",
                (now_str, action, str(result.get("reason", ""))[:500], key),
            )
            stats["validated"] += 1
            stats[action] += 1

        # T06：commit 包入 _write_lock（run_validate 是 async + 循环含 await，
        # 无法长时间持锁，仅 commit 串行化）
        with self.store._write_lock:
            self.store.conn.commit()
        return stats

    def get_status(self) -> dict:
        """返回维护器状态"""
        cur = self.store.conn.execute(
            "SELECT value FROM schema_info WHERE key = 'maintainer_last_run'"
        )
        row = cur.fetchone()
        last_run = row[0] if row else None

        cur2 = self.store.conn.execute(
            "SELECT validation_status, COUNT(*) FROM memory_meta GROUP BY validation_status"
        )
        status_counts = {r[0]: r[1] for r in cur2.fetchall()}

        # 验证用 LLM tier 范围（来自 USE_CASE_REGISTRY.memory_validate.default_tier）
        model_tier = None
        try:
            from server.llm_pool.types import USE_CASE_REGISTRY
            uc = USE_CASE_REGISTRY.get("memory_validate")
            if uc:
                model_tier = list(uc.default_tier)
        except Exception:
            pass

        return {
            "last_run": last_run,
            "stale_threshold_days": self.config.maintain_stale_days,
            "interval_hours": self.config.maintain_interval_hours,
            "validate_enabled": self.config.maintain_enable_validate,
            "model_tier": model_tier,
            "status_counts": status_counts,
            "stale_total": sum(v for k, v in status_counts.items() if k != "keep"),
        }

    def _call_llm_validate_with_429_flag(self, key: str, value: str) -> tuple[dict | None, bool]:
        """通过 pool.call() 调用 LLM 验证单条记忆，返回 (result, is_rate_limited)

        v15：统一走 pool.call(use_case="memory_validate", retries=1)。
        pool 内部已实现 model 级降级 / cooldown / 429 不计 consecutive_fails，
        本函数只需根据 pool 返回的 error 判断是否是限流类型，用于批量熔断。

        is_rate_limited=True 表示 pool 返回限流类错误（429/no available keys/quota），
        调用方（run_validate）据此做熔断 break，避免连发数十次失败调用。
        其他失败仍返回 (None, False)，按原逻辑跳过单条。
        """
        try:
            from server.llm_pool.singleton import get_pool

            system_prompt, user_prompt = self._build_validate_prompt(key, value)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            result = get_pool().call(
                messages=messages,
                temperature=0.1,
                max_tokens=16384,
                retries=1,
                use_case="memory_validate",
            )
            if not result.get("ok"):
                err = result.get("error", "") or ""
                err_lower = err.lower()
                # 识别限流类错误（pool 内部 429 / 全池不可用 / quota 耗尽）
                is_rate_limited = (
                    "429" in err
                    or "rate limit" in err_lower
                    or "quota" in err_lower
                    or "no available keys" in err_lower
                )
                if is_rate_limited:
                    logger.info(f"LLM 验证限流 (key={key}): {err[:120]}")
                else:
                    logger.warning(f"LLM 验证调用失败 (key={key}): {err[:150]}")
                return None, is_rate_limited

            raw = (result.get("content") or "").strip()
            return self._parse_validate_response(raw), False
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning(f"LLM 验证调用异常 (key={key}): {err_str[:150]}")
            return None, False

    @staticmethod
    def _build_validate_prompt(key: str, value: str) -> tuple[str, str]:
        """构建验证 prompt（system + user）"""
        system_prompt = (
            "你是一个记忆验证助手。你的任务是判断一条存储的记忆是否仍然准确和有用。\n"
            "你必须只返回 JSON，不要输出其他任何内容。\n"
            "JSON 格式：\n"
            '{"is_accurate": true/false, "reason": "简短原因(中文,50字内)", "suggested_action": "keep|update|archive"}\n'
            "\n"
            "判断标准：\n"
            "- keep: 记忆内容仍然准确且有用\n"
            "- update: 记忆内容部分过时但仍有参考价值，需要更新\n"
            "- archive: 记忆内容已完全过时或不再相关，应归档\n"
            "\n"
            "特别注意以下情况应判 archive：\n"
            "- 一次性任务状态（如'下载验证'、'环境搭建完成'等已完成的历史记录）\n"
            "- 临时调试/测试残留数据（如简单的 key-value 对、无语义的标记值）\n"
            "- 与当前项目无关的个人数据（如游戏抽卡记录、非项目相关的个人统计）\n"
            "- key 名拼写错误或语义不清（如单字 key、无意义的缩写）\n"
            "- 旧版本的工作进度索引（如 wip_index、task_reminders 等已被任务系统取代）\n"
            "\n"
            "原则：不确定时选 keep（保留优于误删）；但明确的过时/无关数据应果断 archive。"
        )
        value_preview = value[:800] if len(value) > 800 else value
        user_prompt = (
            f"记忆 key: {key}\n"
            f"记忆内容:\n{value_preview}\n"
            f"\n请判断这条记忆是否仍然准确和有用，返回 JSON。"
        )
        return system_prompt, user_prompt

    @staticmethod
    def _parse_validate_response(raw: str) -> dict | None:
        """解析 LLM 返回的 JSON，容错处理 markdown 包裹和文本包裹"""
        if not raw:
            return None

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            data = json.loads(cleaned)
            if not isinstance(data, dict):
                return None
            if "is_accurate" not in data or "suggested_action" not in data:
                return None
            if data["suggested_action"] not in ("keep", "update", "archive"):
                data["suggested_action"] = "keep"
            return data
        except json.JSONDecodeError:
            match = re.search(r"\{[^}]+\}", raw)
            if match:
                try:
                    data = json.loads(match.group())
                    if isinstance(data, dict) and "suggested_action" in data:
                        if data["suggested_action"] not in ("keep", "update", "archive"):
                            data["suggested_action"] = "keep"
                        return data
                except json.JSONDecodeError:
                    pass
            return None
