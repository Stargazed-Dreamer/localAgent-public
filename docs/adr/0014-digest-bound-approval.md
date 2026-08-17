# digest 绑定 > 人工审批（fail closed + digest-bound approval）

release engine 的审批必须 digest-bound：审批凭据携带 `plan_digest`，digest 不匹配的审批无效，digest 匹配的审批才生效。digest 冲突时 fail closed——宁可拒绝构建也不可放过可疑产物。digest 算法用 SHA-256（标准库 `hashlib`，不引入新依赖）。

## Context

重构前 `friend-full.toml` 的审批字段是 `review_scan_cleared_at = "2026-07-30T21:30:00+08:00"`——一个可手动修改的 ISO 时间戳。用户报告明确指出：

> "审批只是 profile 中的可修改时间戳，没有绑定源码摘要 / profile 摘要 / 组件选择 / 扫描结果 / 豁免内容"

这意味着：

1. 攻击者（或粗心开发者）修改 toml 中的时间戳即可"通过审批"。
2. 审批通过后源码、profile、组件选择、扫描结果、豁免内容任何一项变化，审批依然有效——审计和构建消费的是不同时间点的状态。
3. 没有任何机制保证"用户审批时看到的状态 = 实际打包的状态"。

LocalAgent 的 release 产物会分发给朋友（friend-full profile 含私有数据）或公开发布（public profile），一次错误发布不可撤回（朋友拿到含凭据的 ZIP 后无法远程擦除），因此 release 审批是"高代价不可逆操作"，必须有密码学强度的状态绑定。

## Decision

**digest-bound approval**：审批凭据是独立的 `Approval` dataclass，必须携带 `plan_digest` 字段；`build_release()` 接受 `(PreparedRelease, Approval)` 后第一步校验 `approval.plan_digest == prepared.plan_digest`，不匹配直接抛错拒绝构建。

```python
# tools/release/engine/models.py
@dataclass(frozen=True)
class Approval:
    """审批凭据（plan_digest 必须与 PreparedRelease.plan_digest 匹配）"""
    plan_digest: str
    approved_at: str
    approved_by: str
    signature: str | None = None  # phase 2 不实现签名验证，保留字段
```

`PreparedRelease.plan_digest` 由 4 个子 digest 派生：`profile_digest`（profile 文件 bytes 的 SHA-256）+ `source_commit`（git commit sha）+ `components_digest`（组件选择）+ `scan_digest`（扫描结果）。任何输入变化让 `plan_digest` 变化，使旧 approval 自动失效，强制重新审批。

**fail closed > false positive**：digest 不匹配时 `build_release()` 抛错拒绝构建，宁可拒绝也不可放过。SessionManager 不可用、digest 计算异常、approval 字段缺失等所有异常路径都走 fail-closed 分支。

**digest 算法 = SHA-256**：

```python
# tools/release/engine/digest.py
import hashlib

def digest_obj(obj) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()

def digest_file(profile_path: Path) -> str:
    return hashlib.sha256(profile_path.read_bytes()).hexdigest()
```

用标准库 `hashlib`，不引入 BLAKE3 等新依赖（符合项目"不擅自安装额外库"硬约束）。

## Considered Options

1. **保留可手动修改的 ISO 时间戳**——被拒绝：用户报告直接指出此问题（"审批只是 profile 中的可修改时间戳，没有绑定源码摘要 / profile 摘要 / 组件选择 / 扫描结果 / 豁免内容"）；时间戳可被任意修改且不绑定任何状态，等于无审批。
2. **BLAKE3 digest**——被拒绝：BLAKE3 比 SHA-256 更快，但需新依赖 `blake3` 包；release 计划数量级是每周个位数，digest 性能不是瓶颈；SHA-256 标准库 `hashlib` 即可，符合"不擅自安装额外库"。
3. **只 digest 绑定不 fail closed（digest 不匹配时 warning 但继续构建）**——被拒绝：warning 在自动化流程中容易被忽略，等于没有防护；release 是不可逆操作，digest 不匹配必然意味着状态分叉，必须 fail closed。
4. **digest-bound approval + fail closed + SHA-256（本决策）**——采用：密码学强度状态绑定 + 异常路径全部拒绝构建 + 零新依赖。

## Consequences

**正面**：
- 审批从"可修改时间戳"升级为"密码学状态绑定"，攻击者修改 toml 时间戳无效。
- 任何输入变化（源码 / profile / 组件 / 扫描结果 / 豁免）自动使旧 approval 失效，强制重新审批。
- fail-closed 让所有异常路径（digest 计算失败 / SessionManager 不可用 / approval 字段缺失）都走"拒绝构建"，符合 release 不可逆操作的保守默认。
- SHA-256 标准库实现，零新依赖，跨平台稳定。

**负面**：
- 开发期频繁修改源码会导致每次 `prepare_release()` 产出新 `plan_digest`，旧 approval 失效需重新审批——开发体验略差，但可通过"开发模式跳过 approval"配置缓解（生产 release 仍强制审批）。
- `Approval.signature` 字段保留但 phase 2 不实现验证——当前 approval 仍是"携带 digest 的 toml 字段"，攻击者若能写 toml 仍可伪造 approval（但 `plan_digest` 必须匹配，伪造难度从"改时间戳"升级为"匹配 digest"）。

**回退路径**：若未来需要更强的审批凭据，可在 `Approval` 上实现真实签名验证（`signature` 字段已保留），用 GPG / age / SSH key 签名 `plan_digest`。本决策的 digest-bound + fail-closed 是签名验证的前置基础，不会因引入签名而被废弃。

## References

- SDD 来源：`temp/sdd/release-engine/design-decisions.md` 四.2（fail closed > false positive）+ 四.3（digest 绑定 > 人工审批）+ 三.6（SHA-256 而非 BLAKE3）+ 二.5（审批字段现状）
- 关键代码：
  - [tools/release/engine/models.py](file:///f:/<project_root>/tools/release/engine/models.py)（`PreparedRelease.plan_digest` L68 + `Approval.plan_digest` L123，匹配校验注释 L122）
  - [tools/release/engine/digest.py](file:///f:/<project_root>/tools/release/engine/digest.py)（`hashlib.sha256` 实现，L8 import / L23 obj digest / L32 file digest）
  - [tools/release/engine/build.py](file:///f:/<project_root>/tools/release/engine/build.py)（`build_release()` 校验 digest 匹配，fail-closed 分支）
- 相关 ADR：ADR-0013（release engine 编译器式架构，本决策依赖 `PreparedRelease` 不可变性）
