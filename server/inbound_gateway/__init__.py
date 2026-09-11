"""入站网关（Inbound Gateway）· 类 newapi 本地中转

OpenAI 兼容入站端点（/v1/*）+ 管理端点（/inbound/*）。

- spec: temp/sdd/inbound-gateway/spec.md
- Bounds: server/llm_pool/ 整棵子树只读；TTFT 在网关层计时
- 统计口径: 全部聚合由本包的 SQLite 表算出，不复用 pool.project_stats

注意：包级只导出 key 库相关，**不导出 router 属性**——否则 `router` 属性
（APIRouter 实例）会遮蔽同名子模块 `server.inbound_gateway.router`，
破坏 `import server.inbound_gateway.router` 的模块解析（实测踩坑）。
路由经 `from server.inbound_gateway.router import router` 获取。
"""

from server.inbound_gateway.key_store import KeyStore, get_key_store

__all__ = ["KeyStore", "get_key_store"]
