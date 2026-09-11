"""Loop Action 实现 — 场景A 活动追踪 + 场景B 下载监控

场景A（活动追踪）：
    CollectWindowsAction   — 每分钟采集窗口列表 → windows_YYYYMMDD.jsonl
    ScreenVLDescribeAction — 每5分钟截图+VL描述 → screen_YYYYMMDD.jsonl
    HourlySummarizeAction  — 每小时整点 LLM 总结 → YYYYMMDD_HH.md

场景B（下载监控）：
    <data_drive>:/DownloadscanAction     — 每5分钟扫描下载文件夹，超阈值时分类+推送收件箱

数据流：
    [每分钟]    _enum_windows()           → data/activity/raw/windows_YYYYMMDD.jsonl (append)
    [每5分钟]   _capture_fullscreen() +   → data/activity/raw/screen_YYYYMMDD.jsonl (每条含VL文本)
                remote_vl.understand()
    [每小时]    call_llm(powerful) → data/activity/hourly/YYYYMMDD_HH.md (一条总结)
    [每5分钟]   scan <data_drive>:/Downloads → classify_with_disposition → inbox（三类：move/inspect/unknown）

内部调用：Loop Action 在后端进程内运行，直接 import screen/vision/llm_pool 的底层函数，
不通过 HTTP。同步阻塞函数用 asyncio.to_thread 包装。
"""

import asyncio
import io
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from server.model_manager.types import ModelUnavailableError

from .loop_manager import Action

logger = logging.getLogger("localagent.loop_actions")


# ==================== 工具函数 ====================

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _date_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _append_jsonl(path: Path, record: dict) -> None:
    """追加一条 JSON 记录到 jsonl 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    """读取 jsonl 文件全部记录"""
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _filter_by_hour(records: list[dict], hour_str: str) -> list[dict]:
    """过滤出指定小时（ts 字段的 HH == hour_str）的记录"""
    out = []
    for r in records:
        ts = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts)
            if dt.strftime("%H") == hour_str:
                out.append(r)
        except (ValueError, TypeError):
            continue
    return out


def _get_dir(config: dict, key: str, default: str) -> Path:
    """从 config 读取目录路径；相对路径相对于项目根目录解析（不依赖 cwd）。"""
    p = Path(config.get(key, default))
    return p if p.is_absolute() else Path(__file__).resolve().parent.parent.parent / p


# ==================== 系统负载检测（供 VL OCR 兜底决策） ====================

def _get_system_load() -> tuple[float, float | None]:
    """获取当前系统负载

    Returns:
        (cpu_percent, gpu_percent)
        cpu_percent: 0-100，psutil 0.1s 采样
        gpu_percent: 0-100，无 GPU 或查询失败时为 None
    """
    import subprocess
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
    except Exception:
        cpu = 0.0
    # GPU: 优先 nvidia-smi（无需 pip install 额外包）
    gpu: float | None = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if lines and lines[0].strip().isdigit():
                gpu = float(lines[0].strip())
    except Exception:
        pass
    return cpu, gpu


def _ocr_image_fallback(image) -> str | None:
    """OCR 兜底：用 PaddleOCR 识别屏幕文字

    PaddleOCR 未加载时触发懒加载（首次 ~4s，在 asyncio.to_thread 中不阻塞事件循环）。
    VL 已经失败的兜底场景，多等 4s 比直接返回失败好。
    返回 OCR 文本前 500 字符；无文字或异常时返回 None。
    """
    try:
        from server.ocr import _run_classic_ocr
        items = _run_classic_ocr(image)  # get_ocr() 内部触发懒加载
        texts = [it["text"] for it in items if it.get("text")]
        if not texts:
            return None
        return " | ".join(texts)[:500]
    except ModelUnavailableError:
        raise  # 管理器拒绝向上传播：调用方计 skipped 而非 failed（design §6）
    except Exception as e:
        logger.warning(f"OCR 兜底失败: {e}")
        return None


# ==================== CollectWindowsAction ====================

class CollectWindowsAction(Action):
    """每分钟采集窗口列表，append 到 windows_YYYYMMDD.jsonl

    每条窗口记录含 is_foreground 字段（当前前台窗口标记为 True），
    供 activity_signal.compute_signal 检测焦点变化（焦点变化时 VL 优先放行）。
    """

    action_type = "collect_windows"

    async def execute(self, context: dict) -> dict:
        from server.screen import _enum_windows, _get_idle_seconds
        raw_dir = _get_dir(context["config"], "raw_data_dir", "data/activity/raw")
        try:
            windows = await asyncio.to_thread(_enum_windows)
            idle_sec = await asyncio.to_thread(_get_idle_seconds)
            # 精简：只保留有标题的窗口，提取关键字段
            slim = []
            for w in windows:
                title = w.get("title", "")
                if not title:
                    continue
                slim.append({
                    "title": title,
                    "process": w.get("process_name", w.get("process", "")),
                    "is_foreground": bool(w.get("is_foreground", False)),
                })
            record = {
                "ts": _now_iso(),
                "windows": slim,
                "idle_seconds": round(idle_sec, 1),
            }
            _append_jsonl(raw_dir / f"windows_{_date_str()}.jsonl", record)
            return {"success": True, "window_count": len(slim), "idle_seconds": idle_sec}
        except Exception as e:
            logger.exception("CollectWindows 执行失败")
            return {"success": False, "error": str(e)}


# ==================== ScreenVLDescribeAction ====================

class ScreenVLDescribeAction(Action):
    """每5分钟截图 + VL 描述，append 到 screen_YYYYMMDD.jsonl

    智能配额决策流程：
        1. compute_activity_signal() 检测 idle/focus_changed/passive/active
        2. vl_quota.should_call(signal) 决策是否调用 VL
        3. 拒绝（idle/节流/已耗尽）：
            - 按 debug_info["decision"] 精确映射 vl_status（Ticket 05 硬切语义）：
              deny_idle → skipped_idle
              deny_pace → skipped_pace
              deny_min_interval → skipped_min_interval
              deny_exhausted / deny_quota_used → skipped_exhausted
              其他 → skipped_unknown（兜底，理论上不应到达）
            - 配置启用 OCR 兜底 且 CPU<阈值 且 GPU<阈值 时跑 PaddleOCR
            - 高负载时 OCR 兜底也被跳过 → vl_status 改为 skipped_high_load
              （独立的 OCR 维度，与 deny 原因分类叠加；deny 原因已在
              decision_counts 中记录，不会因 vl_status 覆盖而丢失）
        4. 允许 → 调用 VL（保留原指数退避重试逻辑）

    VL 限流处理：限流时等 60 秒重试，最多 3 次；三次都失败才跳过，
    记 vl_status="failed_after_retries"。
    """

    action_type = "screen_vl"

    async def execute(self, context: dict) -> dict:
        from PIL import Image

        from server.activity_tracker.activity_signal import compute_signal
        from server.activity_tracker.vl_quota import vl_quota
        from server.screen import _capture_fullscreen, is_desktop_locked
        from server.vl.vision import remote_vl

        raw_dir = _get_dir(context["config"], "raw_data_dir", "data/activity/raw")
        question = context["config"].get(
            "screen_vl_question",
            "用一句话描述当前屏幕内容。规则：(1) 优先从窗口标题栏识别应用/网页名称，"
            "不要猜测游戏名或应用名；(2) 若画面是游戏/视频/图片等无标题栏内容，"
            "描述画面可见元素即可，不要断言具体作品名；(3) 不确定时如实说明'疑似XX'或'无法识别'。",
        )

        try:
            png_bytes = await asyncio.to_thread(_capture_fullscreen)
            image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        except Exception as e:
            # 显式锁屏判定：锁屏时 GDI/桌面不可捕获属环境原因而非故障，
            # 复用 skipped 第三态（不计 fail_count、正确记录），避免刷 ERROR 与误暂停 loop。
            # 非锁屏的截图失败仍按失败返回（计 fail_count），保留真实错误可见性，不掩盖。
            try:
                if is_desktop_locked():
                    logger.info("ScreenVL 截图跳过：检测到桌面已锁屏")
                    return {
                        "success": True,
                        "skipped": True,
                        "vl_status": "skipped_locked",
                        "skip_reason": "screen locked",
                        "ocr_fallback_used": False,
                    }
            except Exception:
                logger.debug("锁屏检测异常，按截图失败处理", exc_info=True)
            logger.exception("ScreenVL 截图失败")
            return {"success": False, "error": f"截图失败: {e}"}

        # ===== 配额决策 =====
        # 计算活动信号（读最近 2 条 windows 记录判断 idle/focus_changed）
        signal = compute_signal(raw_dir=raw_dir)
        allow, reason, debug_info = vl_quota.should_call(signal)

        if not allow:
            # 配额拒绝 → 跳过 VL，按决策类型精确映射 vl_status（Ticket 05 硬切语义）
            # debug_info["decision"] 来自 vl_quota.should_call 的各决策分支
            # 旧 skipped_quota 不再产生（Anti-Cheat 硬切要求，不保留兼容路径）
            ocr_fallback_used = False
            answer = None
            decision = debug_info.get("decision", "")
            if decision == "deny_idle":
                vl_status = "skipped_idle"
            elif decision == "deny_pace":
                vl_status = "skipped_pace"
            elif decision == "deny_min_interval":
                vl_status = "skipped_min_interval"
            elif decision in ("deny_exhausted", "deny_quota_used"):
                vl_status = "skipped_exhausted"
            else:
                # 兜底：理论上不应到达（should_call 的所有分支都设了 decision）
                # 出现说明 vl_quota.should_call 有遗漏分支，记 unknown 便于排查
                vl_status = "skipped_unknown"
                logger.warning("ScreenVL 收到未知的配额决策: %s", decision)
            cpu_load, gpu_load = None, None

            if context["config"].get("vl_ocr_fallback_enabled", True):
                cpu_load, gpu_load = await asyncio.to_thread(_get_system_load)
                cpu_threshold = float(context["config"].get("vl_ocr_fallback_cpu_threshold", 50))
                gpu_threshold = float(context["config"].get("vl_ocr_fallback_gpu_threshold", 50))
                # D3-B: CPU/GPU 均超阈值才跳过（取较轻的：有些游戏主要吃 CPU 有些主要吃 GPU）
                cpu_ok = (cpu_load is None) or (cpu_load < cpu_threshold)
                gpu_ok = (gpu_load is None) or (gpu_load < gpu_threshold)
                if cpu_ok or gpu_ok:
                    try:
                        ocr_text = await asyncio.to_thread(_ocr_image_fallback, image)
                    except ModelUnavailableError as e:
                        ocr_text = None
                        logger.info(f"OCR 兜底被模型管理器拒绝，跳过: {e.reason}")
                    if ocr_text:
                        answer = f"[OCR兜底] {ocr_text}"
                        ocr_fallback_used = True
                else:
                    # 高负载时 OCR 兜底也被跳过：vl_status 改为 skipped_high_load
                    # 注意：这会"覆盖"原本的 deny 原因分类（如 skipped_pace），
                    # 但 deny 原因已在 decision_counts 中独立记录，不会丢失。
                    # skipped_high_load 在 grant_outcomes 中独立统计 OCR 维度。
                    vl_status = "skipped_high_load"
            # 禁用 OCR 兜底时保持按 decision 映射的 vl_status（不覆盖）

            # 记录调用（skipped 不消耗配额，不更新 last_call）
            vl_quota.record_call(vl_status)

            record = {
                "ts": _now_iso(),
                "question": question,
                "answer": answer,
                "vl_status": vl_status,
                "elapsed_ms": 0,
                "skip_reason": reason,
                "signal_state": signal.get("state"),
                "focus_changed": signal.get("focus_changed"),
                "current_foreground": signal.get("current_foreground"),
                "ocr_fallback_used": ocr_fallback_used,
                "cpu_load": cpu_load,
                "gpu_load": gpu_load,
                "quota_debug": debug_info,
            }
            try:
                _append_jsonl(raw_dir / f"screen_{_date_str()}.jsonl", record)
            except Exception as e:
                logger.warning(f"ScreenVL 写盘失败: {e}")
            logger.info(
                f"ScreenVL 跳过 ({vl_status}): {reason} | signal={signal.get('state')} "
                f"cpu={cpu_load} gpu={gpu_load} ocr_fallback={ocr_fallback_used}"
            )
            # 主动跳过 = 配额决策按设计生效，不算失败
            # 也不算 ok（不增加 used_today），用 skipped=True 标识第三态
            return {
                "success": True, "skipped": True, "vl_status": vl_status,
                "skip_reason": reason, "ocr_fallback_used": ocr_fallback_used,
            }

        # ===== 配额允许 → 调用 VL =====
        retry_count = context["config"].get("vl_retry_count", 3)
        backoff_base = context["config"].get("vl_retry_backoff_base", 30)
        answer = None
        vl_status = "ok"
        ocr_fallback_used = False  # VL 失败后走 OCR 兜底时置 True
        t0 = time.perf_counter()
        remote_vl.reload_config()

        for attempt in range(retry_count):
            if not remote_vl.available:
                wait = backoff_base * (2 ** attempt)
                logger.info(f"ScreenVL 不可用，等{wait}秒重试 ({attempt+1}/{retry_count})")
                await asyncio.sleep(wait)
                remote_vl.reload_config()
                continue
            try:
                result = await asyncio.to_thread(
                    remote_vl.understand, image, question, None, use_case="vl_activity_tracker"
                )
                if result.get("status") == "ok":
                    answer = result.get("answer", "")
                    break
                # 非 ok（限流/错误）→ 重试
                logger.warning(
                    f"ScreenVL 返回非ok ({attempt+1}/{retry_count}): {result.get('detail', '')}"
                )
                if attempt < retry_count - 1:
                    await asyncio.sleep(backoff_base * (2 ** attempt))
            except Exception as e:
                logger.warning(f"ScreenVL 调用异常 ({attempt+1}/{retry_count}): {e}")
                if attempt < retry_count - 1:
                    await asyncio.sleep(backoff_base * (2 ** attempt))
        else:
            vl_status = "failed_after_retries"

        # VL 调用失败时走 OCR 兜底（复用 skipped 分支的兜底逻辑）
        # 场景：所有 provider 都 429/503/exhausted 时，VL 重试耗尽失败
        # 不应直接返回失败，而应降级到 OCR 保证业务正常
        if vl_status == "failed_after_retries":
            if context["config"].get("vl_ocr_fallback_enabled", True):
                cpu_load, gpu_load = await asyncio.to_thread(_get_system_load)
                cpu_threshold = float(context["config"].get("vl_ocr_fallback_cpu_threshold", 50))
                gpu_threshold = float(context["config"].get("vl_ocr_fallback_gpu_threshold", 50))
                cpu_ok = (cpu_load is None) or (cpu_load < cpu_threshold)
                gpu_ok = (gpu_load is None) or (gpu_load < gpu_threshold)
                if cpu_ok or gpu_ok:
                    try:
                        ocr_text = await asyncio.to_thread(_ocr_image_fallback, image)
                    except ModelUnavailableError as e:
                        # 管理器拒绝 OCR 加载/使用：本次画面检查无法完成，但不是任务失败——
                        # 计 skipped 三态（不累计 fail_count、不自动暂停常驻任务，design §6）
                        logger.info(f"OCR 兜底被模型管理器拒绝，本次计 skipped: {e.reason}")
                        vl_quota.record_call(vl_status)  # VL 调用已发生，如实记录
                        return {
                            "success": True, "skipped": True, "vl_status": vl_status,
                            "skip_reason": f"ocr_unavailable:{e.reason}",
                            "ocr_fallback_used": False,
                        }
                    if ocr_text:
                        answer = f"[OCR兜底] {ocr_text}"
                        ocr_fallback_used = True
                        logger.info(f"ScreenVL 失败后走 OCR 兜底成功，ocr_text 长度 {len(ocr_text)}")
                    else:
                        logger.warning("ScreenVL 失败后走 OCR 兜底，但 OCR 返回空")
                else:
                    logger.warning(f"ScreenVL 失败后 OCR 兜底被跳过（高负载 cpu={cpu_load} gpu={gpu_load}）")

        # 记录调用结果（ok/failed 都消耗配额，更新 last_call）
        vl_quota.record_call(vl_status)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        record = {
            "ts": _now_iso(),
            "question": question,
            "answer": answer,
            "vl_status": vl_status,
            "elapsed_ms": elapsed_ms,
            "signal_state": signal.get("state"),
            "focus_changed": signal.get("focus_changed"),
            "current_foreground": signal.get("current_foreground"),
            "ocr_fallback_used": ocr_fallback_used,
            "quota_debug": debug_info,
        }
        try:
            _append_jsonl(raw_dir / f"screen_{_date_str()}.jsonl", record)
        except Exception as e:
            logger.warning(f"ScreenVL 写盘失败: {e}")
            return {"success": False, "error": f"写盘失败: {e}", "vl_status": vl_status}
        # VL 成功 或 VL 失败但 OCR 兜底成功（有 answer）→ success=True
        # VL 失败且 OCR 兜底也失败（answer=None）→ success=False
        return {
            "success": answer is not None,
            "vl_status": vl_status,
            "elapsed_ms": elapsed_ms,
            "signal_state": signal.get("state"),
            "ocr_fallback_used": ocr_fallback_used,
        }


# ==================== HourlySummarizeAction ====================

class HourlySummarizeAction(Action):
    """每小时整点总结上一小时的活动数据

    cron "0 * * * *" 触发时，总结刚过去的完整小时（如 14:00 触发 → 13:00-14:00）。
    读取 windows_YYYYMMDD.jsonl + screen_YYYYMMDD.jsonl，按小时过滤，
    调 LLM powerful 层生成总结，写入 YYYYMMDD_HH.md。
    """

    action_type = "hourly_summarize"

    async def execute(self, context: dict) -> dict:
        raw_dir = _get_dir(context["config"], "raw_data_dir", "data/activity/raw")
        hourly_dir = _get_dir(context["config"], "hourly_data_dir", "data/activity/hourly")

        # 上一整点（14:00 触发 → 13:00-14:00）
        # catchup 模式：用 catchup_target_ts 计算目标整点（如 catchup_target_ts=13:00 → 总结 12:00-13:00）
        if "catchup_target_ts" in context:
            target = datetime.fromtimestamp(context["catchup_target_ts"])
            last_hour = target.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        else:
            now = datetime.now()
            last_hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        date_str = last_hour.strftime("%Y%m%d")
        hour_str = last_hour.strftime("%H")
        hour_label = last_hour.strftime("%Y-%m-%d %H:00")

        # 读取并过滤本小时数据
        windows_records = _filter_by_hour(
            _read_jsonl(raw_dir / f"windows_{date_str}.jsonl"), hour_str
        )
        screen_records = _filter_by_hour(
            _read_jsonl(raw_dir / f"screen_{date_str}.jsonl"), hour_str
        )

        if not windows_records and not screen_records:
            logger.info(f"HourlySummarize {hour_label}: 无活动数据，跳过")
            # 写一个空总结标记
            hourly_dir.mkdir(parents=True, exist_ok=True)
            out_path = hourly_dir / f"{date_str}_{hour_str}.md"
            out_path.write_text(
                f"# {hour_label} 活动总结\n\n> 本时段无采集数据（后端可能未运行）\n",
                encoding="utf-8",
            )
            return {"success": True, "summary_len": 0, "skipped": True, "skip_reason": "无采集数据"}

        # 内容审核保护：预检（二分法 LLM 询问）+ 模糊化敏感标题/VL 描述
        # 防 GLM 内容过滤（code 1301）拒绝生成 hourly 总结
        from server.activity_tracker.content_filter import (
            preprocess_activity_data,
        )
        filter_result = preprocess_activity_data(
            windows_records, screen_records,
            enable_prefilter=context["config"].get("hourly_content_filter_enabled", True),
        )

        # 构造 prompt（封装为函数，便于激进脱敏后重建）
        def _build_prompt() -> tuple[str, str]:
            ws = _format_windows_diff_for_prompt(windows_records)
            ss = _format_screens_for_prompt(screen_records)
            p = (
                f"以下是用户在 {hour_label} 这一小时内的电脑活动采集数据。\n"
                f"请总结用户这一小时主要做了什么，按时间顺序列出关键活动，"
                f"标注活动切换的时间点。如果有 VL 数据缺失时段请说明。\n\n"
                f"## 窗口切换记录（diff 格式，每分钟采集，{len(windows_records)} 条）\n"
                f"格式说明：基准行后只列出有变化的时刻，"
                f"'焦点: A → B' 表示前台窗口从 A 切到 B，"
                f"'+ 新增' 表示打开的窗口，'- 关闭' 表示关闭的窗口。\n"
                f"无变化的时段已合并省略（用户未切换活动）。\n\n"
                f"{ws}\n\n"
                f"## 屏幕VL描述（每5分钟采集，{len(screen_records)} 条）\n{ss}\n"
            )
            sp = (
                "你是用户活动总结助手。根据窗口切换记录和屏幕VL描述，"
                "生成简洁的小时活动总结。用 Markdown 格式，开头用一句话概括，"
                "然后按时间顺序列出关键活动。\n\n"
                "防幻觉规则（必须遵守）：\n"
                "1. 应用名/产品名/游戏名/网站名一律以【窗口标题】为准，不要从 VL 描述中采信具体作品名"
                "（VL 可能误判，如把某游戏画面误判为另一款游戏）\n"
                "2. 不要把多个工具名组合成不存在的产品名（如 Chrome+豆包≠'豆包浏览器'，"
                "应为'在 Chrome 中使用豆包'）\n"
                "3. VL 描述仅作辅助参考，用于补充画面元素（弹窗/错误/加载状态等），"
                "不用于断言应用/游戏名称\n"
                "4. 数据中未明确出现的名称不要编造，不确定时如实说明\n"
                "5. 窗口标题碎片可拼接还原应用名，但要保持原样（如'Trae CN - yihuan_gacha.py'→"
                "在 Trae CN 中编辑 yihuan_gacha.py，不要改成'Trae CN 编辑器'之类的衍生名）\n"
                "6. 窗口记录是 diff 格式：'+ 新增'是本时段打开的窗口，'- 关闭'是关闭的窗口，"
                "'焦点: A → B' 是前台切换；基准行表示开始时的窗口快照"
            )
            return p, sp

        prompt, system_prompt = _build_prompt()
        windows_summary = _format_windows_diff_for_prompt(windows_records)
        screen_summary = _format_screens_for_prompt(screen_records)

        # 调 LLM：tier 范围由 use_case="hourly_summarize" 自动查 USE_CASE_REGISTRY.default_tier
        # 不再读 config.toml [llm.models.powerful]，key 系统统一接管
        # 用 call_llm（非 call_llm_simple）拿到 error 详情，按错误类型区分：
        #   - quota/rate_limit 类（no available keys / 429）→ 退避重试（30s/60s/120s，最多 3 次），
        #     重试耗尽再写占位文件（避免"假成功让出"导致 last_run_at 推进、hourly 文件永久缺失）
        #   - 真失败（empty content / timeout / HTTP 500）→ 直接写占位文件，不重试
        # retries=1：call_llm 内部重试 1 次，外层再退避重试 max_retries 次
        from server.llm_pool import call_llm

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # 退避重试配置（quota/rate_limit 类失败才重试，真失败不重试）
        max_retries = int(context["config"].get("hourly_summarize_max_retries", 3))
        retry_backoff = int(context["config"].get("hourly_summarize_retry_backoff", 30))
        # 重试历史：记录每次失败的 err，最终写入占位文件供回看
        retry_errors: list[str] = []
        llm_result: dict | None = None
        content_filter_triggered = False  # 标记是否触发内容审核

        for attempt in range(max_retries + 1):  # 1 次正常 + N 次重试
            try:
                llm_result = await asyncio.to_thread(
                    call_llm,
                    messages,
                    temperature=0.3,
                    max_tokens=16384,
                    project="loop_hourly_summary",
                    use_case="hourly_summarize",
                    retries=1,
                )
            except Exception as e:
                logger.exception("HourlySummarize LLM 调用异常 (attempt %d/%d)", attempt + 1, max_retries + 1)
                llm_result = {"ok": False, "error": f"LLM 调用异常: {e}"}

            # 剥离思考链标签（某些 LLM 默认输出 <think>...</think>）
            if llm_result.get("ok") and llm_result.get("content"):
                from server.activity_tracker.content_filter import _strip_think_tags
                _stripped = _strip_think_tags(llm_result["content"])
                if _stripped:
                    llm_result["content"] = _stripped
                elif "<think>" in (llm_result.get("content") or ""):
                    # 思考链吃光 max_tokens，实际答案为空
                    llm_result["ok"] = False
                    llm_result["error"] = "empty content after stripping think tags"

            # 成功 + 后置检查通过 = 真成功
            from server.activity_tracker.content_filter import detect_filtered_response
            if llm_result.get("ok") and not detect_filtered_response(llm_result):
                break  # 真正成功，跳出重试循环

            # 成功但触发内容审核（content 过短/含 1301 关键词）→ 视为真失败，不重试
            if llm_result.get("ok") and detect_filtered_response(llm_result):
                content_filter_triggered = True
                retry_errors.append(
                    f"content_filter: {(llm_result.get('content') or '')[:80]}"
                )
                logger.warning(
                    "HourlySummarize %s LLM 返回触发内容审核（视为真失败不重试）",
                    hour_label
                )
                break

            err = llm_result.get("error", "unknown")
            err_lower = err.lower()
            # 1301/contentFilter 类失败 → 内容审核触发，视为真失败不重试
            if any(kw in err for kw in ("1301", "contentFilter", "违禁", "敏感内容", "不安全")):
                content_filter_triggered = True
                retry_errors.append(err)
                logger.warning(
                    "HourlySummarize %s 触发内容审核（不重试）: %s",
                    hour_label, err
                )
                break

            is_quota = (
                "no available keys" in err_lower
                or "429" in err_lower
                or "rate_limited" in err_lower
                or "rate limit" in err_lower
            )
            retry_errors.append(err)

            if not is_quota:
                # D6: empty content 时 fallback 到更高 tier 模型重试一次
                # 当前逻辑是 empty content 直接判定为"真失败不重试"，改为先试更高 tier
                if "empty content" in err_lower and attempt == 0:
                    logger.info(
                        "HourlySummarize %s empty content，fallback 到 tier 4 重试",
                        hour_label
                    )
                    try:
                        llm_result = await asyncio.to_thread(
                            call_llm,
                            messages,
                            temperature=0.3,
                            max_tokens=16384,
                            project="loop_hourly_summary",
                            use_case="hourly_summarize",
                            tier=4,  # 显式指定 tier 4（更强模型）
                            retries=1,
                        )
                    except Exception as e2:
                        logger.exception("HourlySummarize fallback 调用异常")
                        llm_result = {"ok": False, "error": f"fallback 异常: {e2}"}

                    # 剥离思考链
                    if llm_result.get("ok") and llm_result.get("content"):
                        from server.activity_tracker.content_filter import _strip_think_tags as _st
                        _s = _st(llm_result["content"])
                        if _s:
                            llm_result["content"] = _s
                        else:
                            llm_result["ok"] = False
                            llm_result["error"] = "empty content after stripping think tags (fallback)"

                    # 检查 fallback 是否成功
                    from server.activity_tracker.content_filter import detect_filtered_response as _det_fb
                    if llm_result.get("ok") and not _det_fb(llm_result):
                        break  # fallback 成功

                    # fallback 也失败，记录错误并继续（不重试）
                    retry_errors.append(f"fallback: {llm_result.get('error', 'unknown')}")

                # 真失败（empty content fallback 也失败 / timeout / HTTP 500）：直接写占位文件，不重试
                logger.warning(
                    "HourlySummarize %s 真失败（不重试）: %s",
                    hour_label, err
                )
                break

            if attempt < max_retries:
                delay = retry_backoff * (2 ** attempt)  # 30s → 60s → 120s 指数退避
                logger.info(
                    "HourlySummarize %s quota/rate_limit 失败 (attempt %d/%d)，%ds 后重试: %s",
                    hour_label, attempt + 1, max_retries + 1, delay, err
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "HourlySummarize %s 重试 %d 次后仍失败（quota/rate_limit）: %s",
                    hour_label, max_retries, err
                )

        # 兜底：循环结束仍无 result
        if llm_result is None:
            llm_result = {"ok": False, "error": "unknown: no llm_result"}

        # 后置补救：触发内容审核 → 激进脱敏（丢弃 VL + process_name 替代 title）+ 1 次重试
        if content_filter_triggered and not filter_result.get("prefilter_skipped"):
            from server.activity_tracker.content_filter import aggressive_mask
            logger.warning(
                "HourlySummarize %s 触发内容审核，激进脱敏后重试一次",
                hour_label
            )
            aggressive_mask(windows_records, screen_records)
            # 重建 prompt（基于脱敏后的 records）
            prompt, system_prompt = _build_prompt()
            windows_summary = _format_windows_diff_for_prompt(windows_records)
            screen_summary = _format_screens_for_prompt(screen_records)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            try:
                llm_result = await asyncio.to_thread(
                    call_llm,
                    messages,
                    temperature=0.3,
                    max_tokens=8192,  # 缩减，激进脱敏后内容更短
                    project="loop_hourly_summary",
                    use_case="hourly_summarize",
                    retries=1,
                )
            except Exception as e:
                logger.exception("HourlySummarize 激进脱敏后重试异常")
                llm_result = {"ok": False, "error": f"激进脱敏后重试异常: {e}"}
            # 剥离思考链标签
            if llm_result.get("ok") and llm_result.get("content"):
                from server.activity_tracker.content_filter import _strip_think_tags as _strip
                _s = _strip(llm_result["content"])
                if _s:
                    llm_result["content"] = _s
                elif "<think>" in (llm_result.get("content") or ""):
                    llm_result["ok"] = False
                    llm_result["error"] = "empty content after stripping think tags"
            # 重置触发标志，根据新结果判定
            from server.activity_tracker.content_filter import detect_filtered_response as _detect
            if llm_result.get("ok") and not _detect(llm_result):
                content_filter_triggered = False  # 重试成功
            elif llm_result.get("ok") and _detect(llm_result):
                content_filter_triggered = True  # 仍触发
            # 失败时保持原 content_filter_triggered 状态

        if llm_result.get("ok") and not content_filter_triggered:
            summary = llm_result.get("content", "")
        else:
            err = llm_result.get("error", "unknown")
            err_history = " | ".join(retry_errors[-3:])  # 最多记 3 次错误历史
            # 写占位文件，让日总结面板能看到该时段 LLM 失败（而非完全无文件）
            hourly_dir.mkdir(parents=True, exist_ok=True)
            out_path = hourly_dir / f"{date_str}_{hour_str}.md"
            if content_filter_triggered:
                # 内容审核触发：写脱敏后的数据 + 说明，标记 skipped（避免连败自动暂停）
                placeholder = (
                    f"# {hour_label} 活动总结\n\n"
                    f"> ⚠️ 本时段因内容审核保护触发，LLM 拒绝生成详细总结。\n"
                    f"> 已对敏感数据脱敏（windows={filter_result.get('windows_masked_count', 0)} 条，"
                    f"screens={filter_result.get('screens_masked_count', 0)} 条）。\n"
                    f"> 原始数据已采集（windows={len(windows_records)} 条，"
                    f"screen={len(screen_records)} 条），可手动重试。\n\n"
                    f"## 窗口切换摘要（已脱敏）\n{windows_summary[:2000]}\n\n"
                    f"## 屏幕 VL 描述（已脱敏）\n{screen_summary}\n"
                )
                out_path.write_text(placeholder, encoding="utf-8")
                logger.warning(
                    "HourlySummarize %s 内容审核触发（已脱敏写占位文件）%s",
                    hour_label, out_path.name
                )
                # 占位文件已写，不算硬失败（skipped 三态），避免 auto_pause
                return {
                    "success": True, "skipped": True,
                    "skip_reason": "content_filter_triggered",
                    "masked_titles": len(filter_result.get("sensitive_titles", set())),
                    "placeholder_file": out_path.name,
                }
            else:
                placeholder = (
                    f"# {hour_label} 活动总结\n\n"
                    f"> ⚠️ 本时段 LLM 调用失败（重试 {len(retry_errors)} 次仍失败）。\n"
                    f"> 最终失败原因：{err}\n"
                    f"> 错误历史：{err_history}\n"
                    f"> 数据已采集（windows={len(windows_records)} 条，"
                    f"screen={len(screen_records)} 条），可手动重试。\n\n"
                    f"## 原始数据摘要\n\n"
                    f"### 窗口切换记录（前 20 条）\n{windows_summary[:2000]}\n\n"
                    f"### 屏幕 VL 描述\n{screen_summary}\n"
                )
                out_path.write_text(placeholder, encoding="utf-8")
                logger.warning(
                    "HourlySummarize %s LLM 失败（重试耗尽，%s），已写占位文件 %s",
                    hour_label, err, out_path.name
                )
                return {"success": False, "error": err,
                        "placeholder_file": out_path.name,
                        "retry_count": len(retry_errors)}

        # 写 hourly md
        hourly_dir.mkdir(parents=True, exist_ok=True)
        out_path = hourly_dir / f"{date_str}_{hour_str}.md"
        content = f"# {hour_label} 活动总结\n\n{summary}\n"
        # 已存在不覆盖，循环追加 _vN 直到找到可用文件名
        if out_path.exists():
            for n in range(2, 100):
                candidate = hourly_dir / f"{date_str}_{hour_str}_v{n}.md"
                if not candidate.exists():
                    out_path = candidate
                    break
        out_path.write_text(content, encoding="utf-8")
        logger.info(f"HourlySummarize 完成: {out_path.name} ({len(summary)} 字符)")
        return {"success": True, "summary_len": len(summary), "file": out_path.name}

    @staticmethod
    def _format_windows(records: list[dict]) -> str:
        return _format_windows_diff_for_prompt(records)


def _format_windows_diff_for_prompt(records: list[dict]) -> str:
    """格式化窗口记录为 diff 格式（只传递变化，避免 LLM 上下文冗余）

    每条输出格式：
        [HH:MM:SS] 焦点: 旧标题 → 新标题 | +新增窗口 | -关闭窗口

    无变化的两条记录合并显示，只列出真正有变化的时刻。

    若记录 < 2 条（无 diff 可算），回退到全量列表（仅显示前 5 条标题）。
    """
    if not records:
        return "（无窗口采集数据）"
    if len(records) < 2:
        # 数据太少，无法做 diff → 显示全量
        r = records[0]
        ts = r.get("ts", "")[11:19]
        titles = [w["title"] for w in r.get("windows", []) if w.get("title")]
        titles_str = " | ".join(titles[:5])
        if len(titles) > 5:
            titles_str += f" (等{len(titles)}个)"
        return f"- [{ts}] {titles_str}（首条记录，无前序对比）"

    lines = []
    prev = records[0]
    # 先输出第一条作为基准
    prev_ts = prev.get("ts", "")[11:19]
    prev_titles = {w.get("title", "") for w in prev.get("windows", []) if w.get("title")}
    prev_fg = _find_foreground_title(prev)
    base_fg_str = f" 前台:{prev_fg}" if prev_fg else ""
    lines.append(f"- [{prev_ts}] 基准（{len(prev_titles)}个窗口）{base_fg_str}")

    for curr in records[1:]:
        curr_ts = curr.get("ts", "")[11:19]
        curr_titles = {w.get("title", "") for w in curr.get("windows", []) if w.get("title")}
        curr_fg = _find_foreground_title(curr)

        # 计算 diff
        opened = curr_titles - prev_titles  # 新增
        closed = prev_titles - curr_titles  # 关闭
        focus_changed = bool(prev_fg and curr_fg and prev_fg != curr_fg)

        # 只输出有变化的记录
        if not opened and not closed and not focus_changed:
            continue

        parts = [f"- [{curr_ts}]"]
        if focus_changed:
            parts.append(f"焦点: {prev_fg} → {curr_fg}")
        if opened:
            # 截断过长的窗口列表（避免单条 diff 太长）
            opened_str = " | ".join(sorted(opened))
            if len(opened_str) > 200:
                opened_str = opened_str[:200] + f"... (+{len(opened)}个)"
            parts.append(f"+ 新增: {opened_str}")
        if closed:
            closed_str = " | ".join(sorted(closed))
            if len(closed_str) > 200:
                closed_str = closed_str[:200] + f"... (-{len(closed)}个)"
            parts.append(f"- 关闭: {closed_str}")
        lines.append(" ".join(parts))

        prev_titles = curr_titles
        prev_fg = curr_fg

    if len(lines) <= 1:
        # 所有记录都没变化
        return f"（{len(records)} 条记录无窗口/焦点变化，用户在此期间未切换活动）"
    return "\n".join(lines)


def _find_foreground_title(record: dict) -> str:
    """从 windows 记录中找出前台窗口标题"""
    for w in record.get("windows", []):
        if w.get("is_foreground"):
            return w.get("title", "")
    return ""


def _format_windows_for_prompt(records: list[dict]) -> str:
    """格式化窗口记录供 LLM 阅读（保留兼容入口，新逻辑用 diff 格式）

    .. deprecated::
        改用 _format_windows_diff_for_prompt。此函数仅保留向后兼容。
    """
    return _format_windows_diff_for_prompt(records)


def _format_screens_for_prompt(records: list[dict]) -> str:
    """格式化屏幕VL描述供 LLM 阅读"""
    if not records:
        return "（无VL采集数据）"
    lines = []
    for r in records:
        ts = r.get("ts", "")[11:19]
        status = r.get("vl_status", "unknown")
        answer = r.get("answer", "")
        ocr_fallback = r.get("ocr_fallback_used", False)
        if status == "ok" and answer:
            lines.append(f"- [{ts}] {answer}")
        elif ocr_fallback and answer:
            # D3-A: OCR 兜底文字被消费，不再丢弃
            lines.append(f"- [{ts}] {answer}")
        else:
            lines.append(f"- [{ts}] (VL数据缺失: {status})")
    return "\n".join(lines)


# ==================== <data_drive>:/DownloadscanAction（场景B）====================

# 文本类扩展名（直接读取内容）
_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml",
    ".log", ".ini", ".cfg", ".conf", ".py", ".js", ".ts", ".java",
    ".cpp", ".c", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bat",
    ".ps1", ".html", ".css", ".sql", ".rtf", ".svg",
}
# 文档类扩展名（走 docviewer）
_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx"}
# 图片类扩展名（走 VL）
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}


def _extract_file_content(file_path: Path, max_chars: int = 500) -> str | None:
    """提取文件内容摘要，失败返回 None。

    - 文本类：直接读取前 max_chars 字符
    - 文档类（PDF/Word/Excel/PPT）：走 docviewer，取前 3 页文本拼接
    - 图片类：走 remote_vl，让 VL 用一句话描述图片内容
    - 其他（视频/音乐/软件/压缩包）：返回 None
    """
    ext = file_path.suffix.lower()
    try:
        if ext in _TEXT_EXTS:
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                text = f.read(max_chars + 1)
            return text[:max_chars] if text else None
        if ext in _DOC_EXTS:
            from server.docviewer import _read_document
            resp = _read_document(str(file_path), pages="1-3")
            parts = []
            total = 0
            for c in resp.content:
                piece = c.text or ""
                if total + len(piece) > max_chars:
                    piece = piece[: max_chars - total]
                if piece:
                    parts.append(piece)
                    total += len(piece)
                if total >= max_chars:
                    break
            return "\n".join(parts) if parts else None
        if ext in _IMAGE_EXTS:
            from PIL import Image

            from server.vl.remote_vl import remote_vl
            remote_vl.reload_config()
            if not remote_vl.available:
                return None
            with Image.open(file_path) as img:
                img = img.convert("RGB")
                result = remote_vl.understand(
                    img,
                    "用一句话简短描述这张图片的可见内容（用于文件分类辅助）。"
                    "规则：描述画面可见元素（如文档/表格/截图/照片等类型），"
                    "不要猜测或断言具体应用名/游戏名/网站名，不确定时只描述画面元素。",
                    None,
                    use_case="vl_vision",
                )
            if result.get("status") == "ok":
                ans = result.get("answer", "").strip()
                return ans[:max_chars] if ans else None
            return None
        return None
    except Exception as e:
        logger.warning(f"提取文件内容失败 [{file_path.name}]: {e}")
        return None


class <data_drive>:/DownloadscanAction(Action):
    """扫描下载文件夹，文件数超阈值时分类并推送收件箱

    逻辑（ticket 08 起支持文件夹）：
    1. 扫描 watch_dir 顶层条目（文件+文件夹，跳过隐藏文件）
    2. 读取 state.json，跳过已处理条目（分类即已处理机制）
    3. 若数量 <= threshold，跳过
    4. 若已有 source=download_watcher 的 pending inbox 条目，跳过（避免重复推送）
    5. 加载 tools/file_classifier/categories.json
    6. 动态加载 predictor.py + state_manager.py
    7. 文件走 classify_with_disposition，文件夹走 classify_folder（类交互式协议）
    8. Phase 2 内容增强仅对文件生效（文件夹不参与）
    9. 按 disposition 分组推送 inbox（move/inspect/unknown 各一条，空组不推）
    """

    action_type = "download_scan"

    async def execute(self, context: dict) -> dict:
        import importlib.util
        import os

        from server.inbox import get_store

        config = context["config"]
        watch_dir = Path(config.get("watch_dir", "<data_drive>:\<system_data_root>/<data_drive>:/Downloads"))
        threshold = config.get("file_count_threshold", 15)
        batch_size = config.get("classify_batch_size", 50)

        if not watch_dir.exists() or not watch_dir.is_dir():
            # watch_dir 缺失视为设计内跳过（skipped 三态），不计失败、不触发 auto_pause、不刷错误日志。
            # 用户创建目录后下次扫描自动恢复，无需重启后端。
            return {
                "success": True, "skipped": True,
                "skip_reason": f"watch_dir 不存在: {watch_dir}"
            }

        # 1. 扫描顶层条目
        entries = []
        try:
            for entry in os.scandir(watch_dir):
                if entry.name.startswith("."):
                    continue
                try:
                    stat = entry.stat()
                    entries.append({
                        "filename": entry.name,
                        "path": entry.path,  # 绝对路径，供 state_manager 过滤和 classify_folder 使用
                        "size": stat.st_size,
                        "is_dir": entry.is_dir(),
                    })
                except OSError:
                    continue
        except Exception as e:
            return {"success": False, "error": f"扫描失败: {e}"}

        if len(entries) <= threshold:
            return {
                "success": True, "skipped": True, "file_count": len(entries),
                "skip_reason": f"文件数 {len(entries)} <= 阈值 {threshold}",
            }

        # 2. 检查是否已有 pending 的 download_watcher 条目（避免重复推送）
        try:
            store = get_store()
            existing = store.list(status="pending", source="download_watcher", limit=10)
        except Exception as e:
            return {"success": False, "error": f"inbox 不可用: {e}"}

        if existing:
            return {
                "success": True, "skipped": True, "file_count": len(entries),
                "skip_reason": f"已有 {len(existing)} 个 pending 条目待处理",
            }

        # 3. 加载 categories.json
        categories_path = Path(__file__).parent.parent.parent / "tools" / "file_classifier" / "categories.json"
        try:
            categories = json.loads(
                categories_path.read_text(encoding="utf-8")
            ).get("categories", [])
        except Exception as e:
            return {"success": False, "error": f"加载 categories.json 失败: {e}"}

        # 4. 动态加载 predictor.py（tools/ 下，非 server 包）
        predictor_path = Path(__file__).parent.parent.parent / "tools" / "file_classifier" / "predictor.py"
        try:
            spec = importlib.util.spec_from_file_location("predictor", predictor_path)
            if spec is None or spec.loader is None:
                raise ValueError("spec/loader 不可用")
            predictor = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(predictor)
        except Exception as e:
            return {"success": False, "error": f"加载 predictor.py 失败: {e}"}

        # 4.1 动态加载 state_manager.py（ticket 08：读取 state.json 跳过已处理）
        state_mgr_path = Path(__file__).parent.parent.parent / "tools" / "file_classifier" / "state_manager.py"
        sm_instance = None
        try:
            sm_spec = importlib.util.spec_from_file_location("state_manager", state_mgr_path)
            if sm_spec is None or sm_spec.loader is None:
                raise ValueError("spec/loader 不可用")
            state_mgr_module = importlib.util.module_from_spec(sm_spec)
            sm_spec.loader.exec_module(state_mgr_module)
            state_path = str(predictor_path.parent / "state.json")
            sm_instance = state_mgr_module.StateManager(state_path)
        except Exception as e:
            logger.warning(f"加载 state_manager.py 失败，跳过已处理过滤: {e}")

        # 4.2 过滤已处理条目（ticket 08：分类即已处理，跳过 state.json 标记的条目）
        if sm_instance is not None:
            before_count = len(entries)
            entries = [e for e in entries if not sm_instance.is_processed(e["path"])]
            skipped_processed = before_count - len(entries)
            if skipped_processed > 0:
                logger.info(f"<data_drive>:/Downloadscan 跳过 {skipped_processed} 个已处理条目")

        # 5. 分流文件/文件夹（ticket 08：文件夹走 classify_folder 类交互式协议）
        folder_batch_size = int(config.get("folder_batch_size", 10))
        file_entries = [e for e in entries if not e["is_dir"]]
        folder_entries = [e for e in entries if e["is_dir"]]

        # 文件分类（限制 batch_size 个，避免 LLM 调用过多）
        # v8: 不再从 config.toml 读 model；predictor 内部已用 use_case="download_watcher"
        # 走 pool.call(use_case, tier)，由 USE_CASE_REGISTRY.default_tier 自动选 tier 的 key/model。
        # download_watcher 是 sensitive use_case，自动排除 privacy_warning 非空的 key（即 data_safe 过滤）。
        to_classify = file_entries[:batch_size]

        try:
            predictions = await asyncio.to_thread(
                predictor.classify_with_disposition,
                [],            # sample_files（自动触发无样例）
                to_classify,   # all_files
                categories,    # categories
                None,          # user_preferences
                None,          # model=None，由 pool 在 tier 匹配的 key 中选
            )
        except Exception as e:
            logger.exception("<data_drive>:/Downloadscan 分类失败")
            return {"success": False, "error": f"分类失败: {e}"}

        # 5.1 文件夹分类（ticket 08：走 classify_folder 类交互式协议，仅用文件名+扩展名分布）
        folder_predictions = []
        if folder_entries:
            folders_to_classify = folder_entries[:folder_batch_size]
            logger.info(
                f"<data_drive>:/Downloadscan 文件夹分类: {len(folders_to_classify)}/{len(folder_entries)} 个"
            )
            for folder in folders_to_classify:
                try:
                    result = await asyncio.to_thread(
                        predictor.classify_folder,
                        folder["path"], categories,
                    )
                    conf = result.get("confidence", 0)
                    cat = result.get("classification", "未知")
                    cat_path = next(
                        (c.get("path", "") for c in categories if c.get("name") == cat), ""
                    )
                    # 推导 disposition（与 classify_with_disposition 一致的阈值逻辑）
                    if conf < 0.5 or cat == "未知":
                        disp = "unknown"
                    elif conf >= 0.85 and cat_path:
                        disp = "move"
                    else:
                        disp = "inspect"
                    folder_pred = {
                        "filename": folder["filename"],
                        "category": cat,
                        "confidence": conf,
                        "disposition": disp,
                        "is_folder": True,
                        "degraded": result.get("degraded", False),
                        "reason": result.get("reason", ""),
                    }
                    if disp == "move":
                        folder_pred["target_path"] = cat_path
                    folder_predictions.append(folder_pred)
                except Exception as e:
                    logger.warning(
                        f"<data_drive>:/Downloadscan 文件夹分类失败 {folder['filename']}: {e}"
                    )
                    folder_predictions.append({
                        "filename": folder["filename"],
                        "category": "未知",
                        "confidence": 0.0,
                        "disposition": "unknown",
                        "is_folder": True,
                        "degraded": True,
                        "reason": f"分类异常: {e}",
                    })
            predictions.extend(folder_predictions)
            logger.info(f"<data_drive>:/Downloadscan 文件夹分类完成: {len(folder_predictions)} 个")

        # ===== Phase 2: 内容增强再分类（仅 inspect/unknown，仅 privacy_safe 模型）=====
        content_aware = config.get("content_aware", False)
        content_max_chars = int(config.get("content_max_chars", 500))
        content_max_files = int(config.get("content_max_files", 10))

        if content_aware:
            # v14: use_case="download_watcher" 是 sensitive，pool 已自动过滤掉
            # privacy_warning 非空的 key。Phase 1 调用成功即证明当前有 privacy_warning
            # 为空的 key 可用（即 data_safe），所以 Phase 2 可直接执行（同一 use_case 同一 pool）。
            # Phase 2 仅对文件生效（ticket 08：文件夹不参与内容增强，仅用文件名+扩展名分布）
            phase2_files = [
                p for p in predictions
                if p.get("disposition") in ("inspect", "unknown")
                and not p.get("is_folder", False)
            ]
            if phase2_files:
                phase2_limited = phase2_files[:content_max_files]
                logger.info(
                    f"<data_drive>:/Downloadscan Phase2: 提取 {len(phase2_limited)}/{len(phase2_files)} 个文件内容"
                )
                content_map: dict[str, str] = {}
                for pred in phase2_limited:
                    fname = pred.get("filename", "")
                    if not fname:
                        continue
                    # 路径遍历防护：fname 来自 LLM/predictor 输出，必须为纯文件名
                    if "/" in fname or "\\" in fname or fname.startswith(".."):
                        logger.warning(f"<data_drive>:/Downloadscan Phase2: 跳过可疑文件名 '{fname}'")
                        continue
                    fp = watch_dir / fname
                    if not fp.exists() or not fp.is_file():
                        continue
                    content = await asyncio.to_thread(_extract_file_content, fp, content_max_chars)
                    if content:
                        content_map[fname] = content

                if content_map:
                    phase2_entries = [
                        {"filename": p["filename"], "size": next(
                            (e["size"] for e in to_classify if e.get("filename") == p["filename"]),
                            0
                        )}
                        for p in phase2_limited
                        if p["filename"] in content_map
                    ]
                    try:
                        phase2_preds = await asyncio.to_thread(
                            predictor.classify_with_disposition,
                            [], phase2_entries, categories, None, None, content_map,
                        )
                        # 用 Phase 2 结果覆盖 Phase 1 中对应文件
                        updated = {p["filename"]: p for p in phase2_preds}
                        for pred in predictions:
                            if pred["filename"] in updated:
                                pred.update(updated[pred["filename"]])
                        logger.info(
                            f"<data_drive>:/Downloadscan Phase2 完成: {len(updated)} 个文件基于内容重新分类"
                        )
                    except Exception as e:
                        logger.warning(f"<data_drive>:/Downloadscan Phase2 分类失败，保留 Phase1 结果: {e}")
                else:
                    logger.info("<data_drive>:/Downloadscan Phase2: 未能提取到任何文件内容")
            else:
                logger.info("<data_drive>:/Downloadscan Phase2: 无 inspect/unknown 文件，跳过")

        # 6. 按 disposition 分组推送 inbox
        groups: dict[str, list] = {"move": [], "inspect": [], "unknown": []}
        for pred in predictions:
            disp = pred.get("disposition", "unknown")
            groups.setdefault(disp, []).append(pred)

        scan_time = _now_iso()
        pushed = 0
        for disp, items in groups.items():
            if not items:
                continue
            item = {
                "source": "download_watcher",
                "category": disp,
                "title": f"下载文件夹分类·{disp}：{len(items)} 个条目",
                "description": _build_inbox_description(disp, items, watch_dir),
                "payload": {
                    "scan_dir": str(watch_dir),
                    "scan_time": scan_time,
                    "total_files": len(entries),
                    "classified_count": len(items),
                    "files": items,
                },
            }
            store.create(item)
            pushed += 1

        logger.info(
            f"<data_drive>:/Downloadscan 完成: {len(entries)} 文件, 分类 {len(predictions)}, "
            f"推送 {pushed} 条 inbox (move={len(groups['move'])} inspect={len(groups['inspect'])} unknown={len(groups['unknown'])})"
        )
        return {
            "success": True,
            "file_count": len(entries),
            "classified": len(predictions),
            "inbox_pushed": pushed,
            "groups": {k: len(v) for k, v in groups.items()},
        }


def _build_inbox_description(disp: str, items: list, watch_dir: Path) -> str:
    """生成收件箱条目的描述文本"""
    disp_label = {
        "move": "可直接移动（置信度高且已配置目标路径）",
        "inspect": "需人工审查（分类不明确或文件名模糊）",
        "unknown": "无法分类（置信度低）",
    }.get(disp, disp)
    lines = [f"处置：{disp_label}", f"扫描目录：{watch_dir}", "", "条目列表："]
    for it in items[:20]:  # 描述最多列 20 个
        fname = it.get("filename", "")
        cat = it.get("category", "")
        conf = it.get("confidence", 0)
        target = it.get("target_path", "")
        is_folder = it.get("is_folder", False)
        prefix = "📁 " if is_folder else ""
        suffix = f" → {target}" if target else ""
        reason = it.get("reason", "")
        reason_suffix = f"（{reason}）" if reason and conf < 0.85 else ""
        lines.append(f"  - {prefix}{fname} [{cat} {conf:.0%}]{suffix}{reason_suffix}")
    if len(items) > 20:
        lines.append(f"  ... 等共 {len(items)} 个")
    return "\n".join(lines)


# ==================== CleanupActivityAction ====================

class CleanupActivityAction(Action):
    """定期清理过期的 activity 数据

    清理规则（按文件修改时间判断过期）：
    - raw_data_dir/*.jsonl  → 超过 raw_retention_days 删除
    - hourly_data_dir/*.md  → 超过 hourly_retention_days 删除

    daily 报告不清理（已挪到 private_vault/activity/daily/，用户要求生成了就保留）。
    reviewed/ 子目录中的文件同样永久保留（用户审核过的日志）。

    同时清理 inbox 中已解决/忽略超过 auto_cleanup_days 天的条目（[inbox] 配置）。
    """

    action_type = "cleanup_activity"

    async def execute(self, context: dict) -> dict:
        cfg = context["config"]
        raw_dir = _get_dir(cfg, "raw_data_dir", "data/activity/raw")
        hourly_dir = _get_dir(cfg, "hourly_data_dir", "data/activity/hourly")
        raw_retention = int(cfg.get("raw_retention_days", 3))
        hourly_retention = int(cfg.get("hourly_retention_days", 7))

        now = time.time()
        deleted = {"raw": 0, "hourly": 0}

        for dir_path, retention, key, pattern in [
            (raw_dir, raw_retention, "raw", "*.jsonl"),
            (hourly_dir, hourly_retention, "hourly", "*.md"),
        ]:
            if not dir_path.exists():
                continue
            cutoff = now - retention * 86400
            for f in dir_path.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        deleted[key] += 1
                except Exception as e:
                    logger.warning(f"Cleanup 删除 {f} 失败: {e}")

        total = sum(deleted.values())

        # 清理 inbox 已解决/忽略过期条目
        inbox_deleted = 0
        try:
            from server.config import get_inbox_config
            from server.inbox import get_store
            inbox_cfg = get_inbox_config()
            inbox_deleted = get_store().cleanup_resolved(days=inbox_cfg["auto_cleanup_days"])
        except Exception as e:
            logger.warning(f"Cleanup inbox 清理失败: {e}")

        # browser_stats 聚合归档清理（明细超 retention_days 后聚合到 summary 再删除）
        bstats_result = {"aggregated": 0, "deleted": 0}
        try:
            from server.browser.stats import get_stats_store
            from server.config import get_cleanup_config
            cleanup_cfg = get_cleanup_config()
            bstats_result = get_stats_store().aggregate_and_cleanup(
                retention_days=cleanup_cfg["browser_stats_details_retention_days"]
            )
        except Exception as e:
            logger.warning(f"Cleanup browser_stats 清理失败: {e}")

        # storage_growth 告警：监控 memory facts / todos archived / memory.db 文件大小
        # 超阈值推 inbox 告警，同 source pending 去重
        storage_alerts = {"checked": [], "alerts_pushed": [], "alerts_skipped_dedup": []}
        try:
            from server.storage_health import check_storage_growth
            storage_alerts = check_storage_growth()
        except Exception as e:
            logger.warning(f"Cleanup storage_growth 告警检查失败: {e}")

        logger.info(
            f"CleanupActivity 完成: raw={deleted['raw']} hourly={deleted['hourly']} "
            f"inbox={inbox_deleted} "
            f"bstats_agg={bstats_result.get('aggregated', 0)} bstats_del={bstats_result.get('deleted', 0)} "
            f"storage_alerts_pushed={len(storage_alerts.get('alerts_pushed', []))} "
            f"storage_alerts_deduped={len(storage_alerts.get('alerts_skipped_dedup', []))}"
        )
        return {
            "success": True,
            "deleted": deleted,
            "total_deleted": total,
            "inbox_deleted": inbox_deleted,
            "browser_stats": bstats_result,
            "storage_growth": storage_alerts,
        }


# ==================== TodosTriggerCheckAction ====================

class TodosTriggerCheckAction(Action):
    """轮询 triggered 类型的 todos，检查触发条件是否满足。

    首版支持 file_arrived 事件：扫描 trigger_condition.watch_dir 下匹配 pattern
    的文件，用 mtime > (now - check_interval * 2) 判断是否是最近到达的新文件。
    满足条件时调 store.check_trigger，触发后通过 add_message 推送给 agent。
    """

    action_type = "todos_trigger_check"

    async def execute(self, context: dict) -> dict:
        import os

        from server.todos.router import get_todos_store
        from server.user_message import add_message

        config = context["config"]
        check_interval = int(config.get("check_interval", 300))
        # 扫描窗口 = 2 倍轮询间隔，避免漏检（偶尔的轮询延迟兜底）
        scan_window = check_interval * 2
        now_ts = time.time()
        cutoff = now_ts - scan_window

        try:
            store = get_todos_store()
            triggered_todos = store.list_todos(todo_type="triggered")
        except Exception as e:
            logger.exception("TodosTriggerCheck 获取 store 失败")
            return {"success": False, "error": f"store 不可用: {e}"}

        if not triggered_todos:
            return {"success": True, "triggered_count": 0, "checked": 0, "skipped": True, "skip_reason": "无到期任务"}

        checked = 0
        triggered_count = 0
        for todo in triggered_todos:
            checked += 1
            raw_cond = todo.get("trigger_condition")
            if not raw_cond:
                continue
            try:
                cond = json.loads(raw_cond) if isinstance(raw_cond, str) else raw_cond
            except (json.JSONDecodeError, TypeError):
                continue

            if cond.get("event") != "file_arrived":
                continue

            watch_dir = cond.get("watch_dir", "")
            pattern = cond.get("pattern", "*")
            if not watch_dir:
                continue

            try:
                watch_path = Path(watch_dir)
                if not watch_path.exists():
                    continue
            except Exception:
                continue

            # 扫描匹配 pattern 且 mtime > cutoff 的新文件
            import fnmatch
            try:
                new_files = []
                for entry in os.scandir(watch_path):
                    if entry.is_dir() or entry.name.startswith("."):
                        continue
                    if not fnmatch.fnmatch(entry.name, pattern):
                        continue
                    try:
                        if entry.stat().st_mtime > cutoff:
                            new_files.append(entry.name)
                    except OSError:
                        continue
            except Exception as e:
                logger.warning(f"TodosTriggerCheck 扫描 {watch_dir} 失败: {e}")
                continue

            if not new_files:
                continue

            # 对每个新文件调 check_trigger
            for fname in new_files[:5]:  # 单次最多触发 5 个文件，避免刷屏
                event_payload = {
                    "event": "file_arrived",
                    "path": str(watch_path / fname),
                    "filename": fname,
                }
                try:
                    result = store.check_trigger(todo["id"], event_payload)
                    if result.get("triggered"):
                        triggered_count += 1
                        title = todo.get("title", todo["id"])
                        add_message(
                            f"[触发任务] '{title}' 已满足触发条件: {result.get('reason', '')}。"
                            f"请处理后调 todos_mark_done 重置。"
                        )
                        logger.info(f"TodosTriggerCheck 触发 {todo['id']}: {result.get('reason')}")
                        break  # 一个文件触发即可，避免同一 todo 重复触发
                except Exception as e:
                    logger.warning(f"TodosTriggerCheck check_trigger 失败 {todo['id']}: {e}")

        return {
            "success": True,
            "checked": checked,
            "triggered_count": triggered_count,
        }


# ==================== ScreenSummaryTriggerAction ====================

class ScreenSummaryTriggerAction(Action):
    """检查当日 screen_vl 记录量，超阈值时推送"今日总结"提醒。

    阈值配置在 [loops.activity_tracker] 段：
      screen_summary_record_threshold = 20  # 当日 screen 记录数
      screen_summary_char_threshold = 5000  # 当日 answer 总字符数
    满足任一阈值且当日未触发过 → 推送 add_message
    防重复：用 data/activity/.screen_summary_triggered_YYYYMMDD 标记文件
    """

    action_type = "screen_summary_trigger"

    async def execute(self, context: dict) -> dict:
        from server.user_message import add_message

        config = context["config"]
        raw_dir = _get_dir(config, "raw_data_dir", "data/activity/raw")
        record_threshold = int(config.get("screen_summary_record_threshold", 20))
        char_threshold = int(config.get("screen_summary_char_threshold", 5000))

        date_str = _date_str()
        screen_path = raw_dir / f"screen_{date_str}.jsonl"
        if not screen_path.exists():
            return {"success": True, "skipped": True, "skip_reason": "当日无 screen 数据"}

        records = _read_jsonl(screen_path)
        record_count = len(records)
        total_chars = sum(len(r.get("answer", "") or "") for r in records)

        # 防重复：检查标记文件
        marker_path = raw_dir / f".screen_summary_triggered_{date_str}"
        if marker_path.exists():
            return {
                "success": True,
                "skipped": True,
                "skip_reason": "今日已触发过总结提醒",
                "record_count": record_count,
                "total_chars": total_chars,
            }

        if record_count < record_threshold and total_chars < char_threshold:
            return {
                "success": True,
                "skipped": True,
                "skip_reason": f"未达阈值 (records={record_count}/{record_threshold}, chars={total_chars}/{char_threshold})",
                "record_count": record_count,
                "total_chars": total_chars,
            }

        # 达阈值 → 推送提醒 + 写标记文件
        add_message(
            f"[今日总结提醒] 当日屏幕记录已达阈值 "
            f"(records={record_count}, chars={total_chars})。"
            f"建议调 agent_guide(task_type='system.task_closure') 生成今日活动总结。"
        )
        try:
            marker_path.write_text(
                f"triggered at {datetime.now().isoformat(timespec='seconds')}\n"
                f"records={record_count}, chars={total_chars}\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"ScreenSummaryTrigger 写标记文件失败: {e}")

        logger.info(
            f"ScreenSummaryTrigger 触发: records={record_count}, chars={total_chars}"
        )
        return {
            "success": True,
            "triggered": True,
            "record_count": record_count,
            "total_chars": total_chars,
        }


# ==================== 可选组件加载机制 ====================

def _load_optional_actions(manager) -> None:
    """加载 workspace 下所有可选组件的 loop action。

    遍历 workspace/*/loop_actions.py 模块，调用约定的 register(manager) 函数。
    组件不存在或无 loop_actions 模块时静默跳过。主代码库不直接 import 任何
    workspace 组件，删除 workspace/<component>/ 后自动跳过注册。
    """
    import importlib
    import pkgutil
    try:
        import workspace as _ws
    except ImportError:
        return
    for comp_info in pkgutil.iter_modules(_ws.__path__):
        if comp_info.name.startswith('_'):
            continue
        try:
            mod = importlib.import_module(f'workspace.{comp_info.name}.loop_actions')
        except ImportError:
            continue
        if hasattr(mod, 'register'):
            mod.register(manager)


# ==================== MemoryMaintainAction ====================

class MemoryMaintainAction(Action):
    """定期运行记忆维护器：老化 stale 记忆 + LLM 验证准确性。

    原维护器是"懒触发"（只在记忆 API 调用时检查 should_run），若用户长时间
    不调记忆 API，维护器就不会运行。加入 loop 后变成真正的定时触发。

    loop 间隔默认 6 小时（与 maintain_interval_hours 一致），启动后 5 分钟
    首次触发（first_run_delay=300）。直接调 maintainer.run()，不检查
    should_run()——loop 本身就是定时器，避免双重节流。

    配置在 config.toml [loops.memory] 段：
      maintain_interval = 21600          # 秒，默认 6 小时
      maintain_first_run_delay = 300      # 启动后 5 分钟首次触发
      max_retries = 2                      # maintainer.run() 抛异常时退避重试次数
      retry_backoff = 60                   # 退避重试基数（秒），实际等待 = backoff * 2^attempt
    维护器参数（老化天数/验证开关/模型层级）在 [memory.maintain] 段配置。
    """

    action_type = "memory_maintain"

    async def execute(self, context: dict) -> dict:
        from server.memory.manager import get_memory_manager

        mgr = get_memory_manager()
        if not mgr._initialized:
            mgr.initialize()

        # 检查 LLM 是否可用（验证步骤需要 LLM）
        maintainer = mgr.maintainer
        if not maintainer:
            return {"success": False, "error": "记忆维护器未初始化"}

        # 直接调 run()，不检查 should_run()（loop 本身就是定时器）
        # 退避重试：仅对 maintainer.run() 抛异常时生效（如 LLM Pool 未初始化）
        # maintainer 内部已处理 429（broke_on_429 提前终止），正常返回不重试
        cfg = context.get("config", {})
        max_retries = int(cfg.get("max_retries", 2))
        retry_backoff = int(cfg.get("retry_backoff", 60))
        retry_errors: list[str] = []
        result: dict | None = None
        for attempt in range(max_retries + 1):
            try:
                result = await maintainer.run()
                break  # 正常返回（含 broke_on_429）不重试
            except Exception as e:
                logger.exception("MemoryMaintain 异常 (attempt %d/%d)", attempt + 1, max_retries + 1)
                err_str = f"{type(e).__name__}: {e}"
                retry_errors.append(err_str)
                err_lower = str(e).lower()
                # quota/LLM 初始化类失败才重试；DB 错误/代码 bug 不重试
                is_quota = any(p in err_lower for p in [
                    "pool 未初始化", "pool未初始化", "no available keys",
                    "无可用", "未初始化", "429", "rate_limit",
                ])
                if not is_quota:
                    break
                if attempt < max_retries:
                    delay = retry_backoff * (2 ** attempt)
                    logger.info(
                        "MemoryMaintain quota 类失败 (attempt %d/%d)，%ds 后重试: %s",
                        attempt + 1, max_retries + 1, delay, err_str,
                    )
                    await asyncio.sleep(delay)

        if result is None:
            return {
                "success": False,
                "error": retry_errors[-1] if retry_errors else "unknown",
                "retry_errors": retry_errors,
            }

        aging = result.get("aging", {})
        validate = result.get("validate", {})

        # 仅当存在 stale 记忆时推 inbox 告警（archived=1 是 LLM 常态建议，不算待审查事件）
        # 去重：若 inbox 已有 pending 的 memory 维护告警且 stale_count 未变化，跳过避免刷屏
        stale_count = aging.get("stale_count", 0)
        archived = validate.get("archive", 0)
        if stale_count > 0:
            try:
                from server.inbox import get_store
                store = get_store()
                existing = store.list(status="pending", source="memory", limit=10)
                already_pushed = any(
                    item.get("payload", {}).get("aging", {}).get("stale_count") == stale_count
                    for item in existing
                )
                if not already_pushed:
                    store.create({
                        "source": "memory",
                        "category": "maintain_result",
                        "title": f"记忆维护告警: {stale_count} 条 stale, {archived} 条建议归档",
                        "description": (
                            f"老化扫描: {stale_count} 条记忆超过 "
                            f"{aging.get('stale_threshold_days', 7)} 天未更新。\n"
                            f"LLM 验证: {validate.get('validated', 0)} 条，"
                            f"其中 keep={validate.get('keep', 0)}, "
                            f"update={validate.get('update', 0)}, "
                            f"archive={archived}。\n"
                            f"建议归档的记忆需要用户确认后删除。"
                        ),
                        "payload": {
                            "aging": aging,
                            "validate": validate,
                            "last_run": result.get("last_run"),
                        },
                    })
            except Exception as e:
                logger.warning(f"MemoryMaintain 推 inbox 失败: {e}")

        logger.info(
            f"MemoryMaintain 完成: stale={stale_count}, "
            f"validated={validate.get('validated', 0)}, "
            f"archive={archived}"
            + (f", broke_on_429=True (skipped_429={validate.get('skipped_429', 0)})"
               if validate.get("broke_on_429") else "")
        )
        return {
            "success": True,
            "stale_count": stale_count,
            "validated": validate.get("validated", 0),
            "keep": validate.get("keep", 0),
            "update": validate.get("update", 0),
            "archive": archived,
            # 429 熔断监控字段（P0-3）：broke_on_429=True 表示本次因连续 3 次 429 提前终止
            "skipped_429": validate.get("skipped_429", 0),
            "broke_on_429": validate.get("broke_on_429", False),
            "last_run": result.get("last_run"),
        }


# ==================== SmartLimitCheckAction ====================

class SmartLimitCheckAction(Action):
    """VL 智能配额上限调整建议器（冗余模块，只读模式）

    每日运行一次：读取 data/activity/vl_429_log.jsonl 分析最近 N 天的非并发 429 模式。
    若多天意外触发 daily_exhausted → 推送 inbox 建议降低 vl_daily_quota 配置。

    设计约束：
    - 只读 + inbox 推送，不写 config.toml，不修改 vl_quota 的 effective_limit
    - 失败不影响主流程（捕获所有异常）

    配置在 config.toml [loops.smart_limit] 段：
      analyze_days = 7          # 回看天数
      current_quota = 100        # 当前配额（用于计算建议值）
    """

    action_type = "smart_limit_check"

    async def execute(self, context: dict) -> dict:
        from server.llm_pool.smart_limit import run_check

        config = context["config"]
        days = int(config.get("analyze_days", 7))
        current_quota = int(config.get("current_quota", 100))

        try:
            result = await asyncio.to_thread(run_check, "data/activity/vl_429_log.jsonl", days, current_quota)
            analysis = result.get("analysis", {})
            logger.info(
                f"SmartLimitCheck 完成: {analysis.get('log_records', 0)} 条日志，"
                f"{len(analysis.get('daily_exhausted_days', []))} 天触发 daily_exhausted，"
                f"inbox_pushed={result.get('inbox_pushed')}"
            )
            return {
                "success": True,
                "log_records": analysis.get("log_records", 0),
                "daily_exhausted_days": analysis.get("daily_exhausted_days", []),
                "inbox_pushed": result.get("inbox_pushed", False),
                "suggestion": analysis.get("suggestion"),
            }
        except Exception as e:
            logger.exception("SmartLimitCheck 执行失败")
            return {"success": False, "error": str(e)}


class DailySummarizeAction(Action):
    """每天 05:30 自动生成前一天的日报（覆盖前一天 05:00 ~ 当天 05:00）

    cron "30 5 * * *" 触发，catchup=True：后端未启动时错过的触发会在启动时补跑。
    读取该日所有 hourly md（已脱敏），拼接后调 LLM 生成结构化日总结，
    写入 private_vault/activity/daily/YYYYMMDD.md。

    日期划分按 05:00：
    - 07-25 05:30 触发 → 生成 07-24 的日报（覆盖 07-24 05:00 ~ 07-25 05:00）
    - hourly 文件按自然日命名，需跨两个自然日读取：
      前一天 HH=05..23 + 当天 HH=00..04

    内容审核保护：daily 输入是已脱敏的 hourly md，但保险起见加 detect_filtered_response
    后置检查。触发时写占位文件（hourly 文件列表 + 说明），不算硬失败。
    """

    action_type = "daily_summarize"

    async def execute(self, context: dict) -> dict:
        import asyncio
        from datetime import datetime, timedelta

        daily_dir = _get_dir(context["config"], "daily_data_dir", "private_vault/activity/daily")
        hourly_dir = _get_dir(context["config"], "hourly_data_dir", "data/activity/hourly")
        daily_dir.mkdir(parents=True, exist_ok=True)

        # 计算目标日报日期
        # cron 5:30 触发：日报日期 = 触发时刻自然日 - 1（前一天的日报）
        # catchup 模式：catchup_target_ts 指向某天 5:30 → 该日报覆盖前一天
        if "catchup_target_ts" in context:
            target = datetime.fromtimestamp(context["catchup_target_ts"])
        else:
            target = datetime.now()
        daily_date = target - timedelta(days=1)
        date_str = daily_date.strftime("%Y%m%d")
        date_label = daily_date.strftime("%Y-%m-%d")

        # 已存在不覆盖（避免覆盖用户已编辑的日报）
        out_path = daily_dir / f"{date_str}.md"
        placeholder_path = daily_dir / f"{date_str}.placeholder.md"
        if out_path.exists():
            logger.info(f"DailySummarize {date_label}: 已存在，跳过")
            return {"success": True, "skipped": True, "skip_reason": "already_exists"}
        # P0-1: 占位文件允许重生成（删掉重跑，避免失败后永不重试）
        if placeholder_path.exists():
            logger.info(f"DailySummarize {date_label}: 检测到占位文件，重新生成")
            placeholder_path.unlink()

        # 读取该日 05:00 ~ 次日 05:00 的所有 hourly 文件
        # 按自然日命名：前一天 HH=05..23 + 当天 HH=00..04
        prev_str = date_str
        next_str = (daily_date + timedelta(days=1)).strftime("%Y%m%d")
        all_files = sorted(
            list(hourly_dir.glob(f"{prev_str}_*.md")) +
            list(hourly_dir.glob(f"{next_str}_*.md"))
        )

        def _hour_of(name: str) -> int:
            """从 YYYYMMDD_HH.md 或 YYYYMMDD_HH_vN.md 提取小时"""
            return int(name.split("_")[1].split("_v")[0].split(".")[0])

        today_files = [
            f for f in all_files
            if (f.name.startswith(prev_str) and _hour_of(f.name) >= 5)
            or (f.name.startswith(next_str) and _hour_of(f.name) < 5)
        ]

        # P2-4: 按小时去重，保留最新版本（_vN > 无后缀）
        import re as _re
        def _version(name: str) -> int:
            m = _re.search(r"_v(\d+)\.md$", name)
            return int(m.group(1)) if m else 0
        by_hour: dict[int, Path] = {}
        for f in today_files:
            h = _hour_of(f.name)
            if h not in by_hour or _version(f.name) > _version(by_hour[h].name):
                by_hour[h] = f
        today_files = sorted(by_hour.values(), key=lambda f: f.name)

        if not today_files:
            logger.info(f"DailySummarize {date_label}: 无 hourly 数据")
            placeholder_path.write_text(
                f"# {date_label} 工作日志\n\n> 本日无 hourly 采集数据（后端可能未运行）\n",
                encoding="utf-8",
            )
            return {"success": True, "skipped": True, "skip_reason": "no_hourly_data"}

        # 拼接 hourly 内容
        hourly_blocks = []
        for f in today_files:
            try:
                content = f.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"读取 hourly 文件失败 {f.name}: {e}")
                continue
            # P1-3: 跳过 hourly 占位文件（无采集数据），避免占位文本干扰日总结
            if "本时段无采集数据" in content:
                continue
            hourly_blocks.append(f"### {f.stem}\n\n{content}")
        combined = "\n\n---\n\n".join(hourly_blocks)

        # 构造 prompt
        prompt = (
            f"以下是用户在 {date_label} 这一天（05:00 ~ 次日 05:00）的每小时活动总结。\n"
            f"请汇总为一份结构化日总结，按时间线列出关键活动，标注主要成果。\n"
            f"用 Markdown 格式，包含：## 概述、## 时间线、## 主要成果、## 备注。\n"
            f"备注段标注缺失时段或其他说明。\n\n"
            f"{combined}\n"
        )
        system_prompt = (
            "你是用户活动总结助手。根据每小时的总结，生成结构化日总结。"
            "保持简洁，重点突出关键活动和成果，避免重复每小时的细节。"
        )

        # 调 LLM（退避重试，同 HourlySummarizeAction 模式）
        from server.activity_tracker.content_filter import detect_filtered_response
        from server.llm_pool import call_llm

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        max_retries = int(context["config"].get("daily_summarize_max_retries", 3))
        retry_backoff = int(context["config"].get("daily_summarize_retry_backoff", 60))
        retry_errors: list[str] = []
        llm_result: dict | None = None
        content_filter_triggered = False

        for attempt in range(max_retries + 1):
            try:
                llm_result = await asyncio.to_thread(
                    call_llm,
                    messages,
                    temperature=0.3,
                    max_tokens=16384,
                    project="loop_daily_summary",
                    use_case="hourly_summarize",  # 复用同一 use_case tier 配额
                    retries=1,
                )
            except Exception as e:
                logger.exception("DailySummarize LLM 调用异常 (attempt %d/%d)", attempt + 1, max_retries + 1)
                llm_result = {"ok": False, "error": f"LLM 调用异常: {e}"}

            # 剥离思考链标签（某些 LLM 默认输出 <think>...</think>）
            if llm_result.get("ok") and llm_result.get("content"):
                from server.activity_tracker.content_filter import _strip_think_tags
                _stripped = _strip_think_tags(llm_result["content"])
                if _stripped:
                    llm_result["content"] = _stripped
                elif "<think>" in (llm_result.get("content") or ""):
                    llm_result["ok"] = False
                    llm_result["error"] = "empty content after stripping think tags"

            if llm_result.get("ok") and not detect_filtered_response(llm_result):
                break

            if llm_result.get("ok") and detect_filtered_response(llm_result):
                content_filter_triggered = True
                retry_errors.append(f"content_filter: {(llm_result.get('content') or '')[:80]}")
                logger.warning(f"DailySummarize {date_label} LLM 返回触发内容审核")
                break

            err = llm_result.get("error", "unknown")
            err_lower = err.lower()
            if any(kw in err for kw in ("1301", "contentFilter", "违禁", "敏感内容", "不安全")):
                content_filter_triggered = True
                retry_errors.append(err)
                break

            is_quota = (
                "no available keys" in err_lower
                or "429" in err_lower
                or "rate_limited" in err_lower
                or "rate limit" in err_lower
            )
            retry_errors.append(err)
            if not is_quota:
                break
            if attempt < max_retries:
                await asyncio.sleep(retry_backoff * (2 ** attempt))

        if llm_result is None:
            llm_result = {"ok": False, "error": "unknown: no llm_result"}

        if llm_result.get("ok") and not content_filter_triggered:
            summary = llm_result.get("content", "")
        else:
            err = llm_result.get("error", "unknown")
            err_history = " | ".join(retry_errors[-3:])
            if content_filter_triggered:
                placeholder = (
                    f"# {date_label} 工作日志\n\n"
                    f"> ⚠️ 本日因内容审核保护触发，LLM 拒绝生成详细总结。\n"
                    f"> 已对敏感 hourly 文件做脱敏处理。\n"
                    f"> 原始 hourly 数据 {len(today_files)} 条已保留，可手动重试。\n\n"
                    f"## hourly 文件列表\n" +
                    "\n".join(f"- {f.name}" for f in today_files) +
                    "\n"
                )
                placeholder_path.write_text(placeholder, encoding="utf-8")
                logger.warning(f"DailySummarize {date_label} 内容审核触发，写占位文件 {placeholder_path.name}")
                return {
                    "success": True, "skipped": True,
                    "skip_reason": "content_filter_triggered",
                    "placeholder_file": placeholder_path.name,
                }
            else:
                placeholder = (
                    f"# {date_label} 工作日志\n\n"
                    f"> ⚠️ LLM 调用失败（重试 {len(retry_errors)} 次仍失败）。\n"
                    f"> 最终失败原因：{err}\n"
                    f"> 错误历史：{err_history}\n\n"
                    f"## hourly 文件列表\n" +
                    "\n".join(f"- {f.name}" for f in today_files) +
                    "\n"
                )
                placeholder_path.write_text(placeholder, encoding="utf-8")
                logger.warning(f"DailySummarize {date_label} LLM 失败，写占位文件 {placeholder_path.name}")
                return {
                    "success": False, "error": err,
                    "placeholder_file": placeholder_path.name,
                    "retry_count": len(retry_errors),
                }

        # 写 daily md，并清理可能残留的占位文件（P0-1）
        out_path.write_text(summary, encoding="utf-8")
        if placeholder_path.exists():
            placeholder_path.unlink()
        logger.info(
            f"DailySummarize {date_label} 生成成功，{len(today_files)} 条 hourly"
        )
        return {
            "success": True,
            "summary_len": len(summary),
            "hourly_count": len(today_files),
            "output_file": out_path.name,
        }


# ==================== HeadlessSessionAction（headless-agent-session Ticket 04） ====================

class HeadlessSessionAction(Action):
    """headless-agent-session：后端自主启动 agent 会话 + judge agent 判定成败。

    spec: temp/sdd/headless-agent-session/spec.md
    ticket: Ticket 04

    流程：
    1. 从 context["config"] 读 trigger_text / skill / one_shot / 资源限制 / judge 配置
    2. trigger_text 留空 + skill 指定时调 agent_guide(task_type=skill) 取 first_action
    3. 调 headless_runner.run_main_agent(trigger_text, config) → MainAgentResult
    4. main agent 失败（error 非空）→ 不调 judge，整体 failed
    5. 调 headless_runner.run_judge_agent(main_output, trigger_text, config) → JudgeResult
    6. verdict != "success"（含 uncertain）→ 整体 failed
    7. 写 memory_set + wip_create 留档
    8. one_shot=True 时调 loop_manager.pause_task(context["task_id"])

    返回值：
    - 成功：{"success": True, "main_session_id": ..., "judge_session_id": ..., "judge_verdict": ..., "judge_reason": ...}
    - 失败：{"success": False, "error": ..., "main_session_id": ..., "judge_session_id": ...}
    - 跳过：{"success": True, "skipped": True, "skip_reason": "trigger_text 和 skill 都为空"}
    """

    action_type = "headless_session"

    async def execute(self, context: dict) -> dict:
        from server.activity_tracker.headless_runner import (
            parse_headless_config,
            run_judge_agent,
            run_main_agent,
        )

        cfg_dict = context.get("config", {}) or {}
        task_id = context.get("task_id", "")
        started_at = time.time()

        # 1. 解析配置
        try:
            config = parse_headless_config(cfg_dict)
        except Exception as e:
            return {"success": False, "error": f"config 解析失败: {e}"}

        # 2. trigger_text 留空 + skill 指定时调 agent_guide 取 first_action
        trigger_text = config.trigger_text
        if not trigger_text and config.skill:
            try:
                from server.agent_guide import get_agent_guide
                resp = get_agent_guide(task_type=config.skill)
                trigger_text = resp.get("first_action", "") or ""
                if not trigger_text:
                    return {
                        "success": True, "skipped": True,
                        "skip_reason": f"agent_guide(task_type={config.skill}) 未返回 first_action",
                    }
                logger.info(
                    "HeadlessSession %s: 从 skill=%s 解析 trigger_text=%r",
                    task_id, config.skill, trigger_text[:80],
                )
            except Exception as e:
                return {"success": False, "error": f"agent_guide(skill={config.skill}) 失败: {e}"}

        # 3. trigger_text 和 skill 都为空 → skipped
        if not trigger_text:
            return {
                "success": True, "skipped": True,
                "skip_reason": "trigger_text 和 skill 都为空",
            }

        # 4. 调主 agent
        try:
            main_result = await run_main_agent(
                trigger_text=trigger_text,
                config=config,
            )
        except Exception as e:
            logger.exception("HeadlessSession %s: run_main_agent 异常", task_id)
            await self._after_run(
                context, cfg_dict, task_id, trigger_text, started_at,
                main_session_id="", judge_session_id="",
                judge_verdict="uncertain", judge_reason=f"run_main_agent 异常: {e}",
                main_error=str(e), overall_success=False,
            )
            return {
                "success": False, "error": f"run_main_agent 异常: {e}",
                "main_session_id": "", "judge_session_id": "",
                "judge_verdict": "uncertain",
            }

        main_session_id = main_result.session_id

        # 5. main agent 失败/中断 → 不调 judge，整体 failed
        #    中断场景（Ticket 07）：interrupt_event.set() → SessionRunner outcome.status="interrupted"
        #    失败场景：error 非空 / outcome is None / outcome.status != "completed"
        main_outcome_status = getattr(main_result.outcome, "status", "") or ""
        if main_result.error or main_result.outcome is None or main_outcome_status != "completed":
            err = main_result.error or f"main agent status={main_outcome_status or 'None'}"
            logger.warning(
                "HeadlessSession %s: 主 agent 失败/中断 (%s)，跳过 judge", task_id, err,
            )
            await self._after_run(
                context, cfg_dict, task_id, trigger_text, started_at,
                main_session_id=main_session_id, judge_session_id="",
                judge_verdict="failed", judge_reason=f"main agent 失败: {err}",
                main_error=err, overall_success=False,
            )
            return {
                "success": False, "error": err,
                "main_session_id": main_session_id, "judge_session_id": "",
                "judge_verdict": "failed", "judge_reason": f"main agent 失败: {err}",
            }

        # 6. 调 judge agent
        try:
            judge_result = await run_judge_agent(
                main_output=main_result.last_assistant_messages,
                trigger_text=trigger_text,
                config=config,
            )
        except Exception as e:
            logger.exception("HeadlessSession %s: run_judge_agent 异常", task_id)
            await self._after_run(
                context, cfg_dict, task_id, trigger_text, started_at,
                main_session_id=main_session_id, judge_session_id="",
                judge_verdict="uncertain", judge_reason=f"run_judge_agent 异常: {e}",
                main_error="", overall_success=False,
            )
            return {
                "success": False, "error": f"run_judge_agent 异常: {e}",
                "main_session_id": main_session_id, "judge_session_id": "",
                "judge_verdict": "uncertain",
            }

        # 7. verdict != "success" → 整体 failed（uncertain 也算 failed）
        overall_success = (judge_result.verdict == "success")

        # 8. 写 memory + wip + one_shot pause
        await self._after_run(
            context, cfg_dict, task_id, trigger_text, started_at,
            main_session_id=main_session_id,
            judge_session_id=judge_result.session_id,
            judge_verdict=judge_result.verdict,
            judge_reason=judge_result.reason,
            main_error=main_result.error,
            overall_success=overall_success,
        )

        result = {
            "success": overall_success,
            "main_session_id": main_session_id,
            "judge_session_id": judge_result.session_id,
            "judge_verdict": judge_result.verdict,
            "judge_reason": judge_result.reason,
        }
        if not overall_success:
            result["error"] = f"judge verdict={judge_result.verdict}: {judge_result.reason}"
        return result

    async def _after_run(
        self,
        context: dict,
        cfg_dict: dict,
        task_id: str,
        trigger_text: str,
        started_at: float,
        *,
        main_session_id: str,
        judge_session_id: str,
        judge_verdict: str,
        judge_reason: str,
        main_error: str,
        overall_success: bool,
    ) -> None:
        """执行后置动作：写 memory + 写 wip + one_shot 时 pause task。

        所有后置动作都用 try/except 兜底，单个失败不影响其他。整体不抛异常。
        """
        completed_at = time.time()
        duration_secs = round(completed_at - started_at, 2)

        # 写 memory
        try:
            await self._write_memory(
                main_session_id=main_session_id,
                judge_session_id=judge_session_id,
                trigger_text=trigger_text,
                judge_verdict=judge_verdict,
                judge_reason=judge_reason,
                main_error=main_error,
                started_at=started_at,
                completed_at=completed_at,
                duration_secs=duration_secs,
                overall_success=overall_success,
            )
        except Exception as e:
            logger.warning("HeadlessSession %s: 写 memory 失败: %s", task_id, e)

        # 写 wip
        try:
            await self._write_wip(
                task_id=task_id,
                trigger_text=trigger_text,
                main_session_id=main_session_id,
                judge_session_id=judge_session_id,
                judge_verdict=judge_verdict,
                judge_reason=judge_reason,
                overall_success=overall_success,
            )
        except Exception as e:
            logger.warning("HeadlessSession %s: 写 wip 失败: %s", task_id, e)

        # one_shot pause
        if cfg_dict.get("one_shot", False):
            try:
                from server.activity_tracker.loop_manager import get_manager
                mgr = get_manager()
                if mgr is not None:
                    mgr.pause_task(task_id)
                    logger.info("HeadlessSession %s: one_shot 执行后已 paused", task_id)
            except Exception as e:
                logger.warning("HeadlessSession %s: one_shot pause 失败: %s", task_id, e)

    async def _write_memory(
        self,
        *,
        main_session_id: str,
        judge_session_id: str,
        trigger_text: str,
        judge_verdict: str,
        judge_reason: str,
        main_error: str,
        started_at: float,
        completed_at: float,
        duration_secs: float,
        overall_success: bool,
    ) -> None:
        """写 memory_set 留档（含 consumption_contexts + trigger_keywords）。"""
        from datetime import datetime

        from server.memory.manager import get_memory_manager

        mgr = get_memory_manager()
        if mgr is None:
            logger.warning("HeadlessSession: memory manager 不可用，跳过 memory_set")
            return

        key = f"headless_session_{main_session_id}" if main_session_id else (
            f"headless_session_failed_{int(completed_at)}"
        )
        data = {
            "main_session_id": main_session_id,
            "judge_session_id": judge_session_id,
            "trigger_text": trigger_text[:500],  # 限制长度避免 memory 膨胀
            "judge_verdict": judge_verdict,
            "judge_reason": judge_reason,
            "main_error": main_error,
            "overall_success": overall_success,
            "started_at": datetime.fromtimestamp(started_at).isoformat(timespec="seconds"),
            "completed_at": datetime.fromtimestamp(completed_at).isoformat(timespec="seconds"),
            "duration_secs": duration_secs,
        }
        structured = {
            "fact_type": "experience",
            "occurred_at": datetime.fromtimestamp(completed_at).isoformat(timespec="seconds"),
            "consumption_contexts": ["adhoc.headless_session", "system.task_closure"],
            "trigger_keywords": ["headless", "judge", trigger_text[:30]] if trigger_text else ["headless", "judge"],
        }
        mgr.set(key, data, structured=structured)
        logger.info("HeadlessSession: memory_set key=%s verdict=%s", key, judge_verdict)

    async def _write_wip(
        self,
        *,
        task_id: str,
        trigger_text: str,
        main_session_id: str,
        judge_session_id: str,
        judge_verdict: str,
        judge_reason: str,
        overall_success: bool,
    ) -> None:
        """写 wip_create 留档（status 用 completed/failed 区分）。"""
        from server.todos.router import get_todos_store

        store = get_todos_store()
        if store is None:
            logger.warning("HeadlessSession: todos store 不可用，跳过 wip_create")
            return

        title = trigger_text[:50] if trigger_text else f"headless session {main_session_id}"
        wip = store.create_wip({
            "title": title,
            "goal": trigger_text[:500] if trigger_text else "",
            "tags": ["headless", "judge", judge_verdict],
            "next_steps": [],  # 已完成，无后续步骤
            "related_memory_keys": [
                f"headless_session_{main_session_id}" if main_session_id else "",
            ],
            "extra_data": {
                "main_session_id": main_session_id,
                "judge_session_id": judge_session_id,
                "judge_verdict": judge_verdict,
                "judge_reason": judge_reason,
                "loop_task_id": task_id,
            },
        })

        # create_wip 总是 status='active'，需要 update_wip 改成 completed/failed
        if wip and wip.get("id"):
            new_status = "completed" if overall_success else "failed"
            store.update_wip(wip["id"], {
                "status": new_status,
                "progress": 100 if overall_success else 0,
                "current_state": {
                    "judge_verdict": judge_verdict,
                    "judge_reason": judge_reason,
                    "main_session_id": main_session_id,
                },
            })
            logger.info(
                "HeadlessSession: wip_create id=%s status=%s verdict=%s",
                wip["id"], new_status, judge_verdict,
            )


# ==================== 注册函数 ====================

class KeyHealthCheckAction(Action):
    """定时检查 key 健康度，距上次检查超过 HEALTH_CHECK_INTERVAL_DAYS 天则自动执行 check_health()

    解决健康检查纯按需触发（/health 端点被调时才检查）的问题：
    后端长期运行但无人访问 /health 时，死 key 不会被自动发现。
    每 12 小时检查 should_run_health_check()，true 则跑 check_health()。
    """
    action_type = "key_health_check"

    async def execute(self, context: dict) -> dict:
        from server.llm_pool import check_keys_health, should_run_health_check
        if not should_run_health_check():
            logger.debug("KeyHealthCheck: 未到检查间隔，跳过")
            return {"success": True, "skipped": True, "skip_reason": "未到检查间隔"}
        logger.info("KeyHealthCheck: 距上次检查已超间隔，执行 check_health()")
        result = check_keys_health()
        logger.info(
            "KeyHealthCheck 完成: total=%s ok=%s fail=%s removed=%s",
            result.get("total"), result.get("ok"),
            result.get("fail"), result.get("removed"),
        )
        return {"success": True, "result": result}


class SecretBackupAction(Action):
    """定时备份密钥文件（keys.json / secrets.toml / config.toml）到双位置.

    解决 gitignored 敏感文件被 agent 误删后无法恢复的问题：
    每 5 天（可配）把三个 gitignored 敏感文件明文副本存到项目内 backups/secrets/
    + 项目外目录（用户配置）双位置，防单点故障。

    调用 tools/backup_secrets.py 的 run_backup() 函数（直接 import，不走 subprocess），
    失败时返回 success=False 并推 inbox 告警（由 _push_first_failure_inbox 自动处理）。

    配置在 config.toml [loops.secret_backup] 段：
      interval_seconds = 432000    # 5 天（5 * 86400）
      first_run_delay = 300        # 启动后 5 分钟首次触发
      fail_threshold = 3          # 备份失败较严重，阈值低于默认 5
    路径配置在 [secret_backup] 段（internal_dir / external_dir / keep_last_n / keep_within_days）。
    """
    action_type = "secret_backup"

    async def execute(self, context: dict) -> dict:
        # tools/backup_secrets.py 在项目根 tools/ 下，用 importlib 加载避免 sys.path 污染
        import importlib.util
        from pathlib import Path

        tools_dir = Path(__file__).resolve().parent.parent.parent / "tools"
        script_path = tools_dir / "backup_secrets.py"
        if not script_path.exists():
            return {"success": False, "error": f"backup_secrets.py 不存在: {script_path}"}

        try:
            spec = importlib.util.spec_from_file_location(
                "backup_secrets", script_path,
            )
            if spec is None or spec.loader is None:
                raise ValueError("spec/loader 不可用")
            module = importlib.util.module_from_spec(spec)
            # backup_secrets.py 顶部会 sys.path.insert 项目根，加载后可正常 import lib.*
            spec.loader.exec_module(module)
        except Exception as e:
            logger.exception("SecretBackup: 加载 backup_secrets.py 失败")
            return {"success": False, "error": f"加载 backup_secrets.py 失败: {e}"}

        # 调用 run_backup（同步函数，用 asyncio.to_thread 包装避免阻塞事件循环）
        # location 固定 both（双位置备份）；如某位置未配置会自动跳过
        try:
            result = await asyncio.to_thread(module.run_backup, "both", False)
        except Exception as e:
            logger.exception("SecretBackup: run_backup 异常")
            return {"success": False, "error": f"run_backup 异常: {e}"}

        success = bool(result.get("success"))
        files_count = len(result.get("files", []))
        backups_written = sum(
            1 for f in result.get("files", [])
            for b in f.get("backups", [])
            if b.get("written")
        )
        retention_deleted = len(result.get("retention_deleted", []))
        locations = [loc["name"] for loc in result.get("locations", [])]
        errors = result.get("errors", [])

        if success:
            logger.info(
                "SecretBackup 完成: %d 个文件, %d 个备份已写入 (locations=%s, retention_deleted=%d)",
                files_count, backups_written, locations, retention_deleted,
            )
            return {
                "success": True,
                "files_count": files_count,
                "backups_written": backups_written,
                "locations": locations,
                "retention_deleted": retention_deleted,
                "timestamp": result.get("timestamp"),
            }
        else:
            err = result.get("error") or "; ".join(errors) or "unknown backup failure"
            logger.warning(
                "SecretBackup 失败: %s (files=%d, backups_written=%d, errors=%d)",
                err, files_count, backups_written, len(errors),
            )
            return {
                "success": False,
                "error": err,
                "files_count": files_count,
                "backups_written": backups_written,
                "locations": locations,
                "retention_deleted": retention_deleted,
                "errors": errors,
                "timestamp": result.get("timestamp"),
            }


class WorkspaceBackupAction(Action):
    """定时备份 workspace 各组件关键个人数据到双位置（ADR-0029）.

    备份目标由各组件 manifest.toml [backup] 段声明（lib.component_manifest 解析），
    与 secret_backup 对称设计但独立目录和保留策略：
    - 备份位置：项目内 backups/workspace/<component>/ + 项目外目录（用户配置）
    - 保留策略：默认 keep_last_n=7 / keep_within_days=14（比密钥更激进，因数据更新快）
    - 文件 → 原子拷贝；目录（以 "/" 结尾）→ 打 zip

    调用 tools/backup_workspace.py 的 run_backup()（importlib 加载，不走 subprocess），
    失败时返回 success=False 并推 inbox 告警（由 _push_first_failure_inbox 自动处理）。

    配置在 config.toml：
      [loops.workspace_backup]  interval_seconds = 432000  # 5 天
                                 first_run_delay = 600     # 比 secret_backup 晚 5 分钟避免并发
                                 fail_threshold = 3
      [workspace_backup]        internal_dir / external_dir / keep_last_n / keep_within_days

    备份目标声明在 workspace/<component>/manifest.toml [backup] 段 paths = [...].
    """
    action_type = "workspace_backup"

    async def execute(self, context: dict) -> dict:
        import importlib.util
        from pathlib import Path

        tools_dir = Path(__file__).resolve().parent.parent.parent / "tools"
        script_path = tools_dir / "backup_workspace.py"
        if not script_path.exists():
            return {"success": False, "error": f"backup_workspace.py 不存在: {script_path}"}

        try:
            spec = importlib.util.spec_from_file_location(
                "backup_workspace", script_path,
            )
            if spec is None or spec.loader is None:
                raise ValueError("spec/loader 不可用")
            module = importlib.util.module_from_spec(spec)
            # backup_workspace.py 顶部会 sys.path.insert 项目根，加载后可正常 import lib.*
            spec.loader.exec_module(module)
        except Exception as e:
            logger.exception("WorkspaceBackup: 加载 backup_workspace.py 失败")
            return {"success": False, "error": f"加载 backup_workspace.py 失败: {e}"}

        # 调用 run_backup（同步函数，用 asyncio.to_thread 包装避免阻塞事件循环）
        # location 固定 both（双位置备份）；如某位置未配置会自动跳过
        try:
            result = await asyncio.to_thread(module.run_backup, "both", False)
        except Exception as e:
            logger.exception("WorkspaceBackup: run_backup 异常")
            return {"success": False, "error": f"run_backup 异常: {e}"}

        success = bool(result.get("success"))
        components_count = len(result.get("components", []))
        # 总 paths 处理数 = 所有 components 的 files 之和
        files_count = sum(len(c.get("files", [])) for c in result.get("components", []))
        backups_written = sum(
            1 for c in result.get("components", [])
            for f in c.get("files", [])
            for b in f.get("backups", [])
            if b.get("written")
        )
        retention_deleted = len(result.get("retention_deleted", []))
        locations = [loc["name"] for loc in result.get("locations", [])]
        errors = result.get("errors", [])

        if success:
            logger.info(
                "WorkspaceBackup 完成: %d 个组件, %d 个 paths, %d 个备份已写入 "
                "(locations=%s, retention_deleted=%d)",
                components_count, files_count, backups_written,
                locations, retention_deleted,
            )
            return {
                "success": True,
                "components_count": components_count,
                "files_count": files_count,
                "backups_written": backups_written,
                "locations": locations,
                "retention_deleted": retention_deleted,
                "timestamp": result.get("timestamp"),
            }
        else:
            err = result.get("error") or "; ".join(errors) or "unknown backup failure"
            logger.warning(
                "WorkspaceBackup 失败: %s (components=%d, backups_written=%d, errors=%d)",
                err, components_count, backups_written, len(errors),
            )
            return {
                "success": False,
                "error": err,
                "components_count": components_count,
                "files_count": files_count,
                "backups_written": backups_written,
                "locations": locations,
                "retention_deleted": retention_deleted,
                "errors": errors,
                "timestamp": result.get("timestamp"),
            }


def register_loop_actions(manager) -> None:
    """注册场景A + 场景B 的 Action 到 LoopManager（main.py startup 调用）"""
    # 配置 VL 配额管理器（从 activity_tracker 段读取参数）
    try:
        from server.activity_tracker.vl_quota import configure_from_loops_config
        activity_cfg = manager.config.get("tasks", {}).get("activity_tracker", {})
        if activity_cfg:
            configure_from_loops_config(activity_cfg)
            logger.info("VL 配额管理器已从 [loops.activity_tracker] 配置加载")
    except Exception as e:
        logger.warning(f"VL 配额管理器配置失败（将用默认值）: {e}")

    manager.register_action("collect_windows", CollectWindowsAction())
    manager.register_action("screen_vl", ScreenVLDescribeAction())
    manager.register_action("hourly_summarize", HourlySummarizeAction())
    manager.register_action("daily_summarize", DailySummarizeAction())
    manager.register_action("download_scan", <data_drive>:/DownloadscanAction())
    manager.register_action("cleanup_activity", CleanupActivityAction())
    manager.register_action("todos_trigger_check", TodosTriggerCheckAction())
    manager.register_action("screen_summary_trigger", ScreenSummaryTriggerAction())
    manager.register_action("memory_maintain", MemoryMaintainAction())
    manager.register_action("smart_limit_check", SmartLimitCheckAction())
    manager.register_action("headless_session", HeadlessSessionAction())
    manager.register_action("key_health_check", KeyHealthCheckAction())
    manager.register_action("secret_backup", SecretBackupAction())
    manager.register_action("workspace_backup", WorkspaceBackupAction())

    # 加载 workspace 下可选组件的 loop action（独立组件，通过约定接口动态注册）
    _load_optional_actions(manager)
