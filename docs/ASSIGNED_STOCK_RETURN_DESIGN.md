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
- 历史数量和策略元数据复用同一份已验证事件选择，保留用于重述该边界的晚到 void；
  不再次按时间丢弃这些控制记录，也不纳入边界之后的开仓或策略调整。

当前正股估值共用 `market_data_provider.get_stock_valuation_quote`，OpenD 实现归
`opend_market_snapshot_fetching`。接货刷新（包括默认未指定 preserve_evidence 的路径）、
组合估值及当前绩效的结构化 spot 入口均复用这一选择；原始观测接口不改 readiness。
正常交易时沿用新鲜 `quote_observation`。休市或盘前由同一报价入口
查询完整原生日历，仅请求最近完整结束交易日的 `K_DAY / NONE` 未复权日线；不调用
历史回填或用 `prev_close_price` 估算替代。单日结果必须完整分页且只有对应代码/日期
的一行。午休、盘中及收盘竞价不以未完成的当日日线估值，未来时间或币种冲突保留缺口。

此路径在既有估值 owner 中使用 `om.closed-market-carry.v2`（仅单只正股）：
`daily_close` 保留原始请求、行、完整性和实际取得毫秒，`session_observation` 保留
原观测及独立取得时间，`calendars` 保留带取得时间的完整原生日历。收盘生效时间由
同一市场时段政策、交易日期及半日市类型推导，保持在 `quote_time_ms`；本次日线
取得时间另列于 `quote_observed_at_ms` 与 mark 的 `available_at_ms`，不得回填成
收盘时已经取得。凭证在下一正常时段开盘时失效，使用时再次校验日期、价格、代码、
币种及可知时间；已有 v1 封存凭证的严格口径不变。日历查询窗口为当前市场日期前后
14天；无法证明最近完整交易日或下一开盘时刻时返回缺口，不外推。

`quote_refresh.received_symbols` 仅表示收到观测，`qualified_symbols` 与
`refreshed_symbols` 表示最终估值时确实可用；`observation_status` 留存各标的原观测
状态，`qualification_gaps` 及原始错误列保留遗漏原因。取得后因时效到期而被排除也
进入这些缺口；部分可用返回 `partial`，全无可用返回 `missing_quote`，来源错误不
改写成合法空结果。新报价不改变批次的 confirmed/ledger-only 判定或实际费用要求。

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
Asia/Shanghai。期间明细的 `assigned_date` 也从 `assigned_at_ms` 按同一时区
生成，包括账户／标的下钻，避免美股当地日期与北京时间跨日时显示不一致。
不更改指派时间戳或底层生命周期的市场日期。期间只选择指派生效批次，持有/已售状态来自本次固定账本，
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

期间查询的 Put 毛权利金和实际期权费直接消费 canonical economic allocation，
保留 allocation/source-event 身份及精确金额；最后一次关闭的开仓费用余数不可再次按
源开仓合约比例重算。期间明细仅暴露本次口径的金额，报价状态/市场时刻和
`assigned_stock_unrealized_pnl` 别名与合格后的正股指标保持一致，避免混入初始无价
投影的 null 或不相关生命周期/CC 总额。

未归属股票卖出的候选按其经济时点有效库存确定，不能只按指派日期或当前余股判断。
旧 state 的重复观察只有在精确作用域/执行身份、完整源经济内容及已应用分配都得到
证明时才能解除。仅状态字符串或结构上可解析的 sale 不构成已生效分配证明。

Bot 超过观察预算时保留 owner 已计算的完整概览、分户汇总和每项 basis/coverage，
明确省略明细；轻量批次索引与同期间 `stock_lot_id` 选择支持下钻。索引自身截断时
也标注覆盖。绝不从下钻子集重算全局合计，不提高原观察预算。期间请求对不支持的
旧事件过滤参数返回 INPUT_ERROR，诊断用 OpenD host/port 则继续交由原报价入口消费。

来源资格读取沿用入口已选择的账本 runtime 根目录，不重新用代码目录或当前用户环境
选择另一份 intake state。持久化旧富途原始成交只有在 REAL 账户映射、既存 scoped
成交键及 canonical legacy identity 一致，且仅缺新版受理字段时，才按历史来源观察；
不补写账本、不放宽新成交受理。其他身份无效观察保留账户、标的、经济时间和原因，
按该时间的有效库存判受影响批次；已被规范化校验判为矛盾的币种不能用于排除候选。
真正读取失败或 state 缺失仍保留对应 scope 缺口。
声明账户与物理账户映射矛盾时，在筛选请求账户前标记涉及 scope，不泄露来源私有ID。

期间明细的 `valuation_basis` 区分 `confirmed`、`ledger`、`unavailable`；`confirmed`
对象只承载已核实数量/损益，`provisional` 对象明确承载无已知冲突的账本暂估及
`source_history`。既有顶层金额和汇总的账本 basis/partial 语义不变，不代表券商历史
已完整核实。数量待核实时，已售净额和已售份额权利金也不可报告为确定数值。
