# Gateflow Goal Confirmation — Lifecycle Single Source of Truth

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `goal confirmation`
- Date: 2026-08-04
- Status: confirmed by user
- Branch: `fix/lifecycle-single-source-of-truth`
- Base: `main@ed2531e9`

## User problem

2026-08-03 22:30 半点候选通知中，`sy` 账户完成投递，`lx` 账户却在通知提供商调用前因
`option-position context fingerprint mismatch` 失败。同一批 `lx` 到期期权 case 的派生状态在两条路径间
反复震荡：

- history backfill 调用的 lifecycle discovery 用旧的固定 72 小时口径写成
  `needs_review / settlement_evidence_deadline_elapsed`；
- account-scoped canonical due reconciliation 根据 immutable timing policy 写回
  `waiting_settlement_evidence / awaiting_settlement_evidence`。

状态被改写后，同一轮 Position Advice 前后的 canonical fingerprint 不再一致，因此安全门禁按设计
fail closed，造成 `lx` 无通知而 `sy` 有通知。

## Confirmed goal

让 lifecycle case 的派生状态只由 canonical lifecycle read model 与 account-scoped due
reconciliation 决定并通过 ledger 原子 transition writer 落库。Discovery 只负责冻结到期 lot 并创建
case；backfill 只能发现当前明确账户的 case，不得以 `account=None` 扫描或改写其他账户。

## Motivation and direct evidence

- `src/application/ledger/writer.py::discover_expired_lifecycle_cases_atomically()` 在创建 case 后又遍历
  existing cases，直接调用 `derive_lifecycle_read_model()`；该调用没有传入 canonical
  reservations、timing policy 或 `pending_until_ms_override`，却用结果整体覆盖
  `derived_summary`。
- `domain/domain/option_lifecycle.py` 的 fallback deadline 是观察起点后 72 小时，而
  `src/application/trades/lifecycle_reconciliation.py::lifecycle_case_read_model()` 会消费 ledger 中冻结的
  timing policy 和有效 evidence/reservation。
- `src/application/trades/backfill.py::run_history_backfill()` 在回填前后两次用 `account=None`
  调用 discovery，所以 `sy` 的 backfill 可刷新 `lx` case。
- `docs/plans/option-expiry-close-reason-receipt-redesign-plan-20260730.md` 明确指定
  `reconcile_due_lifecycle_cases()` 是无新成交消息时的状态推进 owner，且每个 runtime source
  只能处理其明确绑定账户。

## Success signals

1. 对已存在的 lifecycle case 重放 discovery，不再修改 `status`、`derived_summary`、
   `resolution_revision` 或 `state_fingerprint`。
2. 无 pairing anchor 的 case 在 canonical deadline 之前保持 pending，到期后由 due reconciliation
   通过 canonical read model 原子转入 `needs_review`，并且重放不重复推进 revision 或 Outbox。
3. 已绑定 broker timing policy 的 case 即使超过旧 72 小时 deadline，discovery 也不改写
   canonical waiting 状态和指纹。
4. `run_history_backfill()` 强制接收非空 canonical account，回填前后的 discovery 都仅使用
   该账户；`lx` / `sy` 互不发现、互不改写。
5. fingerprint mismatch 安全门禁保持原样；修复不通过放宽验证恢复通知。
6. lifecycle/backfill/tick 聚焦测试、相关广泛回归、compileall 和 `git diff --check` 通过。

## Non-goals and scope boundary

- 不新增 lifecycle 状态、数据库 schema、配置项、后台服务或第二套投影。
- 不改变 broker settlement evidence 的证明标准、allocation 规则或 terminal event 写入语义。
- 不放宽 decision-state fingerprint 或通知 fail-closed 条件。
- 不修改生产数据、不重试真实通知、不发布、不部署、不升级远端。
- 不在本 work unit 自动修复已被旧路径改写的历史行；代码上线后由既有 canonical due
  tick 按当前 ledger 事实收敛。

## Overdesign deliberately excluded

本轮只移除一个越权的 legacy refresh loop，并补齐既有 due owner 对“无 pairing clock 但
canonical deadline 已到”的收敛分支。不引入新 repository、resolver、scheduler、状态机或数据迁移。

## Blocking open questions

无。
