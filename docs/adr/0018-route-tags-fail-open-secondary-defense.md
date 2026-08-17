# route_tags 保持 fail-open + 二级防护

`_is_agent_callable` 默认 True（新端点自动暴露给 agent），与 docstring 早期声明的 fail-closed 矛盾，code review 建议反转。决策保持 fail-open：加 MCP 端点的目的就是给 agent 用，fail-closed 会让每个新端点额外声明才能暴露。安全防护靠 `classify_safety → approval_level → HTTP 中间件拦截` 三层递进，不靠 `x-agent-callable` 标志；auto_shutdown 等危险端点已通过 T04 加 `dry_run=True` 默认 + 看门狗权限兜底。
