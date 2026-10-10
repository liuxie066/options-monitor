# OM 当前汇率事实与资金展示契约

本文描述当前源码中的汇率归属和消费约束；运行环境是否采用，以版本及运行证据为准。

## 唯一归属

| 责任 | 源码 owner |
| --- | --- |
| 腾讯/新浪解析、逐币种对校验、缓存择新、请求级快照 | `src/infrastructure/exchange_rates.py` |
| 交易时段、连续休市与报价用途资格 | `domain/domain/fx_quote_policy.py` |
| 正式 tick 的不可变汇率快照 | `src/application/current_fx_run.py` |
| 事件时点 FX 证据采集与历史现金转换 | `src/application/cash_conversion.py`、`domain/domain/performance/cash_conversion.py` |
| 决策简报资金汇总与展示 | `src/application/daily_decision_brief_service.py`、`src/application/daily_decision_brief_renderer.py` |

新增消费者通过当前汇率 owner 取得快照并按用途投影，不另建取数、解析、缓存、时效或回退规则。application 向 domain 传入具有用途资格的汇率；domain 保留纯金额运算，不获取外部报价。

## 报价与缓存

- 当前支持 USD/CNY、HKD/CNY；CNY 恒等为 1。每对保留 `rate`、`source`、`quote_at_utc`、`observed_at_utc`，并分别计算质量；顶层时间或来源不能替代逐对证据。
- 腾讯与新浪按各自明确字段布局解析。价格须正且有限，来源及报价时间须可验证；缺字段、未来时间、无来源及格式变化均不能被抓取成功掩盖，也不能用抓取时间补报价时间。
- 运行实例共用 `output_shared/state/rate_cache.json`。每对择最新已验证报价，同一报价时间腾讯优先；更新有跨进程锁和原子落盘，不能覆盖另一币种对的较新报价。缓存写入时间不刷新报价时间。
- 旧缓存只有能逐对验证时才参与选择；单对失败独立保留缺口，不能冒充整份可用汇率。

## 交易时段与用途资格

逐对质量为 `fresh`、`holiday_carried` 或 `unavailable`。`fresh` 要求报价属于最近应有交易时段、没有交易时段断档且符合 24 小时门槛。例行日内闭市到下次开盘前仍按该规则判断；周五夜盘跨周六凌晨归属周五时段。

只有已核实的连续整日休市覆盖报价后的全部应交易时段，才可沿用最后有效报价为 `holiday_carried`；不因连续休市超过固定天数改用其它价格。市场重新开市、缺少应有时段报价、日历年份未知或来源不可验证时，停止沿用。证券市场交易日和币种交割假日不能代替人民币外汇市场休市证据。

`rates_for_purpose` 从同一快照投影 `display` 或 `capacity`，两种用途均接受 `fresh` 和已证明的 `holiday_carried`，并在消费时重新判断资格。快照封存时的可用状态不能永久授权后续容量。Wheel、开仓筛选、跨币种担保和资产情景资金覆盖复用该判定，同币种能力仍受自身现金、持仓及结算约束。

本地日历证据及适用范围由 `domain/domain/fx_quote_policy.py` 保存。适用年前或官方规则调整后须核验并更新；未知日历保持不可用，不能用其它市场日历补足。

## 同批次事实

正式 tick 在账户 worker 启动前封存 `output_runs/<run_id>/state/current_exchange_rates.v1.json`，绑定 `run_id`、快照及内容 hash。已有快照复用原事实；损坏、身份不符或冲突失败关闭，不重抓覆写。不可用报价也保留在本批次快照中，worker 不各自补抓。

账户、期权准备阶段、扫描、资金能力、Wheel 和简报传递同一份快照/hash。晚到的消费者只重验用途资格，不改报价。独立 CLI/Tool 查询各自取得一次请求级快照；输出目录不改变运行实例的汇率来源，也不承诺与另一批次同价。

## 资金展示与领域边界

- 简报先展示完整 CNY 现金和期权开仓展示值，再保留原币种现金及开仓明细，包括负余额与担保独有币种。CNY 经济汇总不证明跨账户、券商或币种资金可自由互抵。
- 现金与担保按同一快照折算后相减；任何非零必要币种缺价，或现金/担保来源不可靠时，完整 CNY 值为不可用。不能把可折算部分冒充总额，不能将两个快照的小计相减。
- 正常假期沿用不增加通知提示；结构化证据保留逐对来源、原报价时间、质量与 hash。不可折算时保留原币种金额及不可靠原因。产品文案见 [通知体验 PRD](OPTION_NOTIFICATION_EXPERIENCE_PRD.md)。
- [资产指派情景](PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md)从同一快照生成估值和资金覆盖用途，已验证休市报价可供两者使用；PM 非富途估值仍由 PM 的报价/汇率证据负责。

## 历史现金与绩效

事件现金转换复用同一交易时段/连续休市政策，按事件时点选择已验证的原报价。手工和定时采集共用 `cash_fx_observation_facts`；逐对原来源/时间形成幂等证据，缓存沿用不能伪装成新的当日观察。

已落库的事件 FX 与现金转换快照不因当前报价改变而回写。当前快照、裸数值或历史 FX facts 都不能替代当前容量资格；历史报告也不能用现价补过去成交。缺失现金转换通过既有 backfill 预览/应用流程处理，见 [绩效设计](OPTION_PERFORMANCE_DESIGN.md)。

## 验证入口

| 约束 | 测试 |
| --- | --- |
| 真实格式、逐对择新、日内闭市/周末/长假/开市、未知日历、并发缓存 | `tests/test_current_exchange_rate_snapshot.py`、`tests/test_exchange_rates_fetch.py` |
| 封存一次、重试复用、损坏与并发冲突 | `tests/test_current_fx_run.py` |
| 当前/历史共同资格、证据一致、历史不回写、Wheel 延迟消费 | `tests/test_unified_fx_consumers.py` |
| 情景共用报价、估值/覆盖用途及账户隔离 | `tests/test_portfolio_assignment_application.py` |

这些源码与测试入口不证明生产缓存格式、服务版本或真实通知送达。生产写入、发布和升级按各自受控流程执行。
