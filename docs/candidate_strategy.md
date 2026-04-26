# Candidate Strategy

这份文档只回答一件事：

> 系统现在是怎样筛选、排序和输出 Sell Put / Covered Call 候选的。

它不是历史设计稿，也不是未来架构草案；它描述的是**当前生产行为**。

---

## 1. 适用范围

当前候选策略覆盖两类输出：

- **Sell Put**
- **Covered Call**

两类候选共享大体流程，但关注点不同：

- **Sell Put**：现金担保能力、年化净收益率、单笔净收益、流动性
- **Covered Call**：可覆盖股数、年化权利金收益、单笔净收益、流动性

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
- 可选 Feishu holdings fallback

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
- Sell Put / Call 的具体脚本调用
- 标签补充
- 现金担保附加过滤
- 事件风险标注
- 报表与 alerts 输出

主要路径：
- `scripts/scan_sell_put.py`
- `scripts/scan_sell_call.py`
- `scripts/sell_put_steps.py`
- `scripts/sell_call_steps.py`
- `scripts/event_risk_filter.py`

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

缺少关键字段的合约会被拒绝。

---

## 3.2 硬约束

### Sell Put
主要硬约束包括：

- `min_dte <= dte <= max_dte`
- `min_strike <= strike <= max_strike`
- put 必须满足基本 moneyness 约束

### Covered Call
主要硬约束包括：

- `min_dte <= dte <= max_dte`
- `min_strike <= strike <= max_strike`
- 必须有足够股票可覆盖 short call

### 说明
这些硬约束主要由候选引擎和扫描脚本共同完成。

---

## 3.3 收益门槛

当前主要收益门槛包括：

- `min_annualized_net_return`（Put）
- `min_annualized_net_premium_return`（Call）
- `min_net_income`

### 优先级
字段优先级仍然是：

1. symbol 级配置
2. template 级配置
3. 代码默认值

### 当前默认值注意
文档不再写死具体默认数值，因为默认值可能在脚本配置中调整。
如果你要看当前默认值，请直接看：

- `scripts/sell_put_config.py`
- `scripts/sell_call_config.py`

---

## 3.4 流动性门槛

当前流动性相关门槛主要是：

- `min_open_interest`
- `min_volume`
- `max_spread_ratio`

### 约束
- 全局模板层允许的硬过滤主要围绕这几个字段
- symbol 级某些旧的快捷风险字段已不再允许
- 具体门禁由 `scripts/validate_config.py` 保证

---

## 3.5 事件风险

当前事件风险不是一个统一的 Engine 内硬拒绝阶段。

更准确地说：

- 候选先扫描出来
- 再由 `scripts/event_risk_filter.py` 做标注 / 风险附加信息处理

也就是说：

> 事件风险当前更接近后处理标注，而不是单一的前置硬过滤总入口。

---

## 4. Sell Put 的现金担保规则

这部分是最容易误解的地方。

当前行为不是“完全在 candidate_engine 的统一阶段里完成”。

更准确地说：

- 先跑 Sell Put 基础扫描
- 再在 `scripts/sell_put_steps.py` 里结合账户现金 context 做补充过滤

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

## 5. Covered Call 的覆盖能力规则

Covered Call 会结合持仓 context 计算：

- 总持股数
- 已被其他 short call 锁定的股数
- 最终还能覆盖多少张 call

如果可覆盖股数不足，则该账户下的 call 候选不会通过。

---

## 6. 排序规则

排序与过滤分离。

### Sell Put
主要按：

1. 年化净收益率
2. 单笔净收益

### Covered Call
主要按：

1. 年化权利金收益
2. 单笔净收益

最终 CSV、summary 和 alerts 使用的是当前生产实现里的简单稳定排序，不再把旧文档里的理想化阶段图当成唯一真相。

---

## 7. 当前真实代码入口

如果你要从代码追当前行为，优先看：

### 核心引擎
- `domain/domain/engine/candidate_engine.py`

### Put 路径
- `scripts/scan_sell_put.py`
- `scripts/sell_put_steps.py`
- `scripts/sell_put_cash.py`
- `scripts/sell_put_config.py`

### Call 路径
- `scripts/scan_sell_call.py`
- `scripts/sell_call_steps.py`
- `scripts/sell_call_config.py`

### 风险 / 报表 / 汇总
- `scripts/event_risk_filter.py`
- `scripts/report_summaries.py`
- `scripts/alert_engine.py`

---

## 8. 这份文档不再声明的内容

为了避免再次过时，这份文档不再写死：

- 某个默认阈值一定是 `0.07` 或 `0.10`
- 所有过滤都一定只发生在某个固定 stage
- 某个 legacy helper 一定是唯一主实现

如果要确认当前数值真源：

- 看配置文件
- 看 `scripts/*_config.py`
- 看 `candidate_engine.py`

---

## 9. 一句话总结

当前候选策略是：

> **候选引擎负责核心筛选语义，扫描脚本和后处理继续承担一部分现金、风险、报表和兼容逻辑。**

如果以后继续重构，目标应该是让实现更集中，但在那之前，这份文档以**当前行为**为准。
