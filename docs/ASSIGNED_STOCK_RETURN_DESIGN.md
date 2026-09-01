# Assigned Stock Return Contract

本文记录 Cash-Secured Put (CSP) 被指派后的正股事实、估值和收益合同。它描述当前实现，
不是未来设计或迁移计划。

## 核心边界

```text
canonical trade_events
  -> option allocation / position_lots
  -> assignment stock settlement
  -> assigned-stock lifecycle projection
  -> option performance + assigned-stock read view
```

- `assignment` 关闭确定的 short option lot，并记录正股交割事实。
- 正股成本按真实交割价和交割费用记录，不扣减期权权利金。
- `assigned_stock_events` 当前记录显式 sale fact；它不是第二套股票交易账本。
- external holdings 只做 reconciliation evidence，不能创建、关闭或缩放
  canonical assigned-stock lot。
- Feishu 不承载 assigned-stock 或 option-position 事实源。

当前范围是 CSP 被指派后买入正股及其后续卖出。普通股票交易、分红、税务和
无法证明成本基础的其他 assignment / exercise 不在自动计算范围；证据不足时返回
review/incomplete，而不是猜测。

## 身份与事实

写入和查询必须保留：

- option `target_lot_id` / `position_lot_id`；
- assignment event identity；
- derived `stock_lot_id`；
- broker、account、symbol、currency；
- assignment price、shares、trade time 和费用 provenance；
- sale 的 `target_stock_lot_id`、shares、price、time、source deal identity；
- strategy group / leg metadata（存在时）。

一个 sale 必须解析到唯一开放 stock lot，且数量不能超过 remaining shares。
broker deal 重放和人工重试必须靠稳定 source identity 保持幂等。

不要用 symbol、当前持仓数量或聚合 `position_key` 猜写入目标。

## Option Performance 命名空间

`option_performance_report` 将 activity、cash 和 PnL 分开：

| 区域 | 当前字段 | 语义 |
|---|---|---|
| Activity | `premium_collected_gross`, `premium_paid_gross`, `contracts_opened/closed`, `assigned_stock_shares_opened/sold` | 发生了什么；premium activity 不是 PnL |
| Cash | `option_trade_cash_gross`, `option_fee_cash`, `stock_settlement_cash_gross`, `stock_settlement_fee_cash`, `assigned_stock_sale_cash_gross`, `assigned_stock_sale_fee_cash`, `total_cash_change_net` | 实际现金移动 |
| PnL aggregate | `realized_*`, `opening_unrealized_*`, `ending_unrealized_*`, `period_total_*` | option 与 assigned stock 的期间合计 |
| PnL components | `option_realized_gross/net`, `assigned_stock_realized_gross/net` | 已实现结果的可审计分量 |
| Lifecycle | `assignment_lifecycle.period`, `assignment_lifecycle.ending_lots`, `assignment_lifecycle.review` | stock lot、估值、费用和 review 证据 |

证据完整且币种相同时：

```text
pnl.realized_*
  = pnl.option_realized_*
  + pnl.assigned_stock_realized_*

pnl.period_total_*
  = pnl.realized_*
  + pnl.ending_unrealized_*
  - pnl.opening_unrealized_*
```

顶层 unrealized 是 option 与 assigned-stock 的合计。只看正股明细时，应读
`assignment_lifecycle` 或 `option_positions_read action=assigned-stock`，不要用
aggregate PnL 反推 stock component。

`cash.stock_settlement_cash_gross` 是交割本金现金流，不是亏损；
`activity.premium_collected_gross` 是权利金活动，不是额外利润。

## 正股成本与生命周期口径

正股本金始终使用真实交割事实：

```text
assignment_notional = assignment_price * assigned_shares
stock_cost_basis_total = assignment_notional + assignment_stock_fees

stock_realized_gross
  = gross_sale_proceeds - sold_settlement_principal

stock_unrealized_gross
  = remaining_market_value - remaining_settlement_principal
```

Option Performance 的 production net 只接受 `actual` fee evidence，包括显式
actual zero。estimated 或 missing fee 保留 gross，并使受影响 net 为 partial/null。

费用事实统一遵循 `actual -> frozen estimated -> missing`。OpenD
`order_fee_query` 经订单终态、币种和成交数量校验后才能成为 actual；未取得实际费用时，
写入边界冻结现有公式费用。读模型只消费已持久化 provenance，不重新估算。

assigned-stock read view 另外保留两类生命周期字段：

- `assignment_lifecycle_pnl` 是兼容 lot 口径：
  `option_premium_attribution - stock_cost_basis_total
  + stock_sale_cash_in_net + remaining_market_value`；
- `lifecycle_pnl_gross/net` 是 read-model 生命周期口径，gross 合并
  option premium attribution、stock PnL 和可归属 Covered Call (CC) PnL，net 再扣
  `fees_used`。

read view 可以展示明确标注的 estimated fee，因此它的 `lifecycle_pnl_net`
不能替代 production `option_performance_report.pnl.period_total_net`。
CC 只有在显式 `stock_lot_id` 关联，或完全可证明的 assigned-stock FIFO
场景下才归属；mixed ordinary/assigned inventory 会 fail closed。

## CNY 与 FX 证据

现金 CNY 和非现金 PnL/估值 CNY 是两条合同：

1. canonical trade/assignment event 在写入事务中把
   `cash_conversion.v1` 保存到 `raw_payload.cash_conversions[fact_kind]`；
   assigned-stock sale 把相同结构保存在 sale fact 的
   `cash_conversions[fact_kind]`。
2. snapshot 必须匹配 `cash_fact_id`、native amount/currency 和
   `quote_currency=CNY`。非零外币只有在汇率证据距离事件不超过 24 小时时
   `status=observed`；否则写 `status=pending`、`amount_cny=null`。
3. 幂等重试保留第一次写入的 conversion snapshot。读取报告不会用后来的汇率
   重算现金；legacy event 没有 snapshot 时保持 native-only/partial。
4. activity、PnL 和 valuation 的 CNY 使用
   `option_performance_evidence.v1` FX facts，并受自己的 as-of 与 stale gate
   约束。这里的 FX evidence 不能反向替代现金 booking snapshot。

所以 `activity.premium_collected_gross.cny` 与
`cash.option_trade_cash_gross.cny` 可能有不同 evidence path。缺证据时保留原币，
CNY 为 null/partial；禁止用当前汇率补历史缺口。

## As-of、报价与历史

- 当前查询可以刷新开放 assigned-stock lot 的 spot，并返回
  `quote_status`、`spot_time`、`quote_source` 和 refresh diagnostics。
- `refresh_quotes=false` 明确禁用实时刷新。
- 历史 `as_of_ms` 只能使用截至该时点的持久 mark / 合格历史证据；不能用今天的
  spot 回填。
- 缺少历史 mark 时保留成本、sale 和现金事实，估值/PnL 显式
  `missing_quote` 或 partial。
- 后来的 void 会通过 canonical replay 影响历史 boundary；调用方不能从当前 lot
  状态手工拼历史结果。

## 公开入口

期间表现：

```bash
./om option-performance report \
  --config-key us \
  --account lx \
  --period month \
  --month 2026-06 \
  --include-rows

./om-agent run --tool option_performance_report \
  --input-json '{"config_key":"us","account":"lx","period":"mtd","include_rows":true}'
```

assigned-stock lot 查询：

```bash
./om-agent run --tool option_positions_read \
  --input-json '{"config_key":"us","action":"assigned-stock","account":"lx","refresh_quotes":false}'
```

人工 sale 先 dry-run：

```bash
./om option-positions assigned-stock-sale \
  --runtime-root /var/lib/options-monitor \
  --target-stock-lot-id <stock-lot-id> \
  --shares 100 \
  --price 105 \
  --trade-time-ms <unix-ms> \
  --dry-run
```

确认 broker/account/symbol、目标 lot、数量、价格和 source identity 后，移除
`--dry-run`，改用 `--apply --confirm` 执行同一业务 payload。人工 sale 不接收
`--fees`；写入时冻结公式费用。只有具备 canonical broker order identity 的
broker sale 才能由统一 OpenD 订单费用同步升级为 actual，包括实际零费用。

Broker stock sell intake 仅在 deal 能唯一匹配开放 assigned-stock lot 时写 sale；
无法安全归属时进入人工 review。

## Review 与失败语义

常见非 ready 状态包括：

- `missing_stock_settlement`
- `incomplete_inventory_basis`
- `missing_stock_sale`
- `missing_quote`
- `source_conflict`
- `manual_review_required`
- `covered_call_unallocated`
- `covered_call_unrealized_missing`

这些状态允许返回已知事实和下一步诊断，但不能输出伪造的完整 lifecycle PnL。
不要用 strike、当前 broker holdings、当前 FX、当前 spot、零费用或另一个账户的数据
补缺口。

## 模块所有权

| 边界 | Owner |
|---|---|
| assigned-stock projection | `domain/domain/assigned_stock.py` |
| production period PnL/cash | `domain/domain/performance/engine.py` |
| ledger input adapters | `src/application/performance/adapters.py` |
| assigned-stock read view | `src/application/positions/assigned_stock_view.py` |
| manual sale workflow | `src/application/positions/workflows.py` |
| canonical ledger boundary | `src/application/ledger/api.py` |
| public Tool Gateway | `src/application/agent_tools/positions.py` |

OM Copilot 只能基于 registry 投影的 pure-read tool observations 表达事实，不能自行
合成 spot、sale、费用、金额或股数。显式命令和待确认写操作仍走 deterministic
Control / CLI 边界。

## 验收约束

- 同一 assignment 不重复创建 stock lot。
- partial/full sale 后 shares、basis 和 cash 守恒。
- option premium、stock price movement、settlement principal 和费用不重复计算。
- `realized = option_realized + assigned_stock_realized` 在完整同币种证据下成立。
- historical as-of 不读取未来 quote、sale、event 或 FX。
- missing/estimated fee、quote、FX 或 lifecycle evidence 保持 partial。
- production performance 与 assigned-stock read view 使用同一 canonical projector，
  但各自口径和 fee quality 不混用。
