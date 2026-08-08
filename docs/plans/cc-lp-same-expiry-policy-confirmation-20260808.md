# CC+LP 策略确认（2026-08-08，同到期结构）

> 状态：策略口径确认完成，待实施方案
>
> 日期：2026-08-08
>
> 范围：Covered Call + Long Put 组合（同到期 `same_expiry_pair`）开仓候选的定位、两腿来源与门槛、资金口径、反转表达腿筛选与排序
>
> 相关文档：[`STRATEGY_ARCHITECTURE.md`](../STRATEGY_ARCHITECTURE.md)、[`candidate_strategy.md`](../candidate_strategy.md)、[`combo-yield-policy-confirmation-20260808.md`](combo-yield-policy-confirmation-20260808.md)

## 0. 关键澄清：反转表达，不是保护

CC+LP 的 Long Put 腿是**反转表达腿（观点腿）**，不是"保护"或"保险"。

- Sell Call 是资金腿：承担被叫走风险，获得确定权利金；
- Long Put 是反转腿：表达"标的转跌"的观点，从下跌中获利；
- 与 SP+LC（Sell Put 资金腿 + Long Call 反转表达腿）严格对称，只是反转方向相反；
- 禁止把 Long Put 描述为保护/保险，禁止按"保护强度"设计参数。

## 1. 定位与定义

CC+LP 是一个合成仓位：**1 张 Sell Call + 1 张 Long Put**（同到期）。

- Sell Call 是资金腿：收权利金，承担上行被叫走风险；
- Long Put 是反转腿：用 call 权利金做看跌反转配置；
- 两条腿一起开、一起管，按组合整体评估收益、风险与生命周期；
- 组合目标：愿意被叫走，但不主动追求被叫走；用 sell call 权利金做看跌反转表达；
- **不存在自掏腰包选项**：组合净权利金必须为正（保留率 > 0）。

## 2. 两腿来源与门槛

### 资金腿（Sell Call）

**独立扫描，继承 Sell Call 全部硬门槛，复用共享 required-data，不依赖 Sell Call step 是否启用或成功。**

与 SP+LC 的 Funding Put 方案对称：Sell Call step 未启用、失败或无候选时，CC+LP 仍可由自身 enabled 独立构造资金腿。

- `min_annualized_net_premium_return`（年化净权利金收益下限）；
- `min_strike`，叠加 `avg_cost * 1.02` 成本底线（`resolve_effective_sell_call_min_strike`）；
- `max_strike`；
- 流动性、期限边界等继承 Sell Call；
- **delta 不是 Sell Call 的门槛**（现状如此，保持不变）。

### 反转腿（Long Put）

复用 Combo Yield 的 delta 区间机制（现有 call 腿同款，角色换成 put 腿）：

- `min_delta = 0.10`、`max_delta = 0.25`；
- 该区间是**反转表达强度**：delta < 0.10 表达不了反转观点，delta > 0.25 时保留率中位转负、候选枯竭；
- 同 symbol、currency、multiplier、同到期；
- Put bid/ask、delta、OI、volume、spread、multiplier 缺失或不合格时 fail closed。

## 3. 结构约束

### 方向（必须）

```text
call_strike > put_strike
```

复用现有 `strike_order` / `strike_relation` 校验逻辑（SP+LC 为 `put_strike < call_strike`，角色互换）。若反转，payoff 变成锁定卖出区间，不再是看跌反转，必须拒绝。

### 间隔（不做硬门槛）

- 不加 `gap` 硬约束；`gap_width_pct = (call_strike - put_strike) / spot` 只做诊断字段；
- 间隔两端由现有机制夹住：反转腿太靠近 call → 贵 → 保留率下限淘汰；太远 → delta 下限淘汰；
- 结构方向 `call_strike > put_strike` + 两腿各自门槛已保证结构成立，无需第三个约束。

## 4. 组合资金与收益口径

### 组合净权利金

```text
call_net_credit = Call bid * multiplier - estimated_sell_fees
put_total_cost = Put ask * multiplier + estimated_buy_fees
net_credit = call_net_credit - put_total_cost
```

### 硬性下限

```text
net_credit_retention = net_credit / call_net_credit > 0
推荐下限：0.20
```

- 卖 call 权利金必须覆盖买 put 成本，**不允许净 debit**；
- 保留率下限 0.20 = 最多 80% 卖 call 权利金用于买反转腿；
- 数据依据：港股生产 run（0700.HK/9992.HK）真实 bid/ask 配对中，delta 0.10~0.25 区间在保留率 ≥0.20 时仍保持 2×2（标的×到期）覆盖；0.30 会导致候选系统性偏向浅反转腿或枯竭。

### 资金占用

CC+LP 持有正股，卖 call 不产生额外资金占用：

```text
capital = 持仓市值 = spot * shares
net_return = net_credit / capital = (call_net_credit - put_total_cost) / (spot * shares)
```

- 分母为持仓当前市值，**不扣净权利金**（卖 call 收现金、买 put 付现金，均在现金侧，不改变股票占用市值）；
- 与 Covered Call 现有收益口径一致（`net_income / covered_notional`，`covered_notional = spot * multiplier`）；
- 与 SP+LC 的现金担保口径不同（SP+LC 为 `put_strike * multiplier - net_credit`），两者分母性质不同，不混用。

## 5. 排序

### 主排序

以 `net_credit_retention`（保留确定收益比例）为主键，从**所有已通过反转表达强度（delta 0.10~0.25）的腿**中优先选保留率最高的。

理由（数据）：

- 保留率与反转腿 delta 强负相关（delta 0.10 → 保留率中位 0.38，delta 0.15 → 0.03，delta 0.20 → −0.24）；
- 若以 delta 为主键，系统会系统性选最贵反转腿、把保留率压到下限，净权利金收益趋零，风险收益结构失衡；
- 保留率优先与 SP+LC 哲学一致：先满足表达强度，再优收益留存。

### 次级排序

同保留率下：

- 反转腿 delta 更接近目标值 **0.12** 优先（数据依据：通过保留率 ≥0.20 的候选 delta 中位约 0.116，0.15 落在分布上沿近乎不可达，0.12 贴着可行候选的中位，次键真实可区分）；
- 两腿 spread、流动性（OI/volume）依次区分；
- 跨标的：以 Sell Call 腿的**期间非年化净权利金收益**为主键（与 SP+LC 跨标的口径一致），次级为 call 接货/被叫走安全、反转腿 delta、保留率、流动性。

## 6. 与 SP+LC 的口径差异

| 维度 | SP+LC | CC+LP |
|---|---|---|
| 资金腿 | Sell Put（跌了接货） | Sell Call（涨了被叫走） |
| 表达腿 | Long Call（涨了跟涨） | Long Put（跌了获利） |
| 反转方向 | 看涨 | 看跌 |
| 结构方向 | put_strike < call_strike | call_strike > put_strike |
| 表达腿 delta | 0.10~0.45（call） | 0.10~0.25（put，推荐） |
| 保留率下限 | 0.60 | 0.20（推荐，>0 必须） |
| 自掏腰包 | 不允许 | 不允许 |
| gap 约束 | 无（诊断字段） | 无（诊断字段） |
| 排序主键 | 保留率优先 | 保留率优先 |

保留率差异的原因：long call 是可选参与腿，预算不够就少参与；long put 是反转表达腿，表达强度必须先达标。两者都用保留率约束成本上限，只是保留率取值不同。

## 7. 候选真源

沿用与 SP+LC 相同原则，待实施方案落地：

- CC+LP 候选进入独立 sealed snapshot（如 `cc_lp_candidate_snapshot.v1`），空结果也封存 `no_candidate`；
- Agent、Daily Brief 只消费快照，不读取 CSV 或二次排序；
- 被拒绝的 Put 与配对尝试进入 pair diagnostics，不进入通知。

## 8. 数据依据与局限

### 数据依据

- 港股生产 run：15 个 run、976 个 Sell Call 候选 × Put 链真实 bid/ask 配对（0700.HK、9992.HK）；
- 美股交叉验证：NVDA/AAPL/TSLA/SPY 对称价外（±5%/±10%/±15%）OpenD 理论价对比，put/call 比率 0.8~1.15；
- 参数推荐：put delta 0.10~0.25 + 保留率下限 0.20。

### 局限

- 港股样本仅 2 个标的（0700.HK/9992.HK）；
- 美股使用 OpenD 理论价（当前 OpenD 无期权实时 bid/ask 权限），非可成交价；
- 落地前建议用有实时报价的标的（如 NVDA）做一次端到端验证。

## 9. 待实施方案差距（相对当前代码）

1. 新增 CC+LP 组合结构（复用 `validate_yield_enhancement_pair` 的方向校验，角色互换）；
2. Sell Call 腿复用现有扫描结果或独立扫描（待方案确认）；
3. Long Put 腿复用 delta 区间机制，新增配置窗口；
4. 组合层保留率下限配置与计算；
5. 排序主键保留率优先 + 次级 delta；
6. CC+LP 候选写入独立 sealed snapshot 并切换消费路径。

## 10. 已确认决策记录

- CC+LP 的 Long Put 是反转表达腿，不是保护/保险；
- 复用现有机制优先：Sell Call 用收益门槛 + strike 窗口（含成本底线），Long Put 用 delta 区间，组合用保留率；
- 不设 gap 硬门槛；
- 不存在自掏腰包，保留率必须 > 0；
- 保留率下限 0.20（推荐，数据支撑）；
- put delta 区间 0.10~0.25（推荐，数据支撑）；
- 排序主键保留率，次键反转腿 delta 趋近目标 0.12；
- Sell Call 腿独立扫描、继承 Sell Call 全部硬门槛、复用共享 required-data。

## 11. 待确认

- ~~资金占用口径~~：已确认 = 持仓当前市值，不扣净权利金（见 §4）；
- ~~Sell Call 腿来源~~：已确认 = 独立扫描、继承门槛、复用数据（见 §2）；
- ~~目标 delta 具体值~~：已确认 = 0.12（见 §5）。
