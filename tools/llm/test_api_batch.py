"""批量测试 OpenAI 兼容 API 端点的可用性

测试内容：
  1. GET /models        — 列出可用模型
  2. POST /chat/completions — 最小 chat 调用，验证推理可用 + 计时
  3. 可选：并发压测 N 次看是否触发限流

结果保存到 workspace/api_tests/result_<timestamp>.json，便于后续工作流读取。

用法:
  uv run python tools/llm/test_api_batch.py                    # 测试默认端点
  uv run python tools/llm/test_api_batch.py --stress 20        # 并发压测20次
  uv run python tools/llm/test_api_batch.py --endpoints my.json # 自定义端点文件

安全说明:
  本文件曾硬编码真实 API key（已通过 git 历史泄露，相关 key 应已轮换）。
  现改为从环境变量或 data/llm/keys/extra_llm_keys.json 读取，DEFAULT_ENDPOINTS
  只保留 base_url 和 models 模板，api_key 字段留空，运行时按 name 匹配注入。
"""
import json
import os
import time
import argparse
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

OUTPUT_DIR = Path(__file__).parent.parent.parent / "workspace" / "api_tests"

# 默认待测端点模板（api_key 运行时从环境变量或 key 文件注入）
DEFAULT_ENDPOINTS = [
    {
        "name": "mimo-official",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "api_key": "",  # 从环境变量 MIMO_OFFICIAL_API_KEY 或 extra_llm_keys.json 读取
        "models": ["mimo-v2.5", "mimo-v2.5-pro"],
    },
    {
        "name": "tdzhywsy-aggregator",
        "base_url": "https://ai.tdzhywsy.shop/v1",
        "api_key": "",  # 从环境变量 TDZHYWSY_API_KEY 或 extra_llm_keys.json 读取
        "models": ["mimo-v2.5", "mimo-v2.5-pro"],
    },
    {
        "name": "endfield-aggregator",
        "base_url": "https://endfield.site:302/v1",
        "api_key": "",  # 从环境变量 ENDFIELD_API_KEY 或 extra_llm_keys.json 读取
        "models": ["mimo-v2.5", "mimo-v2.5-pro"],
    },
]

# key 文件路径（运行时按 base_url 匹配注入 api_key）
_KEY_FILE = Path(__file__).parent.parent.parent / "data" / "llm" / "keys" / "extra_llm_keys.json"


def _load_keys_by_base_url() -> dict:
    """从 data/llm/keys/extra_llm_keys.json 加载 {base_url: api_key} 映射"""
    if not _KEY_FILE.exists():
        return {}
    try:
        entries = json.loads(_KEY_FILE.read_text(encoding="utf-8"))
        return {e.get("base_url", ""): e.get("key", "") for e in entries if e.get("base_url") and e.get("key")}
    except Exception:
        return {}


def _resolve_api_key(ep: dict) -> str:
    """按优先级解析 api_key：环境变量 {NAME}_API_KEY > key 文件按 base_url 匹配 > ep['api_key']"""
    env_var = f"{ep['name'].upper().replace('-', '_')}_API_KEY"
    env_key = os.environ.get(env_var, "")
    if env_key:
        return env_key
    keys_map = _load_keys_by_base_url()
    matched = keys_map.get(ep.get("base_url", ""))
    if matched:
        return matched
    return ep.get("api_key", "")


def now_iso() -> str:
    return datetime.now().isoformat()


def now_filename() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def test_list_models(base_url: str, api_key: str, timeout: int = 30) -> dict:
    """测试 GET /models"""
    url = base_url.rstrip("/") + "/models"
    t0 = time.perf_counter()
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        elapsed = round((time.perf_counter() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            ids = [m.get("id") for m in data.get("data", [])]
            return {"ok": True, "status": 200, "elapsed_ms": elapsed, "models": ids}
        return {"ok": False, "status": resp.status_code, "elapsed_ms": elapsed, "error": resp.text[:300]}
    except Exception as e:
        return {"ok": False, "status": -1, "elapsed_ms": round((time.perf_counter() - t0) * 1000), "error": str(e)[:300]}


def test_chat(base_url: str, api_key: str, model: str, timeout: int = 60) -> dict:
    """测试 POST /chat/completions 最小调用"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "用一句话回答：1+1等于几？只输出数字。"}],
        "max_tokens": 30,
        "temperature": 0,
    }
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        elapsed = round((time.perf_counter() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            finish = data.get("choices", [{}])[0].get("finish_reason", "")
            return {
                "ok": True, "status": 200, "elapsed_ms": elapsed,
                "model": data.get("model", model),
                "content": content[:200],
                "finish_reason": finish,
                "usage": usage,
            }
        return {"ok": False, "status": resp.status_code, "elapsed_ms": elapsed, "error": resp.text[:300]}
    except Exception as e:
        return {"ok": False, "status": -1, "elapsed_ms": round((time.perf_counter() - t0) * 1000), "error": str(e)[:300]}


def stress_test(base_url: str, api_key: str, model: str, n: int, concurrency: int = 7) -> dict:
    """并发压测 n 次，统计成功/失败/限流"""
    results = []
    lock = threading.Lock()

    def _one(_):
        r = test_chat(base_url, api_key, model, timeout=60)
        with lock:
            results.append(r)
        return r

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(_one, range(n)))
    total_elapsed = round((time.perf_counter() - t0) * 1000)

    ok = sum(1 for r in results if r.get("ok"))
    rate_limited = sum(1 for r in results if r.get("status") == 429)
    errors = sum(1 for r in results if not r.get("ok") and r.get("status") != 429)
    latencies = [r["elapsed_ms"] for r in results if r.get("ok")]

    return {
        "n": n, "concurrency": concurrency,
        "success": ok, "rate_limited": rate_limited, "other_errors": errors,
        "total_elapsed_ms": total_elapsed,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0,
        "max_latency_ms": max(latencies) if latencies else 0,
        "min_latency_ms": min(latencies) if latencies else 0,
        "sample_error": next((r.get("error") for r in results if not r.get("ok")), None),
    }


def main():
    parser = argparse.ArgumentParser(description="批量 API 可用性测试")
    parser.add_argument("--endpoints", type=str, default=None,
                        help="自定义端点 JSON 文件路径（不传则用内置默认）")
    parser.add_argument("--stress", type=int, default=0,
                        help="并发压测次数（0=不压测），默认并发7")
    parser.add_argument("--stress-concurrency", type=int, default=7,
                        help="压测并发数（默认7）")
    parser.add_argument("--stress-model", type=str, default="mimo-v2.5",
                        help="压测使用的模型")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.endpoints:
        endpoints = json.loads(Path(args.endpoints).read_text(encoding="utf-8"))
    else:
        endpoints = DEFAULT_ENDPOINTS

    print(f"=== 批量 API 可用性测试 ===")
    print(f"端点数: {len(endpoints)}")
    print(f"时间: {now_iso()}\n")

    report = {
        "generated_at": now_iso(),
        "endpoints": [],
    }

    for ep in endpoints:
        name = ep["name"]
        base_url = ep["base_url"]
        api_key = _resolve_api_key(ep)
        if not api_key:
            print(f"--- [{name}] {base_url} ---")
            print(f"  跳过：未配置 api_key（设置环境变量 {name.upper().replace('-', '_')}_API_KEY 或在 data/llm/keys/extra_llm_keys.json 中按 base_url 配置）\n")
            continue
        models_to_test = ep.get("models", [])

        print(f"--- [{name}] {base_url} ---")

        # 1. list models
        lm = test_list_models(base_url, api_key)
        print(f"  list_models: {'OK' if lm['ok'] else 'FAIL'} ({lm['elapsed_ms']}ms)")
        if lm["ok"]:
            print(f"    可用模型({len(lm['models'])}): {', '.join(lm['models'][:10])}{'...' if len(lm['models'])>10 else ''}")

        # 2. chat 测试每个声明模型
        chat_results = {}
        for m in models_to_test:
            cr = test_chat(base_url, api_key, m)
            chat_results[m] = cr
            if cr["ok"]:
                print(f"  chat[{m}]: OK ({cr['elapsed_ms']}ms) → {cr['content'][:60]}")
            else:
                print(f"  chat[{m}]: FAIL status={cr['status']} ({cr['elapsed_ms']}ms) {cr.get('error','')[:80]}")

        # 3. 压测
        stress = None
        if args.stress > 0:
            stress_model = args.stress_model
            # 选第一个可用的模型压测
            if not chat_results.get(stress_model, {}).get("ok"):
                for m, cr in chat_results.items():
                    if cr.get("ok"):
                        stress_model = m
                        break
            print(f"  压测: {args.stress}次 并发{args.stress_concurrency} 模型{stress_model} ...")
            stress = stress_test(base_url, api_key, stress_model, args.stress, args.stress_concurrency)
            print(f"    成功{stress['success']}/{stress['n']} 限流{stress['rate_limited']} 错误{stress['other_errors']}")
            print(f"    总耗时{stress['total_elapsed_ms']}ms 平均{stress['avg_latency_ms']}ms")

        ep_report = {
            "name": name,
            "base_url": base_url,
            "list_models": lm,
            "chat_tests": chat_results,
            "stress_test": stress,
        }
        report["endpoints"].append(ep_report)
        print()

    # 保存结果
    ts = now_filename()
    result_file = OUTPUT_DIR / f"result_{ts}.json"
    result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== 结果已保存: {result_file} ===")

    # 同时保存一份 latest.json 供下游脚本读取
    latest_file = OUTPUT_DIR / "latest.json"
    latest_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== 最新结果副本: {latest_file} ===")

    # 汇总
    print(f"\n=== 汇总 ===")
    for ep in report["endpoints"]:
        name = ep["name"]
        lm_ok = ep["list_models"]["ok"]
        chat_ok = {m: r["ok"] for m, r in ep["chat_tests"].items()}
        any_chat_ok = any(chat_ok.values())
        status = "可用" if any_chat_ok else "不可用"
        print(f"  [{status}] {name}: list_models={'OK' if lm_ok else 'FAIL'} chat={chat_ok}")


if __name__ == "__main__":
    main()
