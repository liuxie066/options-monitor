# Close Advice Contract

本文前半部记录持仓评估与建议（Position Advice）的现行契约、`remaining_yield_capture.v3` 止盈策略、Short Put 向下移仓及 Covered Call 上滚的调度评估与主动查询；后半部保留 v2 和 v3 的设计过程作为历史资料。运行规则以前半部及代码为准。

Close Advice 判断已有 short Put / short Call 是否值得提前买回：净兑现至少 80% 开仓净权利金，且继续持有到期归零所能多取得的最高期权收益年化不超过 10% 时考虑提示平仓；临期远价外的仓位优先持有。OM 接受 Put 接股及 Covered Call 交股；指派本身不是提前平仓理由。

Close Advice 止盈策略不建议新开仓，不 roll，不比较替代标的，不 reallocate，不根据方向、集中度或 short-vol thesis 产生平仓建议，也不处理 long option 的止盈或止损。Delta 只用于临期持有例外，不单独触发平仓。开仓候选仍由 Candidate Engine 负责。Position Advice 的同级移仓策略在 Tick 中评估 Short Put 向下移仓和 Covered Call 上滚；`short_put_roll_candidates`、`covered_call_roll_candidates` 另提供主动查询。移仓结论保留在独立的 `roll_advice` 中，不改变止盈策略的 `close/hold/not_evaluable` 含义，也不要求先取得 `close` 结论。

## 决策状态

`recommendation_state` 是当前建议状态；休市历史参考另用 `reference_recommendation_state` 保留当时结论，不充当当前建议：

| 状态 | 含义 | 进入日常提醒 |
|---|---|---:|
| `close` | 所有严格条件同时通过，建议买回平仓 | 是 |
| `hold` | 必需经济数据有效，但经济性条件未通过，或满足临期远价外持有例外 | 否 |
| `not_evaluable` | 持仓、日期、双边报价或手续费证据不完整/不一致 | 否 |

新报告不再生成 `tier`、`tier_label`、`exit_state` 或 `close_action` 等平行状态。读取旧报告时，如果没有明确的 `recommendation_state`，或 `policy_version` 不是当前版本，读取面不猜测旧字段的含义，而是保守投影为 `not_evaluable`。Daily Brief 的平仓建议只接受当前版本、当前时点证据有效且决策指标完整的 `close` 行；移仓建议另验证当前 `roll_advice` 的证据与候选。`hold` 仅表示这一止盈规则没有触发平仓，不代表对持仓整体风险的认可。

## 剩余收益止盈策略

当前唯一策略版本是 `remaining_yield_capture.v3`。以下经济条件必须全部成立：

1. 持仓为有效的 short Put 或 short Call。
2. 期权仍为 OTM：Put 要求 `spot > strike`，Call 要求 `spot < strike`。
3. 全成本净兑现比例 `>= 80%`。
4. 剩余 DTE `> 0`。
5. 剩余最高年化 `<= 10%`。

经济条件不通过时输出 `hold`。条件通过后，若同一份封存行情有有效 `|delta| > 0.05`，或独立日历已证实剩余可交易日超过 3 个，输出 `close`；若 `|delta| <= 0.05` 且剩余可交易日不超过 3 个，输出 `hold`，倾向持有到期。其余证据不足以排除临期例外的组合输出 `not_evaluable`，不提醒。Delta 必须有限且 `|delta| <= 1`；日历不依赖 RV/QFQ。

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

`remaining_dte` 是自然日；周末和假期仍计入资金占用与年化。临期例外按相应市场的可交易日计数；未来交易日计入，当天仅在期权快照接收时或之后取得、同一市场日期的 `MORNING`/`AFTERNOON` 状态可证明仍在交易时才计入。已证实收市则不计入；状态未知时分别计算包含和不包含今天的上下界。半日市计 1 日，临时停市可能仍出现在提供方日历中。

平仓价按 ask 而非 mid 计算，不使用 `last_price` fallback。代理分母不是券商冻结保证金或已核验的股票覆盖；年化值不是期望收益，也不是买回后可获得的替代收益。有效宽价差报价可触发建议，价差只作诊断。阈值是版本化的固定策略，不接受运行配置调整。`max_items_per_account` 只限制消息展示条数，不改变任何持仓的决策。历史 `strict_profit_capture.v1` 的 90%、14 DTE、半程、成本比和价差门槛已退役。

## 必需证据

可评估行必须具备：

- 账户和输入身份：账本持仓使用稳定 `position_lot_id`；临时手工输入使用 `input_source=user_provided`、独立 `input_id` 与带时区的 `input_as_of_utc`，不生成账本 lot；
- option type、short side、strike、合约数、multiplier、currency；
- 开仓权利金、到期日；开仓日期若缺失不阻断新公式，若提供但非法或与到期日矛盾则不可评估；
- 同一份封存行情中的 spot、bid 和 ask；
- Futu 开仓与平仓手续费估算及它们的 fee basis。

输出 `close` 还需同一份封存行情中的有效 Delta，或已封存且完整的市场交易日日历能够证实剩余可交易日超过 3 个。低 Delta 的临期持有结论则同时需要有效 Delta 和日历；无法排除该例外时为 `not_evaluable`。

任一必需证据缺失、非法、不一致，或封存行情 receipt/payload 校验失败，都必须 fail closed。算术结果也必须有限；新版本 `close` 缺少有效资金分母、剩余最高年化或净兑现比例时，读取与提醒都 fail closed。

## 生命周期

每次运行取同一个 UTC 起点，分别换算美东和香港市场日期；持仓筛选、生命周期、自然日 DTE、日历和行情日期对同一 lot 必须一致。先分类再请求行情：

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
  + same sealed required-data batch (old contracts and roll candidates)
  + versioned Futu fee schedule + verified CC stock coverage
  -> independent close policy and Put/CC roll policies
  -> close_advice.csv (roll_advice JSON) / close_advice.txt / report manifest
  -> Daily Brief positions (close / roll_down / roll_up)
  -> configured daily notification and position_advice_read
```

调度 Tick 与主动评估入口复用相同 runner、版本化 evaluator、费用 owner、run-scoped required-data plan 和封存 snapshot。Runner 必须同时取得 run ID、snapshot manifest 和该 manifest 绑定的 Close Advice plan；任一缺失或变更都在发布前失败关闭。评估期间不得修复 cache、回退到 last price 或重新请求 OpenD。每行报告保留 plan、binding、snapshot、receipt、payload hash 和观测时间，用于追溯决策输入。

当前报告的 manifest 必须声明 `quote_mode=frozen_snapshot`、非空 `run_id`、snapshot manifest SHA-256 和 Close Advice plan SHA-256。每个 CSV 数据行的 `quote_mode` 及两个 SHA-256 必须与 manifest 完全一致；任一行不一致会使整份当前报告不可读。零行报告仍可有效，但报告级绑定必须完整。

`position_advice_read` 是统一的持仓建议读取工具，从 canonical latest Daily Brief 的 `positions` 读取平仓、Put 向下移仓及 CC 上滚建议，对应 `close`、`roll_down`、`roll_up`。读取沿用简报的账户、市场和完整性校验，不刷新持仓、不拉取行情、不重新评估，也不发送通知。平仓行的 `close_actionable` 由简报生产方按完整策略证据、在通知展示裁剪前封存；读取时再结合简报时效，`notification_eligible` 仅控制通知展示，历史缺少决策资格字段的行不升级为当前可操作。既有 `close_advice_read` 仍可只读平仓报告：默认路径与 `run_id` 路径只读取通过上述 sealed 校验的报告；显式 `report_path` 用于查看历史字节，缺少完整 sealed provenance 或与 manifest 不一致的历史行一律投影为 `not_evaluable`。

### Tick 调度移仓与提醒

启用 Position Advice 后，Tick 在同一批 run-scoped required-data plan 中封存旧仓报价、适用的替代期权链及搜索覆盖证据，评估期间不另拉行情。搜索最晚到期日为本次市场日期加上该标的对应方向配置的 `max_dte`：Put 使用 `sell_put`，Call 使用 `sell_call`，默认均为 60 天；这只规定调度搜索上限，主动查询仍显式传入 `max_new_expiration`。新到期日不得早于旧仓，新仓 DTE 必须大于零；上限容纳不了旧仓时没有合格搜索区间，不扩张用户配置。

每个旧 lot 的移仓结果作为 `roll_advice` JSON 写入 `close_advice.csv`，保留适用性、覆盖缺口、原因和已排序候选，再投影到 Daily Brief `positions`。`actionable_until_utc` 取双腿报价及其现价有效期、股票覆盖有效期（CC）和封存来源有效期的最小值；过期或缺少此字段时，统一读取保留原结果但不标为当前可操作，不生成新滚仓提醒。移仓建议独立于止盈状态；旧仓为 `hold` 不阻止有充分证据的移仓建议，缺证或净支出排除也不改写止盈结论。CC 仍须核实同账户全部 Call 的股票覆盖；Put 和 CC 均要求扣费净现金严格大于零，零现金及净支出均排除。

简短通知展示该 lot 排序首位的目标合约、旧 ask／新 bid、行权价变化、扣费净现金及收益率。通知中的移仓年化统一使用净现金年化 `incremental_annualized_return`，分母沿用各策略：Put 为新仓 `strike × 股数`，CC 为本次 `spot × 覆盖股数`；CC 的条件性交股改善不替代净现金年化。两方向统一按净现金年化优先、方向对应的 strike 调整比例次优排序，首位只代表当前已评价集合；有部分覆盖时保留缺口。通知沿用 Daily Brief 的账户/市场隔离、发送配置、幂等和交付确认；飞书卡片与备用正文完成最终字节预算裁剪后，必须保留同一组完整移仓条目才封存确认身份，未完整显示的条目保留待提醒；主动评估、`roll-down` 和 `roll-up` 查询仅写独立请求报告，均不发送通知。所有路径均不下单、不写业务账本。

### 主动评估与两种输入

`position_advice_evaluate` Tool 与 `om position-advice evaluate --request-file <JSON>` 调用同一服务。请求必须明确配置与账户；两种模式互斥：

- 账本选择：`symbols`、`lot_ids`、`contracts`（symbol/option_type/expiration/strike）可选，提供多类选择时取交集；全部省略则选择该账户及配置券商的 open short Put/Call，最多 100 笔。账本缺失或不可信不当作空持仓。
- 临时输入：`positions` 列表最多 100 笔，每笔必需 input_id、input_as_of_utc、symbol、option_type、expiration、strike；可提供 contracts_open、multiplier、currency、premium、opened_at。缺少数量、成本或费用等证据仍显示已知诊断，不产生支持不足的结论。账户/券商绑定到请求配置；不接受 lot_id/record_id，不写 ledger。

人工 CLI 默认展示合约、原数据时间、评估模式、净兑现比例、剩余最高年化、资金分母、结论及原因；缺失指标显示不可用。追加 `--format json` 保留原业务 JSON 对象，`om tools run --tool position_advice_evaluate --input-file <JSON>`（兼容 `om-agent run`）保留工具 envelope。`success`、`partial`、合法空行、`disabled` 和 `unavailable` 都是完成请求，退出 0；输入或执行错误退出 2。部分与不可评估不会转换为合法空结果。历史参考单列历史结论；hold 仅说明现有策略未触发平仓条件。

多个标的使用已配置且唯一的行情路由；非 watchlist 标的只在可解析到唯一有效路由时评估。行情链发现可能返回额外合约，但只评估所选持仓，不运行开仓候选策略。每次主动请求使用独立目录：`output_shared/position_advice_requests/<request_id>/`，其内部保留 `output_runs/<request_id>/required_data` 与封存报告；不会覆盖调度 latest。主动入口不发送通知，不更新 Daily Brief。显式选择的 runtime root 优先；没有选择实例时，使用所加载配置的父目录，账本、质量门禁和请求报告保持同一实例范围。

### Short Put 向下移仓主动查询

`short_put_roll_candidates` Tool 与 `om position-advice roll-down --request-file <JSON> [--config-key us|hk] [--format text|json]` 共用 `src/application/short_put_roll.py::short_put_roll_candidates`。纯经济计算、准入与排序由现有可选包内 `plugins/position-advice/src/om_position_advice_plugin/short_put_roll.py` 持有，费用复用 `domain/domain/fee_calc.py`；CLI 不重新计算收益或另排候选。它是止盈评估的同级能力，复用 Position Advice 的安装和启用边界，查询不会自动启用插件。

请求必填 `account`、已有 `lot_id`、`max_new_expiration`（YYYY-MM-DD，无默认值）；可选 `min_new_strike > 0`、整数 `limit`（1..50，默认 10，仅限制展示）。提供最低新 strike 时必须低于旧 strike；对进入搜索范围的旧仓，取得有效现价后还要求最低新 strike 严格低于现价，否则返回输入错误且不请求期权链。一次仅处理该账户中一个有效、未到期的标准 short put，替换其全部 `contracts_open`，不合并 lot、不部分移仓、不增加张数。合约乘数来自证据，不默认 100。

旧仓价外幅度为 `(spot - old_strike) / spot`，负值为价内。价内、平值及价外幅度 `<=5%` 纳入；超过 5% 返回 `outcome=not_applicable`、`coverage=null` 和 `old_position_outside_roll_scope`，不搜索替代期权链、不声称没有机会。现价缺失或过期则不可评估。新 put 必须同标的、市场、币种、乘数和交割单位，新 strike 同时严格低于旧 strike 与有效现价；新到期日不早于旧到期日且不超过请求上限，不套用 CSP 的 Delta、IV/RV 或最低开仓收益门槛。

记 `Q` 为剩余张数，`M` 为已验证乘数，`K0/K1` 为旧/新 strike，`T0/T1` 为评估市场日期到旧/新到期日的自然日数。旧仓买回总支出 `C = old_ask × Q × M + old_buy_fee`；新卖出净收入 `N = new_bid × Q × M - new_sell_fee`；净增现金 `R = N - C` 必须严格大于零。两腿费用各按实际 Q 估计并保留币种、fee_basis 和版本；不按一张费用简单乘 Q。旧仓买回可使用 bid 为零而 ask 有效的正常报价；新仓卖出仍要求正 bid，两腿身份、新鲜度、市场状态与行情权限资格均须满足。资金分母 `B0/B1 = K0/K1 × Q × M`：

- 净增现金期间回报：`R / B1`；净增现金年化：`R / B1 × 365 / T1`。
- 旧仓继续持有到期归零最高年化：`C / B0 × 365 / T0`；新仓到期归零最高年化：`N / B1 × 365 / T1`。
- 新旧到期归零最高年化之差以 pp 展示；延期为 `T1 - T0`，strike 降幅为 `(K0 - K1) / K0`。这些是到期归零假设下的上限口径，不是期望收益；净增现金不等于交易盈利。

仅在同一旧 lot 内按净增现金年化降序，其次 strike 降幅降序、延后天数升序、净增现金降序，最后以合约身份稳定排序。先评价和排序已获取集合，再应用 limit；首行只代表该排序口径。缺少新仓 Delta、IV/RV 等展示指标不阻断有充分证据的现金估算，报价、现价、费用或身份缺失则保留不可评估原因。两条单腿分别按 ask/bid 的 `leg_cashflow_estimate` 不是组合盘口，也不保证组合可成交；休市或过期观察不能形成当前正净收入结论。

业务结果为 `short_put_roll.output.v1`，保留旧仓、评估时点、`scope` 请求边界和实际已请求到期日、`coverage` 枚举/计数、候选、展示/省略数及原因。`status=success|partial|disabled|unavailable` 与 `outcome=found|none|unable|not_applicable` 分开表达：覆盖完整才可返回 `none`；有候选且存在未评价范围为 `found + partial`；没有合格候选但存在缺口为 `unable`。`disabled` 表示插件未启用；`unavailable` 的插件缺失与行情依赖失败按原因码分别展示。缺失诊断保留不可用，不补零。新腿证据完整且明确停牌或证券状态不正常时计入排除；伴随缺失或陈旧证据时仍计入未评价，旧腿不合格仍阻断整次查询。文字输出提供旧/新收益比较、两腿费用与证据；JSON 输出原业务对象，Tool 保留 envelope；完成请求退出 0，输入或执行错误退出 2。独立 request/result 报告写入失败返回 `REPORT_WRITE_FAILED` 执行错误，不作为行情依赖失败，也不返回未保存成功的报告路径。

主动查询仅按需读取行情并写独立请求报告，不下单、不写账本、不通知，也不改写调度 Tick 的报告或 Daily Brief。同一纯移仓策略另由 Tick 消费封存批次，按上文合同进入简报与提醒。它不改变旧仓生命周期及 `close/hold/not_evaluable` 含义，也不要求旧策略先判定 `close`。

### Covered Call 上滚主动查询

`covered_call_roll_candidates` Tool 与 `om position-advice roll-up --request-file <JSON> [--config-key us|hk] [--format text|json]` 共用 `src/application/short_put_roll.py::covered_call_roll_candidates` 的取数、费用及证据编排；纯策略和排序由 `plugins/position-advice/src/om_position_advice_plugin/covered_call_roll.py` 持有，复用 Put 的身份与双腿现金校验。安装/启用边界与原 Position Advice 相同。

请求必填 `account`、已有 short call 的 `lot_id`、`max_new_expiration`（YYYY-MM-DD，无默认值，不得早于旧到期日）；可选 `max_new_strike > old_strike`、`limit`（整数 1..50，默认 10，仅限制展示）。仅等量替换一个 active 标准 short call lot 的全部剩余张数。旧 Call 价外幅度为 `(old_strike - spot) / spot`，价内、平值及价外幅度 `<=5%` 纳入；更深价外返回 `success/not_applicable`、`coverage=null`，不搜索候选。新 Call 必须同标的、市场、币种、乘数和交割单位，`new_strike > old_strike` 且 `new_expiration >= old_expiration`，不超过请求上限，新仓 DTE 必须大于零。与 Put 相同，同日到期也可评估，延期天数为零；更早到期以 `new_expiration_before_old` 排除。允许新 Call 仍然价内；这只提升交股价，不证明已消除提前指派。

查询先通过既有 portfolio owner 取得或复用同账户、同券商、同市场且完整、新鲜、未过滤的股票快照（现有股票时效上限 300 秒；缺失或不合格快照按需刷新；不写 portfolio 缓存）。再由 `domain/domain/risk_capacity.py::evaluate_covered_call_stock_coverage` 比较同标的、同币种的实际股票总量与该账户所有未关闭 short call 的交割股数之和，防止不同 lot 重复占用同一批股票；无法确认 Call 义务或乘数时保留不可用。使用总持股而非已扣除锁定股票的可卖数量。确认零持股或股数不足返回 `success/not_applicable`、`insufficient_stock_coverage`、`coverage=null`，不拉期权行情、不建议交易；覆盖证据缺失、过期或账户不符返回 `unable`，不能等同于已确认无持股。`stock_coverage` 单列持股证据与时间，`coverage` 仍专指候选搜索覆盖。股票来自合格券商快照，Call 义务来自可信本地账本；本次不新增券商期权与账本的全量对账。

记 `S = Q × M` 为覆盖股数，`P` 为本次有效现价，`K0/K1` 为旧/新 strike。两腿仍按旧 ask 买回、新 bid 卖出分别扣费：

```text
C = old_ask × S + old_buy_fee
N = new_bid × S - new_sell_fee
R = N - C                                      # net_credit：有符号净现金
D = max(-R, 0)                                 # net_debit：显式净支出
U = (K1 - K0) × S                              # assignment_proceeds_uplift
G = U + R                                      # assignment_net_improvement
B0 = B1 = P × S                                # covered_stock_market_value
assignment_improvement_period_return = G / B1
assignment_improvement_annualized_return = G / B1 × 365 / new_dte
incremental_period_return = R / B1
incremental_annualized_return = R / B1 × 365 / new_dte
```

CC 不返回 Put 的 `accepted`。资格有效时，仅 `R>0` 为 `recommendation=credit_roll`、`cash_flow_type=credit`；`R<=0` 均为 `recommendation=not_recommended`，通过与 Put 相同的 `non_positive_net_credit` 计入排除。零现金保留 `cash_flow_type=cash_neutral`，负现金保留 `cash_flow_type=debit`；这些描述不表示推荐，即使交股价提升大于净支出也不推荐。方向、到期、覆盖等资格不符为 `excluded`。纯策略仍保留被排除候选的净支出、现金年化及条件性交股改善指标以解释原因。金额以 Decimal 比较，先扣除两腿费用，再检查净现金是否严格为正，不用展示取整结果决定资格；任一腿费用都可能把净收入候选变为零现金或净支出而排除。

`G` 比较的是“旧仓与新仓最终都按各自行权价卖出同量股票”时的条件性交股收入改善，不是已实现利润、预期收益或可兑现套利；新仓未被指派、股价变化、持股占用时间及提前指派均可能改变实际结果。本规则不引入主观持股成本、Delta 概率或开仓收益门槛。报告必须同时展示 `net_credit`、`net_debit`、每股 `strike_increase`、总额 `assignment_proceeds_uplift`、`assignment_net_improvement` 及延后天数。被指派交股是 wheel 的第二步，接受旧仓交股仍是有效选择，不把交股描述为失败或告警。

CC 仅对合格的净收现金候选排序，与 Put 使用同一排序逻辑：`incremental_annualized_return` 降序、方向对应的行权价变动比例降序（CC 为 `strike_increase_pct`，Put 为 `strike_drop_pct`）、`extension_days` 升序、`net_credit` 降序，再按合约身份、到期日稳定排序。`assignment_improvement_annualized_return` 仅展示条件性交股差额，不参与排序。JSON 为 `covered_call_roll.output.v1`，沿用 `success/partial/disabled/unavailable`、`found/none/unable/not_applicable` 和 `coverage=complete/partial/unavailable` 语义，只有完整搜索零候选才是 `none`；报告保存在 `output_requests/covered_call_roll/<request_id>/`，写入失败为执行错误。主动查询不下单、不写业务账本、不通知，也不改写 Tick 或 Daily Brief；调度路径另按上文合同发布。

| 口径 | Short Put 向下移仓 | Covered Call 上滚 |
|---|---|---|
| 新行权价与到期 | 严格更低且严格价外；到期不早于旧仓，DTE 为正 | 严格更高；到期不早于旧仓，DTE 为正；可仍价内 |
| 现金判定 | `R>0` 才 `accepted` | `R>0` 才推荐；零现金与净支出均排除 |
| 资金分母 | 各自 `strike × S`，担保资金代理 | 新旧均为 `spot × S`，覆盖股票当前市值代理 |
| 排序主项 | 净增现金年化 | 净增现金年化 |
| 指派语义 | 可能接股 | wheel 第二步卖出持股，仍是有效选择 |
| 持股前置 | 不要求持有股票 | 必须先核实所有 Call 的股票覆盖 |

### 行情时效与休市

行情按需取快照，不持续订阅。快照请求成功不等于实时：封存标的与期权各自的行情权限、观测时间与市场状态，只有双侧为已确认实时权限、报价/标的时间相容且未超过共享 300 秒窗口、同市场日期、连续交易状态一致、期权自身证券状态为 NORMAL 且明确未停牌、快照后的独立状态仍可交易时，才能产生 `assessment_mode=current`。最近成交时间不能证明 bid/ask 更新时间。未知、延迟、权限不足、旧快照或状态冲突均为不可评估；保留可见指标与原因。

午休、盘前盘后、周末及假日等已知非连续状态，只搜索经过报告 hash/封存绑定校验、原始评估资格完整且与账户/券商/提供方/合约/数量/成本匹配的最近报告。保留原时点的 spot、bid/ask、DTE、日历与指标，标为 `historical_reference`；当前 recommendation 为 not_evaluable，当时结论放在 reference_recommendation_state，不产生新平仓通知。手工输入的持仓事实时间必须不晚于参考快照；到期日、过期或没有兼容记录时明确不可用。不会混合盘后标的价格与日间期权 ask，也不会把周五报价套用周日 DTE。旧报告缺少新资格证据时仍可只读，但不自动升级为合格历史参考。

### 可选安装

`om-position-advice-plugin` 是独立安装包。官方分发入口为 `om plugins install position-advice` 预览、追加 `--confirm` 安装；卸载用 `om plugins uninstall position-advice --confirm`。插件须来自精确匹配的 OM Release；v4.6.8 已提供正式 wheel，其他版本须核对各自清单，缺资产时明确失败。源码开发仍可使用原环境的 pip 安装或卸载 `plugins/position-advice`。安装不启用；仍由现有 `close_advice.enabled` 开关控制，无新插件注册中心。缺包或关闭时核心可以启动、扫描，已有报告继续可读。既有内置建议能力迁移时保留明确启用意图；已进入可选包版本后的主动卸载不被升级静默补装。详见[官方可选插件](INSTALL.md#官方可选插件)和[受控升级](RELEASE_PROCESS.md)。

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
| `domain/domain/close_advice.py` / `plugins/position-advice` | 核心输入合同、安全读取/排序/通知资格与懒加载门面；可选包执行唯一版本化策略及结论文案 |
| `src/application/close_advice_required_data.py` | 为 active short Put/Call 计划精确合约及移仓范围行情，并封存独立交易日日历与市场状态 |
| `src/application/close_advice_runner.py` | 装配已封存输入、费用估算、独立止盈与移仓评估及 CSV/文本/审计输出 |
| `src/application/daily_decision_brief_service.py` | 将当前版本且证据完整的平仓与移仓结论投影到 Daily Brief `positions` |
| `src/application/position_advice.py` / `position_advice_history.py` | 主动选择、隔离请求编排与兼容历史参考，不新增财务公式 |
| `src/application/position_advice_evidence.py` | 调度与主动评估共用的时效、时段、行情权限资格 |
| `position_advice_read` Tool | 只读 canonical latest Daily Brief 的平仓和移仓建议，不重新评估 |
| `src/application/agent_tools/close_advice_read_impl.py` | 只读已有平仓报告，不重新评估 |

Close Advice 只是建议，不写 trade event，不修改 position lot，不向 broker 下单。

## 历史设计记录：剩余收益止盈 v2（Devflow 简单流程）

### 目标和范围

目标是为每个有效的 active short Put / short Call 判断：当前可买回时，继续等待期权到期归零所能多取得的最高收益，是否还达到最低资金效率要求。OM 接受 Put 接股和 Covered Call 交股；指派本身不构成平仓理由。`close` 仍只是人工决策提示，不产生订单、roll、换仓或账本写入。Long option、Combo 组合级退出和生产部署不在本次范围。

成功信号：每个具备完整持仓、封存报价与费用证据的适用 short lot 都产生一条可解释的 `close`/`hold` 行；缺失证据继续 `not_evaluable`；重复的 `(account, position_lot_id)` 输入须显式报上下文错误，不可悄悄重复或去重；旧版本报告不被当作新策略建议；Daily Brief 只提示新版本 `close`；费用、Put/Call 资金分母和阈值边界有回归测试。

### 当时事实与复用

实施前的 `strict_profit_capture.v1` 使用 90% 净兑现、14 DTE、半程、0.10% 名义本金和平价差门槛。领域 owner 是 `domain/domain/close_advice.py`；`src/application/close_advice_runner.py` 只组装封存输入与输出；报告读取与 Daily Brief 按 `policy_version` fail closed。当时复用现有 `CloseAdviceInput`、`opening_net_credit`、`all_in_close_cost`、`net_capture_ratio`、三种 `recommendation_state`、sealed required-data barrier 和版本化费用估算，不建平行评分器或新运行配置。新增 `capital_basis` 与 `remaining_max_annualized_return`，因为此前领域结果没有对应指标；计算归领域 owner，CSV/reader/Brief 仅传递和展示，不重复计算。检索范围：上述 owner、`candidate_engine.py` 的 Put/Call 资本口径、报告/读取/Daily Brief/assistant/materialization 消费者、对应测试；旧 `strong_remaining_annualized_max` 仅在配置校验中作为忽略键出现，没有可复用的当时计算。

### 当时的 v2 判定

当时的 `remaining_yield_capture.v2` 固定阈值为 `net_capture_ratio >= 0.80` 且 `remaining_max_annualized_return <= 0.10`，并加入剩余交易时段不超过 3 个、`|delta| <= 0.05` 的临期持有例外。这些阈值尚未经历史结果证明最优；若历史报告不可用，应明确保留验证缺口，不把合成测试冒充校准。

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

有效输入、费用与封存行情仍须满足现行 fail-closed 规则；`remaining_dte > 0` 和正的 `capital_basis` 是新公式必需条件。原始输入和 `opening_net_credit`、`all_in_close_cost`、`capital_basis`、净兑现比例、剩余最高年化等计算结果都须为有限数，否则 `not_evaluable`。若仍为 OTM（Put `spot > strike`，Call `spot < strike`）、净兑现至少 80%、剩余最高年化至多 10%，且未满足上述临期持有例外，输出 `close`；其他可评估行输出 `hold`。ITM 不因可能指派触发 `close`。不再以 14 DTE、原期限前半程、买回成本/行权价 0.10% 或价差 30% 作为经济性门槛；仍保留双边报价合法性和按 ask 加费用的买回成本。有效的宽价差报价（包括 `bid=0`）可以触发 `close`，`spread_ratio` 仅作诊断，不代表成交保证。未提供 `opened_at` 可评估；已提供但非法或与到期日矛盾则 `not_evaluable`。`original_dte` 仅在有效开仓日已知时计算，旧 `remaining_term_ratio` 可为空。

到期日、已过期或未知到期日继续由 runner 的 lifecycle 边界输出 `not_evaluable`，不请求普通买回报价。数据缺失/非法、资金分母无效或报告/receipt 版本不匹配时 fail closed，不生成通知。新策略的报告行及 `strategy_profile` 采用新版本；读取旧报告保守投影，不把旧 `close` 当新策略 `close`。新版本 `close` 若缺失有限且有效的资金分母、剩余最高年化或净兑现指标，通知选择器必须拒绝，只读面投影为 `not_evaluable`；消费者只核对完整性，不重算策略。保留 `net_capture_ratio`、`all_in_close_cost` 及旧诊断列用于审计；新增 `capital_basis` 和 `remaining_max_annualized_return` 到领域结果、CSV、只读投影、Daily Brief 的两条 metrics 路径及 assistant 展示。runner、Brief 和 assistant 的可操作文案主要展示净兑现与剩余最高年化，按 Put/Call 标明代理分母，不突出旧门槛。排序由领域 owner 决定，优先 `close`，再按较低剩余年化排序，同值按净兑现、账户、符号和 lot ID 稳定排序；reader 和摘要复用该顺序，限量展示不得改序。

示例（忽略手续费）：开仓每股 5、当前 ask 1、担保资金每股 50、剩余 30 天，净兑现 80%，剩余最高年化 24.33%，输出 `hold`；ask 降到 0.30 时，净兑现 94%，年化 7.30%，输出 `close`。含手续费时必须以全成本重算，不套用这两个示例结果。

### 实现切片与验证

1. 领域策略：新版本、公式、边界与排序；验证 Put/Call、80%/10% 等号、OTM/ITM、DTE、无效费用和原始 DTE 缺失。
2. 报告消费者：runner 的版本/CSV/文案、重复 lot 检查，read surface、Daily Brief、assistant 和摘要的新版本及指标，并在实现时更新本文件前半部的现行契约；验证每个适用 lot 一行、只有完整 `close` 通知、旧报告 fail closed、封存快照缺失不降级、不同入口排序一致。

两片共同覆盖上述成功信号，第二片依赖第一片。设计 owner 为本文件；实现 owner 保持现有 domain/runner/reader/Daily Brief，不新增配置、状态或外部写入。验证使用领域单测、runner 和 Daily Brief/reader 针对性测试、文档/guardrail 检查，以及只读历史报告回放。历史报告只能衡量建议频率与已封存输入，不足以证明买回后收益最优；正式部署仍需另行授权。

四路 Panel 对原快照的建议经核对后：采纳日期缺失/非法区分、指标及算术完整性、跨入口排序、重复 lot、Call 分母措辞、旧文案同步和宽价差语义；报告 manifest 增加策略版本暂缓，因为已有 CSV 行版本和 manifest 字节校验，零行报告不会生成建议，扩展 manifest 并非本次成功信号所需。未采用要求真实股票覆盖作为 Call 适用前提，因为这会新增账本绑定条件，超出已确认的简化策略；仅明确代理分母不证明覆盖。跨模型 reviewer 调用不可用，本轮四份可用结果均来自同家族原生 subagent，独立模型家族未验证。

拒绝方案：引入替代仓扫描或换仓比较会扩大 Close Advice 职责；指派风险触发平仓与 OM 接受指派的策略前提相冲突；仅使用剩余年化而不设净兑现门槛可能为几乎未获利的仓位发出止盈提示。未决证据：历史回放结果和远端当前可访问性；它们不改变本次固定候选规则，但限制对阈值优劣的结论。

## v3 设计记录：独立交易日证据与临期持有

### 目标、边界和现状

目标：对所有 active short Put / short Call 维持现有 80% 净兑现、10% 剩余最高年化和 OTM 前提；仅当到期不超过 **3 个可交易日** 且 `|delta| <= 0.05` 时，优先建议持有。自然日 DTE 仍用于年化，交易日只用于临期例外。`close` 只是人工提示；接受指派，不加入换仓、roll、方向风险退出、配置化阈值、订单或账本写入。

实施前的 v2 通过 `term_matched_rv_status == ok` 才取 `term_matched_rv_remaining_sessions`，而 Close Advice 专用计划的 `requires_realized_volatility` 为 false；日历缺失时 v2 仍可输出 `close`。`short_vol_metrics.py` 已能调用 OpenD `get_trading_days` 并识别 `WHOLE/MORNING/AFTERNOON/TRADING`，但它的日历结果与 QFQ 历史/RV 状态捆绑，且把当前交易日计入剩余时段。v2 runner 的单一 `business_date` 来自封存计划或 `expiration_business_today()`，后者按上海日期；美股行情日期可能不同。这些是 v3 修订所针对的历史缺口。

成功信号：每个适用 lot 仍恰有一行；有效经济条件未通过时为 `hold`；条件通过时，临期低 Delta 为 `hold`，已证实不满足例外才为 `close`，证据不足以排除例外时为 `not_evaluable` 且不提醒。周末、两地假期及半日市按各自市场日历处理；美股跨上海午夜从持仓筛选到报告、当前交易日已收市以及阈值等号有可运行回归。手动/非封存入口不会绕过同一策略与证据要求，旧版本报告不会被读成新建议。

### 复用和新增契约

检索范围是 `domain/domain/close_advice.py`、`src/application/close_advice_runner.py`、`src/application/close_advice_required_data.py`、`src/application/tick_account_execution.py`、`src/application/opend_symbol_fetching.py`、`src/application/short_vol_metrics.py`、`src/application/opening_quote_evidence.py`、`src/application/agent_tools/materialization_impl.py`、`src/infrastructure/futu_gateway.py` 和本文件；关键词为 `remaining_trading_sessions`、`term_matched_rv`、`get_trading_days`、`market_state`、`business_date`、`legacy_mutable`。未找到独立于 RV 的封存 Close Advice 日历字段；现有 `remaining_trading_sessions` 是领域入参/报告列，不是独立取数证据。

| 概念或实现 | 归属决定 |
|---|---|
| 三态决策、80%/10%/3 日/0.05 阈值、`delta`、`remaining_trading_sessions` 与费用公式 | 复用 `domain/domain/close_advice.py`；状态与数值不另起一套 |
| OpenD 交易日日历调用与日期/半日市解析 | 复用 `src/application/short_vol_metrics.py` 的网关调用和解析语义；提取可独立使用的最小日历计算，不依赖 RV/QFQ 成功 |
| 行情的 `market_state`、观测时间、市场时区与封存 receipt/hash | 复用 `src/application/opening_quote_evidence.py` 及现有 required-data 封存链；新增 `market_state_received_at_utc` 记录该状态的独立接收时间，因为早于期权报价的状态不能证明报价时仍可交易 |
| 市场日期及持仓筛选 | 复用 `src/application/close_advice_required_data.py` 与 `src/application/tick_account_execution.py` 的计划和 lot 视图入口，改为每个市场使用同一次 UTC 运行时间换算的日期 |
| Close Advice 日历观测字段 | 新增到现有 required-data 行：`trading_calendar_market`、`trading_calendar_as_of_market_date`、`trading_calendar_expiration`、`trading_calendar_request_start`、`trading_calendar_request_end`、`trading_calendar_status`（`ok`/`unavailable`）、`remaining_trading_sessions`、`trading_calendar_dates`（ISO 日期 JSON 列表）、`trading_calendar_input_hash`、`trading_calendar_receipt`，不可用时附 `trading_calendar_reason`；封存的列表和请求区间可复算计数，RV 字段不能证明独立日历已取得或与该 lot 对齐 |
| 非封存运行入口 | 这是设计时待收口边界；现行实现已删除 `legacy_mutable` 和 Agent 生成工具，只保留 sealed Tick 生产与 `close_advice_read` 读取 |
| 策略版本 `remaining_yield_capture.v3` | 新增版本标识；v2 对缺失日历的不同处理不能被消费者当成 v3 建议 |

实现时直接替换 `domain/domain/close_advice.py` 的 v2 判定，使它始终只执行一套 v3 策略；删除不再使用的旧阈值、条件和分支，不保留按版本选择 v1/v2/v3 的运行时策略入口。版本号仅标记报告契约：reader 和 Daily Brief 对旧版本 fail closed，不重新执行旧策略。回放对比读取已封存的旧报告或离线输入，不要求在产品代码中并行维护旧评分器。

### 目标判定与失败语义

取数计划按 active lot 的市场和到期日请求交易日，按市场批量调用已有 `get_trading_days_with_receipt`。封存请求的 `market/start/end`、提供方成功 receipt、完整规范化日期列表及其 hash；仅在请求区间确实从观测日至到期日、响应未截断、每行有合法 ISO 日期及 `WHOLE/MORNING/AFTERNOON` 类型且日期落在请求区间时，状态为 `ok`。不得沿用 `short_vol_metrics.py` 静默跳过非法行或把缺类型行计入日历的宽松解析。[富途交易日历接口](https://openapi.futunn.com/futu-api-doc/en/quote/request-trading-days.html)返回请求区间内的交易日，非返回日期按休市日处理；区间完整性以成功响应和无截断为依据，不能把网关 `coverage_complete=True` 单独当作语义保证。无法核验时为 `unavailable`。日历观测绑定 `market + as_of_market_date + expiration`，去重后按日期计数；半日市计 1 日，临时停市未必从提供方日历排除。

计划调用方以一次 `run_started_at_utc` 分别确定美东/香港市场日期，计划中封存 `as_of_market_dates: {US, HK}`；持仓视图 `position_lot_risk_view`、`as_open_position_min`、到期过滤、runner lifecycle 和领域 DTE 对同一 lot 使用该市场日期。封存行情的 `snapshot_received_at_utc` 转换出的市场日期须与计划日期相同，日历字段也须一致；跨日期或市场不匹配时不可评估，不从上海日期或另一市场补值。直接运行若没有封存计划，必须先建立同等封存证据；在此之前可产出 `not_evaluable` 行，但不得现场补取报价后用未封存日历产生 v3 `close`。

临期界限按**仍可交易的日期**计：未来交易日计入；当前市场日期若在日历中，仅凭早于期权报价取得的 `market_state` 不可断定今天仍可交易。能证明在期权快照接收时或之后观测且同一市场日期的状态为 `MORNING`/`AFTERNOON`，才计入今天；能证明其为 `CLOSED`、`AFTERNOON_END` 或 `AFTER_HOURS_*`，才不计入。其他情况包括状态无独立时间、复用早期观测或跨时段边界，均视为未知。未知时以包含/不包含今天分别计算；两种结果均 `<=3` 或均 `>3` 才能照常判定，跨过 3 日界限则日历不可用于排除例外。到期日和已过期 lot 仍由 lifecycle 输出 `not_evaluable`，不以 0 日进入本例外。

先执行现有持仓、报价、费用、OTM、净兑现与年化检查：无效必需证据为 `not_evaluable`；任一有效经济性条件未通过为 `hold`。只有原本会成为 `close` 的行才判断例外：有效 `|delta| >0.05` 或已证实剩余交易日 `>3` 可输出 `close`；有效 `|delta| <=0.05` 且已证实剩余交易日 `<=3` 输出 `hold`；其余无法排除例外的组合输出 `not_evaluable`。Delta 必须是同一封存行情的有限值且 `|delta| <=1`；不从历史报告、RV 或缺失值推断。报告保留日历状态、计数与不可评估原因；只读面和 Daily Brief 继续按版本及完整性 fail closed，不重算策略。

拒绝继续使用 RV 状态作为日历有效性的代理，因为独立 Close Advice 运行不请求 RV；拒绝日历或 Delta 缺失时沿用 80%/10% 输出 `close`，因为这可能覆盖临期持有例外；也不新增替代机会比较或不愿被指派的退出理由。

### 实现与验收

1. **独立封存日历**：在现有 required-data 计划/取数/封存链中产生上述按市场和到期日绑定的日历观测，计划调用方、lot 视图与 runner 统一市场日期；覆盖成功信号中的假期、半日市、跨午夜和封存完整性。验证日历成功而 RV/QFQ 失败、日历失败而 RV 成功、非法/缺类型行、区间截断、错市场/错日期、状态先于报价、当前日开盘/收盘/未知状态及 receipt/hash 失配。
2. **领域判定和消费者**（依赖第 1 片）：领域 owner 只消费已验证的日历计数和 Delta，runner 装配 v3 输入/报告，reader、Daily Brief 按 v3 和完整性筛选；手动/非封存入口接入同等封存证据，暂时无法取得时只产 `not_evaluable`。覆盖逐 lot 三态、无误提醒和旧报告隔离。验证 80%/10%/3 日/0.05 等号，Put/Call、缺 Delta/日历、生命周期、定时与手动入口、所有相关读取入口；最后用只读历史快照逐行回放并报告 v2/v3 建议数量及差异，缺少历史输入则明确标记未校准。

阈值仍未经真实历史结果校准，历史回放也无法单独证明真实交易结果最优；策略校准归 Close Advice owner 的后续研究，生产启用归另行授权的交付流程。富途日历的未来覆盖截至当年末，跨年到期若拿不到完整区间则日历不可用；临时停市也可能未从日历排除。这些提供方限制由 required-data owner 在报告中保留原因，不能用自然日或 RV 代填。此节保留设计依据；当前工作区的运行策略为 v3。
