# Assigned Stock Return Contract

本文记录 Cash-Secured Put (CSP) 被指派后的正股事实、估值和收益合同。它描述当前实现，
不是未来设计或迁移计划。

## 核心边界

```text
canonical trade_events
  -> option allocation / position_lots
  -> assignment stock settlement
  -> assigned-stock lifecycle projection
  -> assigned-stock read view
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

## 与 Option Performance 的边界

`option_performance_report` 不读取 assigned-stock projection，也不包含正股交割本金、
卖出回款、正股费用、正股已实现/未实现 PnL 或行情估值。指派或行权事件只作为期权
lot 的终结状态参与期权胜率；正股事实和收益只由本模块的 assigned-stock read view
提供。两个模块不得互相合成缺失指标。
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

read view 可以展示明确标注的 estimated fee；这些正股指标不进入
`option_performance_report`。
CC 只有在显式 `stock_lot_id` 关联，或完全可证明的 assigned-stock FIFO
场景下才归属；mixed ordinary/assigned inventory 会 fail closed。

按历史时点读取时，期权终结与正股交割分别按各自生效时间判断。期权可以已经终结，正股买入或卖出在内嵌交割时间到达前仍不进入库存与现金投影；撤销图和交割身份验证保持有效。

## CNY 与 FX 证据

现金 CNY 和非现金 PnL/估值 CNY 是两条合同：

1. canonical trade/assignment event 在写入事务中把
   `cash_conversion.v1` 保存到 `raw_payload.cash_conversions[fact_kind]`；
   assigned-stock sale 把相同结构保存在 sale fact 的
   `cash_conversions[fact_kind]`。
2. snapshot 必须匹配 `cash_fact_id`、native amount/currency 和
   `quote_currency=CNY`。新的市场报价折算统一使用 `event_time_market_fx`：选择事件时点
   之前最近的已验证报价，由 `domain.domain.fx_quote_policy` 判断交易时段和连续休市。
   已核实长假期间可沿用原报价；重新开市缺价、交易时段断档或日历未知时写
   `status=pending`、`amount_cny=null`。原报价时间、观察时间、来源及日历依据随 snapshot 保存。
   已有日固定价及官方历史折算仍按原方法验证，不自动重算。
3. 幂等重试保留第一次写入的 conversion snapshot。读取报告不会用后来的汇率
   重算现金；legacy event 没有 snapshot 时保持 native-only/partial。
4. activity、PnL 和 valuation 的 CNY 使用
   `option_performance_evidence.v1` FX facts，并受自己的 as-of 与 stale gate
   约束。这里的 FX evidence 不能反向替代现金 booking snapshot。

分配型正股销售的父事件与各 lot 子分配各有独立现金事实。历史回填及显式 superseding FX 纠正在同一父事件事务中逐项更新，审计绑定各自的旧、新 conversion；预览不写入，重试不重复修复。

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
  --period ytd \
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
| option performance（不含正股） | `domain/domain/performance/weighted_reducer.py` |
| ledger input adapters | `src/application/performance/adapters.py` |
| assigned-stock read view | `src/application/positions/assigned_stock_view.py` |
| manual sale workflow | `src/application/positions/workflows.py` |
| canonical ledger boundary | `src/application/ledger/api.py` |
| public Tool Gateway | `src/application/agent_tools/positions.py` |

Bot 只能基于 registry 投影的 pure-read tool observations 表达事实，不能自行
合成 spot、sale、费用、金额或股数。显式命令和待确认写操作仍走 deterministic
Control / CLI 边界。

## 验收约束

- 同一 assignment 不重复创建 stock lot。
- partial/full sale 后 shares、basis 和 cash 守恒。
- option premium、stock price movement、settlement principal 和费用不重复计算。
- historical as-of 不读取未来 quote、sale、event 或 FX。
- missing/estimated fee、quote、FX 或 lifecycle evidence 保持 partial。
- option performance 与 assigned-stock read view 复用 canonical ledger/projector，
  但期权指标不纳入正股现金、成本或 PnL。

## Put 指派期间当前估值查询

`option_positions_read(action=assigned-stock)` 可指定 `assignment_year`（整数）、
`assignment_month`（YYYY-MM），或同时指定 `assignment_start_date` /
`assignment_end_date`（YYYY-MM-DD，用户日期含两端）。三种选择互斥。
当前年/月按 YTD/MTD，截至请求时刻；过去期间按完整自然期间选批次，均使用
Asia/Shanghai。期间只选择指派生效批次，持有/已售状态来自本次固定账本，
价格为当前合格观察。新期间查询固定 `status=all`，不能与显式 `as_of_ms`
同时使用。旧无期间参数的通用 assigned-stock 和历史查询保持原语义。

示例（读取会请求合格现价与汇率，不写账本或估值缓存）：

```bash
./om tools run --tool option_positions_read --input-json \
  '{"config_key":"us","action":"assigned-stock","assignment_year":2026}'
```

消息命令支持 `/assigned-stock month=2026-11`、`year=2026`、
`from=2026-10-01 to=2026-10-31`，可附账户、标的和 `no-refresh`。
`no-refresh` 或注入冻结报价时不联网取得报价/FX；缺失 FX 保留原币结果。

新结果版本为 `option_positions_read.assignment_period.v1`。`summary.by_currency`
分别提供正股剩余浮盈亏与余股抵扣 Put 净权利金后的盈亏，`cny_estimate`
独立保留汇率证据及覆盖；账户/标的/批次下钻沿用相同未展示舍入的 Decimal
金额。完全售出且数量无冲突的批次两项剩余盈亏为零，不需现价。已售已实现
及对应 Put 净额单列，组合其他腿、CC 收益及期间现金流不叠加。

查询经 ledger owner 的同连接短只读事务取得有效交易和股票卖出事实，再释放
事务取价。trade intake owner 只读发现未入账股票卖出及账户未归属状态；不能从
没有 sale allocation 推断真实零卖出。已知归属冲突或相关来源不可读时，受影响
实际数量与盈亏为 null；`book_shares_sold/book_shares_remaining` 只表示账本事实。
仅缺历史全量证明而无已知冲突时，可返回明确 `basis=ledger`、`coverage=partial`
的估算，并列 `provisional_lot_ids`。原始成交与人工确认事件不能凭相近时间自动
配对。未知与完整零匹配分开，Bot 必须引用完整 owner 汇总，不对截断明细求和。
