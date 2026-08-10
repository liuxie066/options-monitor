# Close Advice Contract

Close Advice 只回答一个问题：已有的 short Put / short Call 是否已经在合约前半程以足够低的全成本锁定了至少 90% 开仓净权利金。

它不建议新开仓，不 roll，不比较替代标的，不 reallocate，不根据方向、delta、集中度或 short-vol thesis 产生平仓建议，也不处理 long option 的止盈或止损。开仓候选仍由 Candidate Engine 负责。

## 决策状态

`recommendation_state` 是唯一决策状态：

| 状态 | 含义 | 进入日常提醒 |
|---|---|---:|
| `close` | 所有严格条件同时通过，建议买回平仓 | 是 |
| `hold` | 数据完整，但任一经济性条件未通过 | 否 |
| `not_evaluable` | 持仓、日期、双边报价或手续费证据不完整/不一致 | 否 |

新报告不再生成 `tier`、`tier_label`、`exit_state` 或 `close_action` 等平行状态。读取旧报告时，如果没有明确的 `recommendation_state`，或 `policy_version` 不是当前严格版本，读取面不猜测旧字段的含义，而是保守投影为 `not_evaluable`。Daily Brief 同样只接受当前严格版本的 `close` 行。

## 严格策略

当前唯一策略版本是 `strict_profit_capture.v1`。以下条件必须全部成立：

1. 持仓为有效的 short Put 或 short Call。
2. 期权仍为 OTM：Put 要求 `spot > strike`，Call 要求 `spot < strike`。
3. 全成本净兑现比例 `>= 90%`。
4. 剩余 DTE `>= 14`。
5. `DTE / original_DTE >= 50%`，即仍处于原合约前半程。
6. 全成本平仓价值 / strike notional `<= 0.10%`。
7. `(ask - bid) / mid <= 30%`。

其中：

```text
opening_gross_credit
= open_premium * multiplier * contracts

opening_net_credit
= opening_gross_credit - estimated_open_fee

all_in_close_cost
= ask * multiplier * contracts + estimated_close_fee

net_capture_ratio
= 1 - all_in_close_cost / opening_net_credit

close_cost_ratio
= all_in_close_cost / (strike * multiplier * contracts)
```

平仓价按 ask 而非 mid 计算，不使用 `last_price` fallback。阈值是版本化的固定策略，不接受运行配置调整。`max_items_per_account` 只限制消息展示条数，不改变任何持仓的决策。

## 必需证据

可评估行必须具备：

- 账户和稳定的 `position_lot_id`；
- option type、short side、strike、合约数、multiplier、currency；
- 开仓权利金、开仓日期、到期日；
- 同一份封存行情中的 spot、bid 和 ask；
- Futu 开仓与平仓手续费估算及它们的 fee basis。

任一证据缺失、非法、不一致，或封存行情 receipt/payload 校验失败，都必须 fail closed。

## 生命周期

每次运行只取一个 business date，先分类再请求行情：

| `position_lifecycle_state` | 处理 |
|---|---|
| `active` | 进入严格策略 |
| `expiry_day` | `not_evaluable`，不请求常规平仓报价 |
| `expired_open` | `not_evaluable`，等待 ledger/lifecycle 对账 |
| `unknown` | `not_evaluable`，不使用 quote DTE 反推持仓日期 |

Close Advice 不推断 assignment、called-away、exercise 或 settlement。这些仍是 ledger/reconciliation 事实。

## 运行与安全边界

```text
SQLite ledger position_lots
  + sealed required-data snapshot (spot/bid/ask)
  + versioned Futu fee schedule
  -> domain.domain.close_advice.evaluate_close_advice
  -> close_advice.csv / close_advice.txt / report manifest
  -> Daily Brief selects only current-policy, priced, complete-evidence CLOSE rows
```

调度 Tick 使用 run-scoped required-data plan 和封存 snapshot。评估期间不得修复 cache、回退到 last price 或重新请求 OpenD。每行报告保留 plan、binding、snapshot、receipt、payload hash 和观测时间，用于追溯决策输入。

保留的是通用安全能力：

- ledger 与账户隔离；
- 封存输入和 hash/receipt 完整性；
- Close Advice report manifest 和审计 trace；
- 数据不完整时 fail closed；
- Daily Brief 的通知幂等与交付确认。

不保留 Position Advice v2 专属的 plan/current pointer、authority mode、promotion gate/timer、notification token、allocator 或 lifecycle reconciliation 外壳。不建空的 v2 兼容层。

## 所有权

| 组件 | 责任 |
|---|---|
| `domain/domain/close_advice.py` | 固定阈值、公式、状态和排序 |
| `src/application/close_advice_required_data.py` | 为 active short Put/Call 计划精确合约行情 |
| `src/application/close_advice_runner.py` | 装配已封存输入、费用估算、CSV/文本/审计输出 |
| `src/application/daily_decision_brief_service.py` | 仅投影 `close` 为日常提醒 |
| `src/application/agent_tools/close_advice_read_impl.py` | 只读已有报告，不重新评估 |

Close Advice 只是建议，不写 trade event，不修改 position lot，不向 broker 下单。
