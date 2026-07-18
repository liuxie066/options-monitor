# 策略架构设计

本文只描述开仓策略架构。全局产品域、模块定义和依赖关系以 [PRODUCT_ARCHITECTURE.md](PRODUCT_ARCHITECTURE.md) 为准。

## 策略范围

开仓机会监控包含三类产品策略：

- Sell Put
- Covered Call
- Combo Yield（组合收益）

开仓机会监控负责推荐“现在是否值得开仓”。持仓管理负责回答“已有仓位是否应该继续持有、调整或关闭”，不在本文展开。两者共享行情、事件、持仓和成交证据，但策略判断不互相偷换。

## 命名与兼容

`insurance_underwriting` 是 Sell Put / Covered Call 的开仓策略语义，不是整个开仓域的统一策略。Sell Put / Covered Call 开仓配置必须显式写 `insurance_underwriting` 或 `return_first`，不再接受 `short_vol` 作为开仓配置值。

这次重构只改变开仓机会推荐。Combo Yield 开仓策略已独立成型；Close Advice、历史持仓里的 short-vol thesis、以及 Combo Yield 持仓侧退出语义后续单独处理。

| Strategy Family | Opening Profile | Close Profile | Status |
|---|---|---|---|
| Sell Put | `insurance_underwriting` / `return_first` | `sell_put_short_vol` / `sell_put_return_first` | active |
| Covered Call | `insurance_underwriting` / `return_first` | `covered_call_short_vol` / `covered_call_return_first` | active |
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
- `strike <= max_strike`
- `strike >= min_strike`，其中 `min_strike` 可为空
- 基础流动性满足配置
- 现金覆盖仍由现有 cash-secured 逻辑负责

`max_strike` 是愿意接货的最高价。通过这个边界后，`strike` 离 `max_strike` 越远越好。

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

排序目标是推荐最优候选项，不是展示所有可能解释。

排序顺序：

1. `strike_safety_margin_pct` 降序
2. `premium_edge_score` 降序
3. `spread_ratio` 升序
4. `open_interest` 降序
5. `net_income_cny` / `net_income` 降序，仅作最终同分项

Sell Put 的 `premium_edge_score` 保留现有字段名以兼容已有 artifact，但改为去重后的承保补偿分：

- `return_edge = 年化收益率 / 最低年化收益率`
- `iv_rv_edge = IV/RV / 最低 IV/RV`
- `iv_minus_rv_edge = (IV-RV) / 最低 IV-RV`
- `vol_edge = min(iv_rv_edge, iv_minus_rv_edge)`
- `premium_edge_score = mean(return_edge, vol_edge)`

每项仍以 `premium_score_cap` 封顶。净权利金保留为硬门槛和最终同分项，不再与已包含它的年化收益重复进入主评分。IV/RV 与 IV-RV 取较弱证据，避免对同一波动率优势重复加权。

`strike_safety_margin_pct = (max_strike - strike) / max_strike`

## Covered Call

Covered Call 的核心目标是：在愿意以某个价格卖出正股的前提下，选择最值得承保的 call。

### 召回

- option type: call
- DTE 在配置窗口内
- `strike >= min_strike`
- `strike <= max_strike`，其中 `max_strike` 可为空
- 基础流动性满足配置
- 持股覆盖仍由现有 share coverage 逻辑负责

`min_strike` 是愿意卖出正股的最低价。通过这个边界后，`strike` 离 `min_strike` 越远越好。

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

1. `strike_upside_margin_pct` 降序
2. `premium_edge_score` 降序
3. `spread_ratio` 升序
4. `open_interest` 降序
5. `net_income_cny` / `net_income` 降序，仅作最终同分项

Covered Call 的 `premium_edge_score` 使用与 Sell Put 相同的去重补偿分：年化收益与 `min(IV/RV 优势, IV-RV 优势)` 取平均，每项仍以 `premium_score_cap` 封顶。净权利金保留为硬门槛和最终同分项，不再重复进入主评分。

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

- 依赖 `sell_put.enabled=true`。
- 使用 Sell Put 自己的 DTE 窗口和接货 strike 边界。
- 在组合构造前先复用完整 Sell Put underwriting 候选；现金、事件、收益、IV/RV、流动性及愿意接货边界均不得因 Long Call 而放宽。

Participation Call：

- 从 required-data Call universe 独立召回，不要求启用 Covered Call 扫描。
- 使用 `combo_yield.call.min_dte/max_dte`；Call 到期日必须晚于 Put。
- 可配置 `call.min_strike/max_strike` 与 `call.min_delta/max_delta`。
- 错期模式不强制 `call strike >= spot`，但最终结构仍必须满足 `put strike < call strike`。
- Call bid/ask、delta、OI、volume、spread 和 multiplier 缺失或不合格时 fail closed。

配对硬约束：

- 同 symbol、currency、multiplier。
- Put 到期早于 Call；Put strike 低于 Call strike。
- `put_net_credit > 0`，`call_total_cost > 0`。
- 默认 `funding_mode=credit_or_even`：`combo_net_credit >= 0`。
- 默认 `max_call_cost_to_put_credit=1`：Call 总成本不超过 Put 净收入；对应 `funding_ratio >= 1`。
- 两腿基础流动性通过，费用按当前费用模型估算。

费用与资金定义：

```text
put_net_credit = Put bid * multiplier - estimated_sell_fees
call_total_cost = Call ask * multiplier + estimated_buy_fees
combo_net_credit = put_net_credit - call_total_cost
call_cost_to_put_credit = call_total_cost / put_net_credit
funding_ratio = put_net_credit / call_total_cost
```

错期组合不计算或硬筛组合年化、同到期 breakeven、expected-move scenario、1.5σ/2.0σ payoff multiple。Put 与 Call 的风险期限不同，把它们压成单一到期日指标会制造错误精度。

### 排序与通知入选

同一 Funding Put 下先选一个 Participation Call：

1. `funding_accepted` 优先
2. `call_delta` 降序
3. `call_cost_to_put_credit` 降序，即在仍满足全额覆盖前提下优先使用更多可用 Put 权利金
4. `call_dte` 降序
5. max(`put_spread_ratio`, `call_spread_ratio`) 升序
6. `call_open_interest` 降序
7. Call 合约标识稳定排序

不同组合进入通知前按以下顺序排列：

1. `funding_accepted` 优先
2. Put `strike_safety_margin_pct` 降序
3. Funding Put `premium_edge_score` 降序
4. `call_delta` 降序
5. `call_cost_to_put_credit` 降序
6. `call_dte` 降序
7. 两腿最大 spread 升序
8. min(`put_open_interest`, `call_open_interest`) 降序
9. symbol、Put 合约、Call 合约稳定排序

因此，通知里只出现：Funding Put 已通过 Sell Put underwriting、Call 通过独立期限/价格/delta/流动性过滤、两腿结构合法、并满足资金覆盖硬门槛的组合。每个 Funding Put 只保留排序最优的一张 Call；被拒绝的 Call 和配对尝试只进入 `<symbol>_combo_yield_pair_diagnostics.csv`，不会进入通知。

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

`combo_yield.call.min_dte=60`、`max_dte=120` 仅是展示独立 Call horizon 的示例，不是生产推荐值。生产参数必须通过 Shadow Replay、pair diagnostics 和 outcome 证据校准。默认仍是 `same_expiry_pair`，本轮没有自动修改生产配置。

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
