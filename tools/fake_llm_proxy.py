"""
Fake LLM Proxy v2 - 学习用虚拟 API 端点 + 实时网页监控
========================================================
接收 OpenAI 格式请求，记录完整请求/响应，返回默认文本 "hello world"。
内置实时网页监控面板，可观察所有参数和数据。

启动:
    .venv\\Scripts\\python.exe temp\\fake_llm_proxy.py

访问监控面板:
    http://127.0.0.1:9999/

对接 Agent 工具:
    base_url = http://127.0.0.1:9999/v1
    api_key  = 随便填
"""
import asyncio
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# 行缓冲：重定向到文件也能实时显示
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

# ==================== 日志配置 ====================
# 直接写文件 + 终端，不依赖 logging 模块（避免 uvicorn 干扰）
_LOG_FILE = Path(__file__).resolve().parent.parent / "temp" / "fake_llm.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
# 启动时清空旧日志
_LOG_FILE.write_text("", encoding="utf-8")
_log_lock = asyncio.Lock()


async def _log(msg: str):
    """写一行日志到文件 + 终端"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    # 终端
    print(line, flush=True)
    # 文件（追加模式，每次写入后关闭确保刷新）
    async with _log_lock:
        with open(str(_LOG_FILE), "a", encoding="utf-8") as f:
            f.write(line + "\n")

app = FastAPI(title="Fake LLM Proxy v2")

# 允许所有跨域（学习用，方便浏览器直接测试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_TEXT = "Hello World —— This is the fake response."
PORT = 9999
MAX_RECORDS = 200  # 内存最多保留多少条请求记录

# ==================== 请求记录器 ====================

class RequestRecord:
    """单条请求的完整记录"""
    def __init__(self, rid: str, method: str, path: str):
        self.id = rid
        self.timestamp = datetime.now()
        self.method = method
        self.path = path
        self.headers: dict = {}
        self.body: dict | None = None        # 解析后的 JSON body
        self.body_raw: str = ""                  # 原始 body 文本（非 JSON 时用）
        self.body_size: int = 0                  # body 字节数
        self.is_json: bool = False
        self.stream: bool = False
        self.model: str = ""
        self.message_count: int = 0
        self.response_preview: str = ""          # 响应摘要
        self.duration_ms: float = 0              # 处理耗时

    def summary(self) -> dict:
        """用于列表展示的摘要"""
        return {
            "id": self.id,
            "time": self.timestamp.strftime("%H:%M:%S."),
            "time_ms": self.timestamp.strftime("%H:%M:%S.") + f"{self.timestamp.microsecond // 1000:03d}",
            "method": self.method,
            "path": self.path,
            "model": self.model,
            "stream": self.stream,
            "messages": self.message_count,
            "body_size": self.body_size,
            "body_size_human": _human_size(self.body_size),
            "duration_ms": round(self.duration_ms, 1),
            "is_json": self.is_json,
            "response_preview": self.response_preview[:200],
        }

    def full(self) -> dict:
        """用于详情展示的完整数据"""
        return {
            **self.summary(),
            "headers": self.headers,
            "body": self.body,
            "body_raw": self.body_raw[:50000] if self.body_raw else "",  # 防止前端卡死
            "timestamp_iso": self.timestamp.isoformat(),
        }

    def slim(self) -> dict:
        """agent 友好的索引版本：摘要 + 路径指针 + 去重标记，便于检索工具按 path 定位原 JSON。
        重复 message 标记 dup_of/similar_to，避免 slim 中重复展示系统提示词。"""
        data = {
            "id": self.id,
            "time": self.timestamp.isoformat(),
            "method": self.method,
            "path": self.path,
            "model": self.model,
            "stream": self.stream,
            "body_size": self.body_size,
            "duration_ms": round(self.duration_ms, 1),
            "response": self.response_preview,
        }
        if self.body and isinstance(self.body, dict):
            # 顶层参数（非 input/messages），递归截断长字符串
            params = {k: _slim_value(v, str_limit=120) for k, v in self.body.items()
                      if k not in ("input", "messages")}
            if params:
                data["params"] = params
            # input 数组（Responses API）或 messages 数组（Chat Completions）→ 索引
            if self.body.get("input"):
                data["api"] = "responses"
                data["item_count"] = len(self.body["input"])
                dedup = _compute_dedup(self.body["input"])
                if dedup:
                    data["dedup_count"] = len(dedup)
                data["items"] = [_index_input_item(i, item, dedup.get(i))
                                 for i, item in enumerate(self.body["input"])]
            elif self.body.get("messages"):
                data["api"] = "chat_completions"
                data["item_count"] = len(self.body["messages"])
                data["items"] = [_index_message_item(i, msg)
                                 for i, msg in enumerate(self.body["messages"])]
        return data

    def to_markdown(self, content_limit: int = 800, include_headers: bool = False) -> str:
        """渲染为 agent 友好的 Markdown：解析结构渲染，非原始 JSON 套用。
        工具调用/输出/思考/工具定义用 <details> 折叠；消息按 role 渲染为段落。"""
        lines = [
            f"# 请求记录 `{self.id}`",
            "",
            f"- **时间**: {self.timestamp.isoformat()}",
            f"- **方法**: `{self.method} {self.path}`",
            f"- **Model**: `{self.model or '(无)'}`",
            f"- **Stream**: `{self.stream}`",
            f"- **Body大小**: {_human_size(self.body_size)}",
            f"- **耗时**: {round(self.duration_ms, 1)}ms",
            "- **返回内容**:",
            f"  > {self.response_preview}",
            "",
        ]
        if not (self.body and isinstance(self.body, dict)):
            if self.body_raw:
                lines += ["## 原始 Body", "", "```", _trunc(self.body_raw, content_limit), "```", ""]
            return "\n".join(lines)

        body = self.body
        # 顶层参数（非 input/messages）→ 紧凑表格，非原始 JSON
        params = {k: v for k, v in body.items() if k not in ("input", "messages")}
        if params:
            lines += ["## 请求参数", "", "| 参数 | 值 |", "|------|-----|"]
            for k, v in params.items():
                vstr = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
                vstr = _trunc(vstr, 200).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{k}` | {vstr} |")
            lines.append("")

        # 输入项：Responses API (input) 或 Chat Completions (messages)
        if body.get("input"):
            dedup = _compute_dedup(body["input"])
            dedup_note = f" · 去重 {len(dedup)} 项" if dedup else ""
            lines += [f"## 输入项 ({len(body['input'])} 项 · Responses API{dedup_note})", "",
                      "> 工具调用/输出/思考/工具定义已折叠；重复 message 标记 ⟳(完全重复)/≈(相似变体)，仅展示首次。", ""]
            for i, item in enumerate(body["input"]):
                lines += _render_input_item_md(item, content_limit, dedup.get(i))
        elif body.get("messages"):
            lines += [f"## 消息列表 ({len(body['messages'])} 条 · Chat Completions)", ""]
            for msg in body["messages"]:
                lines += _render_message_item_md(msg, content_limit)

        if self.body_raw and not self.is_json:
            lines += ["## 原始 Body (非JSON)", "", "```", _trunc(self.body_raw, content_limit), "```", ""]

        if include_headers and self.headers:
            lines += ["## 请求头", "", "| Key | Value |", "|-----|-------|"]
            for k, v in self.headers.items():
                v_str = str(v).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| `{k}` | {v_str} |")
            lines.append("")
        return "\n".join(lines)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}TB"


class RequestStore:
    """线程安全的请求记录存储 + SSE 订阅"""
    def __init__(self, max_records: int = MAX_RECORDS):
        self.records: list[RequestRecord] = []
        self.max = max_records
        self._subscribers: list[asyncio.Queue] = []
        self.stats = {
            "total": 0,
            "stream_count": 0,
            "non_stream_count": 0,
            "by_model": defaultdict(int),
            "by_path": defaultdict(int),
        }

    def add(self, record: RequestRecord):
        self.records.append(record)
        if len(self.records) > self.max:
            self.records.pop(0)
        # 更新统计
        self.stats["total"] += 1
        if record.stream:
            self.stats["stream_count"] += 1
        else:
            self.stats["non_stream_count"] += 1
        if record.model:
            self.stats["by_model"][record.model] += 1
        self.stats["by_path"][record.path] += 1
        # 通知订阅者
        for q in self._subscribers:
            try:
                q.put_nowait(record.summary())
            except asyncio.QueueFull:
                pass

    def get(self, rid: str) -> RequestRecord | None:
        for r in self.records:
            if r.id == rid:
                return r
        return None

    def get_stats(self) -> dict:
        return {
            "total": self.stats["total"],
            "stream": self.stats["stream_count"],
            "non_stream": self.stats["non_stream_count"],
            "by_model": dict(self.stats["by_model"]),
            "by_path": dict(self.stats["by_path"]),
            "recent_count": len(self.records),
        }

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)


store = RequestStore()


# ==================== 工具函数 ====================

def _mask(value: str) -> str:
    if len(value) <= 20:
        return "****"
    return value[:12] + "..." + value[-4:]


def _safe_get_body(body: dict, *keys, default=""):
    """安全地从嵌套 dict 取值"""
    cur = body
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


# ==================== 内容提取 / 精简 / 渲染辅助 ====================

def _trunc(s, limit: int) -> str:
    """截断字符串，超长加后缀标注原长度"""
    if s is None:
        return ""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…({len(s)}字符)"


def _slim_value(v, str_limit: int = 120, depth: int = 0):
    """递归精简值：长字符串截断，深层嵌套折叠"""
    if depth > 4:
        return f"<…深度{depth}>"
    if isinstance(v, str):
        return _trunc(v, str_limit)
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        if len(v) > 10:
            return [_slim_value(x, str_limit, depth + 1) for x in v[:10]] + [f"<…共{len(v)}项>"]
        return [_slim_value(x, str_limit, depth + 1) for x in v]
    if isinstance(v, dict):
        return {k: _slim_value(x, str_limit, depth + 1) for k, x in v.items()}
    return _trunc(str(v), str_limit)


def _extract_content_text(content) -> str:
    """从 content（字符串 / content blocks 数组 / summary 数组）提取纯文本"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if "text" in block:
                    parts.append(str(block["text"]))
                elif t and "image" in t:
                    url = block.get("image_url", "") or block.get("url", "")
                    parts.append(f"[图片 {url[:40]}]" if url else "[图片]")
                else:
                    parts.append(f"[{t}]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


# ---------- slim 索引（指针式） ----------

def _compute_dedup(items) -> dict:
    """计算 input 数组中 message 项的去重信息。
    返回 {index: {"dup_of": first_idx, "len": X} | {"similar_to": first_idx, "len": X}}
    - dup_of: content 完全相同（md5 一致）的首见 index
    - similar_to: 首行相同但全文不同的首见 index（变体，如 AGENTS.md 多次读取有细微差异）
    仅对 message 类型且文本 >= 50 字符生效，避免短消息误判。
    相似检测用首行 hash（第一个换行前的内容，去空白），比前 N 字更鲁棒：
    AGENTS.md 首行固定为 "# AGENTS.md instructions for ..."，即使后续内容有插入也能识别。
    """
    seen_full = {}    # full_hash -> first_index
    seen_prefix = {}  # first_line_hash -> first_index
    result = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        text = _extract_content_text(item.get("content", ""))
        if not text or len(text) < 50:
            continue
        full_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        if full_hash in seen_full:
            result[i] = {"dup_of": seen_full[full_hash], "len": len(text)}
            continue
        # 首行相似检测（仅对 >= 200 字的内容启用，首行 >= 20 字才计算，避免短行误判）
        if len(text) >= 200:
            first_line = text.split("\n", 1)[0].strip()
            if len(first_line) >= 20:
                prefix_hash = hashlib.md5(first_line.encode("utf-8")).hexdigest()
                if prefix_hash in seen_prefix:
                    first = seen_prefix[prefix_hash]
                    result[i] = {"similar_to": first, "len": len(text)}
                    continue
                seen_prefix[prefix_hash] = i
        seen_full[full_hash] = i
    return result


def _index_input_item(i: int, item, dedup_info=None) -> dict:
    """Responses API input 数组项 → 索引摘要（含路径指针 + 去重标记）。
    预览短小（保留关键搜索词），原文用 path 在全量 JSON 中定位。"""
    if not isinstance(item, dict):
        return {"i": i, "type": "?", "preview": _trunc(str(item), 60), "path": f"input[{i}]"}
    t = item.get("type", "?")
    base = {"i": i, "type": t}
    if t == "additional_tools":
        tools = item.get("tools", [])
        tool_idx = []
        for ti, tool in enumerate(tools):
            if isinstance(tool, dict):
                name = tool.get("name", "?")
                desc = str(tool.get("description", ""))
                tool_idx.append({
                    "name": name,
                    "type": tool.get("type", "?"),
                    "desc_len": len(desc),
                    "path": f"input[{i}].tools[{ti}].description",
                })
        base["role"] = item.get("role", "")
        base["tools"] = tool_idx
        base["preview"] = "tools: " + ", ".join(tg["name"] for tg in tool_idx) if tool_idx else "(无工具)"
        base["path"] = f"input[{i}]"
        return base
    if t == "message":
        text = _extract_content_text(item.get("content", ""))
        base["role"] = item.get("role", "?")
        base["text_len"] = len(text)
        base["path"] = f"input[{i}].content"
        if dedup_info:
            if "dup_of" in dedup_info:
                base["dup_of"] = dedup_info["dup_of"]
                base["preview"] = f"⟳ 同 input[{dedup_info['dup_of']}]"
            elif "similar_to" in dedup_info:
                base["similar_to"] = dedup_info["similar_to"]
                base["preview"] = f"≈ 同 input[{dedup_info['similar_to']}] 变体"
            else:
                base["preview"] = _trunc(text, 80)
        else:
            base["preview"] = _trunc(text, 80)
        return base
    if t == "function_call":
        args = str(item.get("arguments", ""))
        base["name"] = item.get("name", "?")
        base["call_id"] = item.get("call_id", "")
        base["args_preview"] = _trunc(args, 80)
        base["args_len"] = len(args)
        base["path"] = f"input[{i}].arguments"
        return base
    if t == "function_call_output":
        out = item.get("output", "")
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False)
        base["call_id"] = item.get("call_id", "")
        base["output_preview"] = _trunc(out, 80)
        base["output_len"] = len(out)
        base["path"] = f"input[{i}].output"
        return base
    if t == "reasoning":
        summary = item.get("summary", [])
        summ_text = _extract_content_text(summary) if summary else ""
        base["preview"] = _trunc(summ_text, 80)
        base["summary_len"] = len(summ_text)
        base["has_encrypted"] = bool(item.get("encrypted_content"))
        base["path"] = f"input[{i}].summary"
        return base
    base["preview"] = _trunc(json.dumps(item, ensure_ascii=False), 80)
    base["path"] = f"input[{i}]"
    return base


def _index_message_item(i: int, msg) -> dict:
    """Chat Completions messages 项 → 索引摘要"""
    if not isinstance(msg, dict):
        return {"i": i, "type": "message", "preview": _trunc(str(msg), 60), "path": f"messages[{i}]"}
    role = msg.get("role", "?")
    text = _extract_content_text(msg.get("content", ""))
    item = {
        "i": i,
        "type": "message",
        "role": role,
        "preview": _trunc(text, 80),
        "text_len": len(text),
        "path": f"messages[{i}].content",
    }
    if msg.get("name"):
        item["name"] = msg["name"]
    if msg.get("tool_calls"):
        tc_idx = []
        for ci, tc in enumerate(msg["tool_calls"]):
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                args = str(fn.get("arguments", ""))
                tc_idx.append({
                    "name": fn.get("name", "?"),
                    "args_preview": _trunc(args, 60),
                    "args_len": len(args),
                    "path": f"messages[{i}].tool_calls[{ci}].function.arguments",
                })
        item["tool_calls"] = tc_idx
    return item


# ---------- Markdown 渲染 ----------

_MSG_EMOJI = {"user": "👤", "assistant": "🤖", "developer": "🛠️", "system": "⚙️", "tool": "🔧"}


def _render_input_item_md(item, content_limit: int = 800, dedup_info=None) -> list:
    """渲染 Responses API input 项为 Markdown 行（解析结构，非原始 JSON）。
    dedup_info 非空时，重复 message 只显示引用标记，不重复展示内容。"""
    if not isinstance(item, dict):
        return [f"- `{_trunc(str(item), 80)}`", ""]
    t = item.get("type", "?")
    if t == "additional_tools":
        tools = item.get("tools", [])
        lines = ["<details><summary>🔧 工具定义 (developer)</summary>", ""]
        lines += ["| # | name | type | description |", "|---|------|------|-------------|"]
        for ti, tool in enumerate(tools):
            if isinstance(tool, dict):
                name = tool.get("name", "?")
                tt = tool.get("type", "?")
                desc = _trunc(str(tool.get("description", "")), 100).replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {ti} | `{name}` | {tt} | {desc} |")
        lines += ["", "</details>", ""]
        return lines
    if t == "message":
        role = item.get("role", "?")
        text = _extract_content_text(item.get("content", ""))
        emoji = _MSG_EMOJI.get(role, "💬")
        # 去重：完全重复或相似变体只显示引用
        if dedup_info:
            if "dup_of" in dedup_info:
                return [f"*{emoji} `{role}` ⟳ 同 input[{dedup_info['dup_of']}] ({dedup_info['len']}字，完全重复已省略)*", ""]
            if "similar_to" in dedup_info:
                first = dedup_info["similar_to"]
                return [f"<details><summary>{emoji} `{role}` ≈ 同 input[{first}] 变体 ({dedup_info['len']}字)</summary>", "",
                        "_(前 200 字符相似，全文见全量 JSON `input[?].content`)_", "", "</details>", ""]
        lines = [f"#### {emoji} `{role}`", ""]
        if len(text) > content_limit:
            text = text[:content_limit] + f"\n\n…(共 {len(text)} 字符，路径 `input[?].content`)"
        lines += [text, ""]
        return lines
    if t == "function_call":
        name = item.get("name", "?")
        args = str(item.get("arguments", ""))
        call_id = item.get("call_id", "")
        lines = [f"<details><summary>📞 function_call: <code>{name}</code>() ({len(args)} 字符)</summary>", ""]
        if call_id:
            lines.append(f"`call_id`: `{call_id}`")
        args_disp = args if len(args) <= content_limit else args[:content_limit] + f"\n…(共 {len(args)} 字符)"
        lines += ["", "```json", args_disp, "```", "", "</details>", ""]
        return lines
    if t == "function_call_output":
        out = item.get("output", "")
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False, indent=2)
        call_id = item.get("call_id", "")
        lines = [f"<details><summary>📤 function_call_output ({len(out)} 字符)</summary>", ""]
        if call_id:
            lines.append(f"`call_id`: `{call_id}`")
        if len(out) > content_limit:
            out = out[:content_limit] + f"\n…(共 {len(out)} 字符)"
        lines += ["", "```", out, "```", "", "</details>", ""]
        return lines
    if t == "reasoning":
        summary = item.get("summary", [])
        summ_text = _extract_content_text(summary) if summary else ""
        lines = [f"<details><summary>💭 reasoning ({len(summ_text)} 字符)</summary>", ""]
        if summ_text:
            if len(summ_text) > content_limit:
                summ_text = summ_text[:content_limit] + f"\n…(共 {len(summ_text)} 字符)"
            lines += [summ_text, ""]
        if item.get("encrypted_content"):
            lines.append("_(含加密内容，已省略)_")
        lines += ["</details>", ""]
        return lines
    return [f"- `{t}`: `{_trunc(json.dumps(item, ensure_ascii=False), 100)}`", ""]


def _render_message_item_md(msg, content_limit: int = 800) -> list:
    """渲染 Chat Completions message 项为 Markdown 行"""
    if not isinstance(msg, dict):
        return [f"- `{_trunc(str(msg), 80)}`", ""]
    role = msg.get("role", "?")
    name = msg.get("name", "")
    text = _extract_content_text(msg.get("content", ""))
    emoji = _MSG_EMOJI.get(role, "💬")
    title = f"#### {emoji} `{role}`" + (f" · `{name}`" if name else "")
    lines = [title, ""]
    if text:
        if len(text) > content_limit:
            text = text[:content_limit] + f"\n\n…(共 {len(text)} 字符)"
        lines += [text, ""]
    if msg.get("tool_calls"):
        for ci, tc in enumerate(msg["tool_calls"]):
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                tname = fn.get("name", "?")
                args = str(fn.get("arguments", ""))
                args_disp = args if len(args) <= content_limit else args[:content_limit] + f"\n…(共 {len(args)} 字符)"
                lines += [f"<details><summary>📞 tool_call[{ci}]: <code>{tname}</code>() ({len(args)} 字符)</summary>", "",
                          "```json", args_disp, "```", "", "</details>", ""]
    return lines


# ---------- HTML 渲染 ----------

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_input_item_html(item, content_limit: int = 4000, dedup_info=None) -> str:
    """渲染 Responses API input 项为 HTML（工具调用/思考/工具定义默认折叠，消息展开）。
    dedup_info 非空时，重复 message 显示为引用气泡，不重复展示内容。"""
    if not isinstance(item, dict):
        return f'<div class="item unknown">{_esc(item)}</div>'
    t = item.get("type", "?")
    if t == "additional_tools":
        tools = item.get("tools", [])
        rows = ""
        for ti, tool in enumerate(tools):
            if isinstance(tool, dict):
                name = tool.get("name", "?")
                tt = tool.get("type", "?")
                desc = _trunc(str(tool.get("description", "")), 200)
                rows += (f'<div class="tool-row"><span class="tool-idx">[{ti}]</span> '
                         f'<span class="tool-name">{_esc(name)}</span> '
                         f'<span class="tool-type">{_esc(tt)}</span>'
                         f'<div class="tool-desc">{_esc(desc)}</div></div>')
        return (f'<details class="item tools"><summary>🔧 工具定义 ({len(tools)} 个)</summary>'
                f'<div class="content">{rows}</div></details>')
    if t == "message":
        role = item.get("role", "?")
        text = _extract_content_text(item.get("content", ""))
        # 去重：完全重复或相似变体显示为引用
        if dedup_info:
            if "dup_of" in dedup_info:
                return (f'<div class="msg bubble role-{role} dup"><div class="bubble-role">{_esc(role)} ⟳ 重复</div>'
                        f'<div class="bubble-text dup-ref">同 input[{dedup_info["dup_of"]}] · {dedup_info["len"]}字 · 完全重复已省略</div></div>')
            if "similar_to" in dedup_info:
                first = dedup_info["similar_to"]
                return (f'<details class="item similar"><summary>≈ {_esc(role)} 同 input[{first}] 变体 · {dedup_info["len"]}字</summary>'
                        f'<div class="content"><div class="reasoning-text">前 200 字符与 input[{first}] 相似，全文见全量 JSON</div></div></details>')
        if len(text) > content_limit:
            text = text[:content_limit] + f"\n…(共 {len(text)} 字符)"
        return (f'<div class="msg bubble role-{role}"><div class="bubble-role">{_esc(role)}</div>'
                f'<div class="bubble-text">{_esc(text)}</div></div>')
    if t == "function_call":
        name = item.get("name", "?")
        args = str(item.get("arguments", ""))
        call_id = item.get("call_id", "")
        args_disp = args if len(args) <= content_limit else args[:content_limit] + f"\n…(共 {len(args)} 字符)"
        return (f'<details class="item tool-call"><summary>📞 <span class="tc-name">{_esc(name)}</span>() '
                f'<span class="tc-meta">{len(args)}字符</span></summary>'
                f'<div class="content"><div class="kv"><span class="k">call_id</span><span class="v">{_esc(call_id)}</span></div>'
                f'<pre class="args">{_esc(args_disp)}</pre></div></details>')
    if t == "function_call_output":
        out = item.get("output", "")
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False, indent=2)
        call_id = item.get("call_id", "")
        out_disp = out if len(out) <= content_limit else out[:content_limit] + f"\n…(共 {len(out)} 字符)"
        return (f'<details class="item tool-output"><summary>📤 输出 <span class="tc-meta">{len(out)}字符</span></summary>'
                f'<div class="content"><div class="kv"><span class="k">call_id</span><span class="v">{_esc(call_id)}</span></div>'
                f'<pre class="output">{_esc(out_disp)}</pre></div></details>')
    if t == "reasoning":
        summary = item.get("summary", [])
        summ_text = _extract_content_text(summary) if summary else ""
        if len(summ_text) > content_limit:
            summ_text = summ_text[:content_limit] + f"\n…(共 {len(summ_text)} 字符)"
        enc = ' <span class="enc">含加密</span>' if item.get("encrypted_content") else ""
        return (f'<details class="item reasoning"><summary>💭 思考 <span class="tc-meta">{len(summ_text)}字符{enc}</span></summary>'
                f'<div class="content"><div class="reasoning-text">{_esc(summ_text)}</div></div></details>')
    return f'<details class="item unknown"><summary>{_esc(t)}</summary><pre>{_esc(json.dumps(item, ensure_ascii=False, indent=2))}</pre></details>'


def _render_message_item_html(msg, content_limit: int = 4000) -> str:
    """渲染 Chat Completions message 项为 HTML"""
    if not isinstance(msg, dict):
        return f'<div class="item unknown">{_esc(msg)}</div>'
    role = msg.get("role", "?")
    name = msg.get("name", "")
    text = _extract_content_text(msg.get("content", ""))
    if len(text) > content_limit:
        text = text[:content_limit] + f"\n…(共 {len(text)} 字符)"
    name_html = f' · {_esc(name)}' if name else ''
    html = (f'<div class="msg bubble role-{role}"><div class="bubble-role">{_esc(role)}{name_html}</div>'
            f'<div class="bubble-text">{_esc(text)}</div></div>')
    if msg.get("tool_calls"):
        for ci, tc in enumerate(msg["tool_calls"]):
            if isinstance(tc, dict):
                fn = tc.get("function", {})
                tname = fn.get("name", "?")
                args = str(fn.get("arguments", ""))
                args_disp = args if len(args) <= content_limit else args[:content_limit] + f"\n…(共 {len(args)} 字符)"
                html += (f'<details class="item tool-call"><summary>📞 tool_call[{ci}]: <span class="tc-name">{_esc(tname)}</span>() '
                         f'<span class="tc-meta">{len(args)}字符</span></summary>'
                         f'<div class="content"><pre class="args">{_esc(args_disp)}</pre></div></details>')
    return html


async def _print_request(record: RequestRecord):
    """终端+文件日志"""
    await _log(f">>> {record.method} {record.path}  "
               f"({record.message_count} msgs, {_human_size(record.body_size)}, "
               f"stream={record.stream})")
    if record.is_json and isinstance(record.body, dict):
        msgs = record.body.get("messages", [])
        for i, m in enumerate(msgs[-5:]):
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = f"[多模态 {len(content)}块]"
            cstr = str(content)
            if len(cstr) > 100:
                cstr = cstr[:100] + "..."
            idx = len(msgs) - 5 + i if len(msgs) > 5 else i
            await _log(f"  [{idx}] {role}: {cstr}")
    await _log(f"  -> 返回: {record.response_preview[:100]}")


# ==================== 核心端点 ====================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """核心端点：模拟 OpenAI /v1/chat/completions"""
    start = time.time()
    rid = uuid.uuid4().hex[:12]
    record = RequestRecord(rid, "POST", "/v1/chat/completions")

    # 安全读取 body（兼容非 JSON）
    raw = await request.body()
    record.body_size = len(raw)
    record.headers = dict(request.headers)

    try:
        record.body = json.loads(raw)
        record.is_json = True
        record.model = record.body.get("model", "")
        record.stream = record.body.get("stream", False)
        record.message_count = len(record.body.get("messages", []))
    except Exception:
        record.body_raw = raw.decode("utf-8", errors="replace")
        record.is_json = False

    # 构造响应
    model = record.model or "fake-model"
    req_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    record.response_preview = DEFAULT_TEXT

    if record.stream:
        record.duration_ms = (time.time() - start) * 1000
        store.add(record)
        await _print_request(record)
        return StreamingResponse(
            _stream_response(req_id, created, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )
    else:
        resp_data = {
            "id": req_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": DEFAULT_TEXT},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": len(DEFAULT_TEXT.split()),
                "total_tokens": 10 + len(DEFAULT_TEXT.split()),
            },
        }
        record.duration_ms = (time.time() - start) * 1000
        store.add(record)
        await _print_request(record)
        return JSONResponse(resp_data)


async def _stream_response(req_id: str, created: int, model: str):
    """生成 OpenAI SSE 流式响应，逐词吐出"""
    words = DEFAULT_TEXT.split(" ")
    for i, word in enumerate(words):
        chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": word + (" " if i < len(words) - 1 else "")},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.15)
    # 结束块
    final = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ==================== 其他 OpenAI 兼容端点（兜底） ====================

@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": "fake-model", "object": "model", "created": now, "owned_by": "fake"},
            {"id": "gpt-4o-mini", "object": "model", "created": now, "owned_by": "fake"},
            {"id": "gpt-4o", "object": "model", "created": now, "owned_by": "fake"},
            {"id": "deepseek-chat", "object": "model", "created": now, "owned_by": "fake"},
            {"id": "claude-3-5-sonnet", "object": "model", "created": now, "owned_by": "fake"},
        ],
    }


@app.api_route("/v1/embeddings", methods=["POST"])
async def fake_embeddings(request: Request):
    """假 embeddings 端点"""
    body = await request.json()
    input_text = body.get("input", "")
    if isinstance(input_text, list):
        n = len(input_text)
    else:
        n = 1
    # 返回固定维度的假向量
    fake_vec = [0.1] * 1536
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": fake_vec} for i in range(n)],
        "model": body.get("model", "text-embedding-ada-002"),
        "usage": {"prompt_tokens": 8, "total_tokens": 8},
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def catch_all(path: str, request: Request):
    """兜底：记录所有其他 /v1/* 请求，返回假数据"""
    rid = uuid.uuid4().hex[:12]
    record = RequestRecord(rid, request.method, f"/v1/{path}")
    raw = await request.body()
    record.body_size = len(raw)
    record.headers = dict(request.headers)
    try:
        record.body = json.loads(raw)
        record.is_json = True
    except Exception:
        record.body_raw = raw.decode("utf-8", errors="replace")
    record.response_preview = f"[兜底] /v1/{path}"
    store.add(record)
    await _print_request(record)
    return JSONResponse({"warning": f"endpoint /v1/{path} is fake", "received": True})


# ==================== 监控面板 ====================

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """实时监控网页"""
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/api/requests")
async def api_requests():
    """获取所有请求记录摘要"""
    return {"requests": [r.summary() for r in reversed(store.records)], "stats": store.get_stats()}


@app.get("/api/requests/{rid}")
async def api_request_detail(rid: str):
    """获取单条请求完整详情"""
    r = store.get(rid)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return r.full()


@app.get("/api/requests/{rid}/download")
async def api_request_download(rid: str, format: str = "json"):
    """下载单条请求记录。format: json(全量) | slim(精简JSON) | markdown"""
    r = store.get(rid)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    ts = r.timestamp.strftime('%Y%m%d_%H%M%S')
    if format == "slim":
        filename = f"request_{rid}_{ts}.slim.json"
        return Response(
            content=json.dumps(r.slim(), ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    elif format == "markdown":
        filename = f"request_{rid}_{ts}.md"
        return Response(
            content=r.to_markdown(include_headers=False),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    else:  # json 全量
        filename = f"request_{rid}_{ts}.json"
        return Response(
            content=json.dumps(r.full(), ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@app.get("/api/stats")
async def api_stats():
    return store.get_stats()


@app.get("/api/export")
async def api_export(format: str = "json"):
    """导出所有请求记录。format: json(全量) | slim(精简JSON) | markdown"""
    if format == "slim":
        data = {
            "exported_at": datetime.now().isoformat(),
            "stats": store.get_stats(),
            "requests": [r.slim() for r in store.records],
        }
        filename = f"fake_proxy_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.slim.json"
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    elif format == "markdown":
        stats = store.get_stats()
        parts = [
            "# Fake LLM Proxy 导出报告 (Markdown)",
            "",
            f"- **导出时间**: {datetime.now().isoformat()}",
            f"- **总请求数**: {stats['total']}",
            f"- **流式**: {stats['stream']}",
            f"- **非流式**: {stats['non_stream']}",
            "",
            "---",
            "",
        ]
        for r in reversed(store.records):  # 最新在前
            parts.append(r.to_markdown(include_headers=False))
            parts += ["", "---", ""]
        filename = f"fake_proxy_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        return Response(
            content="\n".join(parts),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    else:  # json 全量
        data = {
            "exported_at": datetime.now().isoformat(),
            "stats": store.get_stats(),
            "requests": [r.full() for r in store.records],
        }
        filename = f"fake_proxy_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@app.get("/api/export/html")
async def api_export_html():
    """导出所有请求记录为独立 HTML 报告（聊天气泡式，工具调用/思考/工具定义默认折叠）"""
    records_html = []
    for r in reversed(store.records):
        rec = r.full()
        body = rec.get("body") if isinstance(rec.get("body"), dict) else {}
        # 渲染输入项为聊天流水
        transcript = ""
        if body.get("input"):
            dedup = _compute_dedup(body["input"])
            transcript = "".join(_render_input_item_html(item, 4000, dedup.get(i))
                                for i, item in enumerate(body["input"]))
        elif body.get("messages"):
            transcript = "".join(_render_message_item_html(m, 4000) for m in body["messages"])
        elif rec.get("body_raw"):
            transcript = f'<pre class="raw">{_esc(_trunc(rec["body_raw"], 4000))}</pre>'
        # 顶层参数（非 input/messages）→ 紧凑表格
        params = {k: v for k, v in body.items() if k not in ("input", "messages")} if body else {}
        params_html = ""
        if params:
            params_html = '<details class="params"><summary>⚙️ 请求参数</summary><div class="content"><table class="ptable"><tr><th>参数</th><th>值</th></tr>'
            for k, v in params.items():
                vstr = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
                params_html += f'<tr><td class="pk">{_esc(k)}</td><td class="pv">{_esc(_trunc(vstr, 300))}</td></tr>'
            params_html += '</table></div></details>'
        # 请求头（折叠）
        hdr_rows = "".join(f'<div class="kv"><span class="k">{_esc(k)}</span><span class="v">{_esc(v)}</span></div>'
                           for k, v in (rec.get("headers") or {}).items())
        headers_html = f'<details class="headers"><summary>HTTPHeader ({len(rec.get("headers") or {})})</summary><div class="content">{hdr_rows}</div></details>' if hdr_rows else ""
        item_count = len(body.get("input") or body.get("messages") or [])
        api_tag = "Responses" if body.get("input") else ("ChatCompletions" if body.get("messages") else "Other")
        records_html.append(f"""
        <div class="req-card">
          <details class="req-head" open>
            <summary>
              <span class="ts">{_esc(rec.get('time_ms',''))}</span>
              <span class="model">{_esc(rec.get('model','') or '(无)')}</span>
              <span class="ep">{_esc(rec['method'])} {_esc(rec['path'])}</span>
              <span class="tag api">{api_tag}</span>
              <span class="tag {'stream' if rec.get('stream') else 'json'}">{'STREAM' if rec.get('stream') else 'JSON'}</span>
              <span class="meta">{item_count}项 · {_esc(rec.get('body_size_human',''))} · {rec.get('duration_ms','')}ms</span>
            </summary>
            <div class="req-body">
              <div class="kv resp"><span class="k">返回内容</span><span class="v">{_esc(rec.get('response_preview',''))}</span></div>
              {params_html}
              <div class="transcript">{transcript if transcript else '<div class="empty">无输入项</div>'}</div>
              {headers_html}
            </div>
          </details>
        </div>""")
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8"><title>Fake Proxy 导出报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}</title>
<style>
body {{ font-family: system-ui,'Segoe UI',sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:20px; }}
.summary {{ background:#161b22; padding:16px 20px; border-radius:8px; margin-bottom:20px; border:1px solid #30363d; }}
.summary h2 {{ color:#58a6ff; margin:0 0 8px; }}
.summary p {{ margin:4px 0; color:#8b949e; font-size:13px; }}
.hint {{ color:#6e7681; font-size:12px; margin-top:8px; }}
.req-card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; margin-bottom:14px; overflow:hidden; }}
.req-head > summary {{ list-style:none; cursor:pointer; padding:10px 16px; display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:13px; border-bottom:1px solid #21262d; }}
.req-head > summary::-webkit-details-marker {{ display:none; }}
.req-head .ts {{ color:#8b949e; font-size:11px; font-family:Consolas,monospace; }}
.req-head .model {{ color:#d29922; font-weight:600; }}
.req-head .ep {{ color:#58a6ff; font-family:Consolas,monospace; font-size:12px; }}
.req-head .meta {{ color:#6e7681; font-size:11px; margin-left:auto; }}
.tag {{ display:inline-block; padding:1px 7px; border-radius:3px; font-size:10px; font-weight:600; }}
.tag.api {{ background:#1f6feb33; color:#58a6ff; }}
.tag.stream {{ background:#23863633; color:#7ee787; }}
.tag.json {{ background:#6e768133; color:#8b949e; }}
.req-body {{ padding:12px 16px; }}
.transcript {{ margin-top:8px; }}
/* 聊天气泡 */
.bubble {{ margin:8px 0; padding:10px 14px; border-radius:8px; max-width:88%; white-space:pre-wrap; word-break:break-word; font-size:13px; line-height:1.55; }}
.bubble .bubble-role {{ font-size:11px; font-weight:700; text-transform:uppercase; margin-bottom:4px; opacity:.8; }}
.bubble .bubble-text {{ white-space:pre-wrap; word-break:break-word; }}
.role-user {{ background:#1f6feb22; border-left:3px solid #58a6ff; margin-left:0; }}
.role-user .bubble-role {{ color:#58a6ff; }}
.role-assistant {{ background:#23863622; border-left:3px solid #7ee787; margin-left:auto; }}
.role-assistant .bubble-role {{ color:#7ee787; }}
.role-developer {{ background:#6e768122; border-left:3px solid #8b949e; }}
.role-developer .bubble-role {{ color:#8b949e; }}
.role-system {{ background:#6e768122; border-left:3px solid #8b949e; }}
.role-system .bubble-role {{ color:#8b949e; }}
.role-tool {{ background:#d2992222; border-left:3px solid #d29922; }}
.role-tool .bubble-role {{ color:#d29922; }}
/* 折叠项 */
.item {{ margin:6px 0; }}
.item > summary {{ cursor:pointer; padding:6px 10px; border-radius:5px; font-size:12px; list-style:none; user-select:none; }}
.item > summary::-webkit-details-marker {{ display:none; }}
.item > summary::before {{ content:'▶ '; font-size:9px; color:#6e7681; }}
.item[open] > summary::before {{ content:'▼ '; }}
.item > summary:hover {{ background:#21262d; }}
.item .content {{ padding:8px 12px; margin-top:4px; border-left:2px solid #30363d; }}
.tool-call > summary {{ color:#d29922; background:#d2992211; }}
.tool-call .tc-name {{ font-family:Consolas,monospace; font-weight:600; }}
.tool-output > summary {{ color:#8b949e; background:#6e768111; }}
.reasoning > summary {{ color:#bc8cff; background:#bc8cff11; }}
.reasoning-text {{ font-size:12px; color:#bc8cff; font-style:italic; white-space:pre-wrap; }}
.tools > summary {{ color:#58a6ff; background:#1f6feb11; }}
.tool-row {{ padding:6px 0; border-bottom:1px solid #21262d; font-size:12px; }}
.tool-row:last-child {{ border:none; }}
.tool-idx {{ color:#6e7681; font-family:Consolas,monospace; }}
.tool-name {{ color:#d29922; font-family:Consolas,monospace; font-weight:600; margin-left:6px; }}
.tool-type {{ color:#8b949e; font-size:11px; margin-left:6px; }}
.tool-desc {{ color:#c9d1d9; margin-top:3px; font-size:11px; opacity:.85; }}
.tc-meta {{ color:#6e7681; font-size:11px; margin-left:6px; }}
.enc {{ color:#ff7b72; }}
/* 去重引用气泡 */
.bubble.dup {{ padding:6px 12px; opacity:.7; font-size:11px; border-style:dashed; }}
.bubble.dup .dup-ref {{ color:#6e7681; font-style:italic; }}
.item.similar > summary {{ color:#bc8cff; background:#bc8ff11; font-size:11px; }}
.item.similar .reasoning-text {{ font-size:11px; color:#6e7681; }}
pre {{ white-space:pre-wrap; word-break:break-all; font-family:Consolas,'Cascadia Code',monospace; font-size:12px; background:#0d1117; padding:8px; border-radius:4px; max-height:400px; overflow-y:auto; margin:6px 0; }}
pre.args {{ border-left:2px solid #d29922; }}
pre.output {{ border-left:2px solid #8b949e; }}
pre.raw {{ border-left:2px solid #6e7681; }}
.kv {{ display:flex; gap:10px; padding:2px 0; font-size:12px; }}
.kv .k {{ width:160px; color:#8b949e; flex-shrink:0; font-family:Consolas,monospace; }}
.kv .v {{ color:#c9d1d9; word-break:break-all; }}
.kv.resp .v {{ color:#7ee787; }}
.params > summary, .headers > summary {{ cursor:pointer; font-size:12px; color:#8b949e; padding:4px 0; }}
.ptable {{ border-collapse:collapse; width:100%; margin-top:6px; font-size:12px; }}
.ptable th, .ptable td {{ border:1px solid #21262d; padding:4px 8px; text-align:left; vertical-align:top; }}
.ptable th {{ color:#8b949e; background:#161b22; }}
.ptable .pk {{ color:#79c0ff; font-family:Consolas,monospace; white-space:nowrap; width:160px; }}
.ptable .pv {{ word-break:break-all; }}
.empty {{ color:#484f58; padding:20px; text-align:center; }}
</style></head><body>
<div class="summary">
  <h2>Fake LLM Proxy 导出报告</h2>
  <p>导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
  <p>总请求数: {store.get_stats()['total']} | 流式: {store.get_stats()['stream']} | 非流式: {store.get_stats()['non_stream']}</p>
  <p class="hint">消息(用户/助手/开发者)默认展开 · 工具调用/输出/思考/工具定义默认折叠(点击 ▶ 展开)</p>
</div>
{''.join(records_html) if records_html else '<div class="empty">无请求记录</div>'}
</body></html>"""
    filename = f"fake_proxy_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/shutdown")
async def shutdown():
    """优雅关闭服务（网页按钮 / curl 调用）"""
    await _log(">>> 收到关闭请求，0.5秒后退出...")
    async def _do_shutdown():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.create_task(_do_shutdown())
    return {"status": "shutting_down"}


@app.get("/api/log")
async def api_log():
    """获取日志文件内容（最近 500 行）"""
    try:
        lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"log": "\n".join(lines[-500:]), "total_lines": len(lines)}
    except Exception as e:
        return {"log": "", "error": str(e)}


@app.get("/events")
async def sse_events():
    """SSE 端点：实时推送新请求通知给网页"""
    q = store.subscribe()
    async def event_stream():
        try:
            # 先发一条 hello
            yield f"data: {json.dumps({'type': 'hello', 'stats': store.get_stats()})}\n\n"
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps({'type': 'request', 'data': item, 'stats': store.get_stats()})}\n\n"
                except TimeoutError:
                    # 心跳
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            store.unsubscribe(q)
    return StreamingResponse(event_stream(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ==================== 监控面板 HTML ====================

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fake LLM Proxy - 实时监控</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; }
.header { background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 18px; color: #58a6ff; }
.header .stats { font-size: 13px; color: #8b949e; }
.stats span { margin-left: 16px; }
.stats b { color: #7ee787; }
.layout { display: flex; height: calc(100vh - 57px); }
.list-pane { width: 420px; border-right: 1px solid #30363d; overflow-y: auto; }
.detail-pane { flex: 1; overflow-y: auto; padding: 20px; }
.req-item { padding: 12px 16px; border-bottom: 1px solid #21262d; cursor: pointer; transition: background .15s; }
.req-item:hover { background: #161b22; }
.req-item.active { background: #1f2937; border-left: 3px solid #58a6ff; }
.req-item .top { display: flex; justify-content: space-between; font-size: 12px; color: #8b949e; }
.req-item .model { color: #d29922; font-weight: 600; }
.req-item .meta { margin-top: 4px; font-size: 11px; color: #6e7681; }
.req-item .tag { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; margin-right: 4px; }
.tag-stream { background: #1f6feb33; color: #58a6ff; }
.tag-nostream { background: #23863633; color: #7ee787; }
.tag-post { background: #da363033; color: #ff7b72; }
.tag-get { background: #23863633; color: #7ee787; }
.empty { padding: 40px; text-align: center; color: #484f58; }
.detail-section { background: #161b22; border: 1px solid #30363d; border-radius: 6px; margin-bottom: 16px; }
.detail-section h3 { padding: 10px 16px; font-size: 13px; color: #58a6ff; border-bottom: 1px solid #30363d; }
.detail-section .content { padding: 12px 16px; font-size: 13px; }
pre { white-space: pre-wrap; word-break: break-all; font-family: 'Cascadia Code', Consolas, monospace; font-size: 12px; line-height: 1.5; }
.json-key { color: #79c0ff; }
.json-str { color: #a5d6ff; }
.json-num { color: #ffa657; }
.json-bool { color: #ff7b72; }
.json-null { color: #ff7b72; }
.kv { display: flex; padding: 3px 0; }
.kv .k { width: 220px; color: #8b949e; flex-shrink: 0; }
.kv .v { color: #c9d1d9; word-break: break-all; }
.msg-item { padding: 6px 0; border-bottom: 1px solid #21262d; }
.msg-item:last-child { border: none; }
.msg-role { font-weight: 600; font-size: 11px; text-transform: uppercase; }
.role-system { color: #8b949e; }
.role-user { color: #58a6ff; }
.role-assistant { color: #7ee787; }
.role-tool { color: #d29922; }
.msg-content { margin-top: 3px; font-size: 12px; color: #c9d1d9; max-height: 200px; overflow-y: auto; padding: 6px; background: #0d1117; border-radius: 4px; }
.badge { display: inline-block; background: #1f6feb; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; margin-left: 8px; }
.live-dot { display: inline-block; width: 8px; height: 8px; background: #7ee787; border-radius: 50%; margin-right: 6px; animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
.toolbar { padding: 8px 16px; border-bottom: 1px solid #30363d; display: flex; gap: 8px; }
.btn { padding: 4px 12px; background: #21262d; border: 1px solid #30363d; color: #c9d1d9; border-radius: 4px; cursor: pointer; font-size: 12px; }
.btn:hover { background: #30363d; }
.btn-export { background: #1f6feb33; color: #58a6ff; border-color: #1f6feb55; }
.btn-export:hover { background: #1f6feb55; }
.btn-danger { background: #da363033; color: #ff7b72; border-color: #da363055; }
.btn-danger:hover { background: #da363055; }
</style>
</head>
<body>
<div class="header">
  <h1><span class="live-dot"></span>Fake LLM Proxy 监控</h1>
  <div class="stats" id="stats">
    <span>总数: <b id="s-total">0</b></span>
    <span>流式: <b id="s-stream">0</b></span>
    <span>非流式: <b id="s-nostream">0</b></span>
  </div>
</div>
<div class="layout">
  <div class="list-pane">
    <div class="toolbar">
      <button class="btn" onclick="clearList()">清空</button>
      <button class="btn" onclick="loadAll()">刷新</button>
      <button class="btn btn-export" onclick="exportFormat('json')">导出JSON</button>
      <button class="btn btn-export" onclick="exportFormat('slim')">导出精简JSON</button>
      <button class="btn btn-export" onclick="exportFormat('markdown')">导出Markdown</button>
      <button class="btn btn-export" onclick="exportHTML()">导出HTML</button>
      <button class="btn btn-danger" onclick="shutdownServer()">关闭服务</button>
    </div>
    <div id="req-list">
      <div class="empty">等待请求中...</div>
    </div>
  </div>
  <div class="detail-pane" id="detail">
    <div class="empty">点击左侧请求查看详情</div>
  </div>
</div>
<script>
let currentId = null;
let reqList = [];

function fmtSize(n) {
  if (n < 1024) return n + 'B';
  if (n < 1048576) return (n/1024).toFixed(1) + 'KB';
  return (n/1048576).toFixed(1) + 'MB';
}

function syntaxHighlight(json) {
  if (typeof json !== 'string') json = JSON.stringify(json, null, 2);
  json = json.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return json.replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+\.?\d*([eE][+-]?\d+)?)/g, function(m){
    let cls = 'json-num';
    if (/^"/.test(m)) cls = /:$/.test(m) ? 'json-key' : 'json-str';
    else if (/true|false/.test(m)) cls = 'json-bool';
    else if (/null/.test(m)) cls = 'json-null';
    return '<span class="'+cls+'">'+m+'</span>';
  });
}

function renderList() {
  const el = document.getElementById('req-list');
  if (reqList.length === 0) { el.innerHTML = '<div class="empty">等待请求中...</div>'; return; }
  el.innerHTML = reqList.map(r => `
    <div class="req-item ${r.id===currentId?'active':''}" onclick="selectReq('${r.id}')">
      <div class="top">
        <span class="model">${r.model||'(无model)'}</span>
        <span>${r.time_ms}</span>
      </div>
      <div class="meta">
        <span class="tag tag-${r.method.toLowerCase()}">${r.method}</span>
        <span class="tag ${r.stream?'tag-stream':'tag-nostream'}">${r.stream?'STREAM':'JSON'}</span>
        ${r.messages}条消息 · ${fmtSize(r.body_size)} · ${r.duration_ms}ms
      </div>
    </div>
  `).join('');
}

async function selectReq(id) {
  currentId = id;
  renderList();
  const resp = await fetch('/api/requests/'+id);
  const r = await resp.json();
  const el = document.getElementById('detail');

  // 构建消息列表
  let msgsHtml = '';
  if (r.body && r.body.messages) {
    msgsHtml = r.body.messages.map((m,i) => {
      let c = m.content;
      if (Array.isArray(c)) c = JSON.stringify(c, null, 2);
      else c = String(c);
      if (c.length > 5000) c = c.substring(0, 5000) + '\n... (截断, 共'+c.length+'字符)';
      return `<div class="msg-item">
        <div class="msg-role role-${m.role}">[${i}] ${m.role}${m.name?' · '+m.name:''}</div>
        <div class="msg-content">${escapeHtml(c)}</div>
      </div>`;
    }).join('');
  }

  // headers
  let hdrHtml = Object.entries(r.headers||{}).map(([k,v])=>`<div class="kv"><div class="k">${k}</div><div class="v">${escapeHtml(v)}</div></div>`).join('');

  // body 其他字段（非 messages）
  let otherFields = {};
  if (r.body) {
    for (let [k,v] of Object.entries(r.body)) {
      if (k !== 'messages') otherFields[k] = v;
    }
  }

  el.innerHTML = `
    <div class="detail-section">
      <h3>请求概览</h3>
      <div class="content">
        <div class="kv"><div class="k">ID</div><div class="v">${r.id}</div></div>
        <div class="kv"><div class="k">时间</div><div class="v">${r.timestamp_iso}</div></div>
        <div class="kv"><div class="k">路径</div><div class="v">${r.method} ${r.path}</div></div>
        <div class="kv"><div class="k">Model</div><div class="v">${r.model||'-'}</div></div>
        <div class="kv"><div class="k">Stream</div><div class="v">${r.stream}</div></div>
        <div class="kv"><div class="k">消息数</div><div class="v">${r.messages}</div></div>
        <div class="kv"><div class="k">Body大小</div><div class="v">${fmtSize(r.body_size)}</div></div>
        <div class="kv"><div class="k">耗时</div><div class="v">${r.duration_ms}ms</div></div>
        <div class="kv"><div class="k">返回内容</div><div class="v" style="color:#7ee787">${escapeHtml(r.response_preview)}</div></div>
        <div style="margin-top:12px;display:flex;gap:6px;flex-wrap:wrap">
          <button class="btn btn-export" onclick="downloadCurrent('markdown')">下载此记录(MD)</button>
          <button class="btn btn-export" onclick="downloadCurrent('slim')">下载此记录(精简JSON)</button>
          <button class="btn btn-export" onclick="downloadCurrent('json')">下载此记录(全量JSON)</button>
        </div>
      </div>
    </div>
    <div class="detail-section">
      <h3>Body 参数 ${r.is_json?'<span class="badge">JSON</span>':''}</h3>
      <div class="content"><pre>${syntaxHighlight(otherFields)}</pre></div>
    </div>
    <div class="detail-section">
      <h3>消息列表 (${r.messages}条)</h3>
      <div class="content">${msgsHtml||'<div class="empty">无消息</div>'}</div>
    </div>
    <div class="detail-section">
      <h3>请求头</h3>
      <div class="content">${hdrHtml}</div>
    </div>
    ${r.body_raw ? `<div class="detail-section"><h3>原始 Body (非JSON)</h3><div class="content"><pre>${escapeHtml(r.body_raw)}</pre></div></div>` : ''}
  `;
}

function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function updateStats(s) {
  if (!s) return;
  document.getElementById('s-total').textContent = s.total;
  document.getElementById('s-stream').textContent = s.stream;
  document.getElementById('s-nostream').textContent = s.non_stream;
}

async function loadAll() {
  const resp = await fetch('/api/requests');
  const data = await resp.json();
  reqList = data.requests;
  updateStats(data.stats);
  renderList();
}

function clearList() {
  reqList = [];
  renderList();
  document.getElementById('detail').innerHTML = '<div class="empty">点击左侧请求查看详情</div>';
}

function exportFormat(format) {
  window.open('/api/export?format=' + format, '_blank');
}

function exportHTML() {
  window.open('/api/export/html', '_blank');
}

function downloadCurrent(format) {
  if (!currentId) {
    alert('请先在左侧选择一条记录');
    return;
  }
  window.open('/api/requests/' + currentId + '/download?format=' + format, '_blank');
}

async function shutdownServer() {
  if (!confirm('确定关闭 Fake LLM Proxy 服务？')) return;
  try {
    await fetch('/shutdown', { method: 'POST' });
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;color:#8b949e;"><h2 style="color:#ff7b72">服务已关闭</h2><p style="margin-top:12px">Fake LLM Proxy 已停止运行</p></div>';
  } catch(e) {
    alert('服务已关闭');
  }
}

// SSE 实时订阅
const evt = new EventSource('/events');
evt.onmessage = function(e) {
  const msg = JSON.parse(e.data);
  if (msg.type === 'request') {
    reqList.unshift(msg.data);
    if (reqList.length > 200) reqList.pop();
    renderList();
    updateStats(msg.stats);
  } else if (msg.type === 'hello') {
    updateStats(msg.stats);
  }
};

loadAll();
</script>
</body>
</html>
"""

# ==================== 启动 ====================

if __name__ == "__main__":
    print("=" * 50)
    print("Fake LLM Proxy v2 启动中...")
    print(f"监控页: http://127.0.0.1:{PORT}/")
    print(f"对接:   base_url=http://127.0.0.1:{PORT}/v1  key=随便填")
    print(f"日志:   {_LOG_FILE}")
    print("关闭:   POST /shutdown 或网页按钮或 Ctrl+C")
    print("=" * 50)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
