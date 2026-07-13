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

Combo Yield 是与 Sell Put、Covered Call 平行的开仓策略，不是 Sell Put 或 Covered Call 的 overlay。

当前 runtime key 是 `combo_yield`，历史 `yield_enhancement` 只作为旧配置、旧 artifact 和既有持仓的兼容读取口径。产品语义已经按 Combo Yield 独立处理，技术上不继承 Sell Put / Covered Call 的 `insurance_underwriting` RV、event 或 underwriting gate。

核心目标：用一张可接受接货义务的 short put，融资同 symbol、同到期的 long call，形成“保留净权利金，同时获得有限成本上行参与”的组合。

### 召回

- put leg: option type 为 put，DTE 使用 Sell Put 窗口。
- put strike 使用 Sell Put 接货边界：`min_strike` 可空；上界是 `min(spot, max_strike)`，如果 `max_strike` 为空则上界是 spot。
- call leg: option type 为 call，同 symbol、同到期、同 multiplier。
- call strike 使用结构边界：`strike >= max(spot, call.min_strike)`，`call.max_strike` 可空。
- call delta 可用作上行参与区间，默认保留低 delta 参与，不再用 OTM 百分比做召回控制。

### 硬筛

- put strike < call strike。
- `funding_mode` 通过：默认要求扣除 long call 成本和手续费后 `combo_net_credit >= 0`。
- `min_combo_net_credit` 通过。
- `min_net_credit_annualized` 通过。
- `max_call_cost_to_put_credit` 通过。
- `min_net_credit_retention` 通过。
- `max_combo_spread_ratio` 通过。
- put / call 基础流动性通过。

不再作为 Combo Yield 开仓硬筛：

- IV / RV
- expected move / scenario score
- put OTM 百分比
- call OTM 百分比
- upside lift to call cost / put credit
- Sell Put / Covered Call 的 underwriting event gate

原因是 Combo Yield 的第一性目标不是“交易 IV 溢价”，而是判断一组结构是否值得开：接货义务是否在愿意范围内，short put 是否足够融资 long call，组合是否仍保留净权利金，以及 call 是否提供足够上行参与。

### 排序

排序目标是推荐最优组合，不是解释所有可能性。

排序顺序：

1. `funding_accepted` 优先
2. `premium_funding_score` 降序
3. `net_credit_retention` 降序
4. `call_cost_to_put_credit` 升序
5. `call_participation` 降序
6. `put_assignment_margin_pct` 降序
7. `annualized_net_credit_yield` 降序
8. `combo_spread_ratio` 升序
9. `combo_net_credit` 降序
10. `upside_breakeven_pct_above_spot` 升序
11. `net_credit` 降序
12. min(`put_open_interest`, `call_open_interest`) 降序

这里的排序刻意不把 IV/RV 放在核心位置。Combo Yield 的关键不是哪张保单 IV 最贵，而是哪组组合最像“用可接受接货价格，低成本买到上行参与，同时不牺牲太多权利金和执行质量”。

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
- Combo Yield 开仓编排：`src/application/combo_yield_steps.py::run_combo_yield_scan_and_summarize`
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
