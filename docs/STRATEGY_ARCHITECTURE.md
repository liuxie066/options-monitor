# 策略架构设计

本文只描述开仓策略架构。全局产品域、模块定义和依赖关系以 [PRODUCT_ARCHITECTURE.md](PRODUCT_ARCHITECTURE.md) 为准。Sell Put / Covered Call 的召回、数据、筛选、容量和排序细则只以 [candidate_strategy.md](candidate_strategy.md) 为准；本文不复制第二份策略参数。

## 策略范围

开仓机会监控包含三类产品策略：

- Sell Put
- Covered Call
- Combo Yield（组合收益）

开仓机会监控负责推荐“现在是否值得开仓”。持仓管理负责回答“已有仓位是否应该继续持有、调整或关闭”，不在本文展开。两者共享行情、事件、持仓和成交证据，但策略判断不互相偷换。

## 命名与兼容

`insurance_underwriting` 是 Sell Put / Covered Call 唯一的新开仓策略语义，不是整个开仓域的统一策略。新开仓配置不再接受 `return_first` 或 `short_vol`，也不再接受会改变正式排序的 `score_weights`。

历史 artifact 和 Shadow Replay 可为离线开仓研究解释 `return_first` / `short_vol`，但这些兼容语义不能重新进入当前开仓配置或扫描分支。Close Advice 不读取这些 thesis，只使用固定 `strict_profit_capture.v1`。Combo Yield 仍只有独立开仓策略，不定义组合级退出动作。

| Strategy Family | Opening Profile | Close Profile | Status |
|---|---|---|---|
| Sell Put | `insurance_underwriting` | `strict_profit_capture.v1` | active |
| Covered Call | `insurance_underwriting` | `strict_profit_capture.v1` | active |
| Combo Yield | Combo Yield funding / participation | 无组合级退出；short 腿仅按严格策略独立评估 | opening strategy active |

## 通用流水线

每个开仓策略按同一组阶段执行：

1. 召回
   - 选择策略方向、DTE 窗口、strike 边界和基础可交易合约。
2. 硬筛
   - 去掉不满足策略底线的候选项。
3. 排序
   - 在通过硬筛的候选项里，推荐最适合开仓的一项或前 N 项。
4. 输出解释
   - 对拒绝项记录规则、指标值、阈值和回放字段。

硬筛是“能不能做”，排序是“最推荐哪个”。不能把排序分数当硬风险预算，也不能把解释字段升级成新约束。

## Sell Put

Sell Put 的核心目标是：在愿意以合适价格接货、但不主动追求接货的前提下，按 `mid` 挂限价等待，通过整个持有周期的净权利金收益参与投资布局。候选是等待机会，不是即时成交承诺。

架构上，应用层负责冻结 OpenD、账户和 ledger 事实，Candidate Engine 负责唯一的正式过滤与排序，输出账户/run 级不可变候选快照。现金担保、期限匹配 RV、财报日历和失败范围的规范见 [Sell Put / Covered Call 候选策略合同](candidate_strategy.md#5-sell-put)。

## Covered Call

Covered Call 的核心目标是：在愿意以合适价格卖出正股、但不主动追求被叫走的前提下，按 `mid` 挂限价等待，用整个周期的净权利金增强持股收益。

架构上，OpenD 物理账户持仓与 SQLite short-call 锁定共同构成覆盖事实；Candidate Engine 负责唯一的正式过滤与排序。成本底线、召回区间、当前市值收益口径、混合持股归属和收益接近时优先更高 strike 的规范见 [Sell Put / Covered Call 候选策略合同](candidate_strategy.md#6-covered-call)。

## Combo Yield

Combo Yield 是与 Sell Put、Covered Call 平行的开仓策略，不是 Sell Put 或 Covered Call 的 overlay。当前 runtime key 是 `combo_yield`；历史 `yield_enhancement` 只作为旧配置、旧 artifact 和既有持仓的兼容读取口径。

运行所有权同样独立：per-symbol pipeline 分别调用 Sell Put step 与 Combo Yield step。`sell_put.enabled=false`、Sell Put 扫描失败或 Sell Put 无候选都不会隐式禁用 Combo Yield；Combo Yield 只由自身 `enabled` 和共享 required-data 是否可用决定。它可以复用 Sell Put 配置作为 funding-put 的期限、价格边界和 underwriting 输入，但不复用 Sell Put step 的候选结果或成功状态。共享 required-data 获取仍是 symbol 级前置边界。

当前结构由 `structure_mode` 决定：

| 结构 | 期限关系 | 当前定位 |
|---|---|---|
| `same_expiry_pair` | Put 与 Call 同到期 | 默认值；保留既有组合指标、硬筛和排序 |

### `same_expiry_pair`

该模式继续配对同 symbol、同到期、同 currency、同 multiplier、`put strike < call strike` 的 Short Put + Long Call。现有 `min_net_credit_annualized`、`min_net_credit_retention`、`max_combo_spread_ratio`、scenario/breakeven 输出和原排序保持兼容；本轮不改变其产品定义。

### 错期结构（已移除）

V1 的 `staggered_expiry_pair`（Put 早到期、Call 晚到期）已从候选、研究/shadow 与通知面删除，
新扫描仅支持 `same_expiry_pair`。ledger 生命周期、手动配对入口（`pair-combo-yield`）、
归组推理与绩效归因中的错期/`diagonal` 支持也已一并移除；组合收益仅支持同期（same-expiry）结构，
错期历史数据落入 `review_required`（fail-closed），不再自动配对或按同期归因。

### 召回与硬筛

Funding Put：

- 使用 Sell Put 自己的 DTE 窗口和接货 strike 边界。
- 在组合构造前先复用完整 Sell Put underwriting 候选；现金、事件、收益、IV/RV、流动性及愿意接货边界均不得因 Long Call 而放宽。
- 财报事件与 Sell Put/Covered Call 共用同一 `expiry-6天..expiry` 自然日硬窗口；第 7 天及更早仅作软语境，不单设 Combo 窗口。
- Combo Yield 自己启用时即可独立构造 Funding Put，不依赖 Sell Put step 是否启用或成功。

Participation Call：

- 从 required-data Call universe 独立召回，不要求启用 Covered Call 扫描。
- 直接复用 Funding Put 到期日（`same_expiry_pair`）。
- 可配置 `call.min_strike/max_strike` 与 `call.min_delta/max_delta`。
- Call bid/ask、delta、OI、volume、spread 和 multiplier 缺失或不合格时 fail closed。

配对硬约束：

- 同 symbol、currency、multiplier。
- Put 与 Call 同到期；Put strike 低于 Call strike。
- `put_net_credit > 0`，`call_total_cost > 0`。
- `min_net_credit_retention=0.60` 是唯一成本约束：至少保留 Funding Put 60% 的净权利金，即 Participation Call 最多使用 40%。
- 已废弃 `funding_mode` 与 `max_call_cost_to_put_credit`（后者是 retention 的补数，统一由 retention 表达）。
- 两腿基础流动性通过，费用按当前费用模型估算。

费用与资金定义：

```text
put_net_credit = Put bid * multiplier - estimated_sell_fees
call_total_cost = Call ask * multiplier + estimated_buy_fees
combo_net_credit = put_net_credit - call_total_cost
net_credit_retention = combo_net_credit / put_net_credit
call_cost_to_put_credit = call_total_cost / put_net_credit
funding_ratio = put_net_credit / call_total_cost
cash_required = put_strike * multiplier - combo_net_credit
period_net_return = combo_net_credit / cash_required
```

### 排序与通知入选

同一 Funding Put 下先选一个 Participation Call：

1. `funding_accepted` 优先
2. `net_credit_retention` 降序（保留确定收益优先）
3. `abs(call_delta)` 降序，上行参与度
4. max(`put_spread_ratio`, `call_spread_ratio`) 升序
5. min(`put_open_interest`, `call_open_interest`) 降序
6. 接货安全边际（`put_assignment_margin_pct` / `put_otm_pct`）降序
7. Call 合约标识稳定排序

每个标的先按 Sell Put 规则选择一张 Funding Put，再为它选择 Participation Call。不同标的进入通知前按以下顺序排列：

1. `funding_accepted` 优先
2. Funding Put `put_only_period_net_return`（期间非年化净收益）降序
3. Put 净接货折价降序
4. `net_credit_retention` 降序
5. `call_delta` 降序
6. 两腿最大 spread 升序
7. min(`put_open_interest`, `call_open_interest`) 降序
8. symbol、Put 合约、Call 合约稳定排序

因此，通知里只出现：Funding Put 已通过 Sell Put underwriting、Call 通过独立期限/价格/delta/流动性过滤、两腿结构合法、并满足 60% 留存门槛的组合。每个标的只保留一个组合；被拒绝的 Call 和配对尝试进入 sealed Combo snapshot 的 `pair_evaluations`，不会进入通知。

Combo Yield Funding Put 的扫描、标注、资金和 underwriting 在同一内存 DataFrame 上连续计算。Combo Yield 候选、Funding Put 决策、pair diagnostics 和 rank evidence 写入独立的 run/account 级 sealed snapshot（`combo_yield_candidate_snapshot.json`），其完整性由 `candidate_snapshot_manifest.v1.json` 提交；Agent、Daily Brief、Research 与 Shadow Replay 均只消费该 bundle，不从兼容 CSV 恢复候选事实。

### 候选身份、成交意图与回执

- `candidate_pair_id`：扫描推荐身份，用于候选、artifact 和通知追踪，不等于真实成交关系。
- `pair_intent_id`：操作员或成交入口显式提供的真实组合意图。
- `strategy_group_id = combo_yield:<account>:<pair_intent_id>`。
- 有 `pair_intent_id` 的成交按 `funding_put` / `participation_call` 自动归组。
- 没有 `pair_intent_id` 时照常记录单腿，但不猜测组合关系；回执提示“组合关系待确认”。

同期组合由生命周期/归组自动处理，无需手动配对入口；错期专用入口 `pair-combo-yield` 已删除。

对于已经带有完整 `combo_yield`、`strategy_group_id` 和两腿角色，
但尚未建立 immutable identity 的历史持仓（包括同到期组合），必须显式给出
两条 open event 和 lot 的精确身份：

```bash
./om option-positions adopt-combo-identity \
  --strategy-group-id <group_id> \
  --funding-put-record-id <put_lot_id> \
  --funding-put-open-event-id <put_open_event_id> \
  --participation-call-record-id <call_lot_id> \
  --participation-call-open-event-id <call_open_event_id> \
  --expected-contracts <contracts> \
  --dry-run
```

该入口先完整重放 `trade_events`，并要求结果与当前 `position_lots`
逐字段一致；随后核对同 broker、同账户、同 canonical symbol、币种、
multiplier、数量、方向、角色、group id、Put/Call strike 顺序、到期顺序，
以及 lot 与 open event 的精确绑定。
apply 只在单一 SQLite 事务中新增 `strategy_group_identities`，不会搜索相似
合约、补写 adjustment event、改写 open event 或替换 position lot。相同
identity 重复执行为 no-op，任何既有 identity 冲突均失败关闭。

### 配置示例边界

Combo Yield 仅支持 `same_expiry_pair`。`min_expiry_gap_days` / `max_expiry_gap_days` 已移除，
配置校验会显式报错（fail closed），而不是静默忽略或改变语义。`combo_yield.call.min_dte/max_dte`
同样已拒绝，Call 期限从 Sell Put 窗口派生，避免 Put 窗口变化后出现两套期限配置漂移。

### required-data 全局计划

每次 run 在抓取期权链前先完成一次全局决策：

1. 解析所有模板、标的覆盖项和三类策略要求。
2. 每个唯一标的只发现一次 spot 与到期日目录。
3. 按策略 DTE、strike 和 option side 生成精确到期日并集。
4. 用真实到期日数量估算 API 调用预算并拆 wave。
5. 所有标的计划完成后才检查缓存和执行抓取。

`fetch.limit_expirations` 不再裁剪正式策略 universe；底层抓取仍可保留该参数供非策略 CLI 使用。到期日发现失败时，全局计划 fail closed，不能用部分数据声称得到了“全局最优”。DTE 窗口仍有业务意义，窗口数量上限没有。

## 现有实现边界（待按候选策略合同收敛）

本轮实现范围：

- 新增 `insurance_underwriting` 开仓策略核心计算
- Sell Put post-filter 从 short-vol 风险评估切到承保定价评估
- Covered Call post-filter 从 short-vol 风险评估切到承保定价评估
- Sell Put / Covered Call 开仓配置使用 `insurance_underwriting`，不再接受 `short_vol`
- Combo Yield 详细开仓策略从 legacy scenario / OTM 控制切到价格边界、融资经济性、call 参与质量和执行质量

实现映射：

- 承保定价核心：`domain/domain/insurance_underwriting.py`
- Sell Put 开仓入口：`src/application/sell_put_strategy_risk.py::enrich_and_filter_sell_put_underwriting`
- Covered Call 开仓入口：`src/application/covered_call_strategy_risk.py::enrich_and_filter_covered_call_underwriting`
- Combo Yield symbol-level 入口：`src/application/combo_yield_steps.py::run_combo_yield_for_symbol_and_summarize`
- Combo Yield 低层开仓编排：`src/application/combo_yield_steps.py::run_combo_yield_scan_and_summarize`
- 策略语义解析：`src/application/strategy_policy.py`
- 扫描上下文装配：`src/application/pipeline_context.py`

兼容边界：

- 开仓扫描证据使用 `scan_strategy_profile`，Sell Put / Covered Call 的承保扫描记录为 `insurance_underwriting`。
- 历史 `enrich_and_filter_*_short_vol` 开仓别名及对应配置包装已移除，当前开仓只有 underwriting 入口。
- 历史 `short_vol` 解析只服务离线开仓研究；Close Advice 不消费该配置、字段或结论。
- 开仓 underwriting 不再请求全局 path-risk / concentration context；只有明确声明 `scan_uses_path_risk` 的策略才应加载该上下文。

不在本轮实现：

- 修改生产 `config.yaml` / `config.us.json` / `config.hk.json`
- 重构 shadow replay 的历史策略画像
- 重命名 Combo Yield 的 legacy `yield_enhancement` 文件名和持仓标记

### 跨期收益与资金占用归因

Combo Yield 结算维护三种不同语义：真实现金流、canonical economic PnL 和 management
attribution。三者不能互相替代。

- Funding Put 收到的 premium 可以为 Participation Call 提供 funding，但不会把 Call premium
  再次记为 Put 周期损失。
- Participation Call 的 premium 是 Call 自身生命周期成本基础。Call 的 realized PnL 在 canonical
  close allocation 时确认；跨报表期间的价值变化来自 opening/ending marks。
- `strategy_group_id` 表示完整组合；Funding Put、Participation Call、assigned stock 和 residual Call
  分别使用 canonical lot identity 派生 lifecycle ID。
- 本版本支持一张 Funding Put、一张 Participation Call 和 Put 结束后的 residual-call tail；不实现自动
  roll 或一张长期 Call 对多个连续 Put cycles。

资金效率使用风险资本日，而不是净开仓现金流：

```text
Funding Put capital = strike * multiplier * remaining contracts
Participation Call capital = remaining opening premium debit
Assigned stock capital = remaining stock cost basis
capital_days = sum(capital * exact overlap days)
```

`Call debit - Put credit` 可作为资金来源说明，但不得替代 cash-secured Put 的 strike notional；否则
收到 premium 会人为压低风险资本分母。只有 PnL owner、capital owner 和时间窗口完全一致时才输出
annualized efficiency。

如果报表窗口跨过 Put close，而该时点没有精确 Call valuation mark，系统只报告完整 Call lifecycle 和
strategy-group PnL，不把 Call 价格变化启发式切分给 Funding cycle 或 residual tail。
