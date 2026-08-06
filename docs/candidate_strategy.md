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

- **Sell Put**：愿意接货的 strike 边界、IV/RV 承保边际、完整接货资金能力、持有周期净收益、财报风险和执行质量
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
- `realized_volatility_20/60/120`
- `realized_volatility_estimate`
- OpenD 原始 `quote_update_time`

缺少关键字段的合约会被拒绝。

---

## 3.2 硬约束

### Sell Put
主要硬约束包括：

- `min_dte <= dte <= max_dte`
- `min_strike <= strike <= min(max_strike, spot)`；`max_strike` 缺省时以 spot 为上界
- 有效双边报价：`bid > 0`、`ask > 0`、`ask >= bid`
- `(ask-bid)/mid <= 40%`
- 交易时段内 OpenD 原始 `update_time` 不超过 5 分钟
- 最低年化净收益和最低单笔净收入
- `IV/RV >= 1.10` 且 `IV-RV >= 0.05`
- 到期前无财报，且财报事件覆盖完整、来源可用
- 账户能够承担完整 `strike * multiplier` 接货金额

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
- put / call 分别按策略方向规划窗口：
  - `sell_put` 先取 `effective_max_strike=min(configured max, OpenD spot)`；未配置 max 时用 spot，再从该上界向下召回 `20%`
  - Covered Call 的近端边界是 `min_strike`；若只给 `min_strike`，抓取层向上扩 `20%`
- `min_strike=0` 这种 sentinel 语义已移除；未设置边界时请直接省略该字段
- call 未配置任何 strike 边界时，抓取层会退回到默认 spot 窗口 `[spot*1.03, spot*1.20]`
- Sell Put 显式的更严格 `min_strike` 继续生效
- OpenD spot 缺失时，Sell Put 召回 fail closed，不回退旧 required-data CSV，也不只凭配置 max 猜测窗口
- 抓取层窗口不改变扫描硬约束语义

---

## 3.3 收益门槛

Sell Put / Covered Call 新开仓只使用 `insurance_underwriting`。收益门槛用于确认最低保费足够，再继续评估 IV/RV、事件风险、流动性和价格边界。

收益门槛包括：

- `min_annualized_net_return`（Put）
- `min_annualized_net_premium_return`（Covered Call）
- `min_net_income`

### 优先级
字段优先级是：

1. symbol 级配置
2. template 级配置
3. 代码默认值

### 当前默认值注意
默认且唯一的 Sell Put / Covered Call 开仓 profile 是 `insurance_underwriting`。开仓配置不再接受 `strategy=return_first`、`strategy=short_vol` 或 `score_weights`。
如果你要看当前默认值，请直接看：

- `domain/domain/sell_put_config.py`
- `src/application/config_defaults.py`
- `configs/system.json`

---

## 3.4 Sell Put 流动性边界

Sell Put 只保留有效双边报价和 `max_spread_ratio=0.40` 两个流动性硬门槛。

- OI 不设最低硬门槛，只在收益接近时作为次级排序依据；缺失值明确保留，并排在有可靠 OI 的可比候选之后。
- 当日 volume 不设硬门槛，也不参与正式排序，只作为观察字段展示。
- 旧的 `min_open_interest` / `min_volume` Python 与 CLI 参数只为兼容保留，对当前 Sell Put 扫描不起作用。

Covered Call 与 Combo Yield 仍有各自独立的流动性合同，不能从 Sell Put 推导。

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
- Sell Put 只把财报作为本轮事件硬门槛；非财报事件仍可展示，但不会自行升级为拒绝规则
- `ok + earnings coverage=complete + events=[]` 才能证明当前快照内无财报；`error`、`stale` 或 earnings coverage 不完整不能伪装成无财报
- `ok_with_fallback` 表示主源失败但备源成功，必须在 snapshot 中保留各 provider 的 `source_results`
- `event_source_fail_closed=true` 时，`error` / `stale` 默认不能进入承保推荐
- `runtime_status` 暴露最近一轮 `event_prefetch` 摘要，包括 fetch、cache、stale、rate limit 和 error 计数

---

## 4. Sell Put 的现金担保规则

收益率与接货能力使用不同资金口径：

```text
period_net_return = net_income / (strike * multiplier - net_income)
cash_required_native = strike * multiplier
```

接货能力不抵扣权利金。已有开放 Short Put 占用的担保现金先按币种扣除，不能重复使用。

现金转换规则：

- 合约结算币种现金按 100% 计入；
- 其他币种在可靠汇率折算后按 95% 计入；
- 不设置额外通用安全缓冲；
- 汇率最多允许 24 小时；只有过期汇率时，跨币种现金不参与硬门槛，但同结算币种现金仍可使用；
- 输出以 `cash_required_native`、`cash_free_effective_native`、`cash_native_currency` 和 `cash_fx_status` 记录计算口径；汇率过期显式标为 `fx_stale`。

扫描阶段用同一套 native-currency 结果执行现金硬门槛；后续 cash enrichment 负责把相同事实写入候选 CSV 和 trace，不再创建另一套 CNY/USD 决策逻辑。旧 CNY/USD 字段只保留兼容读取。

---

## 5. 承保风险规则

默认 Sell Put 与 Covered Call 开仓 profile 都是 `insurance_underwriting`。这意味着系统把二者视为承保候选：先确认这张保险的收益、波动率边际、事件风险、流动性和覆盖能力是否合格，再做推荐排序。

当前共享评估由 `domain/domain/insurance_underwriting.py` 负责，规则分三组：

- 波动率边际：`IV/RV >= min_iv_rv_ratio` 且 `IV - RV >= min_iv_minus_rv`
- 事件风险：Sell Put 拒绝 expiry 前财报；事件源不可用、过期或 earnings coverage 不完整时 fail closed
- 收益底线：年化收益率和单笔净收入必须达到最低承保价格

价格边界在基础扫描阶段作为硬门槛执行：Sell Put 的 `strike <= min(max_strike, spot)`，Covered Call 的 `strike >= effective_min_strike`。门槛通过后，价格距离不再形成第二套软门禁。

Sell Put 和 Covered Call 都会对 IV/RV、事件源和必要价格输入 fail closed。开仓侧不再把 stress、gap-down、path pressure 或单标的集中度作为硬风险阈值；旧开仓配置字段不再兼容读取，新默认模板也不再输出它们。

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

1. 硬门槛全部通过。
2. 主排序收益使用持有周期非年化净收益：`net_income / (strike * multiplier - net_income)`。
3. 每轮以剩余候选最高收益为锚点，与其相差不超过 `0.002` 的候选组成一个收益区间；不用违反传递性的两两比较器。
4. 同一标的的收益区间内依次比较：净接货折价、较小 spread、可靠且较大 OI、较高净收入、合约标识；集中度不参与同标的选约。
5. 每个标的先选出代表合约。不同标的的收益区间内依次比较：较低接货后 symbol concentration、净接货折价、spread、OI、净收入、symbol 和合约标识。
6. 年化净收益只保留为最低资金效率硬门槛；delta、volume、诊断 score 和研究 score 不改变正式排序。

### Covered Call

1. 硬门槛全部通过。
2. 每个标的选择年化净权利金收益率最高的合约。
3. 不同标的继续按年化净权利金收益率降序。
4. 年化净权利金收益率相同时，strike 上行距离和集中度只作 tie-break；再用流动性、净收益额和合约标识稳定排序。

最终 CSV、summary 和 alerts 使用 `candidate_engine.rank_candidate_rows()` 这一套排序核心。application adapter、报表和通知层不得再实现平行排序。

需要解释“为什么这个候选排在前面”时，用同一套排序核心：

- 当前 `insurance_underwriting` 使用正式固定排序，不读取开仓 `score_weights`
- 历史 `return_first` artifact 仍可由兼容解析和研究工具解释，但不能作为新开仓 profile
- Tool Gateway 调用方可通过 `candidate_rank_explain` 读取已有候选 CSV 做只读诊断

`candidate_rank_explain` 不重新扫描、不发通知、不写报告，只解释已有候选，并把 Sell Put 的持有周期收益显示为主排序依据。

---

## 8. OpenD 数据语义

- OpenD market snapshot 的 `option_implied_volatility` 是百分号前数值，例如 `20` 表示 `20%`；适配层固定除以 100，领域层不再按数值大小猜单位。
- RV 从 OpenD 前复权日线收盘价计算。DTE `<=30` 使用 70% RV20 + 30% RV60；`31–60` 使用 30%/50%/20%；`61–90` 使用 20%/40%/40%。任一所需窗口缺失即 fail closed，不重新归一化。
- required-data CSV 必须保留 `market` 和 OpenD 原始 `quote_update_time`。OpenD 文档说明该字段是“当前价更新时间”，并非 bid/ask 各自的时间戳；当前策略把它作为 provider 新鲜度代理，同时仍要求有效 bid/ask 和 spread 门槛，不能把它表述成逐字段报价时间证明。
- OpenD `update_time` 的无时区字符串按市场本地时区解释：美股为美东时间，港股为香港/北京时间。

---

## 9. 当前真实代码入口

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

## 10. 数值真源

这份文档描述规则边界；具体阈值以运行配置和代码默认值为准。

确认当前数值真源：

- 看配置文件
- 看 `scripts/*_config.py`
- 看 `candidate_engine.py`

当前 Sell Put 稳定常量包括：最大相对价差 `0.40`、交易时段报价年龄 `300` 秒、收益接近阈值 `0.002`、非结算币种现金折扣 `0.95`、FX 最大年龄 `24` 小时。symbol DTE 和 strike 边界继续来自当前配置。

---

## 10. 一句话总结

当前候选策略是：

> **候选引擎负责基础扫描和初次排序，Sell Put 与 Covered Call 后处理共同使用 `insurance_underwriting` 做承保过滤和排序；Covered Call 额外保留持仓覆盖能力规则。**
