# Close Advice Contract

Close Advice 只回答一个问题：已有的 short Put / short Call 净兑现至少 80% 开仓净权利金后，继续持有到期归零所能多取得的最高期权收益，年化是否已低于 10%。OM 接受 Put 接股及 Covered Call 交股；指派本身不是提前平仓理由。

它不建议新开仓，不 roll，不比较替代标的，不 reallocate，不根据方向、delta、集中度或 short-vol thesis 产生平仓建议，也不处理 long option 的止盈或止损。开仓候选仍由 Candidate Engine 负责。

## 决策状态

`recommendation_state` 是唯一决策状态：

| 状态 | 含义 | 进入日常提醒 |
|---|---|---:|
| `close` | 所有严格条件同时通过，建议买回平仓 | 是 |
| `hold` | 数据完整，但任一经济性条件未通过 | 否 |
| `not_evaluable` | 持仓、日期、双边报价或手续费证据不完整/不一致 | 否 |

新报告不再生成 `tier`、`tier_label`、`exit_state` 或 `close_action` 等平行状态。读取旧报告时，如果没有明确的 `recommendation_state`，或 `policy_version` 不是当前版本，读取面不猜测旧字段的含义，而是保守投影为 `not_evaluable`。Daily Brief 同样只接受当前版本且决策指标完整的 `close` 行。

## 剩余收益止盈策略

当前唯一策略版本是 `remaining_yield_capture.v1`。以下条件必须全部成立：

1. 持仓为有效的 short Put 或 short Call。
2. 期权仍为 OTM：Put 要求 `spot > strike`，Call 要求 `spot < strike`。
3. 全成本净兑现比例 `>= 80%`。
4. 剩余 DTE `> 0`。
5. 剩余最高年化 `<= 10%`。

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

capital_basis = strike * multiplier * contracts   # Put: 担保资金代理
capital_basis = spot * multiplier * contracts     # Call: 标的市值代理

remaining_max_annualized_return
= all_in_close_cost / capital_basis * 365 / remaining_dte
```

平仓价按 ask 而非 mid 计算，不使用 `last_price` fallback。代理分母不是券商冻结保证金或已核验的股票覆盖；年化值不是期望收益，也不是买回后可获得的替代收益。有效宽价差报价可触发建议，价差只作诊断。阈值是版本化的固定策略，不接受运行配置调整。`max_items_per_account` 只限制消息展示条数，不改变任何持仓的决策。历史 `strict_profit_capture.v1` 的 90%、14 DTE、半程、成本比和价差门槛已退役。

## 必需证据

可评估行必须具备：

- 账户和稳定的 `position_lot_id`；
- option type、short side、strike、合约数、multiplier、currency；
- 开仓权利金、到期日；开仓日期若缺失不阻断新公式，若提供但非法或与到期日矛盾则不可评估；
- 同一份封存行情中的 spot、bid 和 ask；
- Futu 开仓与平仓手续费估算及它们的 fee basis。

任一必需证据缺失、非法、不一致，或封存行情 receipt/payload 校验失败，都必须 fail closed。算术结果也必须有限；新版本 `close` 缺少有效资金分母、剩余最高年化或净兑现比例时，读取与提醒都 fail closed。

## 生命周期

每次运行只取一个 business date，先分类再请求行情：

| `position_lifecycle_state` | 处理 |
|---|---|
| `active` | 进入剩余收益止盈策略 |
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
  -> Daily Brief selects only current-policy, priced, complete-evidence CLOSE rows with decision metrics
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
| `domain/domain/close_advice.py` | 固定阈值、公式、状态、指标完整性和排序 |
| `src/application/close_advice_required_data.py` | 为 active short Put/Call 计划精确合约行情 |
| `src/application/close_advice_runner.py` | 装配已封存输入、费用估算、CSV/文本/审计输出 |
| `src/application/daily_decision_brief_service.py` | 仅投影当前版本且证据完整的 `close` 为日常提醒 |
| `src/application/agent_tools/close_advice_read_impl.py` | 只读已有报告，不重新评估 |

Close Advice 只是建议，不写 trade event，不修改 position lot，不向 broker 下单。

## 设计依据：剩余收益止盈（Devflow 简单流程）

### 目标和范围

目标是为每个有效的 active short Put / short Call 判断：当前可买回时，继续等待期权到期归零所能多取得的最高收益，是否还达到最低资金效率要求。OM 接受 Put 接股和 Covered Call 交股；指派本身不构成平仓理由。`close` 仍只是人工决策提示，不产生订单、roll、换仓或账本写入。Long option、Combo 组合级退出和生产部署不在本次范围。

成功信号：每个具备完整持仓、封存报价与费用证据的适用 short lot 都产生一条可解释的 `close`/`hold` 行；缺失证据继续 `not_evaluable`；重复的 `(account, position_lot_id)` 输入须显式报上下文错误，不可悄悄重复或去重；旧版本报告不被当作新策略建议；Daily Brief 只提示新版本 `close`；费用、Put/Call 资金分母和阈值边界有回归测试。

### 当前事实与复用

实施前的 `strict_profit_capture.v1` 使用 90% 净兑现、14 DTE、半程、0.10% 名义本金和平价差门槛。领域 owner 是 `domain/domain/close_advice.py`；`src/application/close_advice_runner.py` 只组装封存输入与输出；报告读取与 Daily Brief 按 `policy_version` fail closed。复用现有 `CloseAdviceInput`、`opening_net_credit`、`all_in_close_cost`、`net_capture_ratio`、三种 `recommendation_state`、sealed required-data barrier 和版本化费用估算，不建平行评分器或新运行配置。新增 `capital_basis` 与 `remaining_max_annualized_return`，因为现行领域结果没有对应指标；计算归领域 owner，CSV/reader/Brief 仅传递和展示，不重复计算。检索范围：上述 owner、`candidate_engine.py` 的 Put/Call 资本口径、报告/读取/Daily Brief/assistant/materialization 消费者、对应测试；旧 `strong_remaining_annualized_max` 仅在配置校验中作为忽略键出现，没有可复用的现行计算。

### 现行判定

现行版本 `remaining_yield_capture.v1` 固定阈值为 `net_capture_ratio >= 0.80` 且 `remaining_max_annualized_return <= 0.10`。这两个值由已讨论的候选策略给出；80%/10% 尚未经历史结果证明最优，上线前应完成离线逐行回放并报告新旧建议数量；若历史报告不可用，明确保留此验证缺口，不把合成测试冒充校准。

```text
shares = contracts_open * multiplier
opening_net_credit = opening_premium * shares - estimated_open_fee
all_in_close_cost = ask * shares + estimated_close_fee
net_capture_ratio = 1 - all_in_close_cost / opening_net_credit
capital_basis = strike * shares             # short Put: 接股所需名义资金
capital_basis = spot * shares               # short Call: 标的市值代理值，不证明股票覆盖
remaining_max_annualized_return =
    all_in_close_cost / capital_basis * 365 / remaining_dte
```

“剩余最高”表示相对于现在按 ask 加费用买回，继续持有且期权到期归零时最多可节省的买回成本；不是期望收益或买回后必然可得的替代收益。Call 买回后若原本持有股票，股票仍在，不能称股票市值为释放资金。Put 的 `strike * shares` 是担保资金代理值，不声称为券商实时冻结保证金；Call 的 `spot * shares` 是标的市值代理值，不据此证明股票覆盖关系；报告明确标注。两者与开仓候选的资本口径可能不同。

有效输入、费用与封存行情仍须满足现行 fail-closed 规则；`remaining_dte > 0` 和正的 `capital_basis` 是新公式必需条件。原始输入和 `opening_net_credit`、`all_in_close_cost`、`capital_basis`、净兑现比例、剩余最高年化等计算结果都须为有限数，否则 `not_evaluable`。若仍为 OTM（Put `spot > strike`，Call `spot < strike`）、净兑现至少 80%、剩余最高年化至多 10%，输出 `close`；其他可评估行输出 `hold`。ITM 不因可能指派触发 `close`。不再以 14 DTE、原期限前半程、买回成本/行权价 0.10% 或价差 30% 作为经济性门槛；仍保留双边报价合法性和按 ask 加费用的买回成本。有效的宽价差报价（包括 `bid=0`）可以触发 `close`，`spread_ratio` 仅作诊断，不代表成交保证。未提供 `opened_at` 可评估；已提供但非法或与到期日矛盾则 `not_evaluable`。`original_dte` 仅在有效开仓日已知时计算，旧 `remaining_term_ratio` 可为空。

到期日、已过期或未知到期日继续由 runner 的 lifecycle 边界输出 `not_evaluable`，不请求普通买回报价。数据缺失/非法、资金分母无效或报告/receipt 版本不匹配时 fail closed，不生成通知。新策略的报告行及 `strategy_profile` 采用新版本；读取旧报告保守投影，不把旧 `close` 当新策略 `close`。新版本 `close` 若缺失有限且有效的资金分母、剩余最高年化或净兑现指标，通知选择器必须拒绝，只读面投影为 `not_evaluable`；消费者只核对完整性，不重算策略。保留 `net_capture_ratio`、`all_in_close_cost` 及旧诊断列用于审计；新增 `capital_basis` 和 `remaining_max_annualized_return` 到领域结果、CSV、只读投影、Daily Brief 的两条 metrics 路径及 assistant 展示。runner、Brief 和 assistant 的可操作文案主要展示净兑现与剩余最高年化，按 Put/Call 标明代理分母，不突出旧门槛。排序由领域 owner 决定，优先 `close`，再按较低剩余年化排序，同值按净兑现、账户、符号和 lot ID 稳定排序；reader 和摘要复用该顺序，限量展示不得改序。

示例（忽略手续费）：开仓每股 5、当前 ask 1、担保资金每股 50、剩余 30 天，净兑现 80%，剩余最高年化 24.33%，输出 `hold`；ask 降到 0.30 时，净兑现 94%，年化 7.30%，输出 `close`。含手续费时必须以全成本重算，不套用这两个示例结果。

### 实现切片与验证

1. 领域策略：新版本、公式、边界与排序；验证 Put/Call、80%/10% 等号、OTM/ITM、DTE、无效费用和原始 DTE 缺失。
2. 报告消费者：runner 的版本/CSV/文案、重复 lot 检查，read surface、Daily Brief、assistant 和摘要的新版本及指标，并在实现时更新本文件前半部的现行契约；验证每个适用 lot 一行、只有完整 `close` 通知、旧报告 fail closed、封存快照缺失不降级、不同入口排序一致。

两片共同覆盖上述成功信号，第二片依赖第一片。设计 owner 为本文件；实现 owner 保持现有 domain/runner/reader/Daily Brief，不新增配置、状态或外部写入。验证使用领域单测、runner 和 Daily Brief/reader 针对性测试、文档/guardrail 检查，以及只读历史报告回放。历史报告只能衡量建议频率与已封存输入，不足以证明买回后收益最优；正式部署仍需另行授权。

四路 Panel 对原快照的建议经核对后：采纳日期缺失/非法区分、指标及算术完整性、跨入口排序、重复 lot、Call 分母措辞、旧文案同步和宽价差语义；报告 manifest 增加策略版本暂缓，因为已有 CSV 行版本和 manifest 字节校验，零行报告不会生成建议，扩展 manifest 并非本次成功信号所需。未采用要求真实股票覆盖作为 Call 适用前提，因为这会新增账本绑定条件，超出已确认的简化策略；仅明确代理分母不证明覆盖。跨模型 reviewer 调用不可用，本轮四份可用结果均来自同家族原生 subagent，独立模型家族未验证。

拒绝方案：引入替代仓扫描或换仓比较会扩大 Close Advice 职责；指派风险触发平仓与 OM 接受指派的策略前提相冲突；仅使用剩余年化而不设净兑现门槛可能为几乎未获利的仓位发出止盈提示。未决证据：历史回放结果和远端当前可访问性；它们不改变本次固定候选规则，但限制对阈值优劣的结论。
