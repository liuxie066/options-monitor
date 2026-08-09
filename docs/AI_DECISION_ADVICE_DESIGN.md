# AI Decision Advice 设计合同

> 状态：已确认的 v1 目标设计（2026-08-09）
>
> 权威范围：Sell Put / Covered Call 候选生成后的 AI 决策建议、外部证据采集、
> Daily Brief 展示、运行审计和失败语义
>
> 实施状态：本文是后续实施与验收依据，不表示当前代码已经具备这些能力

AI Decision Advice 是固定工作流中的建议层。它把策略候选、组合分布、开放期权
持仓和可靠外部证据放在同一个冻结上下文中，为当前一轮 Sell Put / Covered Call
候选给出可审计建议。

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
| Python / 配置标识 | `ai_decision_advice` |
| v1 输出契约 | `ai_decision_advice.v1` |
| 用户回执名称 | `AI建议` |

### 1.1 v1 目标

v1 只实现两类当前候选决策：

- Sell Put：在账户共享现金池的合格候选中形成一个建议；
- Covered Call：按标的分别判断，可以同时形成多个建议。

组合分布和开放期权持仓在 v1 中是决策输入，不单独生成“组合建议”或“持仓建议”。

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

## 3. 两阶段架构

功能拆为两个独立阶段：

```text
公开观察标的
  -> External Evidence Collector
     -> DeepSeek Responses + native web_search
     -> 共享、追加写入的外部证据

同一 run 的封存策略候选
+ 冻结组合分布
+ 冻结开放期权持仓
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
- 密钥：环境变量 `DEEPSEEK_API_KEY`。

参考官方文档：[DeepSeek Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)。

模型选择不跟随 OM Copilot 的 `active_model` 切换。内部仍保留窄的 provider adapter
边界，但 v1 不暴露模型、搜索并发、超时或 token 预算配置。

### 4.1 当前代码差距

本文定义的是目标能力，当前实现尚未对齐：

- `src/application/llm_provider_registry.py` 当前把 DeepSeek 声明为
  `chat_completions`；
- `src/application/copilot/model_client.py` 的 Responses 工具映射只处理 function
  tools，不能原样投影 DeepSeek 原生 `web_search`；
- `src/infrastructure/openai_responses.py` 当前没有本功能需要的严格结构化输出和
  DeepSeek 搜索响应审计合同。

后续实施必须补齐独立的 DeepSeek Responses 能力，不能声称复用现有 Copilot client
即可获得原生搜索。

### 4.2 为什么不引入 Pi SDK

Pi SDK 的当前 DeepSeek provider 仍以 OpenAI-compatible Chat Completions 为主要边界，
且其 TypeScript/Node Agent 层会与仓库现有 Python 模型、工具和运行治理重复。v1 直接
扩展窄的 Python Responses adapter，改动更小、所有权更清楚。

## 5. 外部证据观察集合

主动观察集合是以下标的的并集：

1. 配置中的 Sell Put 扫描标的；
2. 当前普通股票持仓标的；
3. 开放期权持仓的底层标的。

所有标的先规范化为 canonical symbol，再跨账户、Sell Put、Covered Call 去重。同一
底层标的的公开证据只采集一次并共享；账户级建议仍分别生成。

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

## 6. 外部证据采集合同

### 6.1 调度与预算

- 每 4 小时刷新一次，全天运行，每日最多 6 次；
- 服务启动时如不存在有效证据快照，立即补刷；
- 一次刷新总预算为 5 分钟，不是每个标的 5 分钟；
- 每批最多 5 个标的；
- 同时最多运行 2 个批次；
- 不提供用户手动刷新入口或 Agent 搜索工具。

搜索优先级固定为：

1. 有开放期权持仓的底层标的；
2. 最近一次已接受的 Sell Put / Covered Call 候选；
3. 当前普通股票持仓；
4. 其余配置扫描标的。

同一优先级内，最久未成功刷新的标的优先。未完成标的进入下一轮同层级队首，防止
观察集合较大时长期饥饿。

### 6.2 增量与全量核对

- 新标的首次搜索：最近 30 天，加仍在持续的历史事项；
- 4 小时刷新：从上次成功 cutoff 开始做增量搜索；
- 每 24 小时：重新核对最近 30 天和此前未解决的重要事项；
- URL 和内容指纹去重；
- 全量核对可以确认事项已经解决或失效，并生成新的语义证据快照；历史记录仍追加保留。

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

### 6.5 默认忽略的内容

- 没有新增事实的单纯涨跌报道；
- 常规分析师评级和目标价；
- 技术分析、市场情绪或社交热度；
- 重复聚合内容；
- 与候选持有期无关的长期叙事。

### 6.6 搜索完成与失败

每个标的独立产生结果。刷新超时或部分失败时：

- 已成功标的立即发布；
- 未完成标的保留上一份成功快照；
- 旧快照不超过 8 小时时可以使用，并显示实际证据时间；
- 超过 8 小时则为 `unavailable: evidence_stale`，不能输出 `keep`；
- 单个标的失败不影响其他标的；
- 采集失败只记录运行状态，不单独发送飞书消息。

搜索成功但没有发现新证据时，更新 `last_checked_at`，语义证据 hash 保持不变，允许
复用既有 AI 建议。

### 6.6.1 证据覆盖完成

“证据覆盖完成”只要求以下三点同时成立：

1. 该标的的搜索范围和查询 cutoff 可审计；
2. 本次或最近一次成功搜索没有执行错误；
3. 证据快照年龄不超过 8 小时。

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

只发送与决策有关的匿名化分布：

- 标的、行业和币种权重；
- 关键集中度；
- 现金和货币基金沿用现有正式资金口径；
- 每个候选新增一张后的确定性分布变化。

不发送总 NAV、总资产绝对值、历史成本、真实账户名称或 broker account id。

### 7.3 开放期权持仓

开放期权持仓只认权威 SQLite ledger 投影。发送字段限于：

- 标的、方向、Call/Put、行权价、到期日、张数；
- 与当前候选的同标的、同方向、同到期窗口关系；
- 已由代码计算的结构性风险比例。

不发送订单 ID、成交 ID、文件路径或个人身份字段。

### 7.4 外部证据

发送每个相关标的的最新有效语义证据快照，包括：

- 证据 ID、主题、事实主张、事件状态和时间；
- 来源标题、发布者、URL、发布时间；
- 搜索覆盖状态、查询 cutoff 和 `last_checked_at`；
- 未解决事项与已解决事项的明确状态。

网页正文是非可信数据，不得成为 Prompt 指令。

### 7.5 证据索引冻结

Advice 运行开始时，从追加日志冻结一份当轮证据索引视图（每个相关标的一条
最新有效快照及覆盖状态），并记录其 hash。Advice 运行途中 Collector 的并发
更新不影响当轮输入；新写入的证据只在下一轮 Advice 生效。

## 8. 一张合约的确定性风险投影

候选阶段不存在实际下单手数，`可开 X 手`只是最大容量。AI Decision Advice 统一按
“新增一张合约”的边际影响判断，不假设用户会把容量全部开满，也不推荐手数。

代码必须先为每个候选计算当前状态和新增一张后的变化：

- 标的、行业和币种集中度；
- Sell Put 指派后的持股分布和现金担保影响；
- Covered Call 被叫走后的持仓分布；
- 同标的已有 Put/Call 的方向叠加；
- 到期日集中关系；
- 与现有组合结构的重叠关系。

这些值是模型可引用的事实，不由模型自行计算。v1 不建设组合级 Greeks 聚合。

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

- 新增一张会明显加重标的、行业、币种或到期日集中度；
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
4. 再检查整体标的、行业、币种、到期日和既有期权叠加；
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

- `keep` 要求候选、组合、期权持仓和外部证据覆盖均完整；
- 搜索返回“无结果”但没有可审计的覆盖证明时，不能据此输出 `keep`；
- `switch / defer` 必须引用精确内部事实或可靠外部证据；
- 风险适用于全部合格候选时使用 `defer` 或 `needs_review`，不能任意改选；
- 组合或期权上下文缺失时最多只能为 `needs_review`；
- 外部搜索不可用不能产生 `keep`；
- 未支持的 `switch / defer` 由 OM 降级为 `needs_review`；
- 候选 ID 不存在、策略不符或 Covered Call 跨标的改选时，拒绝该选择并降级为
  `needs_review`。

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
    "portfolio_context_hash": "...",
    "option_positions_hash": "...",
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

- `account_ref` 是匿名、单次运行引用，不是账户标签；
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

### 12.2 外部证据

共享、追加写入的目标真源：

```text
output_shared/state/ai_decision_advice/external_evidence.jsonl
```

同一个 JSONL 保存：

- batch 原始 Responses / `web_search_call` 审计记录；
- 按标的规范化的语义证据记录；
- 查询、cutoff、来源、状态、内容 hash 和身份绑定；
- 失败和部分成功状态。

batch 原始响应只保存一次，标的记录通过 `evidence_run_id` 引用，避免跨标的复制大块响应。
不额外抓取或归档完整网页正文。

“最新标的证据”只是从追加日志推导的可重建索引或缓存，不是第二真源。不得再维护 SQLite、
CSV 或另一份可独立修改的 latest 数据。

### 12.3 Advice 结果

账户和 run 绑定的目标真源：

```text
output_runs/<run_id>/accounts/<account>/state/ai_decision_advice.jsonl
```

每条记录绑定：

- run、匿名 account ref、市场；
- 候选、组合、开放期权和语义证据 hash；
- evidence run 与实际 cutoff；
- Prompt、Schema、模型和代码版本；
- 原始模型响应、验证结果、接纳或降级后的正式动作；
- 如复用旧建议，记录 `reuse_of_advice_id` 和新的证据覆盖绑定。

Daily Brief 与 Agent 必须读取这同一份正式结果。v1 不新增 DB、CSV 或第二份历史结果库。

## 13. 触发、复用和超时

### 13.1 Advice 触发

AI Decision Advice 覆盖：

- 固定决策简报；
- 新增策略候选提醒。

固定简报展示 Sell Put 和 Covered Call 的完整聚合建议。新增候选提醒只展示受影响策略
模块，但模型仍基于完整候选、组合和开放期权持仓判断。

新增候选提醒本身属于正常监控回执，可以承载 `unavailable` 状态：Advice 超时或
失败时不阻断、不推迟候选提醒，提醒按既有节奏发出并如实显示 AI建议未完成。

每个账户 Advice 总等待预算为 30 秒。超时或失败不能阻断 Candidate Engine 的原始回执。

### 13.2 复用条件

只有以下内容全部未发生实质变化时才能复用：

- 候选快照；
- 组合分布；
- 开放期权持仓；
- 外部证据语义内容和覆盖状态；
- Prompt、模型和输出 Schema 版本。

仅 `last_checked_at` 更新、且搜索确认没有新增证据时，不重复调用 Advice 节点。证据超过 8
小时或覆盖状态降级后，旧建议立即失效。

### 13.3 不触发搜索的读取面

- 正常监控编排先生成并持久化当轮 Advice，renderer 再读取该正式记录生成 Daily Brief；
- Agent 和其他只读面只读取已持久化的证据与 Advice；
- Agent 查询不触发联网搜索或 Advice 刷新；
- v1 不新增手动刷新 CLI；
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
8. Collector 失败或 Advice unavailable 不建立独立失败通知，但在因其他原因生成的回执中
   必须如实显示。

输入未变化时复用 Advice，不能因为模型措辞随机变化制造通知噪声。

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
结论｜现有证据冲突，需要人工判断。
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
- 每个来源显示标题、发布者、日期和链接；
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
- 四类输入 hash 与实际 cutoff；
- 完整内部事实引用；
- 外部证据和来源；
- 运行、校验、复用和降级状态。

`candidate_rank_explain` 继续只解释 Candidate Engine 的确定性筛选和排序，不混入 AI
观点。Agent 查询保持只读，不触发搜索、扫描、模型调用、通知或状态写入。

## 17. 配置合同

唯一用户配置：

```yaml
ai_decision_advice:
  enabled: true
```

规则：

- 一个开关同时控制 External Evidence Collector 和 AI Decision Advice；
- 不保留 `ai_interpretation`、`ai_strategy_advice` 等旧字段或兼容别名；
- 不提供每账户开关；
- 模型、4 小时间隔、5 分钟搜索预算、批大小、并发和 30 秒 Advice 预算均为 v1 固定合同；
- `enabled: true` 但缺少 `DEEPSEEK_API_KEY` 时，配置校验直接失败；
- `enabled: false` 时不运行两个阶段，也不在回执中创建空 AI 区块；
- API key 只从环境读取，不能写入 YAML、JSONL、Prompt 或模型输入。

## 18. 隐私与安全

发送给 DeepSeek 的最小数据合同：

External Evidence Collector：

- 只有公开 symbol、市场、交易所和公司名称；
- 没有账户、持仓或候选数据。

AI Decision Advice：

- 匿名 `account_ref`；
- 候选 ID、合约和必要指标；
- 组合的 symbol/industry/currency 权重和集中度；
- 开放期权的 symbol/side/type/strike/expiry/contracts 与结构关系；
- 已规范化外部证据。

禁止发送：

- 真实账户标签、Futu account id；
- 总 NAV、总资产、历史成本；
- order/trade id、文件路径；
- token、cookie、authorization、webhook 或 API key；
- 任何个人身份信息。

原始 Responses 审计落盘前同样执行密钥和私密字段检查。

## 19. 运行状态与可观测性

内部至少记录：

- 观察集合大小、排队顺序、完成/未完成标的；
- 搜索耗时、批次并发、5 分钟预算消耗；
- 来源数量、去重数量、证据年龄和覆盖状态；
- JSON Schema 首次成功、修复成功和最终失败；
- Advice 30 秒耗时、cache reuse 和 provider usage；
- 各动作数量、validator 降级数量和原因；
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
| 可审计、无错误、覆盖明确的搜索未发现任何事件 | 覆盖完整，可输出 `keep` |
| 组合或开放期权上下文缺失 | 不能 `keep`，最多 `needs_review` |
| 单标的身份无法建立 | 仅该标的 `identity_unavailable` |
| 两次刷新之间首次出现的新标的 | `unavailable: no_evidence`，不能 `keep` |
| 单标的搜索失败，旧证据不超过 8 小时 | 可使用旧证据，显示实际时间 |
| 证据超过 8 小时 | `unavailable: evidence_stale`，不能 `keep` |
| Advice 运行途中 Collector 写入新证据 | 当轮使用开始时冻结的证据索引，不受影响 |
| 单一未经证实消息 | 完全忽略，不触发动作或人工判断 |
| 可靠 Sell Put 下行事件 | 结合持仓暴露后可 `switch / defer`，必须有事实引用 |
| Covered Call 出现强上行催化 | 评估被叫走机会成本，不得脱离持股事实下结论 |
| Covered Call 只有下行消息 | 可报告持股风险，不能自动声称 Short Call 更危险 |
| AI 引用被拒绝候选 | 校验失败并降级 `needs_review` |
| Covered Call 跨标的 `switch` | 校验失败并降级 `needs_review` |
| 分项建议联合后形成风险叠加 | 调整部分为 `defer`；无法取舍则 `needs_review` |
| 只有搜索时间更新、无新增证据 | 复用 Advice，不重复调用模型 |
| Prompt、Schema、候选或持仓 hash 改变 | 禁止复用，重新生成 Advice |
| Collector JSON 首次无效 | 预算内修复一次；仍无效则批次失败 |
| Advice JSON 无效或超时 | `unavailable`，原始策略回执继续 |
| 网页包含 prompt injection | 当作非可信数据，不能改变指令或访问内部能力 |
| Advice 实质动作改变、候选未变 | 进入既有 Daily Brief diff 并随监控回执通知 |
| `needs_review` 恢复为 `keep` | 属于实质变化，进入既有 Daily Brief diff 并通知 |
| 只有来源或措辞改变 | 不发送新通知 |
| AI 改选到折叠候选 | 固定简报强制展示该候选及真实策略排序 |
| 新增候选提醒仍建议旧候选 | AI 区写完整旧合约；新增列表不重复旧候选 |
| 新增候选提醒遇到 Advice 超时或失败 | 提醒按既有节奏正常发出，承载 `unavailable` 显示 |
| 正常 `keep` | 简洁易懂，不展开来源、不写“安全/通过” |
| 持仓展示 | 使用逐项列表，不使用 Markdown 表格 |
| 候选收益展示 | 主显示持有期净收益，年化明确标为门槛值 |

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
