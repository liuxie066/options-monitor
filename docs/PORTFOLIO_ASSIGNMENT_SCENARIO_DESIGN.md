# Portfolio Exposure 全部指派后分布：Holdings 来源与估值设计

状态：来源与估值设计于 2026-10-01 实现；2026-10-09 按用户确认的简化规则移除人工券商名单门禁。本文件描述源码目标，不声称当前运行环境已采用。

## 目标、边界与成功信号

目标：同一组账户的“全部未平短期权假设被指派”分布，以富途账户事实和 OM 期权账本为基线；现有 portfolio.holdings.enabled 只决定是否补充 PM Holdings 中明确为非富途来源的资产。富途资产直接用富途 OpenD 行情估值。

非目标：不改变真实持仓、期权账本或 PM Holdings；不执行真实指派、转账、交易、飞书写入、服务升级；不把独立 Feishu 读取接入此查询；不加入 Long Option；不新建用户输入参数或第二个业务开关。

| ID | 可观察成功信号 |
| --- | --- |
| S1 | 开关关闭时查询不访问 PM；只使用所请求账户的富途股票、现金与已计入现金的 MMF，以及 OM 账本中所有券商的 open short Put/Call。非富途期权仍展示合约与原币种指派变化，其券商终值不可证则标记未知，不进入富途资金覆盖。 |
| S2 | 开关开启时自动增加同账户全部明确为非富途的 PM Holdings；PM 的富途股票、现金、MMF 副本及来源不明行不计入。新增具体非富途来源无需重新确认；已成功读取但符合条件的行数为零是有效零结果。 |
| S3 | 富途现有股票与富途期权假设指派新增股票的同一标的使用同次 OpenD 报价；缺价、异常、失效报价和外汇证据不会由 PM 报价静默替代。 |
| S4 | 非富途现金进入资产分布，但不被视为富途期权指派可动用的现金/MMF；非富途期权的资金变化也不进入富途覆盖。现金按账户、券商、币种保留正负值后计算总资产与负债；组合及分账户 CNY 净额都只表示经济汇总。 |
| S5 | 只对纳入计算的资产判断估值质量；被排除的 PM 富途副本缺价不拖低情景质量。来源、账户、报价时间和不完整原因可回读。 |
| S6 | 现有 CLI/Tool Gateway 账户输入、预览/确认/回读门及只读查询边界保持；开关两态、PM 故障、账户隔离和部分结果均有行为验证。 |

## 当前实现入口与约束

- CLI/Tool Gateway 经 `src/application/portfolio_assignment_scenario.py` 组装富途、账本和可选 PM 证据，再调用 `domain/domain/portfolio_assignment_scenario.py::project_assignment_scenario`；领域函数不做 I/O。
- 期权事实归 `src/application/ledger/api.py::list_open_short_assignment_rows`；富途仓位和现金归 `src/application/futu_portfolio_context.py`。`cash_by_currency` 已含 MMF 现金等价物，不重复加总。
- 同次请求取得一次当前 FX 快照，按 display/capacity 用途投影，并传入各账户上下文。汇率规则归 [当前汇率契约](EXCHANGE_RATE_FACT_DESIGN.md)，不能在此另设 24 小时或假期门槛。
- PM 补充通过 `holdings_scope=non_futu` 的 scoped API，并由 `src/infrastructure/portfolio_management_client.py` 核验账户、scope、来源清单及计数。以下 PM 部分是跨项目接口契约，不能据此认定目标 PM 服务版本已支持。
- 现金按账户、券商、币种保留有符号桶；资金覆盖只用富途现金及富途期权，资产分布保留负债。PM 账户级持仓证据不足以证明非富途券商的完整终局资产。

## 责任归属

责任沿用现有 owner；真实指派正股收益由 [Assigned Stock Return](ASSIGNED_STOCK_RETURN_DESIGN.md) 负责，本情景只做假设投影。

| 概念或实现 | 归属裁定 |
| --- | --- |
| 开关、预览、配置回读 | 复用 OM src/application/config_yaml_holdings.py、config_validator.py；仅保留 portfolio.holdings.enabled；来源清单用于展示和校验完整性，不保存人工批准名单。现有配置发布、备份、生成和回读流程负责开关。旧 approved_non_futu_brokers 字段加载时忽略，重新配置时移除。 |
| 富途账户仓位、现金/MMF 与账户身份 | 复用 OM src/application/futu_portfolio_context.py::fetch_futu_portfolio_context、build_futu_position_snapshot、cash_components_by_currency。 |
| OpenD 批量行情与连接 | 复用 OM src/application/opend_market_snapshot_fetching.py 的批量读取、src/application/futu_quote_routing.py::resolve_futu_quote_route 与 futu_portfolio_context.py::infer_futu_portfolio_settings。新增的只是此情景的估值有效性判定；开仓判定不能复用其业务 status。 |
| 外汇取数和质量 | 复用 OM src/infrastructure/exchange_rates.py::current_exchange_rate_snapshot、project_exchange_rate_snapshot；同次情景共享一份快照及用途判定。 |
| 期权事实、符号、券商归一化与投影 | 复用 OM ledger/api.py::list_open_short_assignment_rows、domain/domain/symbol_identity.py::canonical_symbol、option_position_identity.py::normalize_broker、portfolio_assignment_scenario.py::project_assignment_scenario。账本仍读取全部券商期权；按账户、券商、币种隔离指派现金，非富途缺少完整起始资产时只保留可证实的原币种变化。 |
| PM 估值 HTTP 契约与来源切片 | 复用 PM src/service/http.py、src/service/application.py::get_valuation_evidence、src/app/portfolio_read_service.py::build_valuation_evidence 及 OM src/infrastructure/portfolio_management_client.py。新增可选 holdings_scope；PM 的 src/feishu/repositories/holdings_repository.py 负责通过既有 get_raw_holdings 取得不发布缓存的完整原始账户切片，并在该 owner 内先分类、后转换纳入行。all 保留旧路径。 |
| PM 券商识别 | 复用 PM src/app/holdings_validation.py::holding_broker_scope 与 OM option_position_identity.py::normalize_broker 的已有规则；规范化空白/大小写后，以 futu、moomoo 或富途开头的名称归为富途，空值、既有泛称和 manual 归为 unknown。PM 按既有自由文本合同分类，并在 scoped 响应回传完整原始 broker 清单；OM 按账户接收全部明确为非富途的响应行，并复用客户端分类与完整性校验；富途副本与 unknown 不纳入。新增具体来源不要求人工批准。OM 归一化函数还用于账本身份，扩充别名须验证消费者。 |
| PM scoped 报价及 FX | 复用 PM 现有 pricing/cache、pricing/fx、portfolio_read_service；只为 non_futu 行暴露报价来源、取数时间、缓存状态及 FX 独立来源/时间/失效状态。上游市场时刻仅在字段语义核实并保留时单独标注，不把 fetched_at 冒充市场成交时间，也不把是否有市场时刻作为所有资产的 complete 门槛；all 的公开响应及估值规则保持原合同。 |
| 资金覆盖与分布现金口径 | 复用 OM domain/domain/portfolio_assignment_scenario.py 的现金投影；按账户、券商、币种保留原币种起始现金、指派变化和终局有符号值。富途 cash_coverage 仅汇总富途现金与富途期权，分布总资产/负债先按有符号币种桶折成 CNY 后计算，再做 by_code/by_category 净额摘要。 |
| 公开来源与覆盖文案 | 复用 OM src/application/agent_tools/portfolio.py 的 output_contract 和 src/application/portfolio_assignment_scenario.py 的文本渲染；更正静态来源标签及全部券商指派数与富途资金覆盖的范围说明，不新增输入。 |

## 选定方案

### 来源及经济口径

1. 查询先规范化和验证账户，从 OM 账本取所有券商的 open short Put/Call，再逐账户取富途完整余额与股票仓位。任一请求账户的富途身份不符、cash_balance_reliable 不为 true、position_snapshot_input 缺失或其 errors 非空，均表示富途完整基线不可证，情景 unavailable；不把缺失账户当零，也不回退 PM。Long Option 不进入输入或输出。
2. 关闭时不创建 PM client、不做 PM HTTP 请求。开启时要求 PM 集成可用，只请求同一账户范围的 non_futu Holdings 估值证据；PM 不可用时仍可呈现已核验的富途/账本基线，但整体标记 partial 并说明补充数据缺失，不伪称完整分布。
3. PM 只保留 account 属于请求范围、broker 是具体非富途名称的非期权资产。PM 富途同步使用规范值“富途”；双方规范化空白/大小写后排除以 futu、moomoo 或富途开头的名称，空值、既有泛称及 manual 为 unknown，跳过并使结果 partial。PM 股票与同符号富途股票仍按 account、broker、code 分组，不跨券商抵消。预览按账户展示本次原始切片的每个 broker 原文、分类和行数；查询复用 PM scoped 契约校验账户、完整原始清单、分类/计数及纳入行的来源。不保存批准名单，新增具体非富途来源自动纳入。清单缺失、分类或计数不一致时，PM 补充不进入投影，保留富途/账本基线并标 partial。不能把读取失败或 unknown 当作零补充。
4. 富途现金及 MMF 只用 cash_by_currency 入账一次；cash_components_by_currency 用于解释其组成。富途现金在 (account, broker, currency) 原币种桶中保留正负值，富途期权的指派本金及已知费用先落到同币种富途桶，再由同次 OM FX 观察折成 CNY；缺 FX 的桶终值未知，不先跨币种抵消。PM 非富途现金/MMF 按 PM scoped 证据的 CNY 市值进入分布，不进入富途资金覆盖、分账户富途缺口或富途期限资金阶梯，也不用 OM FX 重算。
5. OM 全部券商的期权合约和原币种指派数量/现金变化仍展示。PM 当前按账户的 Holdings 质量不证明非富途券商完整起始现金及正股仓位；因此非富途期权的终局现金、股票数量与价值均标未知、总分布 partial，不将指派变化加到 PM 现金行或与富途 FX/报价拼成假完整值，也不混入富途 cash_coverage 和期限阶梯。领域层必须先按券商分支，再读取仅供富途行使用的 code 报价表；即使非富途期权与富途持仓同代码，也不能以富途价计算其终局。非富途期权费用沿用缺失状态，不套用富途费率。PM 非富途持仓若未被该券商期权影响，仍可按 PM scoped 估值展示其已知金额；受影响券商的终局资产需标明不完整，不把缺行视为零。
6. 分布保留各 (account, broker, currency) 现金桶的终局有符号 CNY 值以及可证实的正股行，先由未净额的行计算 gross_assets、liabilities、net_assets 和负债明细；by_code/by_category 可以给净额摘要，但不得拿摘要净额反推总资产和负债，也不得把净额权重称为总资产构成。富途 cash_coverage 的组合值及 account_breakdown 的分账户值都是 CNY 经济汇总，不证明跨账户或跨币种资金可调拨。
7. 不改变工具输入，只接受 accounts。结果沿用 complete/partial/unavailable、现有金额单位 CNY、Decimal 计算和 snapshot/provenance。Tool Gateway 的静态 source_label 改为“富途账户 + OM 账本；PM 非富途补充可选”，实际来源仍由 snapshot/holdings_sources 回读；CLI 文本分别标明“全部券商指派数”和“仅富途期权资金覆盖（跨账户、币种 CNY 经济汇总）”，不把后者称为全部 CSP 需求。展示 PM 补充是否启用、纳入/排除数量与不完整原因。不开独立 Feishu 路径。

### PM scoped valuation-evidence 契约

- 在既有 POST /api/v1/analysis/valuation-evidence 请求体增加可选 holdings_scope，允许 all（缺省，旧调用语义不变）或 non_futu；响应 scope 回显该值。OM 开启补充时显式传 non_futu，并拒绝缺少或不匹配的 scope 回显。未升级的 PM 对此新请求不能被当作已支持过滤。
- non_futu 的 PM 仓储 owner 对每个请求账户调用既有 get_raw_holdings(account)，完整取得 Feishu 原始行且不发布本地缓存；读取失败、返回跨账户行或无效的 record_id/fields 外壳使该账户 unavailable。先按原始 broker 分类和计数，再仅将已核实非富途行交给既有严格转换与重复身份校验；被排除的富途副本数量、资产类型或价格字段损坏不污染本次估值。空/无法判定 broker 即使数量为零也计为 unknown、排除估值并令账户 partial。已核实非富途零数量行单独计数但不估值；负数量非现金行不得在分类前静默消失，按现有有符号市值合同纳入，若该资产类型或价格不能可靠估值则该账户 partial 并说明。转换失败、身份重复或来源与投影行不一致时不可称 complete。
- 在 fetch_price_snapshot 和 calculate_valuation 前完成 PM 的富途/unknown 过滤；过滤作用于 holdings、报价请求、account_status、warnings、最终 status 和可核对的 source/included/excluded/zero/unsupported 计数。non_futu 响应按账户回传从同一完整原始切片取得的每个 broker 原文、分类、行数及总行数，包含排除行和零数量行；无完整清单或计数不合时 OM 不接受补充。PM 与 OM 均不保存券商批准名单；OM 复用客户端核验完整来源清单及行归属。成功读取、来源均可解释且零合格行才是 complete 的空 Holdings 集，不是读失败；未知 broker、未支持负仓位不能冒充 ready_empty。
- non_futu 的 pm.holdings_feishu 只证明本次完整 PM 表切片读取、账户范围和行结构；逐行保留该读取时间及记录 updated_at（缺失明确为 unknown），不把任一时间称为券商实时数量证明。用户选定 PM Holdings 是非富途的记录来源，trust 仅指该记录来源完整且此次估值证据可核对。pm.holdings_quantity 质量项来自富途同步回执，不参与 non_futu 的数量判定。记录长期未变更不自动等于数量错误；也不得以本次读取时间改写记录更新时间。
- 仅对纳入且非零的行校验报价与必要 FX。CNY 现金/MMF 的 fixed_identity 单价为 1，不要求市场成交时刻或外部 FX，完整性由 Holdings 读取与数量、行结构证明。其余报价要求价格有限且大于零、来源明确、取数/缓存时间有效、缓存未过期且非 fallback；这些条件满足即可按现有 PM 报价缓存合同参与 complete，但只声明“取数/缓存有效”，不声称市场价实时。若上游有已核实语义的市场时刻，另存 market_as_of 并检查其有效性；缺少该字段时明示 market_as_of=unknown，不把 fetched_at 冒充市场时刻，也不单凭缺此字段降为 partial。已知市场时刻失效、价格或缓存失效才使相关估值 partial。外币现金/MMF 的 fixed_identity 单价同样不要求市场时刻，但必须核验必要 FX。PM 定价 owner 为所有需换汇行附独立 FX 来源、取数/缓存时间和 stale/fallback 状态；24 小时以内且非 fallback 才能称 FX fresh，不能仅凭正汇率或价格 fetched_at 判定。FX 失效、必要证据缺失或金额无法换算时该账户 partial/unavailable、逐行说明；CNY 恒等换算不要求 FX 外部证据。来源读取成功但零合格行不要求无关的 pm.prices/pm.fx artifact。HTTP 包装器不能再用全量账户级价格/FX artifact 覆盖 scoped freshness；all 请求继续原合同。
- OM PM client 验证响应 schema、账户、holdings_scope、snapshot、时间、账户状态、行来源及原始 broker 清单，再交给领域投影；不能只在 OM 过滤完整 PM 响应并继承旧 status/freshness。PM 服务端与 OM 客户端的版本兼容由请求字段及响应回显检测，失败时保留富途基线、情景 partial。
- 配置启用预检复用同一 scoped 请求，展示本次原始切片中各账户 broker 原文、行数及 futu/non_futu/unknown 分类。成功且零合格行、无 unknown/unsupported 时显示 ready_empty/0 eligible。仅将开关状态纳入待发布 YAML 和生成配置，不保存人工批准名单；preview_sha256 继续绑定配置内容、源版本、PM 服务地址和输出目标。apply 重新执行来源质量预检；来源名称变化不使预览失效，读失败、unknown broker、scope 不匹配或必要质量不可证仍拒绝启用。旧批准字段加载时忽略，重新配置 Holdings 时移除。生产配置及部署另行授权。

### 富途报价、汇率与质量

- 用所请求账户富途股票及富途短期权底层标的去重形成报价集合，同一请求内对每个标的只取一次 OpenD 行情，同价用于富途现有股和富途期权假设指派新增股。非富途 PM 持仓按 PM scoped 证据估值；非富途期权终局估值不借用富途报价冒充券商完整基线。先用现有 quote route；无 watchlist binding 时取所请求账户已用于持仓读取的富途连接设置；端点冲突或身份不明则报价不可用，不任意挑第一个账户。
- 估值判定使用原始 last_price、update_time、sec_status、suspension、market_state 与 symbol/currency 一致性；不得仅凭开仓观察的 market_closed status 放行。价格必须有限且大于零，更新时间必须有效且不在未来，证券状态正常且未停牌。300 秒以内可视为 fresh；超过 300 秒但不超过 7 个日历日，只有下述休市交易日历校验通过时可作为完整估值，否则仅作标时的 partial 估值。更旧或无效报价不传入领域 quote map，相关市值与分布标记不完整。300 秒用于交易时新鲜度，7 日是保守的失效上限，不新增运行配置；长假可显式不完整。
- 同次情景调用 `current_exchange_rate_snapshot` 一次，以 `write_cache=False` 保持只读，再从同一快照生成估值及容量用途；各账户接受传入的观察。CNY 恒等为 1，必要币种缺少有效报价时相关金额不可用。已验证连续休市报价可同时用于估值和资金覆盖；交易时段断档或日历未知仍不可用。逐对保留来源、原报价时间及快照证据。PM 非富途市值由 PM scoped 证据负责，不用 OM FX 重算。

- 构造 portfolio.valuation_evidence.v1 时，从每个实际来源的完整性、时间和 trust 推导 status/freshness；保留 Futu、PM、quote、FX、账本各自时间和 snapshot identity。完整富途基线与 PM 补充失败并存时，聚合 status=partial，PM 缺失作为单独原因；已纳入富途数据的 freshness/trust 仍按其自身证据判断，不把 PM unavailable 覆盖成聚合 unavailable，也不伪称 PM fresh/trusted。与此相反，富途完整性失败直接 unavailable。不会因为函数成功返回就设置 fresh/trusted；过期 quote 即使有 is_stale 标志也必须使整体 partial，因为领域 _quote_values 不检查该标志。
- 富途估值报价在市场休市时，只有完整 OpenD 交易日历证明报价不早于最近已开始交易日、且提供方确认非连续交易状态时，才可沿用；保留报价时间和日历回执。开市旧报价、跨交易日缺口、日历读取失败/不完整仍使整体 partial。5 分钟内报价沿用原新鲜度门槛。该规则只用于资产估值，不放宽开仓/平仓实时报价门禁。

### 失败与状态

| 情况 | 行为 |
| --- | --- |
| 富途某账户身份不匹配、cash_balance_reliable 不为 true、position_snapshot_input 缺失或 errors 非空 | 整体 unavailable；不使用 PM 镜像补齐。 |
| 某标的报价或 FX 缺失、异常、过期 | 该金额未知；其余已知行可展示；整体 partial，不回退 PM 富途报价。 |
| PM 只因被排除的富途副本缺价 | scoped 响应不受影响；以真正纳入行的证据判定。 |
| PM 请求失败或返回旧版/错 scope | 保留富途基线，标记 partial 与 PM 补充缺失；不谎称完整。 |
| PM 原始来源清单缺失或不一致 | 本次 PM 补充不进入投影；保留富途/账本基线并标 partial，说明证据缺口。 |
| enabled=true 配置没有券商名单，或明确非富途来源名称新增/变化 | 自动纳入请求账户全部合格的 PM 非富途资产；依据实际证据决定质量，不要求重新批准。 |
| PM 成功返回零合格非富途行 | 有效零补充；若其余证据完整，仍可 complete。 |
| PM 行 broker/账户不明、非富途行估值缺失 | 排除不明行或保留金额未知；整体 partial，逐行说明。 |
| PM 原始外壳损坏、跨账户行或读取失败 | 对应账户 unavailable，组合保留富途基线但整体 partial；不能当作零补充。 |
| PM 非富途零数量或负数量非现金行 | 零行计数但不估值；负行按有符号市值纳入，无法可靠估值则 partial，不可在 broker 分类前丢弃。 |
| PM 报价只有有效取数/缓存时间、无市场时刻 | 可按现有 PM 报价缓存合同估值并明示 market_as_of=unknown；不声称市场实时，也不单凭缺市场时刻降为 partial。 |
| PM 已知市场时刻失效，或 FX 缺少独立来源/时刻、回退过期缓存 | 受影响估值 partial；FX 数值不能单独证明 fresh。 |
| 非富途短期权缺少同账户、券商的可证实起始现金或正股完整基线 | 保留指派事实与已知数量变化；相关终值未知，总分布 partial，富途 cash_coverage 不混入该期权。 |
| 富途账户现金为负，其他券商现金为正 | 按账户、券商保留两条有符号行；总资产与负债分别计算，净额仅用于摘要。 |
| 同一富途账户不同币种现金一正一负 | 各币种先保留原币种指派后正负值，再按同次 FX 折成 CNY 计算总资产和负债；跨币种净额仅作经济摘要。 |
| 富途报价闭市但在保守时间窗内 | 完整交易日历和市场状态证明可沿用时，按该证据估值；缺少证明则仅作标时的 partial 估值，过窗不计价。 |

## 不采用的方案

- PM Holdings 完全替代富途账户仓位：PM 有富途股票、现金、MMF 副本且刷新时差不同，可能重复或覆盖账户事实。
- 开关关闭后仍调用 PM 取行情：增加了不必要的依赖，富途价也失去同源一致性。
- OM 事后过滤 PM 全量估值响应：PM 已为排除行询价，并已用它们决定 status/freshness。
- 把 PM 非富途现金直接并入富途 CSP 覆盖：跨券商可用性没有证据。
- 新建第二个 Holdings 开关、独立 Feishu 读链或新计算 owner：现有开关、PM API 与领域投影足以承载。

## 验证入口

隔离 fixture 覆盖开关两态、零 PM 请求、MMF 去重、单次 FX、报价失效/闭市、来源清单、旧名单忽略、账户/券商/币种隔离、负现金与非富途终值未知。公开输出需区分全部券商指派事实与仅富途资金覆盖。

- 应用与公开入口：`tests/test_portfolio_assignment_application.py`、`tests/test_portfolio_assignment_cli.py`。
- 领域投影与负债：`tests/test_portfolio_assignment_scenario.py`。
- scoped API 与启用预检：`tests/test_portfolio_management_client.py`、`tests/test_config_yaml_holdings.py`。

OM 测试不代替 PM 服务端验收或目标环境的接口支持证据。

## 风险与证据边界

- 非富途报价缺市场时刻时，依既有 PM 缓存合同估值并明示 `market_as_of=unknown`；不能声称实时。FX 缺独立来源/时间或已过期仍使结果不完整。
- 富途估值的 7 日上限保守失败，长假可能不可用；不据此放宽开仓/平仓实时报价门禁。
- 启用 PM 补充前须核验目标服务支持 scoped 契约；旧版或错误回显使结果 partial。
- 来源自由文本及清单不能证明实际持仓真实性；PM 记录管理负责真实性，人工券商批准名单也不能替代。
- OpenD 未识别证券类型不能静默忽略。只有证实某 MMF 仓位与已计入现金的同一资产一致后，才可由适配 owner 去重。

## Position Sizing v2：统一非期权净资产口径

账户指派查询新增 `position_sizing`，与 CSP / CC 候选共用 domain 指派变换。
原查询的费用、总资产权重与负债字段维持原语义；新净资产权重显式为指派费用前，不能与旧 gross 权重混用。
账户查询纳入所选账户全部 open short Put/Call；候选仅纳入本账户目标标的全部已有 short Put/Call，再增加候选一张。

冻结现货价 P 与 FX 后，`Q_after = Q + put_shares - call_shares`，
`delta_cash = -sum(put_strike * shares) + sum(call_strike * shares) + candidate_net_premium`，
`N_after = N_before + delta_shares * P + delta_cash`，`weight = Q_after * P / N_after`。
各金额先按统一 FX 转为 CNY。已有权利金包含在现金；新候选净权利金扣除开仓费用后只加一次。
指派费用另行呈现，不能因费用未知而声称其为零。

prepared portfolio context 在封存前一次收集报价、资产与来源证据；ledger 的可信账户快照提供全券商已有期权。
富途基线与 `portfolio.holdings.enabled` 控制的 PM 非富途补充沿用原 owner，PM 富途副本排除。
普通股票统一按现价重估，不用平均成本。非富途交割必须有对应券商现金基线；缺失资产、报价或 FX 显式不可用。
负现金和负股数保留；净资产非正不出比例。候选开仓资金与 CC 覆盖仍保留富途执行账户约束。

候选字段 `position_sizing_basis=non_option_net_assets_before_assignment_fees.v2` 标明口径；
当前、已有指派后、加候选后对应三个浓度与净资产字段。旧封存结果不修改、仅按历史口径展示。
