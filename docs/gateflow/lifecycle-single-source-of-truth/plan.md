# Gateflow Implementation Plan — Lifecycle Single Source of Truth

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `plan`
- Date: 2026-08-04
- Status: accepted after adversarial re-review
- Goal confirmation: `docs/gateflow/lifecycle-single-source-of-truth/goal-confirmation.md`
- Initial review: `docs/reviews/plan-review-20260804-001846.md`
- Accepted re-review: `docs/reviews/plan-review-20260804-002343.md`
- Branch: `fix/lifecycle-single-source-of-truth`
- Base: `main@ed2531e9`

## 1. Goal, motivation, and completion signal

消除 lifecycle case 的双重状态口径：discovery 只创建 case，canonical
`lifecycle_case_read_model()` + account-scoped `reconcile_due_lifecycle_cases()` 是唯一派生状态
计算/推进主链，所有状态落库继续经过带 generation-token CAS、state fingerprint、revision
和 Outbox 幂等的 `advance_lifecycle_case_state()`。

完成信号：

- discovery replay 对 existing case 零写入；
- default 72h 与 immutable broker timing policy 不再互相覆盖；
- 无 pairing anchor 的超时 case 仍由 due owner fail closed 为 `needs_review`，且不新增历史路径
  不会产生的通知意图；
- `lx` / `sy` backfill 严格账户隔离；
- 同一事实重放不推进 revision、不重复 Outbox、不改变 fingerprint；
- 聚焦与广泛验证全部通过。

## 2. Non-goals and boundaries

- 不改 schema/public CLI 命令/业务状态枚举；
- 不改 decision snapshot/fingerprint validator 或 Position Advice fail-closed 门禁；
- 不改 settlement observation 覆盖率、terminal resolver 或 allocation 语义；
- 不创建新 scheduler、状态表、migration 或通知重试机制；
- 不执行真实通知、生产写入、release 或 deployment。

## 3. First-principles judgment and code evidence

### 3.1 Authority violation

`discover_expired_lifecycle_cases_atomically()` 同时承担“冻结新 case”与“刷新旧 case
派生状态”。后者直接调用 domain fallback model，未读取 canonical case resolution、effective
reservations 和 immutable timing policy，违反了已建立的 ledger read-model authority。

Decision：从 ledger discovery transaction 删除 existing-case refresh；保留结果字段
`refreshed_case_ids` / `would_refresh_case_ids` 且稳定返回空数组，避免不必要的 payload/CLI 破坏。

### 3.2 Fail-closed aging must not disappear

直接删掉 discovery refresh 会让没有 option-close anchor 的 case 丧失旧有“deadline 后转
`needs_review`”能力，因为当前 due selector 对 `pairing_until_ms is None` 直接 skip。这会把
安全问题从“双重写入”变成“永不收敛”。

Decision：在现有 due owner 内增加一个窄分支，但不用 `pairing_until_ms`
猜测 anchor 身份：

1. 从 canonical `lifecycle_case_read_model()` 读取 `pending_until_ms`、`reason_state`、reason codes、
   timing hash、generation token、`lifecycle_evidence_status` 和 reservation evidence ids；
2. `pairing_until_ms is None` 时，deadline 缺失或未到则 skip；
3. deadline 已到且 `lifecycle_evidence_status == "missing"` 、canonical
   `reason_state == needs_review` 时，不采集 broker observation，直接物化这个 no-anchor
   fail-closed 结论；
4. no-anchor 落库调用 `advance_lifecycle_case_state()`，传 read-model generation token，但使用
   `public_transition=None`，保留 legacy deadline aging 不产生 lifecycle notification Outbox 的可见语义；
5. evidence 已存在但 pairing 不可用时，不走 no-anchor materialization；直接调现有
   `reconcile_lifecycle_close_reason()` 做 typed fail-closed 分类，不调 provider collector；
6. repeated due tick 必须命中相同 fingerprint/revision，no-anchor 路径始终不产生 Outbox。

有 effective pairing 的 case 完全保留现有 provider observation +
`reconcile_lifecycle_close_reason()` 主链。新分支不尝试猜测 terminal reason，只物化 canonical read
model 已经给出的 no-anchor `needs_review`；其他 evidence 状态继续交给现有 close-reason
resolver。

### 3.3 Account isolation

`run_history_backfill()` 已拥有本次查询的 `futu_account_ids` 与 canonical `account_mapping`，
但两次把这些身份丢失为 `account=None` 传给 discovery。同时，仓库明确保留一个
`source.account=None` 的 legacy multi-account source，因此不能以“强制每个 source 只有一个 account”
作为修复前提。

Decision：不改 `run_history_backfill()` 签名。用现有 `futu_account_ids` 及
`account_mapping` 导出排序、去重、lowercase 的 explicit discovery account list；每个 account
单独调 discovery，永不传 `None`。任一 configured Futu ID 缺少 mapping 时，该 phase
返回 typed scope failure 且零 discovery 写入，不做部分账户扫描；成交回填本身继续沿用现有
unresolved/fail-closed 处理。

## 4. Contract, schema, state-machine, and public-interface decisions

### Internal account-scope contract

`run_history_backfill()` 的公开/internal 函数签名不变。新的 private scope resolver 契约为：

```python
_lifecycle_discovery_accounts(
    *,
    futu_account_ids: list[str],
    account_mapping: dict[str, str],
) -> tuple[str, ...]
```

- 只消费本次 history query 的 Futu IDs，不扫描 mapping 中的其他账户；
- 所有 configured IDs 必须存在非空 canonical mapping；否则抛出 typed scope error；
- 返回 sorted unique lowercase labels，每个 label 各调一次 discovery。

backfill phase 结果保留既有 union summary keys，并增加 additive `accounts` 和
`account_results`，便于审计 legacy multi-account source；实际 discovery call 中不会出现
`account=None`。

### Discovery result compatibility

`lifecycle_discovery_result.v2` schema 不变。保留：

```json
{
  "refreshed_case_ids": [],
  "would_refresh_case_ids": []
}
```

这两个字段转为明确的 compatibility placeholders，不再代表 discovery 的写权。

### State transition

```text
expired lot
  -> discovery creates immutable lifecycle_case.v2 only
  -> canonical read model resolves case/evidence/allocation/timing facts
  -> account-scoped due reconciliation selects due work
     -> no evidence + no pairing + canonical deadline elapsed: needs_review via atomic state writer, no Outbox
     -> evidence exists but effective pairing unavailable: existing typed close-reason reconciliation, no provider poll
     -> effective pairing present: existing provider-observation/close-reason reconciliation path
```

Schema 无变更；既有 CAS、fingerprint、revision 和 Outbox 不变。

## 5. Affected files/modules

- `src/application/ledger/writer.py`
- `src/application/trades/close_reason_reconciliation.py`
- `src/application/trades/backfill.py`
- `tests/test_position_advice_v2_lifecycle_reconciliation.py`
- `tests/test_settlement_observation.py`
- `tests/test_trades_auto_intake_backfill.py`
- `docs/FUTU_TRADE_HOLDINGS_SYNC.md`
- Gateflow artifacts under `docs/gateflow/lifecycle-single-source-of-truth/`
- review artifacts under `docs/reviews/`

## 6. Implementation slices

### Slice S1 — Canonical lifecycle state ownership

- Objective: 移除 discovery 的 existing-case 写权，同时在 canonical due owner 内保留无 anchor
  case 的 deadline fail-closed 收敛。
- Expected outcome: discovery 只创建；due reconciliation 成为唯一 aging transition owner。
- Allowed files:
  - `src/application/ledger/writer.py`
  - `src/application/trades/close_reason_reconciliation.py`
  - `tests/test_position_advice_v2_lifecycle_reconciliation.py`
  - `tests/test_settlement_observation.py`
- Prerequisites: goal confirmation + accepted plan.
- Exact allowed changes:
  1. 删除 `discover_expired_lifecycle_cases_atomically()` 的 existing-case refresh loop 及其不再需要的
     allocations/void-derived computation imports/locals；保留空 compatibility result keys。
  2. 在 `reconcile_due_lifecycle_cases()` 内用一个 private helper 封装 missing-evidence/no-pairing
     deadline materialization；输入仅为 existing lifecycle case + canonical read model + apply flag。
  3. helper 只接受 `lifecycle_evidence_status == missing` 且 `reason_state == needs_review`，
     构造摘要时直接复用 read model 的
     `close_reason`、reason codes、pairing/deadline/timing hash；apply 时传 generation token 给
     `advance_lifecycle_case_state()`，且 `public_transition=None`。
  4. evidence 已存在但 effective pairing 缺失时，调现有
     `reconcile_lifecycle_close_reason()` 进行 typed 分类，不调 observation collector。
  5. dry-run 返回可观测 decision，不写 case/Outbox；apply 返回 `write_result` 和 fresh
     lifecycle read model。
  6. 不改 pairing-present 的既有 observation/reconciliation 分支。
- Invariants and error handling:
  - deadline 缺失或未到时不写；
  - generation token 变化时依既有 CAS fail closed；
  - no-pairing 分支不调 broker collector；是否存在 anchor 由 canonical evidence status 决定；
  - no-anchor 重放使用同一 state fingerprint/revision 且不产生 Outbox。
- Non-goals: 不改 resolver 业务规则、timing policy 生成、provider collector、schema。
- Focused tests:

  ```bash
  ./.venv/bin/python -m pytest -q \
    tests/test_position_advice_v2_lifecycle_reconciliation.py \
    tests/test_settlement_observation.py
  ```

- Expected assertions:
  - old 72h test 改为 discovery 不 refresh，due apply 在 deadline 物化 `needs_review`；
  - dry-run 零写入；apply 后重放不增 revision，no-anchor apply/replay 均无 Outbox；
  - anchor 已存在但 timing 不可用时，返回 typed timing reason，不被误当成 no-anchor
    deadline elapsed；
  - broker timing policy deadline 迟于 fallback deadline 时，在两者之间重放 discovery 不改
    status/summary/fingerprint，canonical read model 仍 pending。
- Completion signal: focused tests pass and diff contains no discovery status write.
- Stop condition: canonical due branch cannot preserve no-anchor fail-closed semantics without changing public state/schema.

### Slice S2 — Account-scoped history backfill

- Objective: 让 backfill lifecycle discovery 只触及本次查询 Futu IDs 明确映射到的 account。
- Expected outcome: `sy` backfill 不再扫描 `lx`，反之亦然。
- Allowed files:
  - `src/application/trades/backfill.py`
  - `tests/test_trades_auto_intake_backfill.py`
  - `docs/FUTU_TRADE_HOLDINGS_SYNC.md`
- Prerequisites: accepted S1 commit.
- Exact allowed changes:
  1. 增加 private account-scope resolver，只从本次 `futu_account_ids` + `account_mapping` 导出
     explicit accounts；任一 ID 缺失 mapping 则整个 discovery phase typed-fail，不部分扫描。
  2. before/after phase 按 sorted explicit account 各调 discovery 一次，从不传 `None`。
  3. 聚合 per-account result 的 created/would-create/discovered/skipped/compatibility keys，并保留
     `accounts` / `account_results` 审计证据。
  4. 添加 single-account 双 phase capture、legacy two-account 顺序与 missing-mapping no-partial-discovery
     回归。
  5. 文档明确 discovery=create-only，due reconciliation=account-scoped transition owner。
- Invariants and error handling:
  - lifecycle discovery 前验证完整 account scope；missing mapping 不阻止既有 trade payload
    unresolved/audit 路径；
  - 不根据成交 payload 猜 backfill owner，只使用 canonical config mapping；
  - 现有 account mapping/futu account ID 身份校验不放宽。
- Non-goals: 不重构 source config，不修改 history query，不将单 source 扩展为跨账户 batch。
- Focused tests:

  ```bash
  ./.venv/bin/python -m pytest -q tests/test_trades_auto_intake_backfill.py
  ```

- Expected assertions:
  - single-account before/after discovery 均只收到 lowercase explicit account；
  - legacy two-account source 按稳定顺序分别调用，无 `account=None`；
  - 任一 configured ID 缺少 mapping 时两个 phase 都零 partial discovery 并记录 typed failure；
  - 原 backfill 幂等、checkpoint、Inbox 与 audit phase 测试不变。
- Completion signal: focused tests pass and repository has no backfill lifecycle discovery call with `account=None`.
- Stop condition: configured Futu IDs cannot be completely mapped to canonical accounts without changing config authority.

## 7. Validation matrix

After S1:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py
```

After S2:

```bash
./.venv/bin/python -m pytest -q tests/test_trades_auto_intake_backfill.py
```

Aggregate before deepreview:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_position_advice_v2_lifecycle_reconciliation.py \
  tests/test_settlement_observation.py \
  tests/test_trades_auto_intake_backfill.py \
  tests/test_trades_auto_intake_cli.py \
  tests/test_unified_tick_entrypoint.py \
  tests/test_multi_tick_notify_format.py

./.venv/bin/python -m compileall -q domain src scripts
git diff --check
```

Expected aggregate assertions:

- lifecycle discovery/read-model/due reconciliation 回归通过；
- backfill source/account/Inbox/checkpoint 行为不变；
- trade-intake CLI 和 unified tick 调用契约不断裂；
- 通知 formatting 基线不受影响；
- compileall 与 whitespace check 通过。

验证不调用真实 broker provider，不发真实通知，不修改生产 config/runtime data。

## 8. Docs decision

更新 `docs/FUTU_TRADE_HOLDINGS_SYNC.md`，明确：

- discovery 只冻结/创建 case；
- canonical read model + account-scoped due reconciliation 拥有派生状态推进权；
- backfill lifecycle discovery 只使用 current source account。

不更改用户 CLI 示例，因为命令与参数未变。

## 9. Risks, open questions, and residual-risk destinations

| Risk | Classification / mitigation |
|---|---|
| 删除 discovery refresh 使 no-anchor case 永不收敛 | Fixed in S1 by canonical missing-evidence/no-pairing deadline branch. |
| no-pairing 被错当成 no-anchor | Fixed in S1 by canonical evidence-status split and anchor-without-timing regression. |
| 新 due 分支重复推进 revision 或新增通知 | Fixed in S1 with existing fingerprint/CAS, `public_transition=None`, and replay/no-Outbox tests. |
| canonical timing 已绑定但 discovery 仍覆盖 | Fixed in S1 with create-only discovery and policy-stability regression. |
| one account backfill 扫描 another account | Fixed in S2 by explicit mapped account set and per-account calls. |
| required single-account contract breaks legacy multi-account source | Fixed in S2 by preserving the function signature and safely iterating the mapped accounts. |
| production rows 已被旧路径改写 | Assigned to deployment/operations work after this PR; no data repair is authorized here. Existing canonical due path should converge current facts after upgrade, which must be verified separately. |
| newly discovered already-due case waits until next runtime due tick | Accepted bounded behavior; existing cadence is at most 60 seconds and owned by the current runtime design. |
| compatibility placeholder fields may look obsolete | Accepted compatibility tradeoff; documented as always-empty, removal requires a later public-contract work unit. |

Blocking open questions: none.

## 10. Why this is not overdesigned

方案只撤销一个越权 loop，复用已有 canonical read model、due scheduler、atomic state writer、CAS、
fingerprint 和 Outbox。唯一新逻辑是 due owner 内一个 private no-pairing deadline 分支，用于保留现有
fail-closed 语义。没有新 layer、entity、protocol、schema、config 或 worker。

## 11. Completion report format

Final closeout 必须列出：

- changed ownership and account boundary;
- exact focused/aggregate validation commands and results;
- plan/code/aggregate/PR review finding status;
- docs update;
- residual production repair/deployment risk and owner;
- accepted commit hashes and Draft PR URL;
- next entry point: user review/merge; release and upgrade remain separately authorized.
