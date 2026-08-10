# AI Decision Advice 设计合同

> 状态：已确认的 v1 目标设计（2026-08-09）
>
> 权威范围：Sell Put / Covered Call 候选生成后的 AI 决策建议、外部证据采集、
> Daily Brief 展示、运行审计和失败语义
>
> 实施状态：v1 基础能力及 Gateflow drift remediation S1–S7 已完成本地实现与验证
> （2026-08-10）；账户级 prepared 数据源、确定性投影、匿名观察集合、来源绑定、
> 严格校验、回执和 Agent 读取面已对齐本合同。验证记录位于
> `docs/gateflow/ai-decision-advice-drift-remediation/`。代码边界：
> `src/application/ai_decision_advice/`（identity / evidence_store / collector /
> contexts / projection / validation / advice / advice_store / orchestration /
> render）、`src/infrastructure/deepseek_responses.py`、
> 内部 managed-service collector entry；brief 集成在
> `src/application/daily_decision_brief_service.py` 与
> `domain/domain/daily_decision_brief.py`；systemd collector unit 由
> `src/application/service_deploy.py` 在 `ai_decision_advice.enabled` 时渲染。

AI Decision Advice 是固定工作流中的建议层，模型角色定位为“账户级期权决策顾问”。
它把策略候选、组合分布、开放期权持仓和可靠外部证据放在同一个冻结上下文中，
为当前一轮 Sell Put / Covered Call 候选给出可审计建议。

它不是资讯摘要器，也不是第二套 Candidate Engine。外部资讯只是四类输入之一；
产品价值在于把公开事件与账户当前暴露联系起来，判断本轮应维持、改选、暂缓，
还是需要人工判断。

Sell Put / Covered Call 的召回、硬门槛、容量和排序仍以
[candidate_strategy.md](candidate_strategy.md) 为唯一策略细则真源。AI Decision Advice
不得修改或复制这些规则。

## 1. 名称与产品边界

统一名称如下：

| 层级 | 名称 |
|---|---|
| 正式产品名称 | `AI Decision Advice` |
| 模型角色定位 | `账户级期权决策顾问（Account-level Options Decision Advisor）` |
| Python / 配置标识 | `ai_decision_advice` |
| v1 输出契约 | `ai_decision_advice.v1` |
| 用户回执名称 | `AI建议` |

### 1.1 v1 目标

v1 只实现两类当前候选决策：

- Sell Put：在账户共享现金池的合格候选中形成一个建议；
- Covered Call：按标的分别判断，可以同时形成多个建议。

每个 OM 账户独立形成 Advice。该账户的策略候选、组合分布和开放期权持仓必须来自
同一 run 的账户级输入，禁止跨账户合并后再交给模型。组合分布和开放期权持仓在 v1
中是决策输入，不单独生成“组合建议”或“持仓建议”。

### 1.2 明确不做

v1 不做以下事项：

- 不恢复 Candidate Engine 已拒绝的候选；
- 不修改召回窗口、硬门槛、排序、限价、数量或资金口径；
- 不自动下单、创建订单、换汇或写入券商状态；
- 不把年化收益、Delta、DTE 或模型自创评分重新做一遍候选排序；
- 不在线学习，不根据结果自动改 Prompt 或策略参数；
- 不维护用户观点、投资意图或模型生成意图的 JSON/数据库；
- 不要求用户对建议做“接受/拒绝”回执；
- 不输出百分比或高/中/低形式的 AI 置信度；
- 不新增组合级 Delta/Gamma/Vega 实时风险引擎；
- 不引入行业集中度；
- 不引入 Pi SDK；
- 不新增独立 Agent 工具、独立通知渠道或手动搜索入口；
- 不预建 Combo Yield、Close Advice、组合建议或持仓建议的空模块和空字段。

### 1.3 与相邻能力的区别

| 能力 | 回答的问题 | 运行方式 | 是否能改策略参数 |
|---|---|---|---|
| Candidate Engine | 哪些候选合格，原始顺序是什么 | 确定性扫描 | 否 |
| AI Decision Advice | 结合当前组合、期权持仓和外部证据，这一轮怎么选 | 固定、结构化工作流 | 否 |
| OM Copilot | 用户临时提出的问题如何回答 | 通用对话 Agent 与只读工具 | 否 |
| 未来参数优化 workflow | 以后策略规则或参数应如何调整 | 离线回放、评估、人工批准 | 仅能提出方案 |

AI Decision Advice 与 OM Copilot 不共享 Scene、自由对话历史或动态工具循环。
它也不属于未来的策略参数优化工作流。一个回答“这一轮怎么选”，另一个回答
“以后规则怎么改”。

## 2. 不变量与所有权

1. Candidate Engine 是 Sell Put / Covered Call 正式筛选和排序的唯一所有者。
2. AI Decision Advice 只消费同一 run、账户和市场的已封存候选快照。
3. 候选快照不可用、无效或身份不一致时，不创建 AI 建议任务。
4. AI 输出是独立 overlay，不回写或改写候选快照。
5. 模型只能引用 Candidate Engine 已接受的候选 ID。
6. 通知、Agent 查询和审计必须读取同一份 AI Decision Advice 权威产物。
7. 外部证据缺失不能被解释为风险不存在；模型失败不能被解释为建议维持。
8. 模型建议永远不构成自动执行授权。
9. 账户私有输入严格按 OM 账户隔离；公开标的证据可以按 canonical symbol 跨账户复用，
   但 Collector 和共享证据中不得出现账户来源。
10. Futu 现金、股票和可卖容量继续属于 Candidate Engine 的账户运营事实；不得把
    Futu 券商账户持仓静默冒充 AI Advice 的全组合战略分布。
11. AI 组合分布是可选的 PM provider；开放期权唯一真源是 OM SQLite ledger 的
    prepared projection。两者均无隐式 fallback。
12. Advice 只能消费同一 run 已验证并冻结的 prepared 输入；不得在模型预算内同步取数、
    重读 legacy 文件或根据字段形状猜测来源。
13. 模型生成的 source 文本不是来源真源；只有绑定到 provider 原生搜索引用、
    通过 URL 与展示安全校验的证据才能进入正式证据索引。

## 3. 两阶段架构

功能拆为两个独立阶段：

```text
公开观察标的
  -> External Evidence Collector
     -> DeepSeek Responses + native web_search
     -> 共享、追加写入的外部证据

同一 run 的封存策略候选
+ 账户级 prepared PM 组合分布
+ 账户级 prepared SQLite 期权持仓
+ 每个候选新增一张的确定性投影
+ 最新可用外部证据
  -> AI Decision Advice
     -> 严格 JSON 建议
     -> 确定性校验与中文渲染
     -> 既有 Daily Brief / Agent 读取面
```

### 3.1 External Evidence Collector

证据采集器只知道公开标的身份：代码、市场、交易所和 OpenD 公司名称。它：

- 可以使用 DeepSeek 原生 `web_search`；
- 不接触账户、组合、期权持仓、候选收益或个人信息；
- 不能输出 `keep / switch / defer / needs_review`；
- 不发送通知；
- 不使用 DeepSeek `background`，调度、超时、并发和持久化都由 OM 控制。

### 3.2 AI Decision Advice

决策节点在正常监控回执形成过程中运行，包括固定 Daily Brief 和新增候选提醒。它：

- 接收冻结的四类结构化输入；
- 不具有联网搜索或任何 OM 工具；
- 不能读取运行环境、文件系统、配置、密钥或账户标识；
- 只输出严格 JSON；
- 由 OM 校验 ID、动作、证据引用和输出 Schema；
- 由确定性 renderer 生成最终中文回执。

两个阶段分离的目的，是让耗时搜索提前完成，同时保证公开网页内容永远不能获得
账户上下文或工具执行能力。

## 4. 模型与 API 合同

v1 固定使用：

- Provider：DeepSeek；
- Model：`deepseek-v4-flash`；
- API：Responses API；
- 搜索：服务端原生 `web_search`；
- 密钥：逻辑凭据 `llm.deepseek.api_key`；`DEEPSEEK_API_KEY` 仅为显式 env 兼容名。

参考官方文档：[DeepSeek Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)。

模型选择不跟随 OM Copilot 的 `active_model` 切换。内部仍保留窄的 provider adapter
边界，但 v1 不暴露模型、搜索并发、超时或 token 预算配置。

### 4.1 实现边界

本功能使用独立的 `src/infrastructure/deepseek_responses.py`，不复用 Copilot 的
provider registry、Scene 或 function-tool 循环。Collector 可以声明原生
`web_search`；Advice 调用必须关闭工具。两者均使用严格 JSON Schema、最小审计字段
和受 OM 控制的总预算。

DeepSeek 当前官方 Responses API 文档确认 `web_search` 由服务端执行并暴露搜索调用
状态，但没有把“每条模型来源必然携带可绑定的原生 citation/annotation”声明为稳定
合同。实现必须先用 provider adapter 的响应 fixture 固定实际形状；运行时若无法完成
第 6.4 节的原生引用绑定，就丢弃对应证据，不得把模型自报来源提升为可信来源。这个
兼容性缺口不能用宽松解析掩盖。

当前 remediation 的重点不是再建模型适配器，而是把已存在的 adapter 接到正确的
账户级 prepared 输入、观察集合、投影和确定性 validator 上。

### 4.2 为什么不引入 Pi SDK

Pi SDK 的当前 DeepSeek provider 仍以 OpenAI-compatible Chat Completions 为主要边界，
且其 TypeScript/Node Agent 层会与仓库现有 Python 模型、工具和运行治理重复。v1 直接
扩展窄的 Python Responses adapter，改动更小、所有权更清楚。

## 5. 外部证据观察集合

主动观察集合是以下标的的并集：

1. 配置中的 Sell Put 扫描标的；
2. 各账户可用 PM 组合分布中可规范化为市场证券的持仓标的；
3. 各账户开放期权持仓的底层标的；
4. 最近一次已接受的 Sell Put / Covered Call 候选标的。

所有标的先规范化为 canonical symbol，再跨账户、Sell Put、Covered Call 去重。同一
底层标的的公开证据只采集一次并共享；构建并集时只传 symbol 和公开身份，不记录该
symbol 来自哪个账户。账户级建议仍分别生成，并只冻结与该账户候选和持仓相关的证据。
现金、货币基金、内部合成 code 和无法映射到市场证券的资产不进入 web 搜索观察集合，
但仍保留在账户组合权重中。

### 5.1 标的身份真源

搜索身份只认 OpenD：

1. 优先复用同一市场快照中的 `code/name`；
2. 名称缺失时，按明确 `code_list` 调用 OpenD `get_stock_basicinfo`；
3. 冻结 `symbol_identity_snapshot` 后再进入搜索批次。

身份快照至少包含 canonical code、市场、交易所和完整公司名称。可以把明确绑定到
该实体的中英文名称或合法别名作为查询词，但不得让模型把标的重新映射为另一家公司，
也不维护手工公司身份数据库。

单条证据身份含糊时忽略该证据；整个标的身份无法建立时，该标记为
`identity_unavailable`，不能形成 `keep`。

### 5.2 身份快照持久化

冻结后的身份快照唯一真源：

```text
output_shared/state/ai_decision_advice/symbol_identity_snapshot.json
```

规则：

- 原子写整份快照；内容含每个标的的 canonical code、市场、交易所、完整公司
  名称、合法别名和 `observed_at`（UTC），以及完整工件 hash 与语义身份 hash；
- 快照在观察集合或 OpenD 身份数据变化时重建；重建是确定性的，与进程生命
  周期无关；
- 每条外部证据记录携带其生成时的语义身份 hash，保证“先冻结身份再搜索”
  可审计；
- 完整工件 hash 可以覆盖 `observed_at` 以证明文件完整性；证据绑定使用的语义身份 hash
  只覆盖 canonical code、市场、交易所、公司名称、合法别名和身份状态，不包含
  `observed_at`；仅观察时间变化不能使证据失效；
- 语义身份真正变化时，旧 identity hash 的证据不再进入当前索引，并把标的标为
  `identity_changed_pending_refresh`；新身份完成搜索前不能复用旧证据；
- 不维护第二份手工或可独立修改的身份数据库。

### 5.3 匿名观察集合交接

Collector 独立于 Tick 调度，但不能自行遍历账户目录或读取持仓。正常 Tick 在 prepared
输入和候选封存完成后，把各账户来源先在本地合并，再原子发布匿名观察集合：

```text
output_shared/state/ai_decision_advice/observation_set.json
```

该工件按 canonical market 保存分区；每个分区只包含 canonical symbol、市场、确定性
优先级、generation、生成时间和内容 hash，不包含账户、来源账户、持仓数量、权重、期权
合约或候选指标。US/HK 等 market-scoped Tick 只替换自己负责的分区：publisher 必须在
private lock 下读取并严格校验当前 snapshot、更新目标分区、再原子替换整份文件，防止
不同市场或并发 Tick 发生 lost update。损坏的现有 snapshot 必须 fail closed，不能把
当前市场的局部集合冒充完整跨市场集合。

Collector 在一次一致读取后合并全部分区，并为每个 symbol 建立公开身份；发送给
DeepSeek 的搜索请求连优先级也不包含。某市场下一次成功 Tick 会确定性替换该市场旧
分区，因此已经离开四类来源的标的不会永久残留；其他市场分区不受影响。

启动时观察集合尚不存在时，Collector 只使用配置扫描标的补刷。新 PM 持仓、开放期权
或候选在下一次 Tick 发布观察集合后进入 Collector 队列；在其首次搜索完成前沿用
第 6.6.2 节 `no_evidence` 语义。

## 6. 外部证据采集合同

### 6.1 调度与预算

- 每 4 小时刷新一次，全天运行，每日最多 6 次；
- 服务启动时如不存在有效证据快照，立即补刷；
- 一次刷新总预算为 5 分钟，不是每个标的 5 分钟；
- 每批最多 5 个标的；
- 同时最多运行 2 个批次；
- 不提供用户手动刷新入口或 Agent 搜索工具。

所有刷新时间、过期判断和全量核对基准一律使用 UTC，并以证据记录中持久化
的 `last_success_at` 为准，不随进程重启或本地时区变化漂移。

搜索优先级固定为：

1. 有开放期权持仓的底层标的；
2. 最近一次已接受的 Sell Put / Covered Call 候选；
3. 当前可用 PM 组合分布中的持仓；
4. 其余配置扫描标的。

同一优先级内，最久未成功刷新的标的优先。未完成标的进入下一轮同层级队首，防止
观察集合较大时长期饥饿。

### 6.2 增量与全量核对

- 新标的首次搜索：最近 30 天，加仍在持续的历史事项；
- 4 小时刷新：从上次成功 cutoff 开始做增量搜索；
- 每 24 小时：重新核对最近 30 天和此前未解决的重要事项；
- URL 和内容指纹去重；
- 全量核对可以确认事项已经解决或失效，并生成新的语义证据快照；历史记录仍追加保留。

每次标的搜索成功都必须追加一条可完整重建的 `symbol_status`：记录 search mode、当前
identity semantic hash、有序 `active_evidence_refs` 与 `semantic_snapshot_hash`。增量搜索
把上一份成功 snapshot 的 active refs 与本轮新增/更新证据确定性合并；本轮没有增量证据
时 active refs 和 semantic hash 保持不变。24 小时全量核对把本轮返回的完整有效证据集
作为新的 active refs，允许移除已经解决或失效的旧事项；全量零结果会明确清空 active
refs。历史 evidence 行继续追加保留，但不再自动进入当前 frozen view。

### 6.3 搜索范围

搜索从标的和公司开始。只有存在明确因果关系时，才扩展到：

- 合法别名、母公司或子公司；
- 监管机构、交易所、法院；
- 行业、地区政策或供应链事件。

搜索引擎不负责证明“未来没有事件”。近期或正在发展的增量信息是其正式职责。
已知未来财报继续由 OpenD earnings calendar 作为确定性数据源，并由 Candidate Engine
执行持有期硬检查。Web 搜索偶然发现的可靠未来事件只是额外证据；DTE 只判断已发现
事件是否与候选相关，不要求搜索覆盖整个未来持有期。

### 6.4 来源等级

来源优先级如下：

1. 监管机构、交易所、法院和公司正式披露；
2. 有明确作者、原始采访或原始调查的可靠财经媒体；
3. 券商研究、行业媒体和具名专家，按其底层证据判断；
4. 匿名消息、论坛、无原始来源聚合和重复转载不采用。

一条未经可靠来源证实的消息完全忽略：

- 不影响动作；
- 不触发 `needs_review`；
- 不出现在用户回执；
- 同一消息被多次转载不算多个独立证据。

搜索摘要、模型常识或含糊传闻不能单独支持建议改变。可靠的正式来源或有责任主体的
原始报道可以作为早期证据；若可靠来源相互冲突，则进入 `needs_review`。

来源绑定是确定性边界：

- 每条模型输出的证据 URL 必须能绑定到同一 provider response 的原生
  web-search citation/annotation；
- 原生引用未返回、URL 无法绑定或引用与当前标的身份无法对齐时，
  该证据行丢弃，不得用模型自报的 URL、publisher 或 title 补足；
- 搜索完成但经绑定和可靠性校验后为零条证据，仍可表示“未发现可采用事件”；
  前提是搜索执行本身符合第 6.6.1 节。

### 6.5 默认忽略的内容

- 没有新增事实的单纯涨跌报道；
- 常规分析师评级和目标价；
- 技术分析、市场情绪或社交热度；
- 重复聚合内容；
- 与候选持有期无关的长期叙事。

### 6.6 搜索完成与失败

每个标的独立产生结果。刷新超时或部分失败时：

- 已成功标的立即发布；
- 未完成标的保留上一份成功快照，同时记录最新失败状态；失败记录不能覆盖或隐藏
  最近一次成功事实；
- 旧快照不超过 8 小时时可以使用，并显示实际证据时间；
- 超过 8 小时则为 `unavailable: evidence_stale`，不能输出 `keep`；
- 单个标的失败不影响其他标的；
- 采集失败只记录运行状态，不单独发送飞书消息。

搜索成功但没有发现新证据时，更新 `last_checked_at`，语义证据 hash 保持不变，允许
复用既有 AI 建议。

每个标的保留自己的实际 `last_success_at`。策略模块的 `evidence_as_of` 取其相关、可用
标的 `last_success_at` 的最早值（保守覆盖时点）；没有任何成功快照时为 `null`。它不取
Advice 冻结时间、读取时间或最近失败时间。

Freeze 必须选择当前 identity semantic hash 下最近一条成功 `symbol_status`，再严格按其
`active_evidence_refs` 重建 semantic snapshot；不得把该标的 JSONL 中所有历史 evidence
行合并为当前证据。任一 active ref 缺失、重复、绑定到其他 symbol/identity，或重算 hash
不匹配时，该标的证据为 `unavailable: evidence_snapshot_invalid`，不能返回部分 snapshot。
最近失败状态仅用于运行诊断，不覆盖最近成功 snapshot。

### 6.6.1 证据覆盖完成

“证据覆盖完成”只要求以下三点同时成立：

1. 该标的的搜索范围和查询 cutoff 可审计；
2. provider response 至少含一个 `completed` web-search call，且当轮每个请求
   标的都能通过冻结的 symbol/company identity 归因到已完成搜索；
3. 证据快照年龄不超过 8 小时。

模型 `results` 的 symbol 集合必须与本批请求集合严格相等，不允许遗漏、重复或额外
symbol。“结构上缺一个 symbol，然后由 OM 补空数组”属于采集失败，不得记为
`completed`。搜索调用无法归因时同样失败；只保留上一份有效成功快照。

它不要求必须搜索到任何事件。一次可审计、无错误、覆盖明确的“未发现新证据”
结果同样构成完整覆盖。

### 6.6.2 新标的的首次覆盖

观察集合是动态并集。两次 4 小时刷新之间首次出现的新标的，在下一轮刷新完成
前没有任何证据快照，明确标记为 `unavailable: no_evidence`，不能输出 `keep`，
也不允许把“尚未搜索”解释为“无风险”。

### 6.7 输出格式与异常修复

采集结果必须通过严格 JSON Schema。格式异常时，在本次 5 分钟总预算内允许一次格式
修复重试；重试后仍无效则该批次失败。禁止正则提取、模糊解析或根据残缺文本猜测字段。

## 7. 四类冻结输入

AI Decision Advice 每次只接收同一 run、账户和市场冻结后的结构化输入。

组合与期权数据在 Tick 的 prepared-data 阶段按账户各读取一次并封存，不占用 Advice
30 秒模型预算。Advice 编排接收已经校验的对象及其 manifest/hash，不自行读数据库、
调用 PM、重读路径或按文件名猜测数据。每类输入都显式携带
`available / degraded / unavailable` 状态；空集合只有在来源合同完整时才表示真实为空。
Advice 专用 PM preparation 是 soft dependency：失败只能降低 AI 动作上限，不能把账户
移出 Candidate Engine 扫描，也不能阻断原始监控回执。既有期权 prepared context 对
Candidate Engine/持仓工作流的 fail-closed 语义保持不变。

账户级数据拼接只有下表这一条路径：

| 事实 | 唯一来源 | 账户范围 | 用途 |
|---|---|---|---|
| 合格候选与容量 | 当前 OM 账户的 Candidate Engine 封存快照 | 当前 OM 账户 | 保留正式筛选、排序、现金与持股容量结论 |
| 运营现金与持股 | Candidate Engine 按账户配置使用的 Futu 或 holdings 上下文 | 当前 OM 账户 | 只服务候选容量；不作为 AI 战略组合分布 |
| 战略组合分布 | 显式启用的 portfolio-management 单账户 distribution | 当前 OM 账户映射出的一个 PM/holdings 账户 | 提供当前资产、币种、现金与货币基金权重，以及投影所需总市值和持股数量 |
| 开放期权 | OM SQLite ledger 的账户级 prepared projection | 当前 OM 账户 | 提供账户内已有期权义务、方向、期限和已验证组合结构 |
| 投影汇率 | 同一 run 的 prepared option authority 所绑定的 OpenD 汇率观察 | 当前 run | 只在本地换算 Sell Put 一张合约的名义暴露 |

无论当前 OM 账户的运营来源是 Futu 还是 external holdings，AI 战略组合分布都只认
显式配置的 PM provider。Futu 只能描述对应券商账户，不能代表用户在 PM/holdings 中的
完整组合，也不能在 PM 缺失时成为 fallback。反过来，PM 战略分布也不改写 Candidate
Engine 已经封存的现金、持股或可卖容量。

SQLite ledger 可以是多个账户共用的物理数据库，但 prepared option context 必须先按
当前 OM 账户投影、校验并封存；共享物理存储不构成跨账户读取授权。PM 的
`holdings_account` 只是当前 OM 账户到单个 PM 账户的显式映射，也不构成聚合其他账户的
授权。候选、PM 分布、期权持仓任一 run/account binding 不一致时，该输入失败关闭，
不得通过标签相似、默认账户或其他账户数据补齐。

### 7.1 策略候选

候选来源是：

```text
output_runs/<run_id>/accounts/<account>/state/opening_candidate_snapshot.json
```

模型接收完整的最终合格候选集合，而不是只接收通知里展开的 top N。内容包括：

- 候选 ID、策略、标的、合约身份、到期日、行权价和 multiplier；
- Candidate Engine 原始排序；
- 持有周期非年化净收益、年化硬门槛值、净权利金和容量；
- Candidate Engine 已生成、与本决策有关的其他指标。

模型不能恢复快照中已拒绝的候选，也不能把兼容 CSV 当作候选事实源。
它不独立读取或扣除待成交挂单；资金和持仓容量直接引用候选快照的正式结果。

### 7.2 组合分布

AI Advice 的战略组合分布使用可选、显式启用的 portfolio-management provider。它与
Candidate Engine 使用的 Futu/holdings 运营上下文是两个不同职责：后者继续决定现金、
持股和 Covered Call 可卖容量，但不得成为前者的隐式 fallback。

配置与账户绑定：

- `portfolio_distribution.provider` 默认 `none`；只有明确配置为
  `portfolio_management` 才查询 PM，不能通过服务探测自动启用；
- 地址复用 loopback-only `PORTFOLIO_SERVICE_URL`；模型输入中不出现 URL；
- PM 账户优先使用 `account_settings.<om-account>.holdings_account`，未配置时使用
  当前 OM 账户标签；
- 每个账户固定单独请求
  `/api/v1/distribution?account=<mapped_account>&by_asset=true&include_value=true&group_cash=false`，
  禁止 `accounts=all` 或跨账户聚合；
- `group_cash=false` 保留现金与货币基金的币种结构，由 OM 在校验后本地汇总。

PM 响应必须 fail closed 校验：

- `success` 严格为 `true`，`accounts` 严格等于请求的单账户；
- `freshness.status`、`freshness.trust_status` 和 `freshness.observed_at_utc` 完整；
- `by_asset` 是列表；每行具有资产 code、正式 `normalized_type`、currency、quantity
  和有限 `value`；当前 `portfolio.api.v1` producer contract 中 `value` 来自
  `market_value_cny`，prepared artifact 明确记录估值基准币种 `CNY`；
- 行内 `accounts` / `breakdown` 不得出现其他账户；PM 返回 `errors` 或账户范围含糊时
  整份输入不可用；
- OM 以校验后的 `value` 行求和并重新计算权重和组合总市值，不信任上游 `ratio` 或
  `total_value`；非空组合必须能形成有限、正值的组合总市值；OM consumer 以当前
  `portfolio.api.v1` 的 `value=market_value_cny` 行为作为显式 CNY 合同并用 fixture/
  integration test 固定。当前 vendored OpenAPI 尚未声明 `by_asset` 行结构与 value
  单位，因此不能把 schema 宽松接受误写成 producer 已承诺；强化 PM OpenAPI 是独立
  跨仓后续，不在本 work unit 静默修改；
- `fresh + trusted` 且 `by_asset: []` 是合法零资产，不得误报为取数失败。

run-scoped prepared artifact 至少绑定 schema、run、OM 账户、映射后的 PM 账户、provider、
账户配置 hash、payload hash、抓取时间、上游 observed time、freshness/trust、校验结果和
规范化资产行。它可以在本地保存计算所需的绝对值，但发送给模型的组合输入只包含：

- `asset_weights`：按资产 code 的当前权重；
- `currency_weights`：按原始资产币种汇总的当前权重；
- `cash_and_mmf_weight`：沿用 PM 正式资产分类汇总的现金和货币基金权重；
- source status、上游 observed time 和明确 gap；
- 第 8 节代码生成、与候选绑定的一张合约确定性投影。

不发送总 NAV、总资产绝对值、单项 market value、持股绝对数量、历史成本、真实账户
名称、PM 账户、broker、`accounts` 或 `breakdown`。

质量语义：

- `fresh + trusted`：组合输入完整，可参与所有动作校验；
- `stale + trusted` 或 `trust_status=partial`：可以连同时间与 gap 交给模型，但当轮
  最终动作最高为 `needs_review`；
- `unknown / untrusted / unavailable`，或 provider 为 `none`：不发送资产行，明确
  `portfolio_unavailable`，最终动作最高为 `needs_review`；
- 直接沿用 PM 的 freshness/trust/observed time，不在 OM 再发明第二套过期时钟。

v1 不包含行业维度，也不从网页、公司名称或模型推断行业。

### 7.3 开放期权持仓

开放期权持仓只认 OM 权威 SQLite ledger 生成的账户级
`prepared_option_positions_context`。Advice 消费 Tick 已加载和验证的对象，不直接查
SQLite，也不从 Futu、Feishu 或 legacy JSON fallback。有效输入同时满足：

- prepared manifest 的 run、账户、账户配置 hash 和 payload hash 与当轮 authority 一致；
- `context_status=available`；
- `decision_snapshot_status=trusted`；
- `open_positions_min` 存在且为列表；每行账户严格等于当前 OM 账户。

满足以上合同的 `open_positions_min: []` 才表示真实没有开放期权。字段缺失、hash/账户
不匹配、ledger 失败或 snapshot 不可信均为 `option_positions_unavailable`，不能被解释
为空，最终动作最高为 `needs_review`。

确定性代码先在账户内按以下经济合约键聚合并累加 `contracts_open`：

```text
symbol + option_type + side + strike + expiry + multiplier
```

所有开放仓位都参与账户级方向、类型和到期分布汇总。模型明细采用混合范围：候选标的
发送聚合合约明细，其他标的只发送账户级方向/类型/到期张数汇总；不得因 Prompt 展示
预算而忽略任何仓位的确定性计算。发送字段限于：

- 标的、方向、Call/Put、行权价、到期日、张数；
- 与当前候选的同标的、同义务、同到期及相邻到期窗口关系；
- 已由代码计算的结构性风险比例。

若 ledger 提供已验证的 strategy/combo group identity，代码可以在聚合前识别
`SP+LC`、`CC+LP` 等结构，并只发送简化结构标签与比例，不发送内部 group ID。没有
可靠身份时只能陈述同一账户内共同存在，不能推断两腿配对。

不发送 record/position/order/trade ID、premium、成本、note、broker、账户、opened
时间、raw payload、文件路径或个人身份字段。

### 7.4 外部证据

当轮账户相关标的是以下可搜索证券的去重并集：完整候选池、该账户 PM 持仓和该账户
开放期权底层。发送这个集合中每个标的的最新有效语义证据快照，包括：

- 证据 ID、主题、事实主张、事件状态和时间；
- 来源标题、发布者、URL、发布时间；
- 搜索覆盖状态、查询 cutoff 和 `last_checked_at`；
- 未解决事项与已解决事项的明确状态。

网页正文是非可信数据，不得成为 Prompt 指令。

### 7.5 证据索引冻结

Advice 运行开始时，从追加日志冻结一份当轮证据索引视图（每个相关标的一条
最新有效快照及覆盖状态），并记录其 hash。Advice 运行途中 Collector 的并发
更新不影响当轮输入；新写入的证据只在下一轮 Advice 生效。

冻结函数同时接收当前 symbol identity semantic hash，只选择相同 hash 的成功状态与
证据；旧身份记录继续保留用于审计，但不能进入当前 Advice。当前身份尚无匹配成功搜索
时明确为 `identity_changed_pending_refresh`。

## 8. 一张合约的确定性风险投影

候选阶段不存在实际下单手数，`可开 X 手`只是最大容量。AI Decision Advice 统一按
“新增一张合约”的边际影响判断，不假设用户会把容量全部开满，也不推荐手数。

每张 AI Advice 投影只拼接同一 OM 账户的候选、该账户映射后的 PM 分布和该账户的
prepared option context。这里的 Sell Put 组合总市值分母和 Covered Call 持股数量分母均来自
PM，仅用于 AI Decision Advice 的“新增一张后的组合影响”；Candidate Engine 原策略的容量分母仍按账户
配置从 Futu 或 holdings 运营上下文取得，两者不互相改写。
已有期权叠加只来自该账户的 SQLite ledger projection。不得用 Futu 运营持股替代 PM
分母，也不得借用其他账户的 PM 资产、持股或期权仓位来补全投影。

代码必须在模型调用前为每个候选生成独立 fact ID，并计算：

- 当前标的权重、候选币种权重、现金及货币基金权重；
- Sell Put：`strike × multiplier` 按同 run 已验证 OpenD 汇率换算为 PM 估值基准币种，
  再除以当前账户组合总市值，得到“一张指派资金暴露占组合比例”；
- Covered Call：`multiplier ÷ 当前该标的持股数量`，得到“一张潜在被叫走股份占当前
  持股比例”；
- 同义务叠加：Sell Put 只统计同标的已有 Short Put 的实际开放张数；Covered Call
  只统计同标的已有 Short Call 的实际开放张数；
- 结构关系：Long Call、Long Put 分别统计；只有可靠 group identity 才报告已验证
  `SP+LC`、`CC+LP` 等组合，不能用“同方向”模糊替代或猜配对；
- 到期集中：当前账户完全相同 expiry 的实际开放张数，以及前后 7 个日历日内（不含
  完全相同 expiry）的实际开放张数；新增一张后的对应值固定为当前值 `+1`。

7 天窗口是 v1 固定解释性指标，不做配置项、不成为 Candidate Engine 硬门槛。所有计数
按 `contracts_open` 求和，不按数据库行数或聚合行数计数。

组合 absolute values、持股数量和 FX 只在确定性代码内用于计算，不发送给模型。由于
指派、叫走和未来价格都不确定，禁止生成“执行后一张后的标的/币种权重”。投影只输出
上述当前权重和边际比例，不伪造 after-trade 权重。

候选缺少有效 strike、正 multiplier、对应 PM 总市值、Covered Call 当前持股数量或
必要 FX 时，不得默认 multiplier 为 1 或猜值；为该候选生成明确 `projection:*` gap，
该 scope 最终动作最高为 `needs_review`。投影是 Advice 参考事实，不回写候选、不增加
新硬门槛，也不推荐合约数量。v1 不建设组合级 Greeks 聚合。

## 9. 决策动作合同

### 9.1 正式动作

| 动作 | 含义 |
|---|---|
| `keep` | 维持 Candidate Engine 的策略排序第 1 候选 |
| `switch` | 改选同一策略允许范围内的另一合格候选 |
| `defer` | 本轮不新增该范围的仓位 |
| `needs_review` | 证据冲突、不完整或无法形成明确取舍，需要人工判断 |

`unavailable` 是 OM 运行状态，不是模型的投资动作。它表示搜索、身份、上下文、超时
或输出校验失败，不能被渲染成 `keep`。

### 9.2 策略范围

Sell Put：

- 在账户共享现金池的全部合格 Sell Put 候选中形成一个建议；
- 可以在整个已接受 Sell Put 池中 `switch`；
- 多个 Sell Put 候选共享现金容量，不能相加。

Covered Call：

- 按标的分别形成建议，可以同时建议多个标的；
- `switch` 只能发生在同一底层标的的合格 Covered Call 合约之间；
- 跨标的采用分别 `keep / defer`，不能用一个标的的 Call 替换另一个标的。

Sell Put 与 Covered Call 的收益率不得直接比较。

### 9.3 什么可以改变原始选择

外部资讯不是改选或暂缓的必要条件。以下任一情况有充分输入证据时，可以支持
`switch` 或 `defer`：

- 新增一张会明显加重标的、币种或到期日集中度；
- 与现有期权持仓形成明显的同向风险叠加；
- 可靠事件显著改变 Sell Put 指派风险或 Covered Call 被叫走机会成本；
- 多项因素单独不严重，但组合后形成明确风险。

反过来，外部消息必须结合账户当前暴露判断，不能脱离组合和期权持仓单独下结论。

### 9.4 两类策略的外部事件视角

Sell Put 重点判断候选到期前的：

- 下行跳跃风险；
- 指派概率上升后的持有损失；
- 与已有持股或 Short Put 的同向叠加。

Covered Call 重点判断候选到期前的：

- 强上行催化剂；
- 被叫走的机会成本；
- 与已有 Short Call 或减仓结构的重叠。

Covered Call 标的的下行消息可以作为持股风险报告，但不能仅凭下行消息声称 Short Call
本身更危险，也不能自动得出暂缓卖 Call 的结论。

### 9.5 建议组合的一致性检查

模型不能只逐项给出看似合理但互相冲突的建议。流程必须包含：

1. 每个候选先按新增一张评估；
2. Sell Put 保留一个暂定建议；Covered Call 每个标的最多保留一个暂定建议；
3. 把全部 `keep / switch` 组成假设同时执行的一张合约组合；
4. 再检查整体标的、币种、到期日和既有期权叠加；
5. 联合风险明显增加时，把部分建议改为 `defer`。

这里只协调风险，不跨策略比较收益，也不推荐数量。

### 9.6 联合冲突的取舍

不建立一个混合所有风险的统一分数。只有以下判断足够清楚时，AI 才能决定暂缓哪一项：

- 某项贡献了主要集中度或到期日叠加；
- 某项存在其他建议没有的可靠事件风险；
- 暂缓某项能明确降低组合风险。

如果不同风险维度互有优劣、无法明确取舍，则输出 `needs_review`，不能让模型随意决定
Sell Put 与 Covered Call 谁更重要。

### 9.7 动作证据要求

- `keep` 要求候选完整、组合为 `fresh + trusted`、期权 prepared context 完整，且
  第 7.4 节账户相关标的的外部证据覆盖完成；任一缺口都降级为 `needs_review`，不能
  改写为 `defer`；
- 搜索返回“无结果”但没有可审计覆盖证明时不能 `keep`；外部证据缺失本身不是负面
  事实，也不能单独支持 `defer`；
- `switch / defer` 必须引用精确内部事实或可靠外部证据。即使外部证据不可用，完整的
  组合、期权和一张合约投影事实也可以独立支持 `switch / defer`；
- 风险适用于全部合格候选时使用 `defer` 或 `needs_review`，不能任意改选；
- 组合或期权上下文缺失时最多只能为 `needs_review`；模型可以直接输出
  `needs_review`，无需先伪造其他动作；
- 未支持的 `switch / defer` 由 OM 降级为 `needs_review`；
- 候选 ID 不存在、策略不符或 Covered Call 跨标的改选时，拒绝该选择并降级为
  `needs_review`。

Advice 开始时由确定性代码冻结严格事实注册表，模型只能逐字引用其中的 ID：

| 前缀 | 事实范围 |
|---|---|
| `candidate:*` | baseline、允许候选池和候选正式指标 |
| `projection:*` | 第 8 节每个候选的一张合约投影与 gap |
| `portfolio:*` | 当前组合权重、质量状态和缺口 |
| `position:*` | 账户级期权汇总、候选标的合约及已验证结构 |
| `coverage:*` / `evidence:*` | 搜索覆盖与规范化外部证据 |
| `gap:*` | 明确的数据、身份、质量或冲突缺口 |

`internal_fact_refs` 只允许 candidate/projection/portfolio/position/coverage/gap；
`external_evidence_refs` 只允许 `evidence:*`。两类引用都必须在当前账户冻结注册表中解析，
不能引用另一账户、另一 run 或未冻结的记录。

引用最低要求：

- `keep`：baseline candidate、其 projection，以及账户相关标的的全部 coverage；
- `switch`：baseline、selected candidate，以及支持改选的风险事实；
- `defer`：至少一条直接支持暂缓的内部风险或可靠外部证据；
- `needs_review`：至少一条具体 gap 或冲突事实。

空引用、未知引用或无法支持动作时，单个动作降级 `needs_review`。run/account/input hash
绑定错误、scope 集合错误或输出结构无法建立时，整账户 Advice 为 `unavailable`，不能
靠启发式修补。

输出 scope 必须与确定性期望完全相等：有 Sell Put 候选时恰好一条 Sell Put decision；
Covered Call 对每个有候选的 symbol 恰好一条 decision；不得遗漏、重复或增加额外 scope。
首次输出不完整时，可在同一账户 30 秒总预算内做一次结构修复；仍不完整则整账户
`unavailable: incomplete_output`，禁止由 OM 合成缺失的 `needs_review` 决策。

用户文案必须使用“未发现足以改变本轮选择的可靠增量信息”，不能使用“安全、无风险、
检查通过”等表述。

### 9.8 合法零候选

Candidate Engine 合法返回零候选（`no_candidate`）时：

- 不创建 Advice 任务，不调用模型，不生成任何投资动作；
- 不伪造 `defer`；
- 仅由确定性代码在对应策略模块展示“本轮无可供 AI 评估的策略候选”。

## 10. 结构化输出合同

模型只返回一个严格 JSON 值。示意结构如下：

```json
{
  "schema": "ai_decision_advice.v1",
  "run_id": "<run-id>",
  "account_ref": "<anonymous-ref>",
  "market": "US",
  "input_bindings": {
    "candidate_snapshot_hash": "...",
    "portfolio_distribution_hash": "...",
    "option_positions_hash": "...",
    "fact_registry_hash": "...",
    "external_evidence_hash": "...",
    "external_evidence_run_id": "..."
  },
  "strategies": [
    {
      "strategy_family": "sell_put",
      "status": "completed",
      "decisions": [
        {
          "scope_symbol": null,
          "baseline_candidate_id": "...",
          "action": "switch",
          "selected_candidate_id": "...",
          "rationale": {
            "risk_mechanism": "...",
            "candidate_effect": "...",
            "decision_reason": "..."
          },
          "internal_fact_refs": ["..."],
          "external_evidence_refs": ["..."]
        }
      ]
    }
  ]
}
```

约束：

- `account_ref` 是每次新模型调用生成的密码学随机不可预测引用，不是账户标签；
  禁止使用 `hash(run_id + account)` 这类可对低熵账户标签离线枚举的构造。
- `input_bindings` 必须逐项等于 OM 冻结值；`fact_registry_hash` 覆盖模型可引用的
  candidate/projection/portfolio/position/coverage/evidence/gap 注册表；
- `keep` 的 `selected_candidate_id` 必须等于 baseline；
- `defer / needs_review` 不得伪造已选候选；
- `unavailable` 由 OM envelope 表达，不要求模型生成；
- Schema 中不包含 confidence、订单数量、参数修改或执行字段；
- 模型生成的是结构化原因，用户文案由 renderer 生成；
- 原始模型输出、校验结果和最终接纳结果分别保留，禁止静默修补。

如果 Advice 输出格式无效，可以在 30 秒账户总预算内进行一次结构修复；仍无效则为
`unavailable`，不做启发式解析。

## 11. Prompt 管理

Prompt 是版本化代码，不放数据库，不允许运行时编辑。管理模式可以复用现有 Copilot
“Scene 清单 + 有序 Markdown 片段 + 编译 hash”的机制，但不能复用 Copilot Prompt
内容或通用 Scene。

### 11.1 两套 Prompt Pack

External Evidence Prompt Pack：

- 搜索范围；
- 身份约束；
- 来源等级；
- 去噪规则；
- 严格证据 JSON；
- 不得输出账户建议。

Decision Advice Prompt Pack：

- 共同决策边界；
- 不得改变硬门槛、排序、价格或数量；
- 组合与期权风险解释规则；
- Sell Put 专属规则；
- Covered Call 专属规则；
- 严格 Advice JSON。

共享框架负责模型调用、审计、超时和验证；策略适配器分别提供目标、允许动作和校验规则。
以后 Combo Yield、Close Advice 应各自增加适配器，不能把全部策略写进一个万能 Prompt。

### 11.2 版本与审计

- `prompt_version` 与 `output_schema_version` 分别版本化；
- 动态候选、组合、持仓和证据作为 JSON 数据传入，不插入静态指令文本；
- 每次运行记录 fragment 列表、编译 SHA-256、模型版本和代码版本；
- 运行记录不复制另一份可编辑 Prompt 文本；Git 中的版本化片段是 Prompt 真源；
- Prompt 修改必须经过代码提交、测试和发布；回滚跟随软件版本。

### 11.3 Prompt injection 边界

- Collector 只有 `web_search`，没有 OM 工具；
- Advice 节点没有工具，只接收冻结 JSON；
- 网页中的指令、角色声明、密钥请求和策略覆盖文本一律视为非可信数据；
- 外部内容不能请求内部数据、配置、命令或文件；
- 输出必须通过 JSON Schema、候选 ID 和事实引用校验。

## 12. 持久化与唯一真源

### 12.1 候选事实

既有封存文件继续是策略候选唯一真源：

```text
output_runs/<run_id>/accounts/<account>/state/opening_candidate_snapshot.json
```

AI Decision Advice 不能在自己的 JSONL 中复制一份可独立演化的候选事实。

### 12.2 账户级 prepared 输入

PM 组合分布的 run-scoped authority：

```text
output_runs/<run_id>/accounts/<account>/state/prepared_portfolio_distribution.v1.json
```

它是 Advice 专用的不可变 prepared envelope，固定分为 `authority`、`payload` 和
`integrity`：`authority` 至少含 schema、run、OM 账户、映射后的 PM 账户、provider、账户
配置 hash、抓取时间、校验状态和 unavailable 原因；`payload` 包含上游 observed time、
freshness/trust、估值基准币种、规范化资产行和本地派生总计；
`integrity.payload_sha256` 只覆盖 canonical JSON `payload`，不形成自引用。Tick handoff
另以完整文件 bytes 的 SHA-256 绑定 artifact，该值不写回文件本身。loader 必须同时校验
外部 artifact hash（存在时）、payload hash、run/account/config/provider 和 schema。

它不是 PM 数据库的复制真源；下一 run 重新从 PM 读取。provider 为 `none` 或 PM 不可用
时也写确定性 unavailable envelope，避免“文件不存在”被误读为“空组合”。

开放期权继续复用既有：

```text
output_runs/<run_id>/accounts/<account>/state/prepared_option_positions_context.v1.json
output_runs/<run_id>/accounts/<account>/state/option_positions_context.json
```

前者是 manifest authority，后者是其 hash 绑定的 payload；唯一上游真源仍是 SQLite
ledger projection。Advice 不另建 option JSON、CSV、DB 或 fallback。

### 12.3 外部证据

匿名观察集合和公开身份快照分别是：

```text
output_shared/state/ai_decision_advice/observation_set.json
output_shared/state/ai_decision_advice/symbol_identity_snapshot.json
```

二者均为可重建的原子快照：前者只交接匿名有序 symbol 集，后者绑定公开身份；都不是
账户持仓或外部证据的第二真源。

共享、追加写入的目标真源：

```text
output_shared/state/ai_decision_advice/external_evidence.jsonl
```

同一个 JSONL 保存：

- batch 的响应/输出内容 hash、白名单 token usage 和 `web_search_call` 聚合计数；
- 按标的规范化的语义证据记录；
- 查询、cutoff、来源、状态、内容 hash 和身份绑定；
- 失败和部分成功状态。

每条 `symbol_evidence` 有稳定 evidence ref，并绑定 symbol、identity semantic hash 和产生
它的 evidence run。每条成功 `symbol_status` 保存该次完整 active evidence refs 与 semantic
snapshot hash；最新索引只按这份成员清单重建。增量成功可以显式延续上一 snapshot，
全量成功可以替换或清空；store 本身不得因历史行仍存在而隐式延续证据。

Provider 原始响应、搜索 query/call ID 和完整网页正文均不落盘。标的记录通过
`evidence_run_id` 关联同批次的内容无关审计摘要。

“最新标的证据”只是从追加日志推导的可重建索引或缓存，不是第二真源。不得再维护 SQLite、
CSV 或另一份可独立修改的 latest 数据。

### 12.4 Advice 结果

账户和 run 绑定的目标真源：

```text
output_runs/<run_id>/accounts/<account>/state/ai_decision_advice.jsonl
```

每条记录绑定：

- run、匿名 account ref、市场；
- 候选、PM 组合分布、开放期权、事实注册表和语义证据 hash；
- evidence run 与实际 cutoff；
- Prompt、Schema、模型和代码版本；
- 模型响应/输出内容 hash、白名单 usage、验证结果、接纳或降级后的正式动作；
- 如复用旧建议，记录 `reuse_of_advice_id` 和新的证据覆盖绑定。

Daily Brief 与 Agent 必须读取这同一份正式结果。v1 不新增 DB、CSV 或第二份历史结果库。

## 13. 触发、复用和超时

### 13.1 Advice 触发

AI Decision Advice 覆盖：

- 固定决策简报；
- 新增策略候选提醒。

固定简报展示 Sell Put 和 Covered Call 的完整聚合建议。新增候选提醒只展示受影响策略
模块，但模型仍基于完整候选、组合和开放期权持仓判断。

Daily Brief service 已经加载并验证账户级 prepared context 后，必须把同一对象显式传给
Advice；Advice 不得再次按路径读取。开始调用模型前冻结：当前候选及预期 scopes、PM
组合 envelope、期权 context、每候选投影/事实注册表，以及仅与该账户候选和持仓相关的
证据索引。

新增候选提醒本身属于正常监控回执，可以承载 `unavailable` 状态：Advice 超时或
失败时不阻断、不推迟候选提醒，提醒按既有节奏发出并如实显示 AI建议未完成。

每个账户 Advice 总等待预算为 30 秒，对固定简报和新增候选提醒两条路径一致：
两条路径都当轮运行或复用 Advice，超时或失败即 `unavailable`，不能阻断
Candidate Engine 的原始回执，也不推迟提醒发送。复用条件（13.2）在两条
路径相同。

### 13.2 复用条件

只有以下内容全部未发生实质变化时才能复用：

- 候选快照；
- PM 组合分布内容及质量状态；
- 开放期权持仓；
- 一张合约投影与事实注册表；
- 外部证据语义内容和覆盖状态；
- Prompt、模型和输出 Schema 版本。

仅 `last_checked_at` 更新、且搜索确认没有新增证据时，不重复调用 Advice 节点。证据超过 8
小时或覆盖状态降级后，旧建议立即失效。

### 13.3 不触发搜索的读取面

- 正常监控编排先生成并持久化当轮 Advice，renderer 再读取该正式记录生成 Daily Brief；
- Agent 和其他只读面只读取已持久化的证据与 Advice；
- Agent 查询不触发联网搜索或 Advice 刷新；
- `./om` 不公开分发 `ai-evidence-collector`；systemd timer 只调用内部
  managed-service entrypoint，测试也从该内部应用边界注入 runner；
- v1 不提供手动刷新 CLI；
- 后续确有操作需求时，另行设计受控 operator CLI，不能把刷新能力塞入模型工具。

## 14. 通知与变化语义

AI Decision Advice 跟随现有监控回执，不建立独立通知工作流。

1. 外部证据刷新本身不发消息；
2. 下一次正常监控运行生成或复用 Advice；
3. `维持 / 改选 / 暂缓 / 需人工判断` 的实质变化进入现有 Daily Brief diff；
4. 仍使用既有 envelope、去重、发送和 delivery confirmation；
5. 证据时间、来源、措辞或 cache reuse 变化不单独触发通知；
6. 风险解除并从改选/暂缓恢复为维持，属于应通知的实质变化；
7. 从 `needs_review` 恢复为 `keep` 同样属于应通知的实质变化；
8. 动作仍为 `switch`，但 `selected_candidate_id` 从一个合约变成另一个合约，同样属于
   应通知的实质变化；
9. Collector 失败或 Advice unavailable 不建立独立失败通知，但在因其他原因生成的回执中
   必须如实显示。

输入未变化时复用 Advice，不能因为模型措辞随机变化制造通知噪声。

### 14.1 diff 实现合同

AI Decision Advice 的动作状态进入 Daily Brief 的规范化结构
（`normalize_daily_decision_brief`），作为独立 `ai_decision_advice` 段，按
策略记录当前动作。既有 `diff_daily_decision_briefs`
（`domain/domain/daily_decision_brief.py`）扩展比较该段：

- `keep / switch / defer / needs_review` 之间的任意迁移均为 material 变化；
- 相同动作下，正式 `selected_candidate_id` 改变也是 material 变化；
- `unavailable` 的出现、消失或原因变化不产生 material diff（只在回执中如实
  显示）；
- diff 逻辑留在 domain 层，application 层不得平行实现 AI 变化检测。

## 15. Daily Brief 用户合同

### 15.1 信息层级

AI建议在每个策略子模块内聚合展示，不单独建立一个顶层大区块，也不穿插到每条候选
之间。

固定简报目标结构：

```markdown
# OM · 决策简报 · lx
状态｜……
市场｜……
行情及账户数据｜截至……

变化｜……

## Sell Put

### AI建议
结论｜……
原因｜……
外部信息｜截至……

### 策略候选
**策略排序 1｜……**
收益｜持有期净收益…… · 门槛年化……

## Covered Call

### AI建议
汇总｜维持 2 个标的，暂缓 1 个标的。
- AAPL｜维持策略排序 1
- MSFT｜改选策略排序 2
- NVDA｜本轮暂缓新开仓

### 策略候选
**策略排序 1｜AAPL｜……**
……

## 持仓处理
**1｜……｜建议平仓**
参考｜……

## 资金
……

## 提醒
……
```

规则：

- 策略标题只写 `Sell Put` 或 `Covered Call`，不把标的写进标题；
- Covered Call 的多标的 AI建议在模块内聚合展示；
- 候选列表保持 Candidate Engine 原始顺序；
- 候选使用“策略排序 1 / 2 / 3”，不再使用容易与 AI 冲突的“首选/备选”；
- AI 内容只出现在聚合区，不在候选行重复标记；
- 正常成功不显示冗余的 `AI状态｜已完成`；
- 外部信息时间放在对应 AI建议模块内，不用可能误导的全局 cutoff；
- 只有受影响模块显示失败，不让一个标的失败使整份简报看起来失效。

### 15.2 正常与异常文案

`keep`：

```text
结论｜维持策略排序 1。
原因｜综合当前组合、期权持仓和截至 X 的可靠外部信息，暂无足以改变本轮选择的因素。
```

`switch`：

```text
结论｜建议改选策略排序 2：<完整合约>。
原因｜<风险机制>；<为什么改选能避免或降低该风险>。
```

`defer`：

```text
结论｜建议本轮暂缓新开仓。
原因｜<风险机制和当前候选影响>。
```

`needs_review`：

```text
结论｜信息不完整或存在冲突，需要人工判断。
原因｜<具体的数据缺口或冲突>。
```

`unavailable`：

```text
AI建议未完成；以下仅展示策略原始排序，不代表已经完成综合判断。
```

合法零候选：

```text
本轮无可供 AI 评估的策略候选。
```

候选基础数据 blocked 时，继续优先显示既有阻断结论，不创建或展示伪 Advice。

### 15.3 原因文案

用户回执不堆砌“2 张变 3 张”一类浅层事实。底层数值保留在结构化审计和 Agent 查询中；
通知只解释：

1. 风险如何形成；
2. 它为什么影响当前候选；
3. 为什么改选或暂缓更合适。

原因一般不超过两句，使用简洁、常用中文。只有某个数值本身达到明显异常程度、对结论
至关重要时才展示。

禁止使用“边际投影、联合风险因子、通过 AI 检查、安全、无风险”等内部或误导性用词。

### 15.4 来源展示

- `keep` 保持简短，不展开来源列表；
- `switch / defer / needs_review` 最多展示 3 个真正支持结论的来源；
- 每个来源显示已规范化的单行标题、发布者、日期、可见域名和链接；
- URL 只允许经解析和重新序列化的 `https` 链接；标题和发布者移除控制字符、
  换行和 Markdown 结构，不得改变回执层级或隐藏实际目标域名；
- 未经可靠来源证实的单一消息不展示；
- 完整事实引用和原始证据只在结构化读取面提供。

### 15.5 候选与持仓格式修正

Daily Brief 候选展示必须补齐正式排序指标：

- 主展示“持有期净收益”；
- 年化值如保留，明确写成“门槛年化”；
- 不能只展示年化值，却按非年化期间收益排序。

持仓处理从 Markdown 表格改为与策略候选相同的逐项列表。展示范围不变：仍只展开需要
动作或事实核查的持仓，非行动项继续只计入汇总。

### 15.6 展示预算与候选引用

固定简报存在展示上限时，以下候选必须优先展示：

1. 策略排序第 1；
2. AI 实际引用的候选；
3. 本轮发生变化的候选。

AI 引用候选即使超出普通 top N，也不能被折叠；其真实策略排序不得改变。其他候选以
“另有 X 个策略候选未展开”汇总。

新增策略候选提醒中，如果 AI 建议继续选择一个并非本次新增的旧候选，不重复把旧候选
塞进“新增策略候选”列表；AI 聚合区写出该旧候选的完整合约，完整排序留在固定简报和
Agent 查询中。

## 16. Agent 读取面

v1 不新增 Agent 工具。既有 `daily_decision_brief_read` 的结构化结果扩展
`ai_decision_advice`，并读取与通知相同的正式 Advice 产物。

Agent 可读取：

- 正式动作和引用候选；
- 四类业务输入对应的五个完整性 hash（候选、PM 组合、期权持仓、事实注册表、外部
  证据）、外部证据运行标识与实际 `evidence_as_of`；
- 完整内部事实引用；
- 外部证据和来源；
- 运行、校验、复用和降级状态。

读取时以 Daily Brief 中的 `advice_record_id`、run、账户、市场、状态、零候选标记、复用
标记和证据截止时间，唯一匹配同一 run/account 的
`state/ai_decision_advice.jsonl`。缺失、重复、结构损坏或身份不一致时，结构化结果明确
返回 formal record unavailable，不从简报文案拼造正式 actions，也不读取其他 run 或账户
补齐。正式结果只暴露白名单 bindings、actions、fact/evidence refs、validation、versions
和 reuse 关系；不暴露真实 `account_ref`、usage、原始响应审计或 Prompt 正文。

`candidate_rank_explain` 继续只解释 Candidate Engine 的确定性筛选和排序，不混入 AI
观点。Agent 查询保持只读，不触发搜索、扫描、模型调用、通知或状态写入。

## 17. 配置合同

用户配置：

```yaml
ai_decision_advice:
  enabled: true
  portfolio_distribution:
    provider: portfolio_management  # none | portfolio_management
```

规则：

- 一个开关同时控制 External Evidence Collector 和 AI Decision Advice；
- `ai_decision_advice` 和 `portfolio_distribution` 只要出现就必须是 object；`[]`、空字符串、
  `0` 等 falsey 非 object 也必须拒绝，不能被 `or {}` 静默吞掉；
- 默认值为 `false`；把它显式改为 `true` 即表示操作者同意按第 18 节最小数据合同向
  DeepSeek 传输数据；
- `portfolio_distribution.provider` 默认 `none`。`portfolio_management` 是唯一 v1
  外部 provider；PM 未安装或运行失败不阻断 Candidate Engine，而是产生明确
  `portfolio_unavailable` 并使动作最高为 `needs_review`；
- 不保留 `ai_interpretation`、`ai_strategy_advice` 等旧字段或兼容别名；
- 不提供每账户开关；
- 模型、4 小时间隔、5 分钟搜索预算、批大小、并发和 30 秒 Advice 预算均为 v1 固定合同；
- 静态配置校验不读取运行时秘密；`enabled: true` 时 Collector/Advice 启动边界若缺少 `llm.deepseek.api_key` 会失败关闭；
- `enabled: false` 时不运行两个阶段，也不在回执中创建空 AI 区块；
- API key 只从 SecretProvider 读取，不能写入 YAML、JSONL、Prompt 或模型输入；安全后端不会回退到 env。

## 18. 隐私与安全

发送给 DeepSeek 的最小数据合同：

External Evidence Collector：

- 只有公开 symbol、市场、交易所和公司名称；
- 没有账户标识、账户到 symbol 的来源关系、持仓数量或候选指标。

AI Decision Advice：

- 匿名 `account_ref`；
- 候选 ID、合约和必要指标；
- PM 组合的 asset/currency/cash-and-MMF 权重、质量状态和投影比例；
- 候选标的开放期权的 symbol/side/type/strike/expiry/contracts 与已验证结构关系；
- 其他开放期权的账户级方向/类型/到期汇总；
- 严格 fact ID 和明确 gap；
- 已规范化外部证据。

禁止发送：

- 真实账户标签、PM/holdings account、Futu account id；
- 总 NAV、总资产、单项 market value、持股绝对数量、历史成本；
- PM `accounts`/`brokers`/`breakdown`；
- record/position/order/trade/group id、premium、note、raw payload、文件路径；
- token、cookie、authorization、webhook 或 API key；
- 任何个人身份信息。

Provider 原始 Responses 不落盘；只记录响应/输出内容 hash、输出长度、白名单 token
usage、按标的的搜索完成绑定、引用绑定结果和内容无关审计计数。实际
query 只含公开标的身份，可与 cutoff 一起审计；provider 原始响应、call ID、完整网页
正文和未经绑定的模型 source 不落盘。JSONL 与身份快照目录使用 `0700`，文件使用
`0600`，并拒绝最终路径符号链接。

## 19. 运行状态与可观测性

内部至少记录：

- 每账户 PM provider、映射结果、响应校验、freshness/trust/observed time、prepared
  manifest/hash 和 unavailable reason（日志不记录真实账户值或绝对资产值）；
- 期权 prepared manifest/hash/decision snapshot 状态、真实空持仓与不可用的区分；
- 投影完成数、gap 原因、同到期/相邻到期实际张数；
- 观察集合大小、排队顺序、完成/未完成标的；
- 搜索耗时、批次并发、5 分钟预算消耗；
- 来源数量、去重数量、证据年龄和覆盖状态，以及精确 query、cutoff、Prompt/Schema/
  model/code version；
- JSON Schema 首次成功、修复成功和最终失败；
- Advice 30 秒耗时、cache reuse 和 provider usage；
- 各动作数量、validator 降级数量和原因、scope completeness 与 fact-ref 校验结果；
- Prompt、Schema、模型和代码版本。

这些是运行和质量指标，不是用户可见的置信度，也不自动驱动策略参数调整。

## 20. 评估与上线边界

- 不增加独立 Shadow 运行模式；
- 配置关闭时完全不运行，开启后直接显示 AI建议；
- 上线前用固定样本、历史快照和对抗输入做回放测试；
- 每次建议完整留痕，后续可以离线比较 AI建议与策略原始选择的结果；
- 实际交易可以在后续离线评估中与当时建议关联，但 v1 不要求用户反馈；
- 评估结果不能自动修改 Prompt 或策略参数，任何改变都需要人工批准、测试和发布。

## 21. 验收矩阵

| 场景 | 预期结果 |
|---|---|
| 候选快照无效或身份不一致 | 不创建 Advice 任务，保留既有阻断语义 |
| 合法零候选 | 不调用模型、不生成投资动作，仅确定性展示“无可供 AI 评估的策略候选”，不伪造 `defer` |
| 四类输入完整且证据覆盖完成 | 可以输出 `keep / switch / defer / needs_review` |
| PM provider 为 `none`、服务缺失或请求失败 | Candidate Engine 正常继续；prepared 组合为明确 unavailable，不传资产行，动作最高 `needs_review` |
| OM 账户运营来源为 Futu，且 PM provider 可用 | Candidate Engine 容量继续使用 Futu；AI 战略组合与投影分母只使用该账户映射后的 PM 分布，不混用两套持股 |
| PM 返回的账户不是映射后的当前账户，或行内混入其他账户 | 整份组合 fail closed；禁止跨账户行进入模型 |
| PM 为 `fresh + trusted` 且合法零资产 | 组合是完整空集合，不误报取数失败；没有正总市值的比例投影以 gap 表达 |
| PM 为 `stale + trusted` 或 `partial` | 携带 observed time 与 gap，可供理解但动作最高 `needs_review` |
| PM 为 `unknown / untrusted / unavailable` | 不传资产行，动作最高 `needs_review` |
| Futu 账户持仓可用但 PM 不可用 | 不用 Futu 冒充 PM；Candidate Engine 容量不受影响，Advice 组合明确 unavailable |
| 同一 run 以 `prefetch_done=true` 重入 | 只恢复并校验该 run 的 PM envelope，不重新调用 PM；缺失或损坏仅使 Advice 组合 unavailable |
| PM payload 或完整 artifact hash 不匹配 | fail closed，不把被修改的 authority/payload 交给 Advice；Candidate Engine 继续 |
| prepared option context 有效且 `open_positions_min=[]` | 视为真实无开放期权，期权上下文完整 |
| option manifest/hash/account/status 不匹配或 ledger 失败 | 不得解释为空；期权上下文 unavailable，动作最高 `needs_review` |
| 其他账户存在期权仓位 | 不进入当前账户明细、汇总、投影或模型输入 |
| 多账户共用同一 SQLite ledger 文件 | 每个账户先生成并校验独立 prepared projection；不得因物理文件相同而合并逻辑持仓 |
| 同一经济合约存在多行 | 按 `contracts_open` 聚合；到期和同义务风险按张数而非行数计算 |
| 候选 expiry 前后 7 日已有仓位 | 分别显示 exact 与 near-window 当前张数，并给出新增一张后的 `+1` 事实 |
| Sell Put 候选投影完整 | 计算一张指派名义金额占当前 PM 组合市值比例，不伪造指派后的权重 |
| Covered Call 候选投影完整 | 计算一张潜在叫走股份占当前持股比例，不伪造叫走后的权重 |
| strike/multiplier/FX/总市值/CC 持股数量缺失 | 不猜值、不默认 multiplier=1；生成 projection gap，scope 最高 `needs_review` |
| 可审计、无错误、覆盖明确的搜索未发现任何事件 | 覆盖完整，可输出 `keep` |
| 组合或开放期权上下文缺失 | 不能 `keep`，最多 `needs_review` |
| 单标的身份无法建立 | 仅该标的 `identity_unavailable` |
| 两次刷新之间首次出现的新标的 | `unavailable: no_evidence`，不能 `keep` |
| 单标的搜索失败，旧证据不超过 8 小时 | 可使用旧证据，显示实际时间 |
| 证据超过 8 小时 | `unavailable: evidence_stale`，不能 `keep` |
| Advice 运行途中 Collector 写入新证据 | 当轮使用开始时冻结的证据索引，不受影响 |
| US 与 HK Tick 先后或并发发布 observation | 只替换各自 market partition，不丢失另一市场；并发写无 lost update |
| observation snapshot 损坏 | publisher/collector fail closed，不用当前市场局部集合覆盖为完整跨市场集合 |
| 标的语义身份变化但尚未刷新 | 旧 identity hash 证据排除，状态为 `identity_changed_pending_refresh`，不能复用 |
| 单一未经证实消息 | 完全忽略，不触发动作或人工判断 |
| 模型遗漏本批某标的，或无可归因的 completed web-search call | 该批失败，不为遗漏标的补空证据，不写 completed |
| 增量搜索没有新证据 | 显式延续上一成功 active refs，semantic hash 不变；不会把历史全集重新猜成当前 snapshot |
| 24 小时全量核对返回零证据 | active refs 明确清空，历史 evidence 仍留在日志但不进入当前 Advice |
| active evidence ref 缺失、跨 symbol/identity 或 hash 不符 | `unavailable: evidence_snapshot_invalid`，不返回部分证据 |
| 证据 URL 无法绑定 provider 原生引用 | 丢弃该证据行；不相信模型自报的来源字段 |
| 来源标题含换行/Markdown，或 URL 不是 HTTPS | 展示文本展平并转义；非 HTTPS 链接不进入正式证据 |
| 可靠 Sell Put 下行事件 | 结合持仓暴露后可 `switch / defer`，必须有事实引用 |
| Covered Call 出现强上行催化 | 评估被叫走机会成本，不得脱离持股事实下结论 |
| Covered Call 只有下行消息 | 可报告持股风险，不能自动声称 Short Call 更危险 |
| AI 引用被拒绝候选 | 校验失败并降级 `needs_review` |
| Covered Call 跨标的 `switch` | 校验失败并降级 `needs_review` |
| 外部证据不可用，但完整内部事实明确支持改选/暂缓 | 可以 `switch / defer`；不得把“没搜到”本身当支持事实 |
| `keep` 的组合、期权、投影或 coverage 任一不完整 | 降级 `needs_review`，不得自动改成 `defer` |
| 模型遗漏、重复或增加一个预期 scope | 同一 30 秒总预算内修复一次；仍不完整则整账户 `unavailable: incomplete_output` |
| 模型引用未知 fact ID 或动作无支持事实 | 单个动作降级 `needs_review`；binding/scope 结构错误则整账户 unavailable |
| 分项建议联合后形成风险叠加 | 调整部分为 `defer`；无法取舍则 `needs_review` |
| 只有搜索时间更新、无新增证据 | 复用 Advice，不重复调用模型 |
| Prompt、Schema、候选或持仓 hash 改变 | 禁止复用，重新生成 Advice |
| Collector JSON 首次无效 | 预算内修复一次；仍无效则批次失败 |
| Advice JSON 无效或超时 | `unavailable`，原始策略回执继续 |
| 网页包含 prompt injection | 当作非可信数据，不能改变指令或访问内部能力 |
| Advice 实质动作改变、候选未变 | 进入既有 Daily Brief diff 并随监控回执通知 |
| 动作仍为 `switch`，但正式 selected candidate 改变 | 属于实质变化，进入 diff 并通知 |
| `needs_review` 恢复为 `keep` | 属于实质变化，进入既有 Daily Brief diff 并通知 |
| 只有来源或措辞改变 | 不发送新通知 |
| AI 改选到折叠候选 | 固定简报强制展示该候选及真实策略排序 |
| 新增候选提醒仍建议旧候选 | AI 区写完整旧合约；新增列表不重复旧候选 |
| 新增候选提醒遇到 Advice 超时或失败 | 提醒按既有节奏正常发出，承载 `unavailable` 显示 |
| 正常 `keep` | 简洁易懂，不展开来源、不写“安全/通过” |
| 持仓展示 | 使用逐项列表，不使用 Markdown 表格 |
| 候选收益展示 | 主显示持有期净收益，年化明确标为门槛值 |
| 用户运行 `./om ai-evidence-collector` | 公共命令不存在；只有 managed service 内部入口可执行采集 |

## 22. 后续扩展

共享框架未来可以增加以下适配器：

- Combo Yield 候选 Advice；
- Close Advice 的 AI 决策建议；
- 组合层 Advice；
- 持仓层 Advice。

这些只是扩展方向，不属于 v1。实施时必须分别定义目标、允许动作、输入真源和校验规则，
并升级相应契约；不能因为共享名称就把 Sell Put / Covered Call 动作语义直接套用过去。

未来基于历史回放调整参数的 workflow 应使用独立的策略优化名称、审计和批准流程，不能
把参数修改能力添加到 AI Decision Advice。
