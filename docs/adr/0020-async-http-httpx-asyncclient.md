# async HTTP 客户端选型 httpx.AsyncClient

后端 async 端点用同步 requests/urllib 阻塞事件循环 30-120s（batch 1 H1 + batch 4 + batch 7 H4 + batch 8 C1/C2/C3）。决策：后端统一 `httpx.AsyncClient` 单例（`lib/async_http.py`），Qt 面板统一 QThread + `httpx.Client`。httpx 原生 async + MockTransport 测试友好 + 与 FastAPI/OpenAI Client 同生态；requests 不支持 async，aiohttp 生态割裂。
