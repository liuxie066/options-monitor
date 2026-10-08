# OM 当前汇率事实与决策简报资金展示设计

状态：Devflow Improve Design 修订版，待 Planreview；2026-10-02。本文是目标设计，不描述已部署行为。基线：`3e757295`（v4.1.0）。

2026-10-07 后续实现修订：事件现金折算也复用 `domain.domain.fx_quote_policy` 的交易时段和
连续休市判断，按事件时点选择已验证原报价；当前展示与容量按消费时点判断。
手工/定时采集共用 `cash_fx_observation_facts`，原始证据通过现有 FX 表保存。
独立查询使用运行实例的 `output_shared/state/rate_cache.json`，输出目录不决定汇率来源。
Wheel 写入复核重验冻结报价；历史 FX facts 和裸数值不能提供当前跨币种容量。
已有现金快照不自动回写；缺失现金使用现有 backfill 预览/应用流程。此修订覆盖下文旧设计中
“历史政策另行处理”的范围排除，不代表已经发布或升级生产环境。

## 已确认目标与验收

用户要求：OM 当前汇率只保留一套取数、校验、缓存和选择代码；同一批次的消费者使用同一份汇率事实；决策简报以人民币折算金额为主要展示口径。外汇市场假期导致当日汇率失效时，使用各币种对最后一条已验证的有效报价作展示计算，标明来源和原报价时间。账户原币种金额仍可见，历史成交汇率事实不被回写。

| ID | 可观察验收 |
| --- | --- |
| A1 | 同一正式 tick 的账户持仓、期权持仓、扫描、Wheel/资金能力与决策简报引用同一个已封存的汇率快照及 hash；独立查询也通过相同的当前汇率 owner 取得一次请求内快照。源码中不再有第二套当前汇率取数、解析、有效性或缓存选择逻辑。 |
| A2 | 腾讯与新浪的真实格式分别解析出 USD/CNY、HKD/CNY 的正有限价格、来源、明确报价时间和抓取时间；缺字段、错位、未来时间、非有限值、过时响应及单币种失败不冒充整份 `ready`。已验证的较新币种报价不被另一币种的旧报价覆盖。 |
| A3 | 决策简报先显示完整的 CNY 折算现金和期权开仓展示值，再显示原币种现金与可开仓明细，包括负余额。仅当已核实的外汇休市区间覆盖报价后的所有应交易时段，才使用该币种对最后有效报价，保留结构化来源及报价时间，正常假期沿用不提示。缺价、交易日断档或日历未知时 CNY 完整值显示“暂不可用”，保留原币种值。 |
| A4 | 已核实连续休市期间，最新有效报价可用于 CNY 展示、只读估值、开仓筛选、跨币种能力、担保及 Wheel；缺价、交易时段断档或来源不可验证时仍不可用。账本成交/现金转换仍由已落库的事件时点 FXRateFact 和历史选择规则决定，不因当前快照或假期沿用而改变。 |

本轮不包含：待确认成交的归属修复、修改真实交易/账本/配置/通知、生产升级、跨项目改动。PM 服务给非富途资产估值所用的 PM 自有汇率属于另一个项目及证据边界；OM 不用自己的当前汇率覆盖 PM 估值。

## 源码事实与冲突

- `src/infrastructure/exchange_rates.py` 已有腾讯优先、新浪后备、`rate_cache.json` 和 `exchange_rate_observation_status`。当前 `_parse_sina` 按第一字段取价，但已观察的新浪 `fx_s*` 响应为时间、价格等字段且另有日期字段；旧解析无法取得正确价格。`_market_observation` 要求两个币种对报价日都等于抓取日；`get_exchange_rates_or_fetch_latest` 取不到新价时返回未标注用途的旧缓存；`_observation_rates_are_valid` 只要其中一个币种对有效就通过。缓存整体的 `timestamp` 是两个报价时间的最小值，不能代替每个币种对的时间。
- `src/application/tick_account_execution.py` 先调 `prepare_portfolio_contexts`，再调 `prepare_option_positions_contexts`。前者在 `prepared_portfolio_context.py` 的每个账户 worker 内经 `futu_portfolio_context.py` 再次抓市场 FX；后者在 `prepared_option_positions_context.py` 单独调用共享缓存/抓取。两者可在同一批次得到不同价格或状态。`pipeline_context.py` 的非 prepared 路径另行取数，并在拿到旧缓存数值时把状态设为 `ready`。
- `cash_headroom_query.py`、`positions/context_builder.py`、`performance/evidence_collection.py`、`portfolio_assignment_scenario.py` 直接调用当前汇率 helper 或抓取函数，所用缓存位置和状态门槛不同。`notify_symbols.py` 只读另一处缓存；`pipeline_watchlist.py` 把缓存文件 hash 当 FX 依赖。`exchange_rate_loader.py` 的 `fetch_opend_exchange_rate_observation` 与 `futu_portfolio_context.py` 的同名兼容入口实际都转调市场报价；前者和 `get_usd_per_cny_exchange_rate` 的生产调用在本基线未找到，删除前仍须核对公开导出/动态注册。`cash_conversion.py` 读缓存后产生历史 FX 证据，不是当前展示计算的第二套规则。
- `daily_decision_brief_service.py::_build_funds` 从期权上下文读 `exchange_rates`，`cash_totals.py` 折 CNY；`daily_decision_brief_renderer.py::_fund_views` 有 CNY 时只显示折算额，否则只显示原币种。本次 09:40 回执因 FX 门槛失败落到原币种分支。当前 `opening_cny = cash_total_cny - secured_total_cny` 的两项必须核对取自同一汇率快照。负 USD 可开仓金额不能被合计 CNY 值掩盖。
- `domain/domain/performance/cash_conversion.py` 有独立的已落库成交日 FX 选择与官方来源跨非营业日规则；`docs/OPTION_PERFORMANCE_DESIGN.md` 禁止报告读取时用现价补历史。`docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md` §11 现仍要求多币种分开显示、无可靠汇率时不可强行相加；本目标必须保留这些保护，在实现时同步修订其已发布行为描述。
- OM 现有 `trading_day_via_futu(..., market="CN")` 给的是证券市场交易日，不等于外汇市场休市证据。中国外汇交易中心的[交易说明](https://www.chinamoney.com.cn/chinese/mgwhcphjy/)和法定节假日安排才是交易时段与休市日依据；[年度交易币种节假日表](https://www.chinamoney.com.cn/chinese/rdgz/20251218/3254567.html)主要涉及币种交割/清算，单独列出某币种假日不能证明整个人民币外汇市场休市。
- `daily_decision_brief_service.py::_build_funds` 仅遍历现金币种，漏掉只有担保的币种；即使 `cash_total_reliable=False` 仍可能计算 CNY 数值。`domain/domain/daily_decision_brief.py::_normalize_daily_brief_funds` 是字段白名单，新增汇率证据若未加入将不会到 renderer。
- `cash_conversion.py::cash_fx_observation_facts` 现要求两个币种对共用顶层来源；`performance/evidence_collection.py` 在 FX 时间缺失时可能以采集时间代替。`domain/domain/portfolio_assignment_scenario.py` 的显式 FX 证据也从顶层来源/时间生成，并用同组 FX 数字同时计算估值和 `cash_coverage`。

## 设计决定

### 1. 唯一当前汇率 owner 与快照

复用 `src/infrastructure/exchange_rates.py`，使其成为 OM **当前** USD/CNY、HKD/CNY 报价的唯一 HTTP 解析、数值/时间/来源校验、最新有效报价缓存选择及状态计算 owner。汇率结构按币种对保存 `rate`、`source`、`quote_at_utc`、`observed_at_utc`、`quality`；`rates` 仅从已选中的币种对派生，不接受调用方自行凑一份数值。CNY 恒等为 1，不需要外部报价。不要再用单个顶层 `source/timestamp` 证明两个币种对的质量；旧缓存缺少可验证的逐币种报价时间时不能参加假期沿用。

仍使用 `output_shared/state/rate_cache.json` 作为跨批次“各币种对最后有效报价”存储，按币种对只接受时间不倒退的已核实报价；同一文件的更新必须防止并发读改写丢失较新报价，并原子落盘。旧格式只在可逐项验证时读取并转换；不能验证的旧值失效，不伪造报价时间或来源。原始抓取和缓存只是候选，状态由同一 owner 按本次请求时间计算，不靠文件修改时间刷新报价。

正式 tick 在账户 worker 启动前调用该 owner **一次**，将本次完整结果封存在 `output_runs/<run_id>/state/` 中的一份不可变、带 hash 的 FX 快照；显式传给账户/期权准备阶段和下游 pipeline。封存前先检查同一 run 是否已有快照：有则核对 run_id、内容 hash 和已准备的 manifest 引用，直接复用，禁止重抓/覆写；冲突或损坏则 fail closed。缺 FX 也封存同一份不可用状态，禁止 worker 各自补抓。快照记录逐对原报价时间、来源、观察时间、判断时间和日历证据；晚到的消费者按**当前时间**重新判断容量资格，不能用快照当时的 `ready` 永久授权，但不改动本 run 报价和 hash。已有准备阶段和 pipeline 的汇率参数/manifest 可扩展，避免第二条旁路。独立 CLI/Tool 查询各自解析一次请求级快照，并在该请求内传递；它们不声称与另一次 tick 同价。

迁移所有当前消费者：`futu_portfolio_context.py` 不再内抓；`prepared_option_positions_context.py`、`pipeline_context.py`、`cash_headroom_query.py`、`positions/context_builder.py`、`portfolio_assignment_scenario.py`、`performance/evidence_collection.py`、`notify_symbols.py` 只接受同一 owner 的快照/只读选择，保留必要的窄适配参数而删除自行判断新鲜度、单币种直读缓存和重复 provider 包装。唯一性约束的是**报价获取、验证、缓存、择价、用途资格**，不强迫各领域金额计算调用 infrastructure：application 向 domain 传入已获相应用途资格的逐对汇率及状态，domain 只做现有 Decimal 金额运算，不自取报价或重判状态。同层现金汇总复用已有纯算术 helper；事件时点历史选价仍是另一项业务政策。`pipeline_watchlist.py` 的 FX 依赖改绑定实际 run 快照 hash，避免共享缓存后续变化影响本批次证据。删掉无生产消费者的旧汇率 helper/兼容壳时先核对动态入口、测试和公开契约；不能证明无人使用的入口只保留无业务逻辑的薄适配。

### 2. 报价与假期规则

腾讯 `~` 字段按索引 3 取价格、5 取行情时间；本次实测的新浪 `fx_s*` 逗号字段按索引 0 取时间、1 取价格、17 取日期，时间按原源的北京时间解释。解析依据源标识和明确字段布局，不靠“第一个可转成 float 的字段”；源格式改变时失效并记录原因。每个币种对独立校验正有限范围、方向、来源、明确时间、抓取时间和未来时间，禁止以抓取时间冒充行情时间。抓取到旧报价可成为有来源的候选，但不能因 HTTP 成功自动标 `ready`；腾讯缺失或失效的币种对继续向新浪取证，另一来源较新且有效时每对择新，同一报价时间保留现有腾讯优先顺序。

每对状态分为互斥的 `fresh`、`holiday_carried`、`unavailable`，并保留不可用原因。按官方交易时段归属报价，而非按自然日比较：**例行日内闭市**到下个应交易时段开盘前，若上一时段报价仍满足现有 24 小时可信门槛，继续为 `fresh`，保留原有容量能力；**整日休市**（周末/法定节假日，含其后的下次开盘前）才可进入 `holiday_carried`，休市证据完整时保留容量资格，不因自然日龄超过 24 小时失效。周五夜盘跨至周六凌晨仍归属周五交易时段。市场重新开市时必须尝试获取本交易时段报价，不能因为缓存未满 24 小时就跳过；新价尚缺时旧价不具备容量资格。`fresh` 要求明确报价时间属于最近一个应有交易时段、无交易时段断档且满足现有 24 小时门槛。`holiday_carried` 要求此前已验证的最后有效报价、明确对应的外汇交易时段、当前仍未进入下一应交易时段，且报价后没有任何应交易却缺新价的时段。休市判据由现行中国外汇交易中心交易时间规则与国务院法定节假日安排及正式临时调整组成，并记录来源、版本、适用区间；不把交易币种交割节假日表、证券市场交易日或某币种假日当作市场休市。规则/年份缺失、临时状态不明或与事实冲突时不得假期沿用。**不设置固定“过了 N 天就改用其它价格”的兜底**：连续休市且证据完整时用最后有效价；出现交易时段断档立即停止沿用。

假期沿用的报价在快照中保留原 `quote_at_utc` 和 `source`，另记本次判断时间与日历依据；绝不把 `observed_at_utc` 或缓存写入时间改成新的报价时间。两个币种对可由不同源、不同时间组成，聚合值只有在所有实际用到的币种对均满足用途要求时才生成。未来报价、无来源报价、格式损坏、币种对缺失或日历未知都明确 unavailable；来源请求失败和有效零持仓/零现金分别记录，不混为零汇率。

### 3. 用途隔离与资金文案

从同一快照产生两个**用途判定**，不维护两套取数代码：① `display_eligible` 接受 `fresh` 或已证明的 `holiday_carried`，供决策简报和只读 Portfolio Assignment Scenario 的经济估值；② `capacity_eligible` 同样接受 `fresh` 或已证明的 `holiday_carried`，按消费时刻复核报价来源及交易时段完整性；对应观察状态为 `ready`。`Wheel`、CSP、`sell_put_cash`、跨币种资金担保与真实可开仓手数只能读②；不需要外汇换算的同币种能力不受外汇休市影响。CNY 折算资金是阅读用经济汇总，不表示能把账户或币种余额自由互抵；原币种负值与容量限制始终可见。开仓筛选与容量上下文从同一原报价事实投影数值；不得伪造新的报价时间或绕过账户现金、持仓等独立约束。

`_build_funds` 对可核实现金、每币种担保及当前快照统一折算，再相减得到 CNY 展示值；原币种可开仓明细按现金与担保币种的并集计算，担保独有币种出现负值。所有非零必要币种对均须 `display_eligible`，不把两个不同汇率快照的 CNY 小计相减；担保的展示折算在简报内由原币种重算，不复用能力上下文的 `cash_secured_total_cny`。现金或担保来源不可靠时，相应完整 CNY 汇总及开仓展示值为 `null`；缺一个必要价格时完整 CNY 值也为 `null`，不得只加可换算币种冒充总额。未归属/未结算等现有资金门槛保持。`_fund_views` 固定先显示“现金总额（折CNY）”和“可用于期权开仓（折CNY，展示值）”，随后显示按币种的两组明细；负 USD 明确带负号。正常假期沿用不增加提示，来源与原报价时间保留在结构化证据中；若不可折算，CNY 行写“暂不可用”及原因，而原币种明细照常显示且标注其可靠性。将逐对来源/报价时间/状态纳入现有简报 funds 字段白名单，验证 assemble→normalize→render 后仍保留；更新已发布通知 PRD §11 和相关输出合同。

历史账本的 `FXRateFact` 仍以当时已验证报价和既有事件时点政策冻结；当前的 `holiday_carried` 不能成为新的历史成交日官方价，也不能重算旧现金转换或绩效。`cash_conversion.py` 从同一 owner 读取各币种对原始已验证报价，按各自真实来源/时间独立生成幂等候选；单对有效即可产生该对事实，沿用缓存不能作为**新的当日观察**入库。`performance/evidence_collection.py` 的当前 FX 证据同样必须有逐对明确报价时间，不得用采集时刻填缺。是否可记账仍由历史事件时点政策判定，不增加第二套当前取数或历史选价逻辑。

Portfolio Assignment Scenario 可用沿用价计算并清楚标记只读资产估值/分布；其 `cash_coverage` 与资金缺口保持严格容量汇率门槛，若所需币种对仅有沿用价则有关覆盖/缺口为不可用，不给出貌似可执行的资金结论。逐对来源、原报价时间、状态和 FX 快照 hash 进入情景快照/证据 hash；显式 FX 不再统称 `quality=current`。PM 非富途资产估值继续使用 PM 提供的证据。

## 可验证实施切片

| 切片 | 结果与依赖 | 验证入口 |
| --- | --- | --- |
| 1. 当前汇率 owner | 修正腾讯/新浪真实字段解析；逐币种验证、单缓存择新、FX 市场休市证据与互斥用途状态；旧缓存安全读取。独立于应用消费者。 | 用真实结构 fixture 验证价格/时间、单币种失败、正常工作日 03:30/09:29 仍新鲜、09:30 开市强制询价、周五夜盘跨周六、整日休市但报价不足 24 小时、长假至下次开盘前、交易日断档、币种假日但市场开市、日历缺失、并发更新与无来源值；证明缓存不刷新行情时间。覆盖 A2、A3 的汇率前提。 |
| 2. 单批次事实与消费者收敛 | 正式 tick 在 worker 前封存一次快照；各 prepared context、pipeline、独立查询和 legacy 只读入口转用同一汇率事实与用途判定 owner，删除重复取数/选价逻辑；历史 FX 候选按逐对原报价生成，容量用途保持严格。依赖切片 1。 | 从 `multi_account_tick` 的模拟外部源跑双账户正式批次，计数一次 FX 获取并比对账户/期权/扫描/简报 hash；测失败、既存 run 的完整/损坏/冲突 artifact、缓存后续改写、延迟消费、独立查询及 Wheel/跨币种 fail closed；测单对历史事实、缺时间拒绝与幂等，同币种能力可用。静态检查活跃调用者不直接解析或另建缓存，domain 不反向 import `src/`。覆盖 A1、A4。 |
| 3. 决策简报与合同 | 以同一快照计算并展示 CNY 优先、原币种明细、负数和假期沿用证据；情景估值与资金覆盖隔离；同步通知 PRD。依赖切片 2。 | 通过决策简报 assemble→normalize→render 验证新鲜价、连续长假、缺一个币种对、交易日故障、现金无担保币种、担保独有币种、来源不可靠、负 USD、CNY-only、零金额；情景测试覆盖假期沿用价同时支持估值和资金缺口计算、缺少有效汇率时仍不可用，并核对逐对证据/hash；历史账本和绩效回归断言旧事实不变。覆盖 A3、A4。 |

实施前应重放 09:40 的只读输入作为回归样例，但不把历史通知或账本改写当作验收。代码、测试、文档之外的提交、发布、生产写入与服务升级各自等待单独授权。

## 未决证据与风险

1. 新浪当前字段布局来自本次只读实测；切片 1 仍需保留带源时间的脱敏真实报文 fixture，核对腾讯对应字段与源标识，不能依靠旧测试造的价格优先文本。
2. FX 现行交易时段及法定节假日可能临时调整；维护者必须在适用年前核对官方来源，并经受控源码流程更新本地休市证据。未知年份或临时调整无法证实的日期会失去 CNY 沿用展示，但不会放宽开仓能力；币种清算假日表不能补足市场休市证据。
3. 旧缓存有些只存整体时间或缺逐对时间，迁移后可能短暂不能折算；宁可显示不可用，也不凭文件时间补证据。开发前核对目前正在运行的缓存格式及服务绑定；设计不授权改生产缓存。
