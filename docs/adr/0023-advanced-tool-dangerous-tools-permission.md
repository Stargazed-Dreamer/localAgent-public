# 网关端点级权限检查清单 DANGEROUS_TOOLS

`localagent_advanced_tool` 网关对 116 工具直接路由，对 `apikey_set_key`/`memory_delete` 等敏感端点缺二级权限（batch 7 H2）；网关是内部函数调用不经过 HTTP 中间件。决策：维护 `DANGEROUS_TOOLS` 集合，网关调用前查 `classify_safety` → `approval_level=confirm` 的强制走 `command_guard` 审批，其他默认放行。介于"全部强制审批"（用户体验差）和"不动靠中间件"（中间件不拦内部调用）之间。
