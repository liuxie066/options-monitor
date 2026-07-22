# 被指派正股收益设计

本文定义 Sell Put 被指派后，正股持仓、实时估值、历史数据和收益统计的目标语义。

目标不是改变当前期权账本事实，而是在现有 `trade_events -> projection -> position_lots` 之上，补齐被指派后正股生命周期的收益归因。正股成本按真实交割事实记录，权利金留在 option 侧，组合收益在查询时合并计算。

## 1. 成功标准

- Sell Put 被指派后，系统记录接货正股的真实成本。例如 strike 100、权利金 2.5、被指派买入 100 股，正股成本记为 100。
- 不记录扣除权利金后的正股成本。它只是 `option premium + stock PnL` 的等价展示，不应成为持仓事实字段。
- 收益统计不能重复计算权利金。权利金作为 option activity，正股盈亏按真实正股成本计算，组合收益在 performance 层合并。
- 被指派的 option close、正股交割、后续卖出、实时 spot 和历史 mark 都有明确数据来源。
- 缺失数据必须显式暴露，不用当前 spot、当前持仓或推测值回填历史事实。

## 2. 当前事实边界

当前 canonical option ledger 仍然是：

```text
trade_events
  -> projection
  -> position_lots
```

`assignment` 是关闭 short option lot 的事件，不是普通买平，也不是到期归零。它必须解析到确定 `target_lot_id`。

当前 canonical performance / assigned-stock projection 有三类相关输出：

- `activity.premium_collected_gross`：short open 收到的权利金活动。
- `pnl.realized_gross` / `pnl.realized_net`：option lot 平仓、到期、指派或行权的 option 侧已实现收益。
- `cash.stock_settlement_cash_gross`：assignment event 里记录的正股交割现金流。

这些字段表达“发生了指派交割”。接货后的正股 lot、实时浮盈亏、后续卖出后的生命周期收益由
`assigned_stock_lots` / `assigned_stock_events` 和查询时的 quote snapshot
派生。

当前范围只覆盖 Sell Put 被指派后买入正股。Short Call 被指派卖出正股、普通股票交易账本、分红和税务口径不进入本设计的当前实现。

## 3. 核心设计原则

### 3.1 原始事实和派生口径分离

原始事实只记录真实发生的事件：

- short put open：收到权利金。
- assignment option close：option lot 被指派关闭。
- stock settlement：按交割价买入正股。
- assigned stock sale：未来卖出这批正股。
- fee / currency / account / broker / source id：作为事实附属字段记录。

正股成本、浮盈亏、生命周期收益应分层表达：正股成本是交割事实；浮盈亏和生命周期收益是查询时派生口径。

### 3.2 正股成本按交割价记录

示例：

```text
Sell Put: strike=100, premium=2.5, multiplier=100, contracts=1
Assignment: buy 100 shares at 100
```

原始现金流：

```text
option premium in = 2.5 * 100 = 250
stock settlement out = 100 * 100 = 10000
net cash out = 9750
stock cost per share = 100
```

账本存储只保留真实发生的两类事实：

```text
option premium = 250
stock cost basis = 100 * 100
```

组合收益在查询时计算：

```text
assignment_lifecycle_pnl =
  option_premium_cash_in_net
  + stock_market_or_sale_value_net
  - stock_cost_basis
```

这样不会把权利金塞进正股成本，也不会在组合收益里重复计算权利金。

### 3.3 三类口径必须分开

被指派后的报表必须同时区分三类口径：

| 口径 | 含义 | 示例 |
|---|---|---|
| `cashflow` | 真实现金流入流出 | 收权利金 +250，买入正股 -10000 |
| `pnl` | 投资收益或浮动收益 | spot 98 时，option +250，stock -200，组合 +50 |
| `capital_deployed` | 被指派后占用的正股成本 | 100 股 * 100 = 10000，加正股交割费用 |

`assignment_stock_net_cashflow_gross=-10000` 只能说明接货现金流出，不能被当作当月亏损。用户问“收益”时，应优先展示 `pnl` / `assignment_lifecycle_pnl`；用户问“现金流”或“资金占用”时，才展示 cashflow / capital deployed。

## 4. 存储模型

### 4.1 Assignment event

`assignment` event 继续作为 option lot 的 canonical close fact。它至少需要：

```json
{
  "event_type": "assignment",
  "target_lot_id": "...",
  "contracts": 1,
  "price": 0,
  "raw_payload": {
    "stock_settlement": {
      "side": "buy",
      "shares": 100,
      "price": 100,
      "currency": "USD",
      "fees": 0,
      "fee_provenance": {
        "basis": "actual",
        "source": "broker_lifecycle",
        "reason": "broker_reported_fee"
      },
      "source": "broker_lifecycle|manual",
      "source_deal_id": "optional"
    }
  }
}
```

说明：

- `stock_settlement.price` 是交割事实，通常接近 strike；它不是实时 spot。
- `price=0` 的 option close 可以是合法生命周期事实，不应被当成普通 0 价买平。
- `stock_settlement` 只表示真实正股交割事实；扣除权利金后的成本不作为字段存储。

### 4.2 Assigned Stock Lot

当前派生读模型 `assigned_stock_lots` 由 assignment event 和后续 assigned stock sale events 重建。

核心字段：

| 字段 | 语义 |
|---|---|
| `stock_lot_id` | 稳定 lot id，可由 assignment event id 派生 |
| `source_assignment_event_id` | 来源 assignment event |
| `source_option_lot_id` | 被指派的 option lot |
| `account` / `broker` / `symbol` / `currency` | 账户、券商、正股、币种 |
| `opened_at_ms` | 正股交割时间 |
| `shares_opened` | 被指派买入股数 |
| `shares_remaining` | 当前未卖出股数 |
| `assignment_price` | 交割价格，通常为 strike |
| `assignment_notional` | `assignment_price * shares_opened` |
| `assignment_fees` | 正股交割相关费用 |
| `stock_cost_per_share` | 正股账面成本，默认等于 `assignment_price` |
| `stock_cost_basis_total` | `assignment_notional + assignment_fees` |
| `basis_policy` | `assignment_stock_cost_basis` |
| `status` | `open` / `partially_sold` / `closed` |
| `sale_event_ids` | 后续卖出事件 |

`assigned_stock_lots` 是 projection，不是人工直接编辑的事实表。它可以保留 `source_option_lot_id` 用于查询时取 option premium，但不把 option premium 写入正股成本。修复应通过原始 assignment event、sale event、adjust/repair event 完成，再重建 projection。

### 4.3 Stock event 归属

`assigned_stock_sale` 不进入 canonical option `TradeEvent` 枚举。当前 option ledger 是 option-contract-centric，强行把股票卖出塞进 `trade_events` 会把边界扩大成通用股票账本。

当前独立投影/事实边界：

```text
trade_events
  -> assigned_stock_lots

assigned_stock_events
  -> assigned_stock_lots
  -> assignment_lifecycle_rows
```

其中：

- `trade_events` 继续只保存 option open / close / assignment / exercise 等事实。
- `assigned_stock_events` 只保存由 assignment 产生的正股 lot 的 sale / adjust / void / repair 事实。
- `assigned_stock_lots` 是 read model，可由两条事件流重建。
- 普通股票买卖不进入 `assigned_stock_events`，除非它明确关闭某个 `assigned_stock_lot`。

### 4.4 Assigned Stock Sale Event

被指派正股卖出需要记录。否则系统只能展示“仍持有时按 spot 的浮动收益”，无法确认真实已实现生命周期收益。

独立 stock sale fact 不伪装成 option close，也不写入 canonical option `trade_events`：

```json
{
  "event_type": "sale",
  "stock_event_id": "assigned-stock-sale-...",
  "target_stock_lot_id": "...",
  "account": "lx",
  "broker": "富途",
  "symbol": "NVDA",
  "side": "sell",
  "shares": 100,
  "price": 105,
  "currency": "USD",
  "fees": 1.2,
  "fee_provenance": {
    "basis": "actual",
    "source": "broker_payload.total_fee",
    "reason": "broker_reported_fee"
  },
  "trade_time_ms": 1780000000000,
  "source": "broker_deal|manual",
  "source_deal_id": "optional"
}
```

部分卖出按 lot 级显式 `target_stock_lot_id` 关闭。为了避免误配，当前手工录入必须显式指定目标 lot；broker intake 可以在唯一匹配开放 lot 时自动归属，并必须输出匹配证据和剩余股数变化。

### 4.5 幂等和目标匹配

写入 `assigned_stock_events` 前必须通过以下校验：

- 幂等键：broker 来源使用 `(broker, account, source_deal_id)`；manual 来源使用归一化 payload hash。
- 目标 lot：`target_stock_lot_id` 必须存在，且 account / broker / symbol / currency 一致。
- 数量：`shares > 0`，且 `shares <= shares_remaining`；超额卖出必须 fail closed。
- 价格：`price >= 0`；缺价格不能写 sale fact。费用可显式提供 `fees >= 0`，
  也可在支持的券商/币种上省略并按标准费表估算。
- 时间：`trade_time_ms >= assigned_stock_lot.opened_at_ms`。
- 状态：closed lot 不能再写 sale；void/repair 必须指向确定 stock event。

当前手工入口不做模糊匹配。若用户只输入 symbol 和 shares，系统只能返回候选 lot 让用户确认，不能自动猜。

## 5. 数据来源

### 5.1 被指派关闭的数据来源

优先级：

1. Broker lifecycle evidence：Futu option zero-price leg 与 stock settlement leg 匹配后，写入 canonical `assignment` event。
2. Manual CLI / Inbound preview：当 broker evidence 缺失、账户走 external holdings、或历史数据无法自动匹配时，由用户手动确认。
3. Repair workflow：已有 event 不完整或错配时，通过受控 repair/void/adjust 订正，不直接改 projection。

被指派关闭不是用 spot 推导出来的。spot 只能用于估值，不能作为 assignment settlement fact。

### 5.2 被指派正股卖出的数据来源

优先级：

1. Broker stock deal intake：从券商成交明细读取真实股票卖出成交，带 source deal id 做幂等。
2. Manual stock sale entry：自动来源缺失时，由用户录入卖出日期、股数、价格和目标 stock lot；费用可录入 actual，也可省略后估算。
3. Reconciliation only：external holdings / Feishu holdings 可以提示“持仓减少了，可能有未记录卖出”，但不能直接作为卖出事实写账。

如果卖出没有记录，收益报表必须显示该 stock lot 仍是 open 或 data incomplete，而不是用当前持仓差额猜测卖出价。

当前 broker stock deal intake 的实现边界：

- 只处理 `option_type=null` 且 `side=sell` 的股票成交；stock buy 仍由 assignment/exercise lifecycle settlement 逻辑处理。
- 自动写入前必须唯一匹配开放的 assigned-stock lot：`account`、`broker`、`symbol`、`currency` 一致，`shares_remaining >= 成交股数`，且 `trade_time_ms >= opened_at_ms`。
- 成功匹配后只写 `assigned_stock_events`，`source=broker`，`source_deal_id=<券商成交 id>`；不会把股票卖出写入 canonical option `trade_events`。
- 同一个 `source_deal_id` 重复进入时按既有 `assigned_stock_events` 做幂等；payload 不一致时返回冲突，不静默覆盖。
- 没有 matching assigned-stock lot 的普通股票卖出继续按 `skipped/not_option_deal` 处理；存在候选 lot 但无法唯一安全归属时返回 unresolved，等待人工确认目标 lot。

### 5.3 Reconciliation 的边界

broker holdings、external holdings 或 Feishu holdings 只能作为 reconciliation evidence：

- holdings 显示股数减少，但没有 broker deal / manual sale event：标记 `missing_stock_sale`。
- holdings 与 `shares_remaining` 不一致：标记 `source_conflict`。
- holdings 不能反推卖出价、成交时间或费用，也不能直接关闭 `assigned_stock_lot`。

只有 broker stock deal intake 或用户确认后的 manual sale entry 可以写入 sale fact。

## 6. 实时 Spot 获取

用户查看被指派正股收益时，实时 spot 是估值 snapshot，不是账本事实。

推荐查询顺序：

1. 当前时点且调用方未传 `quote_snapshots` 时，默认通过 OpenD / Futu quote
   adapter 获取开放 assigned-stock lot 的正股最新报价。
2. `refresh_quotes=false` 显式关闭刷新；调用方提供 `quote_snapshots` 时不隐式覆盖，
   但仍可显式传 `refresh_quotes=true` 请求刷新。
3. 若实时 quote 不可用，可使用本次运行已有 required-data / quote snapshot，但必须带 `quote_source`、`quote_time` 和 stale 标记。
4. 若仍不可用，返回 `quote_status=missing_quote`，正股成本和已实现现金流可展示，浮动收益和总收益为 `null`。

返回结果应包含：

| 字段 | 语义 |
|---|---|
| `spot` | 当前或指定 as-of 的正股价格 |
| `spot_time` | 行情时间 |
| `quote_source` | `opend_realtime` / `required_data_snapshot` / `manual_snapshot` |
| `quote_status` | `fresh` / `stale` / `missing_quote` |
| `remaining_market_value` | `shares_remaining * spot` |
| `stock_unrealized_pnl` | `remaining_market_value - remaining_stock_cost_basis` |

实时 spot 不写入 `trade_events`。如果需要历史复盘，应写入独立 mark snapshot，不要污染交易事实。
当前实现也不把 realtime spot 写入 `assigned_stock_events`；它只作为本次查询的
`quote_snapshots` 输入重算 `remaining_market_value`、`assigned_stock_unrealized_pnl`
和 `assignment_lifecycle_pnl`。

### 6.1 As-of 与历史 mark

收益查询应支持两种时间语义：

- `as_of=now`：读取实时 quote 或最新 snapshot，输出 `quote_status` 和 `spot_time`。
- `as_of=<历史时间>`：只能使用已保存 mark snapshot、broker/market data 历史行情，或用户显式 manual snapshot。

历史 as-of 查询不能用今天的 spot 回填。若指定时间没有可用 mark，返回 `missing_quote`，只展示事实现金流、正股成本和缺失诊断。
因此 `refresh_quotes=true` 与历史 `as_of_ms` 同时出现时，查询会跳过 realtime refresh，
返回 `quote_refresh.status=skipped_historical_as_of` 和 warning。

## 7. 收益统计口径

### 7.1 报表明确拆分

收益查询按以下区块派生：

- Option income：short put open premium、buy close、expire、assignment option close。
- Assignment stock lots：被指派形成的正股 lot、真实正股成本、剩余股数、spot、浮动收益。
- Assigned stock realized sales：被指派正股卖出后的已实现收益。
- Lifecycle total：从 sell put open 到正股完全卖出的完整生命周期收益。

### 7.2 Lifecycle total 公式

正股仍持有时：

```text
lifecycle_pnl =
  option_premium_cash_in_net
  - assignment_stock_notional
  - assignment_stock_fees
  + remaining_shares * spot
  + stock_sale_cash_in_net
```

正股全部卖出后：

```text
lifecycle_pnl =
  option_premium_cash_in_net
  - assignment_stock_notional
  - assignment_stock_fees
  + stock_sale_cash_in_net
```

这个口径等价于“权利金收入 + 正股盈亏”，但不记录扣除权利金后的正股成本，也不会把权利金加第二遍。

### 7.3 费用、汇率和时间口径

费用归属：

- `option_premium_cash_in_net = option_open_premium_gross - option_open_fees`。
- `assignment_stock_notional = assignment_price * shares_opened`。
- `assignment_stock_fees` 只包含正股交割相关费用。
- `stock_sale_cash_in_net = sale_price * shares_sold - sale_fees`。
- `assigned_stock_unrealized_pnl` 不含 option premium，只等于 remaining market value 减 remaining stock cost basis。
- `assignment_lifecycle_pnl` 包含 option premium、已卖出正股收益、剩余正股 mark value 和全部相关费用。

汇率归属：

- 交易事实使用成交币种保存。
- CNY 汇总优先使用交易日或 mark as-of 日对应汇率。
- 若没有对应日期汇率，只能使用 canonical performance evidence 中显式标注时点和来源的汇率；否则 CNY 保持缺失。
- 缺汇率时 CNY 字段为 `null`，原币字段仍保留。

时间归属：

- option premium 归属 open event 月。
- assignment stock settlement cashflow 归属 assignment event 月。
- stock sale realized PnL 归属 sale event 月。
- assignment lifecycle PnL 可以按 query as-of 展示，不应强行塞进单个月度已实现收益，除非 stock lot 已完全关闭。

### 7.4 与 canonical option performance 的关系

被指派正股收益作为组合口径加入，不改写 canonical namespace 的字段含义：

- `cash.stock_settlement_cash_gross` 表示交割现金流。
- `activity.premium_collected_gross` 表示 short open 权利金活动。
- assignment lifecycle 字段必须标明它是组合口径，已经包含 option premium 与 stock PnL 两部分。

字段命名：

| 字段 | 语义 |
|---|---|
| `assigned_stock_cost_per_share` | 正股成本，默认等于交割价 |
| `assigned_stock_unrealized_pnl` | 正股自身浮盈亏，不含 option premium |
| `assigned_stock_realized_pnl` | 正股自身已实现盈亏，不含 option premium |
| `assignment_lifecycle_pnl` | 完整生命周期口径，等于 option premium + stock PnL |
| `option_premium_attribution` | option 侧权利金归因，和 stock PnL 分开展示 |

### 7.5 查询入口

- `/income` 和其他报表调用方统一使用 `option_performance_report`：

- `option_performance_report` 分开输出 activity、cash、PnL、capital 和 assignment lifecycle。
- 只读查询能力 `option_positions_read action=assigned-stock` 与 performance service 共用 canonical assigned-stock projection。
- 只读查询能力 `option_positions_read action=assigned-stock` 专门回答被指派正股 lot、spot、浮盈亏、卖出和 lifecycle PnL；当前时点默认补实时估值，`refresh_quotes=false` 显式关闭。
- Inbound `/income` 不把 `cash.stock_settlement_cash_gross=-10000` 解释为亏损；用户问“被指派股票收益”“接货后盈亏”“assigned stock”时才调用 assignment lifecycle 口径。
- 自然语言入口由 Agent Composer 基于工具 evidence 表达；LLM 不能自己合成 spot、卖出价、
  missing sale、金额或股数。若 guard 不通过，回退到 deterministic renderer。

## 8. 历史数据处理

历史数据分三类处理。

### 8.1 已有完整 assignment stock settlement

如果 assignment event 已有 `stock_settlement`：

- 直接重建 `assigned_stock_lots`。
- 正股成本按 `stock_settlement.price` 和带 provenance 的实际/估算费用重建；
  没有 provenance 的历史零费用不能自动认定为 actual。
- 若没有 sale event，则只展示 open lot 和实时估值。

### 8.2 assignment event 缺 stock settlement

不能静默用 strike 或当前持仓补齐。

处理流程：

1. 进入 review 队列，标记 `missing_stock_settlement`。
2. 若 broker lifecycle evidence 有匹配 stock leg，用 repair 写入事实。
3. 若没有 broker evidence，要求用户手动确认 stock side、shares、price、date；费用若未知可保留 estimated/missing provenance，不能伪装成 actual zero。
4. 可以提供默认建议 `shares=contracts*multiplier`、`price=strike`，但必须由用户确认后才写入。

### 8.3 历史卖出缺失

如果用户已经卖出被指派正股，但系统没有 sale event：

- 优先导入 broker 历史 stock deals。
- 导入失败时，用户手动录入卖出事实。
- 当前 holdings 只能作为 reconciliation evidence，不能反推卖出成交价。

### 8.4 历史 spot

历史 as-of 收益不能用今天的 spot 回填。

可用来源：

- 已保存的 mark snapshot。
- broker / market data 历史行情接口。
- 用户显式输入的 manual snapshot。

没有历史 spot 时，历史报表只输出已实现现金流和缺失诊断。

### 8.5 Review 状态机

历史扫描和在线 reconciliation 应输出结构化状态：

| 状态 | 含义 | 可执行动作 |
|---|---|---|
| `ready` | assignment、stock settlement、sale/remaining shares 和 quote 都可解释 | 可计算 lifecycle PnL |
| `missing_stock_settlement` | assignment 缺正股交割事实 | 查 broker lifecycle evidence 或用户确认 settlement |
| `missing_stock_sale` | 持仓减少或用户声称已卖出，但没有 sale event | 导入 broker stock deal 或用户录入 sale |
| `missing_quote` | open assigned stock 缺 as-of spot | 获取实时 quote、历史 mark 或 manual snapshot |
| `source_conflict` | broker/external holdings 与 assigned stock lots 不一致 | 人工 review，不自动修 |
| `manual_review_required` | 自动匹配证据不足或多 lot 可疑 | 用户确认目标 lot / 数量 / 价格 / 费用 |

任何非 `ready` 状态都不能输出完整 lifecycle PnL。可以输出已知事实、缺失字段和下一步建议。

## 9. 当前已落地能力

1. `assigned_stock_lots` 由 assignment event 和 `assigned_stock_events` 重建。
2. `assigned_stock_events` 记录被指派正股 sale fact，保留 source id 幂等和目标 lot 校验。
3. `option_positions_read action=assigned-stock` 返回 lot、spot、浮盈亏、已实现正股盈亏和 lifecycle PnL。
4. 当前时点默认获取开放 assigned-stock lot 的实时 spot；`refresh_quotes=false` 关闭，历史 `as_of_ms` 不用实时 spot 回填。
5. `om option-positions assigned-stock-sale` 支持人工录入 sale，默认 dry-run / confirm，并要求显式目标 stock lot；省略费用时估算，显式 `--fees 0` 才是 actual zero。
6. Broker stock sell intake 可以在唯一匹配开放 assigned-stock lot 时写入 sale fact；无法安全归属时等待人工确认。
7. `option_performance_report` 和 assigned-stock read surface 共用 canonical projection，并输出一致的 assignment lifecycle facts。

## 10. 验收用例

### 10.1 Open assigned stock

输入：

```text
sell put 100P, premium 2.5, assigned buy 100 shares at 100, current spot 98
```

期望：

```text
stock_cost_per_share = 100
assigned_stock_unrealized_pnl = (98 - 100) * 100 = -200
option_premium_attribution = 250
lifecycle_pnl = 50
不记录扣除权利金后的成本字段
```

### 10.2 Sold assigned stock

输入：

```text
sell put 100P, premium 2.5, assigned buy 100 shares at 100, sell 100 shares at 105
```

期望：

```text
stock_cost_per_share = 100
assigned_stock_realized_pnl = (105 - 100) * 100 = 500
option_premium_attribution = 250
lifecycle_pnl = 750
不记录扣除权利金后的成本字段
```

### 10.3 Missing spot

输入：

```text
assignment fact complete, realtime quote unavailable
```

期望：

```text
stock_cost_per_share 可展示
remaining_shares 可展示
unrealized_pnl = null
quote_status = missing_quote
```

### 10.4 Missing stock sale

输入：

```text
assigned_stock_lot exists, current broker holdings no longer contains shares, no sale event
```

期望：

```text
report status = incomplete
diagnostic = assigned_stock_sale_missing
不从持仓差额推导卖出价
```

### 10.5 Assignment cashflow is not monthly loss

输入：

```text
sell put 100P in April, premium 2.5
assigned buy 100 shares at 100 in May
current spot 98 in May
```

期望：

```text
May assignment_stock_net_cashflow_gross = -10000
May capital_deployed = 10000
May assigned_stock_unrealized_pnl = -200
option_premium_attribution = 250
assignment_lifecycle_pnl_as_of = 50
不能把 -10000 当作 May PnL
```

## 11. 暂不纳入

- 税务口径。
- 分红收益归因。
- 全量普通股票交易账本。
- 未被 assignment 产生的普通股票持仓成本。
- 用 LLM 自动修账或自动确认历史成交。

## 11. S5 统一投影与期权收益集成

S5 将被指派正股生命周期的语义所有权收敛到：

```text
domain.domain.assigned_stock.project_assigned_stock_lifecycle
```

`positions.reporting` 只负责把旧报表参数适配给该投影，不再保留第二套 assignment lot、sale、fee、covered-call 或 holdings reconciliation 算法。新期权收益服务通过 `src.application.ledger.api.assigned_stock_event_log` 读取正股 sale facts；repository capability probing 只允许存在于 ledger query boundary。缺能力、读取异常、非 list payload 和坏行都返回结构化 diagnostics，不静默假设为空。

### 11.1 Performance 口径

新 performance 口径严格区分 principal、fee 和 option premium：

```text
stock_realized_gross = sale_gross_cash - sold_settlement_principal
stock_unrealized_gross = market_value - remaining_settlement_principal
stock_period_total = realized + ending_unrealized - opening_unrealized
```

- assignment option premium 仍由 canonical option allocation 计入 option realized PnL；stock side 不重复加入。
- assignment settlement fee 在 assignment 时点只计一次 net PnL / cash fee fact。
- sale fee 在 sale 时点只计一次 net PnL / cash fee fact。
- `actual zero` 是完整费用证据；`estimated` / `missing` 保留 gross，但对应 net 为 partial。
- 旧 monthly lifecycle 展示仍可保留 estimated fee 估算；production option-performance net 不使用估算费用。

### 11.2 Boundary、估值和 void

opening / ending assigned-stock inventory 和 option inventory 使用同一 restated boundary 规则。重建历史边界时会纳入合法的 later void，因此一个后来被 void 的 assignment 不会在更早边界重新生成幽灵 stock lot。

历史报告只选择持久化 `StockInstrumentKey` mark，不调用实时 quote。当前 partial report 才能把开放 assigned-stock instruments 交给 read-through collector；读取报告本身不持久化 mark。

### 11.3 Covered Call 归因

Covered Call 与 assigned-stock lot 的关联顺序：

1. 优先使用 open call 上显式的 `stock_lot_id` / `target_stock_lot_id` / `source_stock_lot_id`。
2. 没有显式 link 时，只有在 holdings evidence 不显示普通股与 assigned stock 混仓时，才允许 FIFO 推导。
3. FIFO 归因必须输出 `covered_call_allocation_quality=heuristic`，并将 lifecycle quality 降级。
4. mixed ordinary/assigned inventory、股数不足或显式目标不成立时 fail closed，输出 `covered_call_unallocated`。
5. share reservation 防止同一股数支撑重叠 call；closed call realized 和 open call marked unrealized 都进入 lifecycle 展示。
6. Covered Call option economics 不再次加入 top-level option-performance PnL，因为 canonical option facts 已经拥有这些收益。

### 11.4 当前范围外

S5 仍不创建普通股票账本，也不处理 dividend、tax、split、short-call assignment stock basis 或未知来源股票 inventory。上述场景保持 `incomplete_inventory_basis` 或独立后续 work unit，不用推测值补齐。
