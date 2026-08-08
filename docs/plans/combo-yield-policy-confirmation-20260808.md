# Combo Yield 策略确认（2026-08-08）

> 状态：策略口径确认完成，待实施方案
>
> 日期：2026-08-08
>
> 范围：Combo Yield 开仓候选的定位、put 腿来源、资金口径、硬门槛、call 腿筛选与排序
>
> 相关文档：[`STRATEGY_ARCHITECTURE.md`](../STRATEGY_ARCHITECTURE.md)、[`candidate_strategy.md`](../candidate_strategy.md)

## 1. 定位与定义

Combo Yield 是一个合成仓位：**1 张 Sell Put + 1 张 Long Call**。

- Sell Put 是资金腿：承担接货风险，获得确定权利金；
- Long Call 是参与腿：用 put 权利金做上行风险配置；
- 两条腿一起开、一起管，按组合整体评估收益、风险与生命周期；
- 组合目标是“愿意接货，但不主动追求接货；最多牺牲 40% 的确定性权利金做 long call 风险配置”。

Combo Yield 是与 Sell Put、Covered Call 平行的独立开仓策略，不是 overlay。运行时以自身 `enabled` 和共享 required-data 是否可用决定，不依赖 Sell Put step 是否启用或成功。

## 2. put 腿来源与硬门槛

采用方案 B：**Combo 独立执行 put 扫描，但继承 Sell Put 全部硬门槛**。

- 复用 Sell Put 的期限边界、strike 边界、收益率、流动性、现金与 underwriting 门槛；
- `min_net_income` 必须继承 Sell Put 策略（当前 Combo 扫描显式传 `0.0`，需修正为继承值）；
- 不直接复用 Sell Put step 的候选结果或成功状态；
- Sell Put 未启用、失败或无候选时，Combo 仍可由自身 enabled 独立构造 Funding Put。

## 3. 组合资金与收益口径

### 组合净权利金

```text
put_net_credit = Put bid * multiplier - estimated_sell_fees
call_total_cost = Call ask * multiplier + estimated_buy_fees
net_credit = put_net_credit - call_total_cost
```

### 资金占用

```text
cash_required = put_strike * multiplier - net_credit
```

采用“扣净权利金”口径：组合净 credit 时占用小于 put 现金担保，净 debit 时占用更大。

### 收益率

- 期间（整个持有周期）非年化净收益：`net_credit / cash_required`，用于排序；
- 年化净收益：`period_net_return * 365 / DTE`，仅用于硬门槛的统一横向比较。

## 4. 硬门槛

### 保留

| 门槛 | 默认值 | 用途 |
|---|---|---|
| `min_net_credit_retention` | 0.60 | call 最多使用 put 净权利金的 40%，至少保留 60% 确定收益 |
| `min_combo_net_credit` | None（按需配置） | 组合净权利金绝对值下限 |
| `min_net_credit_annualized` | 0.08 | 年化组合净收益下限，统一不同 DTE 的评判标准 |
| `max_combo_spread_ratio` | 0.5 | 组合两腿总价差成本上限 |
| put 接货安全 | 继承 Sell Put | put OTM、breakeven 折价、接货边界 |
| 两腿结构 | 固定 | 同 symbol、currency、multiplier；`put strike < call strike`；put 到期不晚于 call |

### 删除 / 废弃

| 字段 | 原因 |
|---|---|
| `funding_mode`（credit_or_even / max_debit） | 与 `min_net_credit_retention` 作用重叠 |
| `max_call_cost_to_put_credit` | 是 `min_net_credit_retention` 的补数，保留一个即可 |

统一由 `min_net_credit_retention = 0.60` 表达成本约束。

## 5. call 腿筛选

- 从 required-data Call universe 独立召回，不要求启用 Covered Call 扫描；
- delta 范围保留默认：`min_delta=0.10`、`max_delta=0.45`；
- 流动性保留 Combo 默认：US OI 100 / 量 5 / 价差 0.35；HK OI 50 / 量 0 / 价差 0.35；
- 结构保留两种可配置：`same_expiry_pair`（同到期）与 `staggered_expiry_pair`（错期，`min/max_expiry_gap_days` 精确校验 Call DTE - Put DTE）；
- Call bid/ask、delta、OI、volume、spread、multiplier 缺失或不合格时 fail closed。

## 6. 排序

### 主排序（同一 Funding Put 下选 Participation Call）

以 `net_credit_retention`（保留确定收益比例）为主键，弃用 `premium_funding_score` 组合分作为主键；收益接近时再用 call 参与度、两腿价差、流动性等次级键区分。

### 跨标的排序

以 Funding Put 的**整个周期非年化净收益**为主键（替代现有 `put_only_annualized_net_return`），次级为接货安全折价、call delta、`net_credit_retention`、两腿 spread、流动性。

错期结构不计算或硬筛组合年化、同到期 breakeven、expected-move scenario、1.5σ/2.0σ payoff multiple。

## 7. 候选真源与 sealed snapshot

Combo Yield 候选纳入与 Sell Put / Covered Call 同一套不可变 sealed snapshot：

- 每个账户/run 封存不可变、可校验的 Combo Yield 决策快照（含 scope_results，空结果也封存 `no_candidate`）；
- Agent、Daily Brief、Position Advice 只消费该快照，不再读取 `*_combo_yield_candidates.csv`；
- Daily Brief 不得自行对 CSV 做第二套排序，只读取快照中的正式排序结果；
- 被拒绝的 Call 与配对尝试进入 pair diagnostics，不进入通知；
- Combo Yield 有自己的独立 snapshot 语义，不修改 Sell Put / Covered Call 决策。

## 8. 待实施差距（相对当前代码）

1. Combo put 扫描继承 Sell Put 硬门槛，修正 `min_net_income=0.0`；
2. 删除 `funding_mode` 与 `max_call_cost_to_put_credit` 配置与计算路径，统一 retention 约束；
3. 排序主键从 `premium_funding_score` 改为 `net_credit_retention` 优先；
4. 跨标的排序从 `put_only_annualized_net_return` 改为期间非年化净收益；
5. 保留 `min_net_credit_annualized` 作为年化硬门槛（不改为期间口径）；
6. Combo Yield 候选写入独立 sealed snapshot，切换 Agent / Daily Brief / Position Advice 消费路径并移除 CSV 正式读路径。
