# lib/secret 密钥读取统一中转层

密钥原本散落在 `config.toml`（tushare_token / github_token）、`data/llm/keys.json`（LLM keys）等处，且路径 `"data/llm/keys.json"` 硬编码在 server/ client/ tools/ 10+ 文件中。2026-08-06 建立 `lib/secret` 作为项目级密钥读取中转层：路径常量（`get_llm_keys_path()` / `get_secrets_toml_path()` / `get_config_path()`）作为单一真源，非 LLM 密钥集中存到 `data/secret/secrets.toml` `[tokens]` 段由 `get_secret(key)` 读取，保留 `server/llm_pool/key_store.py` 成熟实现委托路径获取（不重写）。

## Considered Options

- **选项 A（被拒绝）**：强制迁移所有密钥物理位置，重写 `key_store.py` 为 `lib/secret` 的一部分。拒绝理由：`key_store.py` 是经过多轮测试和生产的成熟实现（v8 tier 系统 + 健康检查 + 并发池），重写风险大且无增量价值；`lib/secret` 只需作为路径真源 + 非 LLM 密钥读取层即可消除硬编码。
- **选项 B（被拒绝）**：继续在 `config.toml` 中存非 LLM token。拒绝理由：明文存储 + 散落各处 + 路径硬编码，且 `config.toml` 进 release 包（friend-full profile）会泄露密钥。
- **选项 C（采纳）**：`lib/secret` 作为门面层，路径真源 + 非 LLM 密钥读取，`key_store.py` 委托 `lib.secret.get_llm_keys_path()` 获取路径。`data/secret/secrets.toml` 被 `.gitignore` 的 `data/*` 规则覆盖，不进 release。

## Consequences

- 两套密钥读取体系并存：LLM 密钥经 `key_store.py` 加载（路径来自 `lib/secret`），非 LLM 密钥经 `lib.secret.get_secret()` 读取。这是**有意的权衡**——避免重写成熟代码，同时统一路径真源。
- 未来加密层（v2）只需扩展 `lib/secret`（如 `encrypt()` / `decrypt()`），不需要改消费方。
- 项目规则（`.agents/rules/project_rules.md` + `AGENTS.md`）已更新"密钥统一管理"约束：禁止密钥硬编码到代码或 `config.toml`，新增密钥必须存到 `secrets.toml` 或 `keys.json`，读取必须经过 `lib/secret` 中转。
- `tools/migrate_secrets.py` 负责将旧密钥从 `config.toml` 迁移到 `secrets.toml`（幂等 + `--dry-run`），迁移后 `config.toml` 不再存任何密钥。

## References

- [lib/secret/__init__.py](file:///<project_root>/lib/secret/__init__.py)
- [lib/secret/paths.py](file:///<project_root>/lib/secret/paths.py)
- [lib/secret/reader.py](file:///<project_root>/lib/secret/reader.py)
- [server/llm_pool/key_store.py](file:///<project_root>/server/llm_pool/key_store.py)（`_get_unified_keys_path` 委托 `lib.secret.get_llm_keys_path`）
- [tools/migrate_secrets.py](file:///<project_root>/tools/migrate_secrets.py)
- [SECURITY-RISKS.md](file:///<project_root>/SECURITY-RISKS.md)（第 7-13 项：lib/secret 相关风险）
- [temp/sdd/lib-secret/spec.md](file:///<project_root>/temp/sdd/lib-secret/spec.md)（SDD 流程产物，gitignored）
- [.agents/rules/project_rules.md](file:///<project_root>/.agents/rules/project_rules.md) "密钥统一管理"段
