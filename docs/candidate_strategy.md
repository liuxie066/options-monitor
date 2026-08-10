# Sell Put / Covered Call 候选策略合同

> 状态：已确认的目标口径（2026-08-06）
>
> 权威范围：Sell Put / Covered Call 的召回、数据、筛选、容量、排序和候选快照
>
> 实施状态：本文件是后续实现与验收依据，不表示当前代码已经全部对齐

本文是 Sell Put / Covered Call 开仓候选策略的唯一细则真源。产品域和依赖方向见
[STRATEGY_ARCHITECTURE.md](STRATEGY_ARCHITECTURE.md)；Combo Yield 保持独立策略，
不从本文件推导其组合筛选和排序规则。

## 1. 策略目标

### Sell Put

核心目标是承担“价格合适时接货”的风险，用权利金参与投资布局并获取收益：

- 愿意接货，但不主动追求接货；
- 按可执行的 `mid` 限价等待，不追价；
- 在现有 DTE 窗口内，偏好覆盖较长周期的一次性权利金；
- 正式比较整个持有周期的非年化净收益，不用年化收益主导排序；
- 接货能力必须真实存在，但候选不是自动下单或自动换汇授权。

### Covered Call

核心目标是在愿意以合适价格卖出正股的前提下，用权利金增强持股收益：

- 愿意被叫走，但不主动追求被叫走；
- 按可执行的 `mid` 限价等待，不追价；
- 在现有 DTE 窗口内比较整个周期的非年化净权利金收益；
- 收益接近时优先更高行权价，降低在相近收益下被叫走的概率；
- 必须有普通股票可覆盖，不允许裸卖 Call。

## 2. 共同流水线与所有权

每个账户、市场和 run 使用同一条流水线：

1. 应用层一次性收集并冻结 OpenD 行情、合约、事件、持仓、现金、汇率和账本事实；
2. Candidate Engine 执行召回后的归一化、硬筛、容量计算和正式排序；
3. 生成不可变、可校验、已封存的账户级 opening decision snapshot；
4. Agent、通知和 Position Advice 只消费该快照，不重新筛选或平行排序。

正式排序唯一所有者是：

- `domain/domain/engine/candidate_engine.py`

应用层不得再实现第二套评分、排序或候选裁剪。Combo Yield 可以共享标准化证据，
但必须使用 `strategy=combo_yield` 的独立策略结果，不能修改 Sell Put / Covered Call 决策。

## 3. 共同报价与合约合同

### 3.1 市场状态与新鲜度

- 市场状态只认 OpenD `market_state`。
- 只有连续交易时段生成正式可参与候选；闭市返回 `market_closed`。
- 标的 spot 只认同一 run 的 OpenD `last_price + update_time + market_state + sec_status`。
- 标的 spot 在连续交易时段内不得超过 5 分钟；标的 `update_time` 是 OpenD 最新价更新时间。
- 期权 snapshot 的 `update_time` 同样只是最新价更新时间，不能证明 bid/ask 何时变化；
  它只作为最新价活跃度诊断，不再作为候选可用性门槛。
- 期权候选的 5 分钟窗口约束的是 OM 取得 OpenD snapshot 到完成候选决策的时间
  （`snapshot_age_seconds <= 300`），用每批请求的 request/receive receipt 证明。
- 期权 bid/ask 的可用性由本次 snapshot 的盘口合法性（`bid > 0`、`ask >= bid`、
  tick、spread、合约身份与状态）判定，不由 `update_time` 判定。
- OpenD 明确返回 `bid=0` 且其余身份/盘口/状态证据完整时，视为当前无有效
  买盘的合法市场状态，合约归为 `ineligible`，不作为“证据缺失”告警；`bid`
  缺失、非有限或为负仍 `data_unavailable`。
- 不回退旧 required-data、CSV、收盘价或 `last` 作为 bid/ask 替代。
- 市场状态、spot 或关键时间戳缺失时，在最小受影响范围内 `data_unavailable`。

### 3.2 报价、价格档位和价差

```text
raw_mid = (bid + ask) / 2
spread_ratio = (ask - bid) / raw_mid
sell_limit = ceil(raw_mid / price_tick) * price_tick
```

- 必须满足 `bid > 0`、`ask > 0`、`ask >= bid`、`raw_mid > 0`。
- `spread_ratio <= 0.40` 是唯一通用流动性硬门槛。
- spread 使用原始盘口 `raw_mid`，不受价格档位向上取整影响。
- `price_tick` 使用同一合约、同一 run 的 OpenD `price_spread`；缺失、非正或冲突时合约不可用。
- 挂单、费用和收益计算都使用 `sell_limit`。

### 3.3 合约身份与交割物

- 只接受 OpenD `option_standard_type=STANDARD` 的标准合约。
- `stock_owner` 必须与目标标的一致，合约不得处于 suspended 状态。
- 交割物必须是普通股票；`NON_STANDARD` 合约直接拒绝，类型未知则不可用。
- multiplier 必须由同一合约的 chain 与 snapshot 绑定并一致。
- multiplier 缺失或冲突时只阻断该合约；不按标的缓存猜测，也不默认 `100`。
- 候选 ID 必须包含逻辑账户、物理 `futu_account_id`、市场、策略和合约身份。

### 3.4 可选观察字段

- OI 不设硬门槛，只在收益接近时参与排序。
- OI `0` 是已知值，必须与缺失区分；缺失排在可靠值之后。
- volume 和 delta 只展示，不参与硬筛和正式排序；缺失显示 `—`。
- bid/ask size 可作为诊断证据，但不新增硬门槛。
- OpenD IV 是百分号前数值，适配层固定除以 `100`；Delta 保持 `[-1, 1]`。

## 4. 收益、费用与共同硬门槛

### 4.1 权利金和费用

```text
gross_premium = sell_limit * multiplier
net_premium = gross_premium - estimated_full_sell_fees
```

- 候选阶段使用版本化的富途官方费用表，并采用保守上界估算完整卖出费用。
- 当前已确认的平台费率输入包括：美股 `USD 0.60/contract`、港股 Tier 1
  `HKD 3/contract`；其他适用交易所、监管和平台费用仍须按版本化费率表完整计算。
- 实际业绩只认券商回传的真实费用，不用候选估算覆盖成交事实。

### 4.2 最低收益

- 单张合约净权利金折算后必须至少为 `CNY 50`。
- 年化净收益必须至少为 `10%`，但年化值只作硬门槛。
- 年化使用日历 DTE：`period_return * 365 / DTE`。
- 正式排序使用持有周期非年化净收益。

### 4.3 波动率边际

- `IV / term_matched_RV >= 1.10`；
- `IV - term_matched_RV >= 0.05`；
- 任一必要 IV/RV 证据缺失时，在最小受影响范围内不可用，不做回退。

## 5. Sell Put

### 5.1 召回窗口

保持现有市场和 symbol DTE 配置窗口。strike 召回按以下顺序计算：

```text
recall_upper = min(configured_max_strike, live_opend_spot)
```

未配置 `max_strike` 时使用 live spot。再从 `recall_upper` 向下召回 20%，并保留
显式、更严格的 `min_strike`：

```text
recall_lower = max(configured_min_strike, recall_upper * 0.80)
```

- `max_strike` 是愿意接货的最高价，spot 是自然上限。
- spot 缺失时 fail closed，不用旧 spot 或单独的 configured max 猜窗口。
- DTE 不增加额外加分；较长周期偏好通过持有周期收益自然体现。

### 5.2 收益口径

```text
assignment_notional = strike * multiplier
net_cash_basis = assignment_notional - net_premium
period_net_return = net_premium / net_cash_basis
annualized_net_return = period_net_return * 365 / DTE
```

- 主收益是 `period_net_return`。
- 收益分母继续采用扣除权利金后的净资金口径。
- 接货能力采用未抵扣权利金的 gross `assignment_notional`，两者不得混用。

### 5.3 现金担保能力

- 只在同一物理 Futu 账户内计算，不跨物理账户聚合。
- 先使用合约同币种资金；不足部分再按 OpenD 汇率折算其他币种资金。
- 不对跨币种资金增加 `0.95` 或其他通用安全折扣。
- 跨币种汇率从 `observed_at` 起最长有效 24 小时；时间缺失或超过 24 小时即为 stale。
- stale / missing FX 对应的外币资金不参与覆盖；同币种资金仍可使用。只有确实需要该外币
  才能达到一张合约时，候选因资金证据不足 fail closed。
- 保留当前现金组成：明确的分币种 cash 加 OpenD `fund_assets`。
- 已知限制：OpenD `fund_assets` 是聚合基金资产，无法区分普通基金与货币基金；本策略接受当前口径。
- 已有开放 Short Put 先按 gross `strike * multiplier` 扣除担保占用，不抵扣历史权利金。
- 不额外读取或扣除待成交挂单、`frozen_cash`；操作员负责避免候选间重复使用。

```text
max_new_contracts = floor(effective_free_cash / assignment_notional)
```

- `max_new_contracts >= 1` 才具备开仓能力。
- 各候选共享同一现金池，候选容量不能相加。
- 每张候选按一张合约计算收益、费用和 CNY 50 门槛；实际数量由 Position Advice 或人工决定。

### 5.4 不作为硬门槛的指标

以下指标不阻断 Sell Put 候选：

- stress / gap-down / sigma stress；
- 路径压力和资本 charge；
- delta band；
- 单笔、单标的或总组合集中度；
- OI、volume、bid/ask size。

集中度只在跨标的且收益接近时参与排序。

### 5.5 排序

1. 仅排序全部硬门槛通过且容量至少一张的候选。
2. 以最高 `period_net_return` 为锚，将与其差值不超过 `0.002` 的候选组成收益区间，再处理下一组。
3. 同一标的、同一收益区间内依次比较：
   - 更高净接货折价；
   - 更小 spread；
   - OI 已知优先，再比较更高 OI；
   - 更高净权利金；
   - 稳定合约 ID。
4. 每个标的先选一张代表合约。
5. 不同标的、同一收益区间内依次比较：
   - 较低的接货后 symbol concentration；
   - 更高净接货折价；
   - 更小 spread；
   - OI 已知且更高；
   - 更高净权利金；
   - symbol 和合约 ID。

```text
breakeven = strike - net_premium / multiplier
net_assignment_discount_pct = (spot - breakeven) / spot
```

## 6. Covered Call

### 6.1 strike 底线与召回窗口

```text
sale_floor = max(configured_min_strike, opend_average_cost * 1.02)
recall_min = max(sale_floor, live_opend_spot)
recall_max = recall_min * 1.20
```

如果配置了 `max_strike`：

```text
recall_max = min(configured_max_strike, recall_min * 1.20)
```

- 保持现有 DTE 配置窗口。
- spot 缺失时 fail closed。
- `recall_max < recall_min` 表示当前没有可行窗口，返回等待，不改写边界。
- OM `avg_cost` 的唯一定义是平均成本价，直接映射同一物理账户、同一 symbol 的 OpenD `average_cost`。
- OpenD `cost_price` / `diluted_cost` 是摊薄成本口径，不得写入或回退为 OM `avg_cost`。
- OpenD `average_cost` 缺失时成本上下文 fail closed；不跨账户平均。

### 6.2 收益口径

```text
current_market_value = live_opend_spot * multiplier
period_net_premium_return = net_premium / current_market_value
annualized_net_premium_return = period_net_premium_return * 365 / DTE
```

- 主收益是整个持有周期的 `period_net_premium_return`。
- 分母使用当前市值，不使用历史成本或 strike。
- 年化收益只作 10% 硬门槛。
- 可展示被叫走时的事实收益，但该指标不参与开仓排序。

### 6.3 覆盖能力

- 持仓事实来自同一物理 Futu 账户的 OpenD `qty`、`can_sell_qty`、`average_cost` 和 currency；其中 `average_cost` 映射为 OM `avg_cost`。
- symbol 配置可以在多账户间共用；当前账户未持有该 symbol 时，该 Covered Call scope 以 `covered_call_underlying_not_held` 正常跳过，不会将其他已完成 scope 降级为 `data_unavailable`。
- 若当前账户的 Covered Call scope 全部因未持股而跳过，账户级结果是合法的 `no_candidate`；持仓上下文缺失或损坏仍为 `data_unavailable`。
- 已开放 Short Call 的股票锁定来自权威 SQLite option-position ledger。
- OpenD 持仓与 SQLite short-call 锁定无法一致解释时 fail closed。
- 不跨账户借用股票，也不默认 multiplier。

```text
shares_available_for_cover = min(qty, can_sell_qty) - shares_locked_by_open_short_calls
max_new_contracts = floor(shares_available_for_cover / multiplier)
```

- `max_new_contracts >= 1` 才可进入候选。
- 不另行扫描待成交挂单；OpenD `can_sell_qty` 作为券商当前可卖事实。
- 交割物就是普通股票。

普通持股与 Sell Put 指派股混合且无法归属时，股票仍可按账户总量判断覆盖能力，
但保持未分配，不计算批次级 Wheel 收益。只有显式 `stock_lot_id` 或充分 FIFO 证据
才能把 Covered Call 归属到指定指派批次。

### 6.4 不作为硬门槛的指标

以下指标不阻断 Covered Call 候选：

- gap-up opportunity cost；
- path stress；
- delta band；
- 单标的集中度；
- OI、volume、bid/ask size。

### 6.5 排序

1. 仅排序全部硬门槛通过且容量至少一张的候选。
2. 以最高 `period_net_premium_return` 为锚，将差值不超过 `0.002` 的候选组成收益区间。
3. 同一标的、同一收益区间内依次比较：
   - 更高 strike，从而降低相近收益下被叫走概率；
   - 更小 spread；
   - OI 已知优先，再比较更高 OI；
   - 更高净权利金；
   - 稳定合约 ID。
4. 每个标的先选一张代表合约。
5. 不同标的、同一收益区间内依次比较：
   - 较低的被叫走后剩余 symbol concentration；
   - 更大的 strike-above-spot 距离；
   - 更小 spread；
   - OI 已知且更高；
   - 更高净权利金；
   - symbol 和合约 ID。

## 7. 期限匹配 RV

Sell Put 与 Covered Call 使用完全相同的正式 RV：

1. 数据只认 OpenD 前复权（QFQ）已完成日线和 OpenD 交易日历；
2. 计算从扫描时点到到期日剩余的交易 session 数；
3. `lookback = max(20, remaining_trading_sessions)`；
4. 使用已完成收盘价的对数收益；
5. 使用样本标准差并乘以 `sqrt(252)` 年化；
6. 得到唯一 `term_matched_RV`，参与 IV/RV 两个硬门槛。

RV20 / RV60 / RV120 只保留为诊断字段，不加权生成正式 RV。历史不足时不可用，
不回退其他窗口，也不重新归一化。

日线缓存按 `market + symbol + QFQ` 持久化共享：

- 不在每次 intraday run 全量重复抓取；
- 新的已完成交易日出现时增量更新；
- 每次回抓最后 5 个 session 以吸收修订；
- 发现 QFQ 历史修订时刷新所需完整区间；
- 每次 run 仍按目标 expiry 重新计算期限匹配 RV，并记录输入区间与 hash；
- 缺口或更新失败只使受影响 expiry 不可用。

新算法先与旧算法做 shadow comparison，再晋升为正式口径。

## 8. 财报风险

### 8.1 唯一数据源

- 唯一正式来源是 OpenD `get_earnings_calendar`。
- 删除 yfinance 依赖、fallback、probe、配置和事件实现。
- 不再使用 `get_financials_earnings_price_history` 证明未来覆盖，也不预测财报日期。
- Futu SDK 与 OpenD 必须支持财报日历接口；不支持时相关范围 `data_unavailable`。

### 8.2 获取与完整性

- 每个市场、每个 run 只获取一套市场级财报日历，所有账户和 symbol 共享。
- 从扫描日到本轮最远 expiry 按最多 7 天的连续、不重叠区间查询。
- 所有相关区间成功后，OpenD 未列出某 symbol 才解释为“当前没有已知财报安排”。
- OpenD 不提供单 symbol 的覆盖完整标记；本策略明确接受“以 OpenD 当前已知日历为准”的剩余风险。
- 任一区间失败，只阻断需要该区间才能证明安全的 expiry，不扩大成整个 run 失败。

### 8.3 筛选

- 从扫描时点起，到期日当天结束前存在尚未发生的财报，则候选拒绝。
- 财报发生在 expiry 当天也属于持有期内事件。
- 扫描当天优先比较 `earnings_timestamp`：已发布则不再阻断，尚未发布则拒绝。
- 当天只有日期、缺少可靠发布时间时，该范围 `data_unavailable`。
- 完整查询成功且期间为空可以通过；错误、覆盖区间不完整或时间证据不足时 fail closed。

## 9. 运行状态与失败范围

账户/run 的正式结果只使用以下状态：

- `candidates_found`
- `no_candidate`
- `data_unavailable`
- `partial_data`
- `market_closed`

失败按最小范围传播：

- 账户事实失败：阻断该账户；
- symbol 事实失败：阻断该 symbol；
- expiry chain 或财报覆盖失败：阻断相关 expiry；
- 单合约 snapshot、multiplier、tick 或报价失败：阻断该合约。

OI、volume、delta 等可选字段缺失不产生 `partial_data`。

每个账户/run 即使无候选也必须封存 snapshot，并包含 `scope_results`。snapshot 必须：

- 绑定 run、账户、物理账户、市场、策略与配置/证据 hash；
- 区分 `strategy_status`、`capacity_status` 和最终 `opening_status`；
- 完成后不可变；
- 通过 seal/hash 校验后才能被 Agent 或通知消费。

Candidate Agent 工具必须显式提供 account。run 可省略，但只能解析到该账户最新已封存且
hash 有效的 snapshot；不允许调用方传任意文件系统路径。

## 10. 组合口径、Position Advice 与消费边界

- Sell Put 的接货后 concentration 和 Covered Call 的被叫走后剩余 concentration
  继续使用当前组合 NAV 计算口径；资产按当前市值计量，货币基金计入 NAV。
- concentration 只在跨 symbol 且收益接近时参与选择，不变成硬风险门槛。
- 美股和港股使用同一套公式、状态和失败范围；市场配置窗口、费用表、时区和交易日历分别取对应市场事实。
- opening snapshot 只负责开仓候选和容量事实。Position Advice 自行计算
  `hold / replace / reallocate / manual-review` 的资格，不由候选生产者预先写死。
- 可以复用现有 producer payload / receipt 基础设施，但 opening candidate domain
  拥有 payload、seal、hash 和状态语义。
- 人工没有导出 CSV 查看需求，正式分析入口是 Agent。CSV/JSONL 不再是公共合同或事实真源。
- 候选容量是建议能力，不授权自动下单、自动换汇、自动滚动、自动接货或自动叫走。

## 11. 已确认决策索引

本节用于逐项核对本轮对话中已确认的问题。详细计算以对应正文为准；索引本身不建立
第二套规则。

### 11.1 策略与排序

| ID | 已确认结论 | 正文 |
|---|---|---|
| S01 | Sell Put 是愿意按合适价格接货并赚取权利金，不主动追求接货 | §1、§5 |
| S02 | Covered Call 是愿意按合适价格卖股并增强收益，不主动追求被叫走 | §1、§6 |
| S03 | 两者都按可执行 mid 限价等待，不追价 | §1、§3.2 |
| S04 | 保持现有 DTE 召回和筛选窗口，不新增 DTE 奖励 | §5.1、§6.1 |
| S05 | 主收益统一为整个周期的非年化净收益；年化只作 10% 硬门槛 | §4.2、§5.2、§6.2 |
| S06 | Sell Put 收益分母使用扣除净权利金后的净资金 | §5.2 |
| S07 | Covered Call 收益分母使用正股当前市值 | §6.2 |
| S08 | 单张合约净权利金至少 CNY 50 | §4.2 |
| S09 | IV/RV 同时满足 ratio 1.10 和 spread 0.05 | §4.3 |
| S10 | spread 40% 是硬门槛；OI 只在收益接近时排序，volume/delta 只展示 | §3.2、§3.4 |
| S11 | `0.002` 使用锚定收益分组，不使用非传递性的两两“接近”比较 | §5.5、§6.5 |
| S12 | 每个 symbol 先选一张代表合约，再做跨 symbol 排序 | §5.5、§6.5 |
| S13 | Sell Put 同 symbol 收益接近时优先净接货折价、spread、OI、净权利金 | §5.5 |
| S14 | Covered Call 同 symbol 收益接近时先选更高 strike，再看 spread、OI、净权利金 | §6.5 |
| S15 | 跨 symbol 时 concentration 仅作接近收益的排序参考，不作硬门槛 | §5.5、§6.5、§10 |
| S16 | stress、gap、path、delta band、gamma、vega 和旧评分不进入正式开仓决策 | §5.4、§6.4、§12 |

### 11.2 召回、容量与资金

| ID | 已确认结论 | 正文 |
|---|---|---|
| C01 | Sell Put 先取 `min(max_strike, spot)`，再从该上界向下召回 20% | §5.1 |
| C02 | Sell Put 显式 `min_strike` 继续作为更严格下界 | §5.1 |
| C03 | Covered Call 最低卖价为 `max(min_strike, avg_cost*1.02)` | §6.1 |
| C04 | Covered Call 召回最小值再与 spot 取大，最大值为其上方 20% 并受 configured max 限制 | §6.1 |
| C05 | 现金、持仓和锁定全部按物理 Futu 账户隔离 | §5.3、§6.3 |
| C06 | Sell Put 先用同币种现金，不足部分按新鲜 OpenD 汇率折算其他币种 | §5.3 |
| C07 | 跨币种资金不保留 0.95 或其他安全折扣 | §5.3 |
| C08 | 现金组成保留当前 `cash_by_currency + fund_assets` 口径，货币基金算入 | §5.3 |
| C09 | 已有 Short Put 按 gross strike notional 占用担保，不抵扣历史权利金 | §5.3 |
| C10 | 不考虑 Sell Put 待成交挂单或 frozen cash，候选共享资金不可相加 | §5.3 |
| C11 | 每张候选按一张合约计算，最大张数用资金或股票整除 multiplier | §5.3、§6.3 |
| C12 | Covered Call 使用 OpenD qty/can_sell_qty 和 SQLite 已开放 Short Call 锁定 | §6.3 |
| C13 | 普通持股与指派股混合可用于账户级覆盖，但无法归属时不算批次 Wheel 收益 | §6.3 |
| C14 | 交割物是普通股票，不支持裸 Call 或非标准交割 | §3.3、§6.3 |
| C15 | 组合集中度延续当前 NAV 口径，按当前市值且货币基金计入 | §10 |

### 11.3 OpenD 数据合同

| ID | 已确认结论 | 正文 |
|---|---|---|
| D01 | 正式市场数据只认 OpenD，不回退旧 CSV、last 或其他行情源 | §3.1 |
| D02 | OpenD market_state 决定连续交易、闭市和不可用状态 | §3.1 |
| D03 | spot 只认 live OpenD last_price 及同一份状态/时间/证券状态证据 | §3.1 |
| D04 | 标的 spot 在连续交易时段使用 5 分钟最新价新鲜度；期权使用 OM 取得 snapshot 的 5 分钟取得新鲜度，期权 `update_time` 仅作活跃度诊断 | §3.1 |
| D05 | mid、spread 和向上取整的卖出限价使用同一合约 snapshot 与 price tick | §3.2 |
| D06 | 只接受 STANDARD、stock_owner 匹配、未停牌、普通股票交割合约 | §3.3 |
| D07 | multiplier 必须 chain/snapshot 合约级一致，不默认 100 | §3.3 |
| D08 | OpenD IV 从百分数除以 100，Delta 保持 `[-1,1]` | §3.4 |
| D09 | OI 0 与缺失分开；可选 OI/volume/delta 缺失不产生 partial_data | §3.4、§9 |
| D10 | 正式 RV 是按剩余交易 session 匹配的唯一指标，RV20/60/120 仅诊断 | §7 |
| D11 | RV 日线持久化缓存、增量更新、回抓 5 个 session，QFQ 修订时刷新 | §7 |
| D12 | 美股与港股共享算法，只使用各自 OpenD 日历、时区、费用和配置 | §10 |
| D13 | 候选费用使用版本化官方保守全费用，成交业绩使用券商真实费用 | §4.1 |
| D14 | 汇率唯一正式来源是 OpenD，最长有效 24 小时；stale 外币不参与，同币种资金仍可用 | §5.3 |
| D15 | snapshot 缺单合约只阻断该合约，chain 缺单 expiry 只阻断该 expiry | §9 |

### 11.4 财报

| ID | 已确认结论 | 正文 |
|---|---|---|
| E01 | 财报是 Sell Put 与 Covered Call 的正式持有期硬风险 | §8.3 |
| E02 | 唯一正式来源为 OpenD `get_earnings_calendar` | §8.1 |
| E03 | 删除 yfinance，包括依赖、fallback、probe、除息/拆股和事件实现 | §8.1、§12 |
| E04 | 不用财报价格历史证明未来覆盖，也不预测财报日期 | §8.1 |
| E05 | 每个市场/run 查询一次，按最多 7 天分段覆盖到最远 expiry | §8.2 |
| E06 | 所需区间全部成功后，空结果解释为 OpenD 当前无已知财报安排 | §8.2 |
| E07 | 财报在 expiry 当天也拒绝 | §8.3 |
| E08 | 当天财报按 timestamp 判断过去/未来；时间不可靠则不可用 | §8.3 |
| E09 | SDK/OpenD 不支持财报日历时不可用，不回退其他来源 | §8.1 |

### 11.5 快照、状态与代码收敛

| ID | 已确认结论 | 正文 |
|---|---|---|
| A01 | 账户/run 状态为 candidates_found、no_candidate、data_unavailable、partial_data、market_closed | §9 |
| A02 | 失败按账户、symbol、expiry、contract 的最小范围传播 | §9 |
| A03 | 每个账户/run 即使空结果也封存包含 scope_results 的不可变 snapshot | §9 |
| A04 | Candidate Engine 是唯一正式过滤/排序所有者 | §2 |
| A05 | Candidate ID 包含逻辑账户和物理 futu_account_id | §3.3 |
| A06 | Agent 查询必须给 account；省略 run 只能取最新 seal/hash 有效 snapshot | §9 |
| A07 | Position Advice 自行判断 replacement，不复制候选生产者决策字段 | §10 |
| A08 | Combo Yield 只共享标准化证据，保留独立策略和 snapshot | §2 |
| A09 | 人工不依赖 CSV 导出；Agent 是正式分析入口 | §10 |
| A10 | 删除 runtime CSV/JSONL 候选流水线、重复 Position Advice artifact 和旧字段 | §12 |
| A11 | 开仓候选不考虑自动下单、自动换汇或自动归属 Wheel 批次 | §10 |
| A12 | 当前代码与目标合同的差距必须明确，不能把文档目标声称为已上线 | §13 |

## 12. 明确删除或退出正式路径

以下内容不再进入正式候选决策：

- yfinance 全部运行依赖及事件模型；
- 通用 ex-dividend / split 事件门槛；
- 旧的多数据源事件 resolver、fallback、stale event store 和重复事件 artifact；
- RV20/60/120 分档加权正式算法；
- `strategy_score`、`premium_edge_score`、`risk_distance`、`path_risk_score`、
  gamma、vega、gap-up/gap-down 等旧开仓评分；
- OI / volume / delta 硬门槛；
- 第二套 application 排序；
- runtime CSV/JSONL 候选流水线和重复 Position Advice 候选 artifact；
- multiplier `100` 默认值、旧 spot/last/CSV fallback；
- 旧字段、旧 CLI 参数和只为已删除决策保留的兼容读取。

历史 artifact 可以继续只读解释，但不能重新进入当前开仓候选路径。

## 13. 实施状态与上线边界

`feat/opening-candidate-policy-alignment` 分支已按本合同完成源码实施和离线验证，
并已合并 `main`、随 v1.10.17 发布：

- 正式开仓候选使用 OpenD 报价、合约、市场状态、财报日历、QFQ 日线、
  交易日历和汇率证据；
- 期限匹配 RV 是 Candidate Engine 唯一正式 RV；RV20/60/120 和旧加权值只保留为
  诊断或离线 shadow 对照，不进入正式 decision；
- Sell Put / Covered Call 的计算、硬筛和排序由 Candidate Engine 唯一所有；
- 现金、汇率、持仓与锁定能力绑定物理 Futu 账户，OpenD FX 最长有效 24 小时，
  无 `0.95` haircut；
- 每个账户/run 封存不可变 `opening_candidate_snapshot.v1`，Agent、Daily Brief 和
  Position Advice 读取同一封存事实；
- yfinance、旧事件 resolver、旧开仓评分、runtime 候选 CSV/JSONL 权威路径和重复
  Position Advice 候选 artifact 已退出当前开仓路径；历史读取只保留在明确的
  Close Advice、Combo Yield、research/archive/shadow 兼容边界。

上述结论证明当前 `main` 源码与 v1.10.17 发布产物已按本合同运行。受控远程升级是
独立授权边界；在完成升级和运行时验证前，不得宣称生产环境已按本合同运行。

## 附：CC+LP（Covered Call + Long Put）变体

CC+LP 是 `combo_yield` 模块下的同到期变体（`combo_yield.variant=cc_lp`），完整口径见
[`docs/plans/cc-lp-same-expiry-policy-confirmation-20260808.md`](plans/cc-lp-same-expiry-policy-confirmation-20260808.md)：

- 定位：Sell Call 资金腿（收权利金、承担被叫走风险）+ Long Put 看跌反转腿（表达转跌观点），
  与 SP+LC 严格对称，不是保护/保险；
- Sell Call 腿独立扫描，继承 Sell Call 全部硬门槛（收益下限、`max(min_strike, avg_cost*1.02)`、max_strike、流动性、期限），
  无持仓上下文 → `not_applicable` 跳过；
- Long Put 反转腿 delta 区间 0.10~0.25，目标 delta 0.12；
- 结构方向 `call_strike > put_strike`（复用 `strike_order` 角色参数化）；
- 保留率 `net_credit / call_net_credit >= 0.20`（不允许净 debit/自掏腰包），无 gap 硬门槛（`gap_width_pct` 仅诊断）；
- 资金占用 = 持仓当前市值 `spot * multiplier`（1 张合约覆盖股数），不扣净权利金；
- 排序：保留率主键，次键反转腿 delta 趋近 0.12，再 spread/OI；
- 候选写入独立 `cc_lp_candidate_snapshot.v1`，Daily Brief 加载快照到数据源（不渲染）；
- 当前不启用（`combo_yield.variant` 默认 `sp_lc`），启用由运行时配置决定。
