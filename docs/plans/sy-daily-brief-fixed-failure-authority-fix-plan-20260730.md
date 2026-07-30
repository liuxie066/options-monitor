# sy Daily Brief Fixed Failure Authority 修复计划

> 2026-07-30 revision：根据 `docs/reviews/plan-review-20260730-115457.md` 收敛方案。当前 work unit 只恢复受限、不可行动的 `fixed_failure` 投递；不把 portfolio identity receipt 当成正常 v1 报告完整性的证明，不在本 work unit 开放正常 v1 degraded fallback。

## 1. 目标

修复 scheduled fixed run 中以下用户可见故障：

```text
当前 Account Run 缺少成功的 position_advice_sources.v2.json
  -> Daily Brief normal authority 不可用
  -> domain 已选择 fixed_failure
  -> application 用 notification_allowed=false 覆盖为 none
  -> delivery_key=null、无 provider attempt、账户完全无消息
```

完成后必须满足：

1. 当前 run 存在唯一、fresh、immutable、账户/市场/run 一致的 portfolio source receipt，且 Position Advice authority 可解析时，即使正常 Daily Brief 不可靠，scheduled fixed run 仍可准备并投递一条不可行动的 `fixed_failure`；
2. 缺少成功 PA summary 时绝不生成 normal `fixed_report` 或 `candidate_alert`，不声称 lifecycle human-review、候选、现金容量或备兑能力完整；
3. receipt 缺失、重复、stale、path 越界、schema/hash/payload/run/account/market/identity 不一致，或 authority conflict 时保持 no-send；
4. 已有成功 summary 的正常 v1、v2_shadow、v2 报告路径保持原语义；
5. fixed failure 继续使用现有 plain-text renderer、scan-failure source、delivery repository、exact retry 和 notification authority 执行边界；
6. `lx` 与 `sy` 独立解析和投递，任一账户失败不改变另一账户的 token、delivery key 或 provider attempt；
7. 不发送真实通知，不修改生产 config，不自动补发历史消息。

本计划的成功定义是“sy 不再无声失败，而是收到明确的本轮失败消息”，不是“本轮恢复正常策略报告”。

## 2. 已确认事实与根因边界

### 2.1 直接事实链

- `publish_account_run_sources()` 在 candidate capture、FX、option-position fingerprint、cash capacity 和 share coverage 校验前，先发布 immutable portfolio source receipt；
- 任一后续 source graph 失败都会使成功态 `position_advice_sources.v2.json` 缺失；
- `_daily_brief_advice_authority()` 当前把该缺失映射为 normal notification authority 不可用；
- `decide_daily_brief_notification()` 已在 unreliable + fixed due 时返回 `fixed_failure`；
- `tick_notification_flow.py` 随后用 `notification_allowed=false` 把该决定覆盖为 `none`；
- fixed-failure repository 已约束其 source 必须是 `scan_failure`、消息必须是 plain text、candidate identities 必须为空；
- notification executor 会在发送锁内重新解析 authority，并把 delivery key、source digest、message hash 与 transport idempotency key 记录到 authority receipt。

### 2.2 本次不再采用的假设

以下推理被明确禁止：

```text
portfolio identity receipt valid
  != PA source graph complete
  != normal v1 report reliable
  != lifecycle human-review complete
```

因此，不论缺失 summary 的具体后续原因是 ordinary candidate coverage gap，还是 FX、账本、指纹、容量、receipt integrity 或未知异常，本 work unit 都只允许 fixed failure。typed source outcome 与 normal v1 degraded allowlist 留给独立后续 work unit。

## 3. 范围与非目标

### 3.1 本 work unit 包含

- 当前 Account Run portfolio identity evidence 的只读、安全解析；
- 正常投递授权与 fixed-failure 投递授权分离；
- purpose-restricted fixed-failure token v2 与现有 normal token v1 / 旧 envelope exact-retry 兼容；
- Daily Brief domain decision 接收两个通用 permission 输入；
- 删除 application 层对 domain `fixed_failure` 的无条件覆盖；
- fixed-failure token 的 action-specific 选择与 exact envelope 绑定；
- pending/confirmed/ambiguous、exact retry、账户隔离和 mixed old/new envelope 回归测试；
- 结构化审计字段和 blocker/reason code。

### 3.2 本 work unit 不包含

- 不改变 candidate capture completeness；
- 不增加 PA source graph 的降级成功态；
- 不修改成功态 `position_advice_sources.v2.json` schema；
- 不新建 typed PA source outcome artifact；
- 不复用历史 run 的 PA summary、portfolio receipt 或 lifecycle rows；
- 不从 ledger 平行推导 lifecycle human-review；
- 不修改 fixed-failure 用户文案、通知渠道、scheduler 或 provider retry；
- 不改变 authority policy、generation、promotion 或 first-use bootstrap；
- 不处理 OpenD `EMPTY_CHAIN` 的上游数据质量；
- 不 commit、release、upgrade、重启服务或发送真实通知。

## 4. 核心设计

### 4.1 身份 evidence 使用窄只读边界

新增：

```text
src/application/position_advice_account_identity_reader.py
```

唯一公开函数：

```python
read_current_run_portfolio_identity(
    *,
    account_state_dir: Path,
    account_run_id: str,
    expected_account: str,
    expected_market: str,
    now: datetime,
) -> dict[str, Any]
```

该 reader：

1. 只在当前 `account_state_dir` 下定位：

   ```text
   position_advice_producers/portfolio/
     <sha256({"producer_run_id": account_run_id})>/
     <payload_hash>/receipt.json
   ```

2. 要求该 run-key 目录下恰好一个 regular-file receipt；0 个、多个、symlink 或非 regular file 都失败；
3. receipt bytes 前后各读一次，变化则失败；
4. 使用 `validate_source_receipt()` 校验：
   - `source_kind=portfolio`
   - expected account
   - expected producer account run id
   - freshness
   - payload path containment
   - receipt schema、snapshot id、payload hash；
5. 要求 receipt 路径与 `payload_relpath` 都位于同一个 expected run-key / content-hash 目录，文件名分别固定为 `receipt.json` 与 `payload.json`；
6. payload bytes 前后各读一次，并重新校验 raw SHA-256；变化或与 `validate_source_receipt()` 返回的 hash 不同则失败；
7. 解析这组已稳定验证的 payload bytes，要求 `schema_version=position_advice_portfolio_source.v1`，并要求目录 content hash 等于 payload canonical SHA-256；
8. 从 payload 的 `normalized_portfolio_source` 与非空 list `portfolio_context.source_account_identifiers` 重新计算 `portfolio_account_identity_hash()`；
9. 重算值必须等于 receipt 中的 identity hash；
10. receipt `included_markets` 必须包含 expected market；
11. 返回 JSON-safe typed evidence：

   ```json
   {
     "status": "available",
     "normalized_account": "sy",
     "normalized_portfolio_source": "futu",
     "portfolio_account_identity_hash": "<sha256>",
     "producer_account_run_id": "<run_id>",
     "included_markets": ["HK"],
     "snapshot_id": "<sha256>",
     "receipt_hash": "<sha256>",
     "payload_sha256": "<sha256>",
     "source_observed_at": "<utc>",
     "expires_at": "<utc>"
   }
   ```

reader 不得：

- 导入或调用 `position_advice_account_sources.py` publisher；
- 打开 ledger；
- 扫描其他 run；
- 选择“最新”receipt；
- 修复字段或忽略额外 receipt；
- 写任何 artifact。

通用 receipt/path/hash 校验继续归 `position_advice_source_receipts.py`；新 reader 只负责 current-run portfolio identity 的组合约束。

### 4.2 Normal advice authority 保持 fail-closed

`_daily_brief_advice_authority()` 的 missing-summary 分支改为确定性结果：

```json
{
  "mode": "authority_conflict",
  "available": false,
  "blocker": "position_advice_source_summary_missing",
  "rows": [],
  "human_review_rows": [],
  "preview": {}
}
```

删除“没有历史目录时默认 v1 normal available”的捷径，也不再用历史目录是否非空判断当前 run 身份。

这保证：

- 正常 actions/positions 不会因 identity receipt fallback 被恢复；
- lifecycle human-review completeness 不会被默认为 true；
- `status/actionability` 继续 blocked；
- normal fixed report 与 candidate alert 均不可授权。

### 4.3 独立构造 fixed-failure authority

`_daily_brief_notification_authority()` 接收两类证据：

```python
_daily_brief_notification_authority(
    advice_authority,
    *,
    current_run_identity_evidence,
    account,
    account_run_id,
)
```

返回契约：

```json
{
  "selected_advice_contract": null,
  "resolved_mode": "v1",
  "authority_generation": 3,
  "authority_policy_hash": "<sha256>",
  "normal_delivery_allowed": false,
  "fixed_failure_delivery_allowed": true,
  "notification_allowed": false,
  "blocker": "position_advice_source_summary_missing",
  "normal_delivery_token": null,
  "fixed_failure_delivery_token": {
    "schema_version": "position_advice_notification_authority_token.v2",
    "authorized_delivery_kinds": ["fixed_failure"]
  },
  "token": null,
  "identity_evidence": {
    "status": "available",
    "snapshot_id": "<sha256>",
    "receipt_hash": "<sha256>"
  }
}
```

兼容规则：

- `notification_allowed` 保留为 `normal_delivery_allowed` 的 legacy alias；
- `token` 保留为 `normal_delivery_token` 的 legacy alias；
- fixed failure 使用独立字段 `fixed_failure_delivery_token`，不改变旧调用方对 `notification_allowed=false -> token=null` 的理解；
- brief 只保存 receipt/snapshot hash 等审计摘要，不复制 portfolio payload 或账户标识明文。

fixed-failure authority 的构造步骤：

1. 读取 current-run identity evidence；
2. 用 evidence 中的 source + identity 调用 `read_authority_resolution()`；
3. 仅 `resolution.notifications_allowed=true` 时继续；
4. authority mode 到 formal advice contract 的映射固定为：
   - `v1 -> v1`
   - `v2_shadow -> v1`
   - `v2 -> v2`
5. 使用新增的 `build_fixed_failure_notification_authority_token()` 构造 purpose-restricted v2 token；token 代表“该账户/run 当前 formal authority 已解析，并且只允许 fixed failure”，不代表正常报告数据完整；
6. conflict、异常、identity evidence unavailable 时 `fixed_failure_delivery_allowed=false` 且 token 为空。

新的 failure-only token 契约：

```json
{
  "schema_version": "position_advice_notification_authority_token.v2",
  "normalized_account": "sy",
  "portfolio_scope_id": "<sha256>",
  "normalized_portfolio_source": "futu",
  "portfolio_account_identity_hash": "<sha256>",
  "selected_advice_contract": "v1",
  "resolved_mode": "v1",
  "authority_generation": 3,
  "authority_policy_hash": "<sha256>",
  "account_run_id": "<run_id>",
  "authorized_delivery_kinds": ["fixed_failure"],
  "token_hash": "<sha256>"
}
```

约束：

- 现有 `build_notification_authority_token()` 与 `position_advice_notification_authority_token.v1` 保持不变，继续只服务 normal `fixed_report` / `candidate_alert`；
- 新增 `NOTIFICATION_AUTHORITY_FAILURE_TOKEN_SCHEMA_V2` 与窄 builder，只生成 `authorized_delivery_kinds=["fixed_failure"]`；
- v2 的 `authorized_delivery_kinds` 必须精确等于 canonical `["fixed_failure"]`；
- token hash 必须包含完整 `authorized_delivery_kinds`；
- 不允许调用方传入任意 kind、空 list、normal kind 或混合 kind；
- 成功 PA summary 路径按现有 advice availability 继续构造 normal v1 token；
- identity 与 authority 已解析时可以同时构造 normal v1 token 与 fixed-failure v2 token；
- domain 在 normal report 可靠时优先选择 normal action。

### 4.4 Domain 拥有最终 action matrix

扩展：

```python
decide_daily_brief_notification(
    *,
    ran_scan: bool,
    pipeline_reliable: bool,
    fixed_due: bool,
    pending_candidate_identities: Sequence[str],
    normal_delivery_allowed: bool,
    fixed_failure_delivery_allowed: bool,
    retryable_envelope_kind: str | None = None,
) -> dict[str, Any]
```

两个 permission 都是必传参数，不提供 permissive default；所有生产调用点和 domain tests 必须显式给值。

状态矩阵：

| 条件 | 结果 |
|---|---|
| `ran_scan=false` 且有 retryable envelope | `retry_exact`，继续使用 envelope 内原 token |
| `ran_scan=false` 且无 retry | `none` |
| `fixed_due=true`、pipeline reliable、normal allowed | `fixed_report` |
| `fixed_due=true`、pipeline unreliable、failure allowed | `fixed_failure` |
| `fixed_due=true`、pipeline reliable、normal denied、failure allowed | `fixed_failure` |
| `fixed_due=true` 且 failure denied、normal 不可用 | `none` |
| 非 fixed、pipeline reliable、normal allowed、有 pending candidates | `candidate_alert` |
| 非 fixed 且 pipeline/normal 任一不可用 | `none` |

reason code 分别使用：

```text
fixed_report_due
fixed_scan_failed
fixed_normal_authority_unavailable
fixed_failure_authority_unavailable
pending_candidates
normal_authority_unavailable
retryable_envelope
```

`tick_notification_flow.py` 删除当前 `if not authority_allows_notification: decision=none` 的事后覆盖。application 只把事实布尔值传给 domain，不再二次改写 action。

application 的准备顺序固定为：

1. `pipeline_reliable` 只表示 scan/brief 数据链本身可靠，不包含 notification authority；
2. 计算 `normal_report_reliable = pipeline_reliable and normal_delivery_allowed`；
3. `normal_report_reliable=true` 时才持久化 successful brief 并计算 pending candidates；
4. `normal_report_reliable=false` 时写当前 run 的 failure artifact，供可能的 `fixed_failure` envelope 绑定；
5. 将原始 `pipeline_reliable`、两个 permission 和 pending candidates 交给 domain；
6. domain 返回 normal action 时必须已有 successful brief；返回 `fixed_failure` 时必须已有 failure artifact；断言不成立则 fail closed，不临时拼 source。

### 4.5 Action-specific token 选择

application 按 domain action 选择 token：

| action | 必须使用 |
|---|---|
| `fixed_report` / `candidate_alert` | `normal_delivery_token` |
| `fixed_failure` | `fixed_failure_delivery_token` |
| `retry_exact` | 已持久化 envelope 的 `notification_authority_token` |
| `none` | 不准备 envelope |

若 action 对应的 token 缺失，必须在 prepare 前 fail closed 为 `none` 并记录 `daily_brief_authority_token_missing`；不得换用另一类 token。

选中的 token 继续写入 persisted envelope：

```text
render_context.notification_authority_token
```

provider send 前，executor 继续从同一 envelope 取：

- token；
- `delivery_kind`；
- `delivery_key`；
- `source_digest`；
- `message_sha256`；
- transport idempotency key。

`delivery_kind` 不能只信任 caller mapping。新增 repository read-only validator：

```python
validate_daily_decision_brief_delivery_identity(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str,
    delivery_key: str,
    source_digest: str,
    message_sha256: str,
    transport_idempotency_key: str,
) -> dict[str, Any]
```

它在现有 delivery lock 下定位 persisted envelope，复用 account/date/key/source/message/transport 全量校验，并返回真实 `delivery_kind` 与 status。executor 不从 caller 自报 kind 做授权结论。

这组已验证的 `delivery_identity` 由 notification authority receipt 固化。对 failure token v2，executor 必须在 provider call 前验证：

```text
persisted_envelope.delivery_kind == caller.delivery_kind
persisted_envelope.delivery_kind in token.authorized_delivery_kinds
persisted_envelope.status == pending
```

不匹配返回 `AUTHORITY_NOTIFICATION_DELIVERY_KIND_DENIED`，attempt count 为 0。fixed failure 仍由 repository 强制：

- `delivery_kind=fixed_failure`
- `source_kind=scan_failure`
- plain-text only
- `candidate_identities=[]`
- exact source reference + digest。

failure-token `delivery_identity` 增加 `delivery_kind`：

- 对 failure v2 token 必填并参与 authority receipt；
- 对旧 v1 token 可缺失，以支持已持久化 envelope 的 exact retry；
- reconciliation 必须保留该字段，但不改变既有 source/message/delivery-key 比对；
- receipt schema 暂不升级，因为其 `delivery_identity` 本来就是 mapping；validator 必须同时接受 legacy mapping 和带 kind 的新 mapping。

锁顺序固定为 notification authority global shared -> scope shared -> send lock -> 短暂 delivery lock；不得先持有 delivery lock 再等待 authority lock。persisted-envelope validation 完成并释放 delivery lock后才调用 provider。

限制 fixed-failure capability 的边界是 failure token v2 的 authorized kind、action-specific token 字段、domain action、repository envelope contract、exact delivery identity 和 send-time authority re-resolution 的组合，而不是把 normal report completeness 写进 token。

## 5. Delivery 状态与兼容性

### 5.1 同 run fixed delivery

沿用现有 fixed delivery key：

```text
market + market_trading_date + account + scheduled_target_market
```

状态规则：

- `pending fixed_failure`：允许现有 repository 原子升级为 `fixed_report`；包括尚未尝试和已确认 provider definite failure 后仍为 pending 的 envelope，但不包括 ambiguous/unknown；
- `confirmed fixed_failure`：同 run 不再发送 fixed report；
- `ambiguous/unknown fixed_failure`：冻结并交给现有 reconciliation，不自动换消息；
- exact retry 必须复用原 envelope bytes、message hash、delivery key 和 token；
- notification authority receipt 的 account-run/channel dedupe 保持“一 run 一 channel 一个终态发送”。

### 5.2 Mixed old/new state

- 现有 normal token v1 与旧 envelope 内的 token v1 继续按原契约执行；executor 仍校验 account/source/identity/mode/generation，不新增 delivery-kind 要求；
- 所有新准备的 fixed-failure envelope 必须使用 failure token v2，并在 delivery identity 中携带 `delivery_kind=fixed_failure`；
- 新 brief 的 `notification_allowed` 与 `token` 仍只表示 normal delivery；
- 没有 `fixed_failure_delivery_allowed` 的旧 brief 按 false 解释，不凭空构造 failure token；
- 不迁移、不回填历史 brief、delivery envelope 或 authority receipt；
- 新代码必须能读取旧 receipt 中没有新增审计摘要的 `delivery_identity`。

## 6. Observability

Daily Brief lifecycle audit 与 tick metrics 增加：

```json
{
  "normal_delivery_allowed": false,
  "fixed_failure_delivery_allowed": true,
  "authority_identity_source": "current_run_portfolio_receipt",
  "authority_resolution_status": "resolved",
  "authority_resolved_mode": "v1",
  "authority_generation": 3,
  "identity_snapshot_id": "<sha256>",
  "identity_receipt_hash": "<sha256>",
  "decision": "fixed_failure",
  "decision_reason": "fixed_normal_authority_unavailable"
}
```

失败 reason 至少区分：

```text
current_run_portfolio_receipt_missing
current_run_portfolio_receipt_ambiguous
current_run_portfolio_receipt_invalid
current_run_portfolio_receipt_stale
current_run_portfolio_identity_mismatch
position_advice_authority_conflict
position_advice_failure_token_build_failed
```

日志不得包含 payload、broker account identifiers、完整 receipt 或 webhook secret。

## 7. 实施切片

### Slice 1 — Current-run identity reader

文件：

- 新增 `src/application/position_advice_account_identity_reader.py`
- 更新 `tests/test_position_advice_account_identity_reader.py`

验收：

- 唯一合法 receipt 返回重算后的 source/identity；
- 缺失、多个、symlink/path escape、坏 JSON、schema/hash/payload mismatch、stale、run/account/market mismatch 全部 fail closed；
- reader 无写入、无 ledger/publisher 依赖；
- receipt/payload 在读取期间变化时拒绝。

### Slice 2 — Purpose-restricted failure token v2

文件：

- `src/application/position_advice_notification_authority.py`
- `src/application/daily_decision_brief_repository.py`
- `docs/POSITION_ADVICE_V2_CONTRACT.md`
- `tests/test_position_advice_notification_authority.py`
- `tests/test_daily_decision_brief_repository_v2.py`

验收：

- 现有 normal builder/token v1 行为不变；新窄 builder 只生成 failure token v2；
- v2 failure token 拒绝 `fixed_report` / `candidate_alert`，且在 provider call 前失败；
- caller 自报 kind 与 persisted envelope kind 不一致时 provider attempt 为 0；
- normal/旧 v1 token 的 validation、发送与 exact retry 继续成功；
- 新 authority receipt 保留 delivery kind，旧 receipt 继续可读和 reconciliation；
- public contract 文档同步 token purpose、v1 compatibility 和 one-terminal-send 语义。

### Slice 3 — Advice 与 notification authority 分离

文件：

- `src/application/daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_service.py`

验收：

- summary missing 永远不恢复 normal v1；
- valid identity + resolved v1/v2_shadow/v2 分别产生 formal `v1/v1/v2` failure token；
- conflict/invalid identity 无 failure token；
- `human_review_rows=[]` 时 report 必须 blocked，不能作为 normal delivery；
- legacy normal aliases 保持兼容。

### Slice 4 — Domain action 与 application orchestration

文件：

- `domain/domain/daily_decision_brief.py`
- `src/application/tick_notification_flow.py`
- `tests/test_daily_decision_brief_notification_flow.py`

验收：

- domain 覆盖完整矩阵，application 不再事后覆盖；
- normal action 只能取 normal token，fixed failure 只能取 failure token；
- fixed failure envelope 保持 plain text、无 candidates、绑定 scan failure digest；
- send-time authority conflict 仍阻断 provider call；
- `lx` normal fixed report 与 `sy` fixed failure 可在同一 tick 各自产生独立 delivery key/provider attempt；
- 任一账户 token/receipt 错误不影响另一账户。

### Slice 5 — Retry、状态转换与回归

验收：

- pending failure 可升级为 normal report；
- confirmed/ambiguous failure 不升级；
- old envelope exact retry；
- duplicate tick/provider retry 不重复发送；
- normal v1/v2_shadow/v2 paths、nonfixed candidate alerts、`no_send`、multi-market 现有行为不回归。

## 8. 测试与验证

先运行 focused tests：

```bash
python3.12 -m pytest -q \
  tests/test_position_advice_account_identity_reader.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_daily_decision_brief_repository_v2.py \
  tests/test_position_advice_notification_authority.py \
  tests/test_position_advice_v2_authority_service.py \
  tests/test_position_advice_promotion.py
```

再运行通知与 tick 回归：

```bash
python3.12 -m pytest -q \
  tests/test_notify_symbols_markdown.py \
  tests/test_multi_tick_notify_format.py \
  tests/test_multi_tick_*.py \
  tests/test_unified_tick_entrypoint.py
```

若 import graph 改变：

```bash
python3.12 scripts/generate_dependency_graph.py
python3.12 scripts/generate_dependency_graph.py --check
```

实现后的 read-only dry-run 必须证明：

```text
sy:
  decision=fixed_failure
  delivery_key!=null
  rendered message 为不可行动失败文案
  candidate identities=[]
  no real provider call

lx:
  原 normal fixed report 行为不变
```

不得用真实 webhook 作为测试信号。

## 9. Rollout 与回滚

本计划只定义代码修复，不授权 release 或 upgrade。

后续若另行授权发布/升级：

1. 先验证 focused + regression tests；
2. 用受控 dry-run 检查双账户 lifecycle audit；
3. 发布与远端升级保持独立边界；
4. 升级后只读核对每账户 `decision`、`delivery_key`、authority receipt 与 provider attempt；
5. 若发现 normal action 被 failure token 授权、跨账户干扰或 duplicate send，回滚到上一 release，保留 delivery/authority receipts，不删除状态。

## 10. Deferred follow-up

以下问题另开 work unit，不作为本修复的隐含扩展：

1. 为 PA source graph 输出 typed run outcome，区分 `coverage_incomplete`、`dependency_unavailable`、`integrity_failure`、`unknown_failure`；
2. 建立 lifecycle review completeness contract；
3. 只 allowlist 明确的 symbol-scoped coverage failure 后，评估是否开放正常 v1 degraded report；
4. 诊断并改善 `EMPTY_CHAIN` 的上游 provider/data-quality 行为；
5. 若未来正式支持绕过 `tick-cron` market lock 的同账户/同市场并发 tick，单独设计 persisted-envelope prepare/upgrade 与 provider in-flight claim 的原子状态；当前生产 guarded tick 串行假设不扩展为通用并发承诺。

在这些条件完成前，identity receipt fallback 只能授权 fixed failure，不能授权 normal report。
