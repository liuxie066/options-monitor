# 策略架构设计

本文只描述开仓策略架构。全局产品域、模块定义和依赖关系以 [PRODUCT_ARCHITECTURE.md](PRODUCT_ARCHITECTURE.md) 为准。

## 策略范围

开仓机会监控包含三类产品策略：

- Sell Put
- Covered Call
- Combo Yield（组合收益）

开仓机会监控负责推荐“现在是否值得开仓”。持仓管理负责回答“已有仓位是否应该继续持有、调整或关闭”，不在本文展开。两者共享行情、事件、持仓和成交证据，但策略判断不互相偷换。

## 命名与兼容

`insurance_underwriting` 是 Sell Put / Covered Call 唯一的新开仓策略语义，不是整个开仓域的统一策略。新开仓配置不再接受 `return_first` 或 `short_vol`，也不再接受会改变正式排序的 `score_weights`。

这次收敛只改变新开仓机会推荐。历史持仓、历史 artifact、Shadow Replay 和 Close Advice 仍可解释 `return_first` / `short_vol`，但这些兼容语义不能重新进入当前开仓配置或扫描分支。Combo Yield 开仓策略独立成型；Combo Yield 持仓侧退出语义后续单独处理。

| Strategy Family | Opening Profile | Close Profile | Status |
|---|---|---|---|
| Sell Put | `insurance_underwriting` | `sell_put_short_vol`；历史 `sell_put_return_first` 兼容 | active |
| Covered Call | `insurance_underwriting` | `covered_call_short_vol`；历史 `covered_call_return_first` 兼容 | active |
| Combo Yield | Combo Yield funding / participation | pending combo close design | opening strategy refactored |

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

Sell Put 的核心目标是：在愿意以某个价格接货的前提下，选择最值得承保的 put。

### 召回

- option type: put
- DTE 在配置窗口内
- `strike <= min(max_strike, spot)`；`max_strike` 为空时只受现价上限约束
- `strike >= min_strike`，其中 `min_strike` 可为空
- 基础流动性满足配置
- 现金覆盖仍由现有 cash-secured 逻辑负责

`max_strike` 是愿意接货的最高价，现价是自然上限。二者组成同一个硬门槛，不再在门槛通过后重复增加一层“价格安全边际”筛选。

### 硬筛

- 年化净收益率达到阈值
- 单笔净收益达到阈值
- `IV / RV` 达到阈值
- `IV - RV` 达到阈值
- 事件风险可接受
- spread、open interest、volume 等基础流动性通过

不再作为 Sell Put 开仓硬筛：

- stress / gap-down
- sigma stress loss
- symbol exposure after assignment
- max total assignment NAV
- single-trade / symbol / total concentration
- capital charge
- delta band

这些指标在当前产品目标下要么依赖大量主观假设，要么会把策略重新推回“交易波动率/路径压力”的旧模型。

### 排序

排序目标是推荐最优候选项，不是继续叠加一套软评分。

排序顺序：

1. 所有硬门槛通过。
2. 每个标的只保留 `annualized_net_return_on_cash_basis` 最高的合约。
3. 不同标的继续按 `annualized_net_return_on_cash_basis` 降序。
4. 年化净收益相同时，依次用净接货折价、`concentration_score`、spread、OI 和净收益额稳定破同分。

`net_assignment_discount_pct = (spot - breakeven) / spot`。它和集中度只负责破同分，不覆盖年化净收益主排序。`strategy_score`、`premium_edge_score` 等字段继续用于解释和研究，但不再改变正式推荐顺序。

## Covered Call

Covered Call 的核心目标是：在愿意以某个价格卖出正股的前提下，选择最值得承保的 call。

### 召回

- option type: call
- DTE 在配置窗口内
- `strike >= min_strike`
- `strike <= max_strike`，其中 `max_strike` 可为空
- 基础流动性满足配置
- 持股覆盖仍由现有 share coverage 逻辑负责

`min_strike` 是愿意卖出正股的最低价。通过这个边界后，`strike` 离 `min_strike` 的距离只在年化净权利金收益相同时用于破同分。

### 硬筛

- 年化净权利金收益率达到阈值
- 单笔净收益达到阈值
- `IV / RV` 达到阈值
- `IV - RV` 达到阈值
- 事件风险可接受
- spread、open interest、volume 等基础流动性通过

不再作为 Covered Call 开仓硬筛：

- gap-up opportunity cost
- concentration
- delta band
- path stress

Covered Call 的上行放弃是这个策略的自然代价，应通过 `min_strike` / `max_strike` 和排序表达，而不是在开仓推荐里引入额外路径压力模型。

### 排序

排序顺序：

1. 所有硬门槛通过。
2. 每个标的只保留 `annualized_net_premium_return` 最高的合约。
3. 不同标的继续按 `annualized_net_premium_return` 降序。
4. 年化净权利金收益相同时，依次用 `strike_upside_margin_pct`、`concentration_score`、spread、OI 和净收益额稳定破同分。

`strike_upside_margin_pct = (strike - min_strike) / min_strike`

## Combo Yield

Combo Yield 是与 Sell Put、Covered Call 平行的开仓策略，不是 Sell Put 或 Covered Call 的 overlay。当前 runtime key 是 `combo_yield`；历史 `yield_enhancement` 只作为旧配置、旧 artifact 和既有持仓的兼容读取口径。

运行所有权同样独立：per-symbol pipeline 分别调用 Sell Put step 与 Combo Yield step。`sell_put.enabled=false`、Sell Put 扫描失败或 Sell Put 无候选都不会隐式禁用 Combo Yield；Combo Yield 只由自身 `enabled` 和共享 required-data 是否可用决定。它可以复用 Sell Put 配置作为 funding-put 的期限、价格边界和 underwriting 输入，但不复用 Sell Put step 的候选结果或成功状态。共享 required-data 获取仍是 symbol 级前置边界。

当前结构由 `structure_mode` 决定：

| 结构 | 期限关系 | 当前定位 |
|---|---|---|
| `same_expiry_pair` | Put 与 Call 同到期 | 默认值；保留既有组合指标、硬筛和排序 |
| `staggered_expiry_pair` | Put 早到期，Call 晚到期 | 新增错期全额融资结构 |

### `same_expiry_pair`

该模式继续配对同 symbol、同到期、同 currency、同 multiplier、`put strike < call strike` 的 Short Put + Long Call。现有 `min_net_credit_annualized`、`min_net_credit_retention`、`max_combo_spread_ratio`、scenario/breakeven 输出和原排序保持兼容；本轮不改变其产品定义。

### `staggered_expiry_pair` 的核心关系

V1 使用严格的一对一关系：

```text
1 Short Put : 1 Long Call
put.expiration < call.expiration
put.strike < call.strike
put.multiplier == call.multiplier
put.currency == call.currency
```

Funding Put 是资金腿，Long Call 是参与腿。不是“多张 Put 共同融资一张 Call”，也不在候选或成交阶段做启发式拆分。这样可以让资金覆盖、风险归因、通知和持仓生命周期都保持可解释。

### 召回与硬筛

Funding Put：

- 使用 Sell Put 自己的 DTE 窗口和接货 strike 边界。
- 在组合构造前先复用完整 Sell Put underwriting 候选；现金、事件、收益、IV/RV、流动性及愿意接货边界均不得因 Long Call 而放宽。
- Combo Yield 自己启用时即可独立构造 Funding Put，不依赖 Sell Put step 是否启用或成功。

Participation Call：

- 从 required-data Call universe 独立召回，不要求启用 Covered Call 扫描。
- 同到期结构直接复用 Funding Put 到期日。
- 错期结构使用 `combo_yield.min_expiry_gap_days/max_expiry_gap_days`；对每个 Put 精确校验 `Call DTE - Put DTE`，不再配置一套绝对 Call DTE。
- 可配置 `call.min_strike/max_strike` 与 `call.min_delta/max_delta`。
- 错期模式不强制 `call strike >= spot`，但最终结构仍必须满足 `put strike < call strike`。
- Call bid/ask、delta、OI、volume、spread 和 multiplier 缺失或不合格时 fail closed。

配对硬约束：

- 同 symbol、currency、multiplier。
- Put 到期早于 Call；Put strike 低于 Call strike。
- `put_net_credit > 0`，`call_total_cost > 0`。
- 默认 `funding_mode=credit_or_even`：`combo_net_credit >= 0`。
- 默认 `min_net_credit_retention=0.60`：至少保留 Funding Put 60% 的净权利金，即 Participation Call 最多使用 40%。
- 两腿基础流动性通过，费用按当前费用模型估算。

费用与资金定义：

```text
put_net_credit = Put bid * multiplier - estimated_sell_fees
call_total_cost = Call ask * multiplier + estimated_buy_fees
combo_net_credit = put_net_credit - call_total_cost
net_credit_retention = combo_net_credit / put_net_credit
call_cost_to_put_credit = call_total_cost / put_net_credit
funding_ratio = put_net_credit / call_total_cost
```

错期组合不计算或硬筛组合年化、同到期 breakeven、expected-move scenario、1.5σ/2.0σ payoff multiple。Put 与 Call 的风险期限不同，把它们压成单一到期日指标会制造错误精度。

### 排序与通知入选

同一 Funding Put 下先选一个 Participation Call：

1. `funding_accepted` 优先
2. `abs(call_delta)` 降序，直接最大化上行参与度
3. `net_credit_retention` 降序
4. max(`put_spread_ratio`, `call_spread_ratio`) 升序
5. `call_open_interest` 降序
6. 较短 Call DTE 优先
7. Call 合约标识稳定排序

每个标的先按 Sell Put 规则选择一张 Funding Put，再为它选择 Participation Call。不同标的进入通知前按以下顺序排列：

1. `funding_accepted` 优先
2. Funding Put `put_only_annualized_net_return` 降序
3. Put 净接货折价降序
4. `call_delta` 降序
5. `net_credit_retention` 降序
6. 两腿最大 spread 升序
7. min(`put_open_interest`, `call_open_interest`) 降序
8. symbol、Put 合约、Call 合约稳定排序

因此，通知里只出现：Funding Put 已通过 Sell Put underwriting、Call 通过独立期限/价格/delta/流动性过滤、两腿结构合法、并满足 60% 留存门槛的组合。每个标的只保留一个组合；被拒绝的 Call 和配对尝试只进入 `<symbol>_combo_yield_pair_diagnostics.csv`，不会进入通知。

### 候选身份、成交意图与回执

- `candidate_pair_id`：扫描推荐身份，用于候选、artifact 和通知追踪，不等于真实成交关系。
- `pair_intent_id`：操作员或成交入口显式提供的真实组合意图。
- `strategy_group_id = combo_yield:<account>:<pair_intent_id>`。
- 有 `pair_intent_id` 的错期成交按 `funding_put` / `participation_call` 自动归组。
- 没有 `pair_intent_id` 时照常记录单腿，但不猜测组合关系；回执提示“组合关系待确认”。

已分别入账的两腿可通过精确 lot id 确认：

```bash
./om option-positions pair-combo-yield \
  --put-record-id <put_lot_id> \
  --call-record-id <call_lot_id> \
  --pair-intent-id <intent_id> \
  --dry-run
```

该入口校验同账户、同 canonical symbol、Short Put/Long Call 方向、开放合约数、1:1、multiplier、到期顺序和 strike 顺序。确认写入时，两条 immutable adjustment event 在同一 SQLite 事务中提交；不按合约条件搜索或猜测 lot。

### 配置示例边界

错期配置只接受相对间隔，例如 `min_expiry_gap_days=30`、`max_expiry_gap_days=90`；该数值是示例，不是通用最优值。`combo_yield.call.min_dte/max_dte` 已拒绝，避免 Put 窗口变化后出现两套期限配置漂移。默认仍是 `same_expiry_pair`。

### required-data 全局计划

每次 run 在抓取期权链前先完成一次全局决策：

1. 解析所有模板、标的覆盖项和三类策略要求。
2. 每个唯一标的只发现一次 spot 与到期日目录。
3. 按策略 DTE、错期间隔、strike 和 option side 生成精确到期日并集。
4. 用真实到期日数量估算 API 调用预算并拆 wave。
5. 所有标的计划完成后才检查缓存和执行抓取。

`fetch.limit_expirations` 不再裁剪正式策略 universe；底层抓取仍可保留该参数供非策略 CLI 使用。到期日发现失败时，全局计划 fail closed，不能用部分数据声称得到了“全局最优”。DTE 窗口仍有业务意义，窗口数量上限没有。

## 当前实现边界

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
- `enrich_and_filter_*_short_vol` 只保留为旧调用方兼容别名，内部转发到 underwriting 入口；配置层不再接受 `strategy=short_vol`。
- Close Advice 继续使用 `short_vol` thesis 字段，不在本轮重命名；当前 Sell Put / Covered Call 配置参数从顶层 `insurance_underwriting` 字段读取。
- 开仓 underwriting 不再请求全局 path-risk / concentration context；只有明确声明 `scan_uses_path_risk` 的策略才应加载该上下文。

不在本轮实现：

- 修改生产 `config.yaml` / `config.us.json` / `config.hk.json`
- 重命名 close advice 的 short-vol thesis 字段
- 重构 shadow replay 的历史策略画像
- 重命名 Combo Yield 的 legacy `yield_enhancement` 文件名和持仓标记

### 跨期收益与资金占用归因

错期 Combo Yield 同时维护三种不同语义：真实现金流、canonical economic PnL 和 management
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
