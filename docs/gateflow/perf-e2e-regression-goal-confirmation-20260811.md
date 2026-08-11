# Gateflow Goal Confirmation — perf-e2e-regression

- Gate: `goal confirmation`
- Work unit: `perf-e2e-regression`
- Date: 2026-08-11
- Branch: `perf-e2e-regression`
- Baseline: `db2c361e` (tag `v1.13.9`); origin/main 已前进到 `c9d64129`（v1.13.10 release + health-check fix），rebase 留到 PR 准备阶段，本 work unit 期间锁定 baseline。
- Artifact path: `docs/gateflow/perf-e2e-regression-goal-confirmation-20260811.md`
- Status: `awaiting user confirmation`

## Goal

新增一条性能报告端到端回归测试，覆盖真实生产数据链：

`resolver 开仓真实写 SQLite (apply_changes=True) -> trade_events/projection -> build_option_period_performance -> attribution groups`

验证在真实 SQLite 持久化 + `strategy_snapshot` 元数据（`strategy` / `leg_role` / `strategy_group_id` / `expiry_structure`）下，
Combo Yield 的 funding / participation 归因仍然正确，防止 resolver 落账、事件解码、projection 或 attribution 任一层回归。

## Motivation

- `tests/test_performance_service.py` 用 `repo.upsert_trade_event(TradeEvent)` 直插事件，未经过 resolver 真实写库路径。
- `tests/test_performance_strategy_attribution.py` 直接构造内存事件，不覆盖 `resolve_trade_deal(apply_changes=True)`、SQLite 持久化与 projection 解码。
- 目前没有任何测试覆盖 `resolver 写库 -> performance 报告` 的单一链路；该链路是 production 报告的权威数据路径。
- 生产 attribution 依赖 `raw_payload.strategy_snapshot` 与 `lot_id` / `target_lot_id` 关联，是最容易在重构中静默损坏的部分。

## Success Signals

1. 新增 `tests/test_performance_resolver_projection_e2e.py`，全部通过。
2. 开仓事件经 `resolve_trade_deal(..., apply_changes=True)` 真实写入 SQLite `trade_events` 表。
3. close/expire 事件经 writer 的真实持久化路径（`persist_trade_event_object` / SQLite repo）落库，`raw_payload` 显式携带 `strategy_snapshot`。
4. `build_option_period_performance` 输出断言与现有 attribution 基线一致：
   - `attribution.groups[0]` 存在，且 funding / participation group 归类正确；
   - `funding.put_open_credit_gross == 500.0`、`funding.call_open_debit_gross == 400.0`、
     `funding.call_cost_funded_by_put == 400.0`、`funding_surplus == 100.0`；
   - participation `realized_gross == 300.0`（USD）；
   - 总账 `realized_gross == 800.0`（USD）；
   - `attribution.conservation.realized_gross.residual_by_currency == {"USD": 0.0}`。
5. 测试不发送真实通知、不写 `output/` / `output_runs/` / 状态文件，不触碰主仓库并行会话产物。

## Non-Goals / Scope Boundary

- 不修改 resolver / projection / performance engine 生产代码；本 work unit 只新增测试与必要测试夹具。
  若测试暴露真实 bug，记录为 finding，修复范围需单独确认，不顺手扩实现。
- 不做错期/组合残留清理（`_with_combo_yield_long_call_payload` / `_with_combo_yield_sell_put_payload` 等 dead code 归后续 work unit）。
- 不发布、不升级生产、不 merge、不删除分支；merge / delete 是独立授权边界。
- 不触碰主仓库工作树（并行会话在改写）。
- 不做性能优化、不重构 attribution 语义、不新增 repo 抽象或配置项。

## Direct Code Evidence

- `src/application/performance/service.py:21` `build_option_period_performance`
- `src/application/performance/adapters.py:37` `load_ledger_performance_inputs`（trade_event_log -> projection）
- `src/application/ledger/writer.py:4097` `_trade_event_from_normalized_deal`（保留 `raw_payload`，close 解析 `target_lot_id`）
- `src/application/trades/workflows.py:24/43` `apply_trade_open_with` / `apply_trade_close_with`
- `src/application/ledger/commands.py:166/372` `persist_trade_open_event_with_ledger` / `persist_trade_close_events_with_ledger`
- performance attribution 调用点：`engine.py` 中 `_append_capital_exposure_segments`（约 628 行）与 `_event_fact_kwargs`（约 1556 行）通过 `lot_id_for_open_event` / `target_lot_id` 关联 lifecycle
- 现有测试基线：`tests/test_performance_strategy_attribution.py`（`_event` 元数据形态与数值断言）、`tests/test_performance_service.py`、`tests/test_trades_resolver_close.py`

## 不会做的过度设计

- 不新增 repository 层抽象、不改 SQLite schema、不引入新依赖。
- 测试夹具保持最小：一个真 SQLite repo + 一组 open/close 事件。
- 不把实现层发现的其他风险升级为本 work unit 目标。

## Blocking Open Questions

- 无。Plan 阶段的确定性决策：
  - 方案 A（推荐）：open 事件经 resolver 落账，close/expire 经 writer 真实持久化并显式携带 `strategy_snapshot`；
  - rebase 到 origin/main 仅在 draft PR 准备时执行。

## Deferred Follow-ups（不属于本 work unit）

- 生产升级到已发布版本（v1.13.9 / v1.13.10）：需用户单独授权并确认目标版本。
- 错期/组合残留与 dead code 清理。
- 并行会话发布产物（release/CHANGELOG）的后续核对。

## Completion Report Format

Final closeout 将包含：changed files、验证命令与结果、docs decision、finding status、residual risks / owners、draft PR URL、next entry point。
