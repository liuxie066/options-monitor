# Candidate Strategy

这份文档只回答一件事：

> 系统现在是怎样筛选、排序和输出 Sell Put / Covered Call 候选的。

它描述的是**当前生产行为**。

---

## 1. 适用范围

当前候选策略覆盖两类输出：

- **Sell Put**
- **Covered Call**

术语说明：`config.yaml` authoring 使用 `covered_call`；生成后的 runtime JSON、CSV、trace 和代码模块里的内部 key 仍是 `sell_call`。这份文档用用户可见名称 **Covered Call** 描述同一能力。

两类候选共享大体流程，但关注点不同：

- **Sell Put**：IV/RV 波动率边际、现金担保能力、年化净收益率、单笔净收益、strike 安全距离、流动性
- **Covered Call**：可覆盖股数、IV/RV 波动率边际、strike 上行距离、年化权利金收益、单笔净收益、流动性

---

## 2. 当前实现分层

当前实现不是“所有规则都在一个 Engine 里一次完成”，而是分成三层：

### A. 数据准备层
负责：
- required data 获取
- 持仓 / 现金 context 获取
- 汇率与乘数补全

主要来源：
- OpenD / Futu API
- SQLite `position_lots`
- 可选 Feishu holdings 数据源

### B. 核心候选引擎层
负责：
- 输入归一化
- 硬约束判断
- 收益门槛判断
- 流动性 / 风险门槛判断
- 基础排序语义

当前核心实现主要在：
- `domain/domain/engine/candidate_engine.py`

### C. 扫描脚本与后处理层
负责：
- Sell Put / Covered Call 的具体脚本调用
- 标签补充
- 现金担保附加过滤
- 事件风险标注
- 报表与 alerts 输出

主要路径：
- `src/application/scan_sell_put.py`
- `src/application/scan_sell_call.py`
- `src/application/sell_put_steps.py`
- `src/application/sell_call_steps.py`
- `src/application/events/`

---

## 3. 候选筛选流程

## 3.1 输入归一化

候选输入会先标准化为统一字段，例如：

- `symbol`
- `option_type`
- `expiration`
- `dte`
- `spot`
- `strike`
- `bid`
- `ask`
- `mid`
- `open_interest`
- `volume`
- `multiplier`
- `currency`
- `implied_volatility`
- `realized_volatility_estimate`

缺少关键字段的合约会被拒绝。

---

## 3.2 硬约束

### Sell Put
主要硬约束包括：

- `min_dte <= dte <= max_dte`
- `min_strike <= strike <= max_strike`
- put 必须满足基本 moneyness 约束
- 当 `sell_put.strategy=insurance_underwriting` 时，必须有可评估的收益、IV/RV、事件风险、现金需求和流动性输入

### Covered Call
主要硬约束包括：

- `min_dte <= dte <= max_dte`
- `min_strike <= strike <= max_strike`，其中 Covered Call 的有效 `min_strike` 不低于持仓 `avg_cost * min_strike_cost_multiplier`
- 必须有足够股票可覆盖 short call

### 说明
这些硬约束主要由候选引擎和扫描脚本共同完成。

### required_data 抓取补充
- 抓取层与扫描层已分离：
  - 扫描层仍按 `min_dte/max_dte/min_strike/max_strike` 做 stage1 硬过滤
  - 抓取层会先为 put / call 分别规划 expiration 与 strike 窗口，再尽量合并请求
- put / call 现在按同一套“边界模式”规划窗口，只是方向相反：
  - `sell_put` 的近端边界是 `max_strike`；若只给 `max_strike`，抓取层向下扩 `20%`
  - Covered Call 的近端边界是 `min_strike`；若只给 `min_strike`，抓取层向上扩 `20%`
- `min_strike=0` 这种 sentinel 语义已移除；未设置边界时请直接省略该字段
- call 未配置任何 strike 边界时，抓取层会退回到默认 spot 窗口 `[spot*1.03, spot*1.20]`
- 抓取层允许加 buffer 防漏抓，但 buffer 不改变扫描硬约束语义

---

## 3.3 收益门槛

收益门槛对 `return_first` 和 `insurance_underwriting` 都是开仓过滤条件，但语义不同：`return_first` 用它确认收益条件合格；`insurance_underwriting` 用它确认这张保险的最低保费足够，再继续评估 IV/RV、事件风险、流动性和 strike 安全距离。

`return_first` 可使用的收益门槛包括：

- `min_annualized_net_return`（Put）
- `min_annualized_net_premium_return`（Covered Call）
- `min_net_income`

### 优先级
字段优先级是：

1. symbol 级配置
2. template 级配置
3. 代码默认值

### 当前默认值注意
默认 Sell Put / Covered Call 开仓 profile 是 `insurance_underwriting`。开仓配置不再接受 `strategy=short_vol`。
如果你要看当前默认值，请直接看：

- `domain/domain/sell_put_config.py`
- `src/application/config_defaults.py`
- `configs/system.json`

---

## 3.4 流动性门槛

当前流动性相关门槛主要是：

- `min_open_interest`
- `min_volume`
- `max_spread_ratio`

### 约束
- 全局模板层允许的硬过滤主要围绕这几个字段
- symbol 级风险字段必须使用当前配置 schema
- 具体门禁由 `src/application/config_validator.py` 保证

---

## 3.5 事件风险

事件风险已经成为 `insurance_underwriting` profile 的正式风险输入。

更准确地说：

- tick run 先由 `src/application/events/prefetch.py` 按 symbol 去重准备事件数据
- 多数据源选择、primary/fallback、市场规则和 resolved snapshot 由 `src/application/events/orchestrator.py` 管理
- 单个 provider 的成功、失败、限流冷却和 stale fallback 由 `src/application/events/store.py` 管理
- candidate scan 只读本轮 `event_snapshot.json`，再由 `src/application/events/annotator.py` 做标注
- 最后由 `domain/domain/insurance_underwriting.py` 按 `reject_event_risk` 和 `event_source_fail_closed` 决定是否通过

也就是说：

> 事件数据的获取是 run 级 source-data 准备，不是 candidate scan 的副作用；事件风险是否允许进入推荐结果，则由承保评估契约统一判断。

验收边界：
- 同一 tick 内同一 symbol 跨账户、Sell Put、Covered Call、Yield Enhancement 只能走同一份 resolved 事件快照；每个 provider 是否发起获取由 provider chain 和缓存状态决定
- `ok + events=[]` 表示可信无事件；`error` / `stale` 不能伪装成无事件
- `ok_with_fallback` 表示主源失败但备源成功，必须在 snapshot 中保留各 provider 的 `source_results`
- `event_source_fail_closed=true` 时，`error` / `stale` 默认不能进入承保推荐
- `runtime_status` 暴露最近一轮 `event_prefetch` 摘要，包括 fetch、cache、stale、rate limit 和 error 计数

---

## 4. Sell Put 的现金担保规则

这部分是最容易误解的地方。

当前行为不是“完全在 candidate_engine 的统一阶段里完成”。

更准确地说：

- 先跑 Sell Put 基础扫描
- 再在 `src/application/sell_put_steps.py` 里结合账户现金 context 做补充过滤

关键逻辑：

- 优先看 `cash_required_cny` vs `cash_free_cny`
- 如果没有 CNY 口径，再 fallback 到 USD 口径
- 超过现金可用额度的候选会在后处理阶段被剔除

### 重要含义
因此，Sell Put 的现金担保约束：

- 是真实生效的
- 但不是完全在单一 Engine 阶段内完成的
- 某些 reject log 口径与“纯 Engine 硬过滤”并不完全一致

---

## 5. 承保风险规则

默认 Sell Put 与 Covered Call 开仓 profile 都是 `insurance_underwriting`。这意味着系统把二者视为承保候选：先确认这张保险的收益、波动率边际、事件风险、流动性和覆盖能力是否合格，再做推荐排序。

当前共享评估由 `domain/domain/insurance_underwriting.py` 负责，规则分四组：

- 波动率边际：`IV/RV >= min_iv_rv_ratio` 且 `IV - RV >= min_iv_minus_rv`
- 事件风险：默认拒绝 expiry 前有财报等事件的候选；事件源不可用时 fail closed
- 收益底线：年化收益率和单笔净收入必须达到最低承保价格
- 边界距离：Sell Put 越低于 `max_strike` 越好；Covered Call 越高于有效 `min_strike` 越好

Sell Put 和 Covered Call 都会对 IV/RV、事件源和必要价格输入 fail closed。开仓侧不再把 stress、gap-down、path pressure 或单标的集中度作为硬风险阈值；旧配置里的这些字段暂时兼容读取，但新默认模板不再输出它们。

---

## 6. Covered Call 的覆盖能力规则

Covered Call 会先结合持仓 context 计算覆盖能力：

- 总持股数
- 已被其他 short call 锁定的股数
- 最终还能覆盖多少张 call

如果可覆盖股数不足，则该账户下的 call 候选不会通过。

---

## 7. 排序规则

排序与过滤分离。

### Sell Put
`return_first` profile 仍可用于收益优先排序。默认 `insurance_underwriting` profile 会综合：

1. 保费边际：收益率、单笔净收入、IV/RV、IV-RV 相对阈值的综合得分
2. strike 安全距离：离 `max_strike` 越远越好
3. 单笔净收入
4. 价差

### Covered Call
默认 `insurance_underwriting` profile 会综合：

1. 保费边际：收益率、单笔净收入、IV/RV、IV-RV 相对阈值的综合得分
2. strike 上行距离：高于有效 `min_strike` 越多越好
3. 单笔净收入
4. 价差

最终 CSV、summary 和 alerts 使用与当前 opening profile 对应的排序核心。

需要解释“为什么这个候选排在前面”时，用同一套排序核心：

- `return_first` 使用 `build_candidate_rank_key(...)` / `explain_candidate_rank(...)` 解释收益优先排序
- `insurance_underwriting` 使用 `domain/domain/insurance_underwriting.py::rank_underwriting_candidates(...)` 对齐承保排序
- Agent 可通过 `candidate_rank_explain` 读取已有候选 CSV 做只读诊断

`candidate_rank_explain` 不重新扫描、不发通知、不写报告，只解释已有候选。

---

## 8. 当前真实代码入口

如果你要从代码追当前行为，优先看：

### 核心引擎
- `domain/domain/engine/candidate_engine.py`
- `domain/domain/engine/candidate_strategy.py` 只保留 DataFrame 适配、排序、分层和 reject log 转换；收益/成本/价差门禁委托给 `candidate_engine.py`

### Put 路径
- `src/application/scan_sell_put.py`
- `src/application/sell_put_steps.py`
- `src/application/sell_put_cash.py`
- `src/application/sell_put_strategy_risk.py`
- `src/application/short_vol_metrics.py`
- `domain/domain/sell_put_config.py`

### Covered Call 路径
- `src/application/scan_sell_call.py`
- `src/application/sell_call_steps.py`
- `domain/domain/sell_call_config.py`

### 风险 / 报表 / 汇总
- `src/application/events/`
- `src/application/report_summaries.py`
- `src/application/alert_engine.py`

---

## 9. 数值真源

这份文档描述规则边界；具体阈值以运行配置和代码默认值为准。

确认当前数值真源：

- 看配置文件
- 看 `scripts/*_config.py`
- 看 `candidate_engine.py`

---

## 10. 一句话总结

当前候选策略是：

> **候选引擎负责基础扫描和初次排序，Sell Put 与 Covered Call 后处理共同使用 ShortVolRiskAssessment 做 short-vol 风险评估和二次排序；Covered Call 额外保留持仓覆盖能力规则。**
