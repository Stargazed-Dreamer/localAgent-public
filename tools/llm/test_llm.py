"""快速测试 LLM 池是否可用"""
import requests, json
payload = {
    "messages": [{"role": "user", "content": "回复OK"}],
    "temperature": 0.1, "max_tokens": 10, "timeout": 30,
    "retries": 2, "project": "test",
}
r = requests.post("http://127.0.0.1:8766/llm/pool/call", json=payload, timeout=60)
print(f"HTTP {r.status_code}")
d = r.json()
print(f"ok={d.get('ok')} error={d.get('error','')[:100]}")
if d.get("ok"):
    print(f"content={d.get('content','')[:50]}")
