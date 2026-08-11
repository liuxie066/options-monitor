# Gateflow Plan — perf-e2e-regression

- Gate: `plan`
- Work unit: `perf-e2e-regression`
- Date: 2026-08-11
- Branch: `perf-e2e-regression`（baseline `db2c361e`）
- Goal confirmation: `docs/gateflow/perf-e2e-regression-goal-confirmation-20260811.md`（user confirmed 2026-08-11）
- Artifact path: `docs/gateflow/perf-e2e-regression-plan-20260811.md`
- Status: `awaiting plan review`

## Goal / Motivation / Success Signal

Goal（来自 goal confirmation）：新增一条端到端回归测试，覆盖真实生产链：

`resolver 开仓真实写 SQLite (apply_changes=True) -> trade_events/projection -> build_option_period_performance -> attribution groups`

Success signal：

1. 新增 `tests/test_performance_resolver_projection_e2e.py` 通过。
2. 开仓经 `resolve_trade_deal(..., apply_changes=True)` 真实写入 SQLite。
3. close/expire 事件经 writer 真实持久化路径落库，`raw_payload` 携带 `strategy_snapshot`。
4. attribution 断言与现有基线一致：
   - `attribution.groups[0]` 存在；
   - `funding.put_open_credit_gross == 500.0`、`call_open_debit_gross == 400.0`、
     `call_cost_funded_by_put == 400.0`、`funding_surplus == 100.0`；
   - participation `realized_gross == 300.0`（USD）；
   - 总账 `realized_gross == 800.0`（USD）；
   - `attribution.conservation.realized_gross.residual_by_currency == {"USD": 0.0}`。
5. 不发送真实通知、不写 `output/` / `output_runs/` / 状态文件。

## Non-Goals / Scope Boundary

- 不改 resolver / projection / performance 生产代码；只新增测试文件。
- 不做错期/组合残留清理（`_with_combo_yield_long_call_payload` / `_with_combo_yield_sell_put_payload` 等归后续 work unit）。
- 不发布、不升级、不 merge、不删除分支。
- 不触碰主仓库工作树（并行会话产物不动）。
- 不引入新依赖、不改 SQLite schema、不加配置项。

## Goal Alignment

| Plan element | Aligned goal / success signal |
|---|---|
| 真 SQLite repo + resolver open | Success signal 2（resolver 写库真实路径） |
| writer 持久化 close/expire + strategy_snapshot | Success signal 3（事件解码与 projection） |
| attribution 数值断言 | Success signal 4（attribution 正确性） |
| 仅新增测试文件 | Non-goal：不改生产代码 |

## Design Document Alignment

无传入 design_doc；以 goal confirmation + 代码事实为准。

## First-Principles Judgment and Direct Code Evidence

1. `src/application/performance/service.py:21` `build_option_period_performance(repo, period, account, now_ms)` — performance 报告唯一公共入口。
2. `src/application/performance/adapters.py:37` `load_ledger_performance_inputs(repo)` — 只经 `ledger_api.trade_event_log(repo)`（SQLite `list_trade_events`）读取并 projection。
3. `src/application/trades/resolver.py:302` `resolve_trade_deal(deal, repo, state, apply_changes=True)` — 开仓真实写库入口；`_trade_open_ledger_inputs`（`src/application/ledger/preflight.py:792`）构造 `TradeEvent(event_id=broker_external_event_key(deal), lot_id=f"lot_{event_id}", raw_payload=dict(deal.raw_payload))`，因此 deal 的 `raw_payload["strategy_snapshot"]` 会原样落库。
4. `src/application/ledger/commands.py:1808` `record_normalized_trade_event` → `persist_trade_event` → `persist_trade_event_object`（writer.py:335），对 SQLite repo 走真实事务并重建 projection。
5. `src/application/ledger/writer.py:4097` `_trade_event_from_normalized_deal` — close 时 `target_lot_id` 从 `raw_payload["target_lot_id"]` / `["record_id"]` 解析，`raw_payload` 原样保留（含 `strategy_snapshot`）。
6. `domain/domain/performance/attribution.py::resolve_event_attribution` — 从 `raw_payload.strategy_snapshot` 读 `strategy` / `leg_role` / `strategy_group_id` / `expiry_structure`；open 事件 lifecycle source = `lot_id`，close 事件 = `target_lot_id`（engine.py 约 1556 行 `_event_fact_kwargs`）。
7. 现有基线测试：`tests/test_performance_strategy_attribution.py`（相同数值断言）、`tests/test_performance_service.py`（SQLite + service 入口）、`tests/test_trades_resolver_close.py`（resolver + SQLite 真实落库断言）。

## Affected Files / Modules

- 新增：`tests/test_performance_resolver_projection_e2e.py`（唯一代码改动）。
- 文档：`docs/gateflow/perf-e2e-regression-plan-20260811.md`（本文件）及后续 gate artifacts。

## Contract / Schema / State-Machine / Public-Interface Changes

无。不改生产代码、公开接口、schema 或状态机。

## Implementation Decisions

### 决策 1：开仓经 resolver 真实写库（方案 A，goal confirmation 已确认）

- 构造两个 `NormalizedTradeDeal`（put short open + call long open），`position_effect="open"`，`raw_payload` 显式携带：
  ```python
  "strategy_snapshot": {
      "strategy": "combo_yield",
      "leg_role": "funding_put" | "participation_call",
      "strategy_group_id": "combo_yield:lx:pair-1",
      "expiry_structure": "same_expiry",
  }
  ```
- 分别 `resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)`，断言 `status == "applied"`。

### 决策 2：close/expire 经 writer 真实持久化

- 从 `repo.list_position_lots()` 读取 resolver 生成的开仓 lot `record_id`（= open event 的 `lot_id`，`lot_{event_id}`），作为 close 事件的 `target_lot_id`。
- 构造 `TradeEvent`（put `expire_close` price=0；call `close` price=7），`raw_payload` 携带同一 `strategy_snapshot`，经 `persist_trade_event_object(repo, event)` 落库。
- 理由：resolver 对零价到期 close 会走 lifecycle pending 分支，绕开会引入无关状态；writer 真实持久化已覆盖事件解码 + projection，且与生产 attribution 所需元数据形态一致。

### 决策 3：报告入口与断言

- `build_option_period_performance(repo, period={"period": "month", "month": "2026-05"}, account="lx", broker="futu", now_ms=NOW_MS, include_rows=False)`。
- 断言与现有 `test_group_attribution_keeps_call_basis_out_of_put_pnl_and_conserves_group_pnl` 数值完全一致。
- 额外断言 `repo.list_trade_events()` 中含 strategy_snapshot 的 open/close 事件，证明元数据真实落库并完成 round-trip。

## Implementation Slices

### Slice 1（唯一 slice）：`tests/test_performance_resolver_projection_e2e.py`

- **Objective**：一条端到端回归测试通过。
- **Expected outcome**：resolver 开仓写库 + writer close/expire 落库 + performance attribution 数值正确。
- **Allowed files**：仅 `tests/test_performance_resolver_projection_e2e.py`。
- **Exact allowed changes**：新增测试文件，包含：
  - 常量 `TZ` / `NOW_MS` / `_ms()` 辅助；
  - `_open_deal(role, option_type, side, strike, price, event_time)` 构造 `NormalizedTradeDeal`；
  - `_close_event(...)` 构造 `TradeEvent`（含 `strategy_snapshot` 与 `target_lot_id`）；
  - `test_resolver_open_to_performance_attribution_round_trip(tmp_path)`。
- **Functions/call path/data flow**：见 Implementation Decisions；错误路径不做额外覆盖。
- **Non-goals**：不覆盖 resolver close 真实写库（已有 `test_trades_resolver_close.py`）；不覆盖 live collector / evidence / FX。
- **Tests/validation**：见下节验证命令。
- **Completion signal**：`pytest tests/test_performance_resolver_projection_e2e.py -q` 全绿；再跑相关回归集合全绿。

## Tests / Validation Commands and Expected Assertions

```bash
cd <worktree-root>
PYTHONPYCACHEPREFIX=<tmp-cache> <repo-root>/.venv/bin/python -m pytest tests/test_performance_resolver_projection_e2e.py -q -p no:cacheprovider
```

Expected：1 passed。

相关回归集合：

```bash
PYTHONPYCACHEPREFIX=<tmp-cache> <repo-root>/.venv/bin/python -m pytest \
  tests/test_performance_resolver_projection_e2e.py \
  tests/test_performance_strategy_attribution.py \
  tests/test_performance_service.py \
  tests/test_trades_resolver_close.py \
  tests/test_assigned_stock_sale_intake.py \
  -q -p no:cacheprovider
```

Expected：全绿（数值断言见 Success Signal 4）。

## Docs Decision

- 新增 gate artifacts（goal confirmation、plan、plan review、implementation、code review、aggregate deepreview、PR review、final closeout）至 `docs/gateflow/`。
- 不更新用户文档：无公共命令、工具 payload、输出路径或安全边界变化。

## Risks / Open Questions

- 若测试暴露真实生产 bug（例如 resolver 落库后 attribution 数据不一致），只记录 finding，不顺手改生产代码；是否修复由用户单独确认。
- Baseline 与 origin/main 漂移（`c9d64129`）：rebase 仅在 draft PR 准备时执行，避免本 work unit 期间引入并行会话冲突。
- `persist_trade_event_object` 对 expire_close 事件与 projection 的兼容性：已由现有 projection 语义保证（`expire_close` ∈ CLOSE_EVENT_TYPES）；若验证失败会记录为 finding，不改语义。

## Completion Report Format

Final closeout 将包含：changed files、验证命令与结果、docs decision、finding status、residual risks / owners、draft PR URL、next entry point。
