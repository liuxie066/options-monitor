# 策略术语对照

本文把 options-monitor 内部策略命名对应到金融专业领域的标准术语，供对外沟通、行情研究和复盘时使用。
它不是策略参数或筛选规则的权威来源：CSP / CC / Combo Yield 的开仓边界以
[STRATEGY_ARCHITECTURE.md](STRATEGY_ARCHITECTURE.md) 与 [candidate_strategy.md](candidate_strategy.md) 为准，
Wheel 以 [WHEEL_STRATEGY_PRD.md](WHEEL_STRATEGY_PRD.md) 为准。

## 总览

| OM 内部 key | 项目内名称 | 金融专业术语（EN） | 金融专业术语（中） | 结构一句话 |
|---|---|---|---|---|
| `sell_put` | Cash-Secured Put (CSP) | Cash-Secured Put | 现金担保认沽 | 卖一张现金担保的认沽 |
| `sell_call` | Covered Call (CC) | Covered Call | 备兑看涨 | 持有正股 + 卖一张看涨 |
| `combo_yield`（variant `sp_lc`） | Combo Yield SP+LC | Bullish Risk Reversal（Cash-Secured） | 看涨风险反转（现金担保变体） | 卖现金担保认沽 + 买同到期看涨 |
| `combo_yield`（variant `cc_lp`） | Combo Yield CC+LP | Collar（Credit Collar） | 领口策略（信用领口） | 持有正股 + 卖看涨 + 买同到期认沽 |
| `wheel` | 轮转策略 | Wheel Strategy | 轮转策略 | 按权威指派事实创建 Call / Put 分支，监控股票或现金容量 |

## CSP — Cash-Secured Put（现金担保认沽）

- OM 内部 key：`sell_put`；兼容别名 `put` / `sell put` / `csp` / `cash secured put`。
- 结构：卖出一张认沽期权，账户预留足额现金覆盖行权价（现金担保）。
- 目标：在愿意以合适价格接货、但不主动追求接货的前提下，按 `mid` 挂限价等待，赚取整个持有周期的净权利金。
- 术语说明：Cash-Secured Put 描述的是"卖认沽 + 全额现金担保"的保证金处理，属于单腿卖权策略，不是期权组合。

## CC — Covered Call（备兑看涨）

- OM 内部 key：`sell_call`（兼容别名 `covered_call`）；别名 `call` / `sell call` / `cc`。
- 结构：持有正股，卖出一张看涨期权，持仓提供覆盖。
- 目标：在愿意以合适价格卖出正股、但不主动追求被叫走的前提下，用整个周期的净权利金增强持股收益。
- 术语说明：Covered Call 即"持有股票 + 卖出看涨"，业界通常归入 yield enhancement（收益增强）家族；上交所组合策略中称"备兑开仓"。

## Combo Yield SP+LC — Bullish Risk Reversal（看涨风险反转）

- OM 内部 key：`combo_yield`，`combo_yield.variant=sp_lc`（默认）。
- 结构：卖出现金担保认沽（Funding Put，低 strike）+ 买入同到期看涨（Participation Call，高 strike）；同标的、同币种、同乘数、`put strike < call strike`。
- 融资关系：Put 净权利金为 Call 买单，`net_credit_retention ≥ 0.60`（Call 最多使用 Put 净权利金的 40%）。
- 收益特征：下行有限（最大亏损 ≈ Put strike − 净权利金）、中间区间保留净权利金、上行因 Long Call 开放（理论上无限）。
- 专业术语：业界标准名称是 **Bullish Risk Reversal（看涨风险反转）**，部分平台（如 Fidelity）也称 **Bullish Split-Strike Synthetic**；同 strike 的"买入 Call + 卖出 Put"则叫 **Synthetic Long（合成多头 / 合成股票多头）**，是国内交易所官方组合策略之一。本仓库是跨 strike 变体，且 Put 为现金担保而非裸卖，更精确的说法是 **Cash-Secured Bullish Risk Reversal（现金担保型看涨风险反转）**。

## Combo Yield CC+LP — Collar（领口策略）

- OM 内部 key：`combo_yield`，`combo_yield.variant=cc_lp`（当前默认不启用）。
- 结构：持有正股 + 卖出备兑看涨（Funding Call，高 strike）+ 买入同到期认沽（Reversal Put，低 strike）；`call strike > put strike`，Put 反转腿默认 `0.15 ≤ |delta| ≤ 0.35`（可配置）；排序次键目标仍为 0.12。
- 融资关系：Call 权利金覆盖 Put 成本，`net_credit / call_net_credit ≥ 0.20`，不允许净 debit。
- 收益特征：上行被 Short Call 封顶，下行被 Long Put 保护，中间区间保留净权利金。
- 专业术语：**Collar（领口策略 / 领式策略）**——"持有股票 + 卖出看涨 + 买入认沽"的经典结构；由于要求净收权利金，可称 **Credit Collar（信用领口）**。
- 项目定位说明：OM 把 Long Put 定义为"看跌反转腿"（表达转跌观点），与 SP+LC 严格对称，不是经典 protective collar（保护性领口）的保险定位；结构上两者相同。

## Wheel — Wheel Strategy（轮转策略）

- OM 内部 key：`wheel`；中文名"轮转策略"。
- 结构：普通 CSP / CC 或身份与成员已验证的外部 Combo Yield 卖腿被权威指派后，按事件发生时的启用窗口创建对应方向分支；Call 分支引用真实 `stock_lot_id`，Put 分支使用稳定的 `wheel_branch_id`，不虚构股票批次。
- 生命周期：当前实现支持双向分支。既有 Wheel 的真实指派按实际转换量创建 `pending_decision` 子分支，用户显式决定 `start` 或 `end`；不会自动无限轮转。新候选和 `start` 仍受当前监控启用门约束。
- 兼容语义：旧单向 Call 生命周期及 `called_away` / `manual_ended` 终态仍按历史合同解释，不回建 Put 子分支。具体事件、容量、启用与回退边界见 [Wheel PRD §13](WHEEL_STRATEGY_PRD.md#13-双向-wheel-实现合同)。

## 关系与边界

- Combo Yield 是与 CSP / CC 平行的独立开仓策略族，不是 overlay；`sell_put.enabled=false` 不会隐式禁用 Combo Yield。
- Combo Funding Put 被权威指派后同样可启动 Wheel；其 Participation Call 独立管理，不占用正股覆盖额度。
- 术语"combo / 组合"：在期权市场（尤其 HKEX 等亚洲市场）指同时下一腿 Put 与一腿 Call 的组合单，本仓库 `combo_yield` 的命名与该习惯一致。
- 术语"yield enhancement / 收益增强"：业界把"卖权利金收租金"的策略统称 yield enhancement；用一条腿的权利金为另一条腿买单的做法，口语常称 premium financing（权利金融资），没有统一的学院派名称。

## 别名表（策略词表）

| 内部 key | 兼容别名 |
|---|---|
| `sell_put` | put, sell put, sell-put, csp, cash secured put |
| `sell_call` | call, sell call, sell-call, covered_call, covered call, covered-call, cc |
| `combo_yield` | yield_enhancement, yield enhancement, yield-enhancement, enhancement, combo yield, combo-yield, ye |
| `wheel` | — |

## 参考

- Fidelity Options Strategy Guide — Bullish Split-Strike Synthetic (Risk Reversal)：https://www.fidelity.com/learning-center/investment-products/options/options-strategy-guide/bullish-split-strike-synthetic
- SpotGamma — Bullish Risk Reversal：https://support.spotgamma.com/hc/en-us/articles/16876574022675-Bullish-Risk-Reversal
- Fidelity Options Strategy Guide — Collar：https://www.fidelity.com/learning-center/investment-products/options/options-strategy-guide/collar
- 上交所投教 — 期权组合策略知多少（合成股票多头 / 领式策略等官方组合策略）：https://toujiao.sfccn.com/h5/newsdetail?id=1491
