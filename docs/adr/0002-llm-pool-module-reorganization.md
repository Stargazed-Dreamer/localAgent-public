# llm_pool 模块重组：USE_CASE_REGISTRY 迁入 types.py + 删除 key_manager.py compat shim

`server/llm_pool/key_manager.py` 是 299 行的 compat shim，混合了转发函数、遗留格式解析和唯一真实实现（`_load_provider_policy`）。同时 `types.py` 的 `LLMKey.use_case_eligible()` 通过函数级 lazy import 引用 `key_store.USE_CASE_REGISTRY`，构成 types ↔ key_store 的循环依赖信号。本次重构把 `UseCaseDef` + `USE_CASE_REGISTRY` 迁入 `types.py`（在根源处打破循环），把 `key_manager.py` 的有用函数迁入 `key_store.py`，并删除 compat shim。

## Context

### 循环依赖结构

```
types.py  ──(lazy import USE_CASE_REGISTRY)──▶  key_store.py
   ▲                                                │
   └──────────────(import LLMKey, ProviderPolicy)───┘
```

`types.py:LLMKey.use_case_eligible()` 和 `pool.py:LLMPool._acquire()/call()` 都用 `from server.llm_pool.key_store import USE_CASE_REGISTRY`（函数内 lazy import）规避循环。这是 mattpocock `improve-codebase-architecture` skill 列出的"函数级 import 是循环依赖信号"典型反模式。

### key_manager.py 的内容分布

| 类别 | 行数 | 说明 |
|------|------|------|
| 纯转发 shim | ~50 | `load_provider_keys`/`check_keys_health`/`cleanup_expired_keys`/`should_run_health_check` 仅 wrap `key_store` 同名函数，参数 `keys_file` 完全未使用 |
| 遗留格式辅助 | ~150 | `_read_text_lines`/`_extract_key_records_from_file`/`_load_keys_records` 等，处理旧 mimo txt + v4 tested JSON 格式 |
| 唯一真实实现 | ~20 | `_load_provider_policy` 读取 `config.toml [llm.providers.<name>.pool]` 构造 `ProviderPolicy` |
| 死代码 | ~80 | `save_tested_keys`/`_save_keys_records`/`_backup_keys_file`/`_should_run_health_check_legacy`/`load_keys_from_tested_file`（v9 mimo provider 退役后 fallback 路径永不触发）+ `_TEST_MESSAGES`/`HEALTH_CHECK_MAX_FAILS` 常量 |

deletion test 验证：删除 compat shim 后复杂度集中在 `key_store.py`（已有 `check_health`/`cleanup_expired`/`should_run_health_check`/`load_llm_keys_for_pool` 真实实现），而非转移到新位置。

## Decision

1. **`UseCaseDef` + `USE_CASE_REGISTRY` + `TIER_NAMES` + `_LEGACY_TIER_MAP` + `SENSITIVE_USES` 迁入 `types.py`**：这些都是纯数据声明（frozen dataclass + dict literal + set comprehension），与 `LLMKey`/`ProviderPolicy` 同属"数据结构"层。`types.py` 不再 lazy import `key_store`。
2. **`key_store.py` 反向导入这些符号**：`from server.llm_pool.types import USE_CASE_REGISTRY, ...` 保持外部 `from server.llm_pool.key_store import USE_CASE_REGISTRY` 兼容（`apikey.py`、`tests/test_key_store.py` 等无需改动）。
3. **`key_manager.py` 有用函数迁入 `key_store.py` 末尾**：`_load_provider_policy`、`_get_status_dict`、`_load_keys_records`、`_read_text_lines`、`_extract_key_records_from_file`、`_collect_key_records_from_files`。这些与 `key_store._read_raw_keys_file`/`_load_keys_cached` 同属"key 文件读写"职责。
4. **`__init__.py` 改为从 `key_store` 导入**：用 `as` 别名保留 compat 名称（`load_provider_keys`/`check_keys_health`/`cleanup_expired_keys`），调用方可逐步切换到 `key_store` 原名，无需一次性改完全项目。
5. **删除死代码**：`_TEST_MESSAGES`、`HEALTH_CHECK_MAX_FAILS`（注：`key_store.py` 自己已有同名常量）、`save_tested_keys`、`_save_keys_records`、`_backup_keys_file`、`_should_run_health_check_legacy`、`load_keys_from_tested_file`。
6. **删除 `key_manager.py`**。

## Considered Options

- **A. 仅删 compat shim，保留 `_load_provider_policy` 在原文件**：否决，因为 `key_manager.py` 仅剩一个函数后变成"极浅模块"，且该函数与 `ProviderPolicy`（在 `types.py`）和 `key_store` 的 key 加载流程紧密耦合，留在孤立文件反而增加跳转成本。
- **B. 把 `USE_CASE_REGISTRY` 放到独立 `registry.py`**：否决，因为 `types.py` 已经是数据结构归属地，新增文件引入更多导入路径。
- **C. 把 `_load_provider_policy` 放到 `types.py`**：否决，因为 `types.py` 应保持纯数据，无 config 读取副作用。`key_store.py` 已经有 `_load_keys_cached` 等 config-aware 函数，是更自然的归属。

## Consequences

- **正向**：循环依赖在根源处消除，`types.py` 和 `pool.py` 不再有 lazy import；`key_manager.py` 299 行 compat shim 消失；外部 import 路径不变（`from server.llm_pool import ...` 仍可用）。
- **负向**：`key_store.py` 行数从 1257 增至 ~1340；`USE_CASE_REGISTRY` 的"物理位置"（`types.py`）与"逻辑归属"（use case 路由）分离，新读者可能短暂困惑，靠 `types.py` 顶部注释解释。
- **迁移路径**：调用方仍可使用 `from server.llm_pool import load_provider_keys`（compat 别名），但推荐逐步切换为 `from server.llm_pool.key_store import load_llm_keys_for_pool`，未来某次大清理时移除 `__init__.py` 中的 `as` 别名。
- **未来反转成本**：若需重新拆分（例如 `key_store.py` 过大需要再分），按已合并函数的 `# Provider policy + 旧格式文件辅助` 分隔注释即可切回，不需要重新设计边界。
