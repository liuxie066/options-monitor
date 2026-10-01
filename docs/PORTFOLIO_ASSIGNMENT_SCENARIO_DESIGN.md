# Portfolio Exposure 全部指派后分布：Holdings 来源与估值设计

状态：经 Planreview 与用户确认，并已按本设计实现（2026-10-01）。本文件描述源码目标，不声称当前运行环境已采用。

## 目标、边界与成功信号

目标：同一组账户的“全部未平短期权假设被指派”分布，以富途账户事实和 OM 期权账本为基线；现有 portfolio.holdings.enabled 只决定是否补充 PM Holdings 中明确为非富途来源的资产。富途资产直接用富途 OpenD 行情估值。

非目标：不改变真实持仓、期权账本或 PM Holdings；不执行真实指派、转账、交易、飞书写入、服务升级；不把独立 Feishu 读取接入此查询；不加入 Long Option；不新建用户输入参数或第二个业务开关。

| ID | 可观察成功信号 |
| --- | --- |
| S1 | 开关关闭时查询不访问 PM；只使用所请求账户的富途股票、现金与已计入现金的 MMF，以及 OM 账本中所有券商的 open short Put/Call。非富途期权仍展示合约与原币种指派变化，其券商终值不可证则标记未知，不进入富途资金覆盖。 |
| S2 | 开关开启时仅增加同账户、启用时已核对券商名称的非富途 PM Holdings；PM 的富途股票、现金、MMF 副本及来源不明行不计入。启用后出现未经核对的新券商名称时暂停整个 PM 补充并标 partial；已成功读取但符合条件的行数为零是有效零结果。 |
| S3 | 富途现有股票与富途期权假设指派新增股票的同一标的使用同次 OpenD 报价；缺价、异常、失效报价和外汇证据不会由 PM 报价静默替代。 |
| S4 | 非富途现金进入资产分布，但不被视为富途期权指派可动用的现金/MMF；非富途期权的资金变化也不进入富途覆盖。现金按账户、券商、币种保留正负值后计算总资产与负债；组合及分账户 CNY 净额都只表示经济汇总。 |
| S5 | 只对纳入计算的资产判断估值质量；被排除的 PM 富途副本缺价不拖低情景质量。来源、账户、报价时间和不完整原因可回读。 |
| S6 | 现有 CLI/Tool Gateway 账户输入、预览/确认/回读门及只读查询边界保持；开关两态、PM 故障、账户隔离和部分结果均有行为验证。 |

## 当前事实与约束

- 公开查询经 src/interfaces/cli/portfolio_ops.py 或 src/application/agent_tools/portfolio.py 进入 src/application/portfolio_assignment_scenario.py，再调用 domain/domain/portfolio_assignment_scenario.py::project_assignment_scenario。期权来源是 src/application/ledger/api.py::list_open_short_assignment_rows；领域函数不做 I/O。
- src/application/futu_portfolio_context.py::fetch_futu_portfolio_context 按账户读取富途余额与仓位；build_futu_position_snapshot 保留股票仓位，cash_by_currency 已包含富途 MMF 现金等价物，cash_components_by_currency 保留拆分。不能把 MMF 再加一次。
- 富途持仓返回价格/市值，行情快照返回 last_price、update_time。为同次情景一致性，本设计选择按标的批量读取 OpenD 行情并统一估值；持仓价格/市值只用于核对，不作为失败时的隐藏回退。
- 当前 src/application/opend_market_snapshot_fetching.py 的 underlier 观察服务复用开仓报价规范化。src/application/opening_quote_evidence.py::normalize_underlier_observation 使用 300 秒开仓门槛，且闭市分支先于价格、时间和证券状态检查；它的 status 不能直接当作持仓估值结论。
- 富途持仓上下文中的汇率实际由 src/infrastructure/exchange_rates.py::fetch_market_exchange_rates 获取：腾讯优先，新浪后备，不是富途提供；现有 exchange_rate_observation_status 用 24 小时门槛核验。
- PM 原有 /api/v1/analysis/valuation-evidence 读取所有 Holdings，再对全部行询价；即使 supplemental_codes 为空也如此。src/app/portfolio_read_service.py::build_valuation_evidence 会因任一持仓缺市值或报价 stale 将账户标为 partial；src/service/http.py::_public_result 还会用账户级质量 artifact 覆盖 freshness。因此 OM 事后过滤富途行不能消除质量污染。Slice B 草稿增加了 non_futu，但尚未满足本版完整切片与时效契约。
- PM 的 pm.holdings_quantity 质量项来自富途同步回执，只证明富途副本；src/feishu/repositories/holdings_repository.py::get_holdings_fresh 虽直接读取 Feishu，却先转换、可在 include_empty=False 时丢弃零数量及非现金负数量行，并把结果发布到本地持久缓存。get_raw_holdings 可只读取得完整账户原始切片，且核对返回账户；它的成功读取时间不证明非富途券商实时余额。
- PM src/pricing/payload.py::normalize_price_payload 可在获取时新填 fetched_at，该时间并非市场成交时刻；src/pricing/fx.py::fetch_exchange_rates 可回退到过期汇率缓存但目前只返回数值。现有 non_futu 草稿仅检查正汇率和报价 fetched_at，不能据此声明 FX fresh/trusted。
- PM 定价链中，腾讯 A/HK/ETF 报价 payload 带 time，单只基金路径可带 nav_date，Finnhub 官方 /quote 示例含 t 但当前 PM 适配器丢弃；默认优先的新浪美股及批量基金路径没有可用市场时刻。现有 PM valuation-evidence 只把 fetched_at 作为 observed_at，价格缓存也不保存上游市场时刻。以上只证明部分源有字段，不证明目标账户各类资产都有可靠的市场时刻；富途 OpenD 的 update_time 是另一条已明确的来源。
- PM 富途同步默认写 broker=富途；PM 已有 holding_broker_scope 将富途已知别名与缺失/泛称区分，但把其它任意非空文本都视为非富途。PM cash-flow 设计也以 broker=富途 和其它文本区分来源。当前测试中的 IBKR、平安证券等值是 fixture，不能证明真实 Holdings 使用这些值。
- OM 的 config_yaml_holdings.py 已有预览、确认、写入及回读门，但预览摘要目前不绑定 PM 当时的 broker 集合，config_validator.py 只允许 portfolio.holdings.enabled。若要把启用时核对的来源约束到后续只读查询，须让同一配置生成物保存该集合并在查询时核对；单次预览不能约束随后变化的自由文本。
- domain/domain/portfolio_assignment_scenario.py::_portfolio_evidence_quality 使用 success、schema、status、freshness、scope 和 snapshot 判断质量；_quote_values 不检查 is_stale。调用方必须正确归类报价并构造真实的 evidence status，不能仅因取数函数返回就填 fresh/trusted。
- 当前领域计算把所有 cash 持仓汇成 starting_cash，同一金额既用于分布又用于 cash_coverage。若 PM 非富途现金直接进入该池，会错误增加富途指派的资金覆盖。
- OM ledger/api.py::list_open_short_assignment_rows 按账户读取所有券商的 open short Put/Call，领域层当前把它们的指派现金变化汇入同一个池；非富途短期权可能因此错误消耗富途现金。非富途期权手续费现有投影会标为 unsupported_broker_fee_schedule，不可套用富途费率。
- 领域层目前把现金合成一条 combined CASH+MMF 行，再按 code 净额汇总计算 gross_assets/liabilities。若富途现金 -100、非富途现金 +200，应为总资产 200、负债 100、净资产 100；同一富途账户也可能有不同币种的一正一负余额，现有先净额的算法都会掩盖负债。富途 cash_by_currency 接受有符号金额，不能在折算前抹平币种。
- 富途上下文即使正常返回，也可能给出 cash_balance_reliable=false 或 position_snapshot_input.errors；这些标志意味着完整富途基线不可证。多账户上下文目前各自调用市场 FX，应用层则从各上下文挑首个可用币种汇率。
- PM 的富途券商识别含 moomoo，但 OM normalize_broker 尚不含；PM 又未覆盖 OM 已识别的全部富途中英文变体。两端若不对齐，PM 富途副本可能被误算为非富途。
- PM scoped valuation-evidence 只按账户给 Holdings 数量质量，不能仅从“该券商缺少现金/股票行”证明该券商的完整起始余额或仓位；非富途短期权不能借此推定缺行等于零，也不能把 OM 的富途 FX 与 PM 的非富途资产估值混为一次同源估值。
- 当前 Tool Gateway 的 source_label 固定声称来源为 PM 估值 + OM 账本；CLI 文本把全部券商指派数与未注明券商范围的“CSP 总需求”并列。开关关闭或非富途期权存在时，这两个公开口径会误导。
- portfolio.holdings.enabled 是现有默认关闭的开关；portfolio_management.enabled 是 PM 集成总开关，不是第二个分布业务开关。config_yaml_holdings.py::_probe_holdings 当前要求 PM 总体证据 complete/fresh/trusted 且存在任意 Holdings，尚不符合“非富途补充”口径。
- 当前 Holdings 专用 worktree 有未批准的代码草稿；本文的设计决策优先于草稿。主仓的并行任务 scope 保持原状；本 worktree 基线中的旧任务记录可由 Git 历史回读。

## 复用与新增归属

检索范围：OM 的 docs/INDEX.md、CONFIGS.md、CONFIGURATION_GUIDE.md、上述应用/领域/基础设施 owner 及其直接测试；PM 的 src/service/http.py、src/service/application.py、src/app/portfolio_read_service.py、src/app/holdings_validation.py。检索词包括 portfolio_assignment_scenario、portfolio.holdings.enabled、valuation-evidence、holdings_scope、broker_scope、cash_components_by_currency、normalize_broker、get_underlier_observations_opend。没有找到“全部指派后分布”的专属 living design owner，也没有找到现有 PM valuation-evidence 的 Holdings 来源过滤请求字段；docs/ASSIGNED_STOCK_RETURN_DESIGN.md 负责真实指派正股收益，不负责本情景。

| 概念或实现 | 归属裁定 |
| --- | --- |
| 开关、预览、配置回读 | 复用 OM src/application/config_yaml_holdings.py、config_validator.py；在现有 portfolio.holdings 下增加 approved_non_futu_brokers_by_account，保存启用时核对的各账户非富途 broker 原文集合。它是现有开关的来源约束，不是第二个业务开关或查询输入。现有配置发布、备份、生成和回读流程负责它。 |
| 富途账户仓位、现金/MMF 与账户身份 | 复用 OM src/application/futu_portfolio_context.py::fetch_futu_portfolio_context、build_futu_position_snapshot、cash_components_by_currency。 |
| OpenD 批量行情与连接 | 复用 OM src/application/opend_market_snapshot_fetching.py 的批量读取、src/application/futu_quote_routing.py::resolve_futu_quote_route 与 futu_portfolio_context.py::infer_futu_portfolio_settings。新增的只是此情景的估值有效性判定；开仓判定不能复用其业务 status。 |
| 外汇取数和质量 | 复用 OM src/infrastructure/exchange_rates.py::fetch_market_exchange_rates、exchange_rate_observation_status；同次情景只选择一个观察。 |
| 期权事实、符号、券商归一化与投影 | 复用 OM ledger/api.py::list_open_short_assignment_rows、domain/domain/symbol_identity.py::canonical_symbol、option_position_identity.py::normalize_broker、portfolio_assignment_scenario.py::project_assignment_scenario。账本仍读取全部券商期权；Slice A 即按账户、券商、币种隔离指派现金，非富途缺少完整起始资产时只保留可证实的原币种变化。 |
| PM 估值 HTTP 契约与来源切片 | 复用 PM src/service/http.py、src/service/application.py::get_valuation_evidence、src/app/portfolio_read_service.py::build_valuation_evidence 及 OM src/infrastructure/portfolio_management_client.py。新增可选 holdings_scope；PM 的 src/feishu/repositories/holdings_repository.py 负责通过既有 get_raw_holdings 取得不发布缓存的完整原始账户切片，并在该 owner 内先分类、后转换纳入行。all 保留旧路径。 |
| PM 券商识别 | 复用 PM src/app/holdings_validation.py::holding_broker_scope 与 OM option_position_identity.py::normalize_broker 的已有规则；规范化空白/大小写后，以 futu、moomoo 或富途开头的名称归为富途，空值、既有泛称和 manual 归为 unknown。PM 按既有自由文本合同分类，并在 scoped 响应回传完整原始 broker 清单；OM 只接受配置中同账户已确认的具体非富途原文，对响应行再排除富途及 unknown。新原文使本次 PM 补充整体 partial，不靠猜测式代码白名单。OM 归一化函数还用于账本身份，扩充别名须验证消费者。 |
| PM scoped 报价及 FX | 复用 PM 现有 pricing/cache、pricing/fx、portfolio_read_service；只为 non_futu 行暴露报价来源、取数时间、缓存状态及 FX 独立来源/时间/失效状态。上游市场时刻仅在字段语义核实并保留时单独标注，不把 fetched_at 冒充市场成交时间，也不把是否有市场时刻作为所有资产的 complete 门槛；all 的公开响应及估值规则保持原合同。 |
| 资金覆盖与分布现金口径 | 复用并修正 OM domain/domain/portfolio_assignment_scenario.py 的现金投影；按账户、券商、币种保留原币种起始现金、指派变化和终局有符号值。富途 cash_coverage 仅汇总富途现金与富途期权，分布总资产/负债先按有符号币种桶折成 CNY 后计算，再做 by_code/by_category 净额摘要。 |
| 公开来源与覆盖文案 | 复用 OM src/application/agent_tools/portfolio.py 的 output_contract 和 src/application/portfolio_assignment_scenario.py 的文本渲染；Slice A 即更正静态来源标签及全部券商指派数与富途资金覆盖的范围说明，不新增输入。 |
| 设计文档 | 新增本文件；现有索引没有该情景 owner，既有退役及正股收益设计均不适合承载。 |

## 选定方案

### 来源及经济口径

1. 查询先规范化和验证账户，从 OM 账本取所有券商的 open short Put/Call，再逐账户取富途完整余额与股票仓位。任一请求账户的富途身份不符、cash_balance_reliable 不为 true、position_snapshot_input 缺失或其 errors 非空，均表示富途完整基线不可证，情景 unavailable；不把缺失账户当零，也不回退 PM。Long Option 不进入输入或输出。
2. 关闭时不创建 PM client、不做 PM HTTP 请求。开启时要求 PM 集成可用，只请求同一账户范围的 non_futu Holdings 估值证据；PM 不可用时仍可呈现已核验的富途/账本基线，但整体标记 partial 并说明补充数据缺失，不伪称完整分布。
3. PM 只保留 account 属于请求范围、broker 是具体非富途名称的非期权资产。PM 富途同步使用规范值“富途”；双方规范化空白/大小写后排除以 futu、moomoo 或富途开头的名称，空值、既有泛称及 manual 为 unknown，跳过并使结果 partial。PM 股票与同符号富途股票仍按 account、broker、code 分组，不跨券商抵消。启用预览按账户展示本次原始切片的每个 broker 原文、分类和行数；确认后将其中具体非富途原文集合写入现有 portfolio.holdings 配置。开启查询时，OM 用 PM scoped 响应的完整原始 broker 清单核对该配置：每个被 PM 归为 non_futu 的原文必须属于同账户已确认集合，unknown 不得消失，纳入行还须经 OM 独立排除富途及 unknown。清单缺失、分类/计数不一致或出现新 non_futu 原文时，整份 PM 补充都不进入投影，保留富途/账本基线并标 partial、说明须重新预览确认；已有 broker 的持仓行数增减不触发重确认。不能把读取失败或 unknown 当作零补充。
4. 富途现金及 MMF 只用 cash_by_currency 入账一次；cash_components_by_currency 用于解释其组成。富途现金在 (account, broker, currency) 原币种桶中保留正负值，富途期权的指派本金及已知费用先落到同币种富途桶，再由同次 OM FX 观察折成 CNY；缺 FX 的桶终值未知，不先跨币种抵消。PM 非富途现金/MMF 按 PM scoped 证据的 CNY 市值进入分布，不进入富途资金覆盖、分账户富途缺口或富途期限资金阶梯，也不用 OM FX 重算。
5. OM 全部券商的期权合约和原币种指派数量/现金变化仍展示。PM 当前按账户的 Holdings 质量不证明非富途券商完整起始现金及正股仓位；因此非富途期权的终局现金、股票数量与价值均标未知、总分布 partial，不将指派变化加到 PM 现金行或与富途 FX/报价拼成假完整值，也不混入富途 cash_coverage 和期限阶梯。领域层必须先按券商分支，再读取仅供富途行使用的 code 报价表；即使非富途期权与富途持仓同代码，也不能以富途价计算其终局。非富途期权费用沿用缺失状态，不套用富途费率。PM 非富途持仓若未被该券商期权影响，仍可按 PM scoped 估值展示其已知金额；受影响券商的终局资产需标明不完整，不把缺行视为零。
6. 分布保留各 (account, broker, currency) 现金桶的终局有符号 CNY 值以及可证实的正股行，先由未净额的行计算 gross_assets、liabilities、net_assets 和负债明细；by_code/by_category 可以给净额摘要，但不得拿摘要净额反推总资产和负债，也不得把净额权重称为总资产构成。富途 cash_coverage 的组合值及 account_breakdown 的分账户值都是 CNY 经济汇总，不证明跨账户或跨币种资金可调拨。
7. 不改变工具输入，只接受 accounts。结果沿用 complete/partial/unavailable、现有金额单位 CNY、Decimal 计算和 snapshot/provenance。Tool Gateway 的静态 source_label 改为“富途账户 + OM 账本；PM 非富途补充可选”，实际来源仍由 snapshot/holdings_sources 回读；CLI 文本分别标明“全部券商指派数”和“仅富途期权资金覆盖（跨账户、币种 CNY 经济汇总）”，不把后者称为全部 CSP 需求。展示 PM 补充是否启用、纳入/排除数量与不完整原因。不开独立 Feishu 路径。

### PM scoped valuation-evidence 契约

- 在既有 POST /api/v1/analysis/valuation-evidence 请求体增加可选 holdings_scope，允许 all（缺省，旧调用语义不变）或 non_futu；响应 scope 回显该值。OM 开启补充时显式传 non_futu，并拒绝缺少或不匹配的 scope 回显。未升级的 PM 对此新请求不能被当作已支持过滤。
- non_futu 的 PM 仓储 owner 对每个请求账户调用既有 get_raw_holdings(account)，完整取得 Feishu 原始行且不发布本地缓存；读取失败、返回跨账户行或无效的 record_id/fields 外壳使该账户 unavailable。先按原始 broker 分类和计数，再仅将已核实非富途行交给既有严格转换与重复身份校验；被排除的富途副本数量、资产类型或价格字段损坏不污染本次估值。空/无法判定 broker 即使数量为零也计为 unknown、排除估值并令账户 partial。已核实非富途零数量行单独计数但不估值；负数量非现金行不得在分类前静默消失，按现有有符号市值合同纳入，若该资产类型或价格不能可靠估值则该账户 partial 并说明。转换失败、身份重复或来源与投影行不一致时不可称 complete。
- 在 fetch_price_snapshot 和 calculate_valuation 前完成 PM 的富途/unknown 过滤；过滤作用于 holdings、报价请求、account_status、warnings、最终 status 和可核对的 source/included/excluded/zero/unsupported 计数。non_futu 响应按账户回传从同一完整原始切片取得的每个 broker 原文、分类、行数及总行数，包含排除行和零数量行；无完整清单或计数不合时 OM 不接受补充。PM 不需要读取或存储 OM 的批准集合，也不新增估值请求字段；OM 对照同一配置生成物的批准集合。成功读取、来源均可解释且零合格行才是 complete 的空 Holdings 集，不是读失败；未知 broker、未支持负仓位不能冒充 ready_empty。
- non_futu 的 pm.holdings_feishu 只证明本次完整 PM 表切片读取、账户范围和行结构；逐行保留该读取时间及记录 updated_at（缺失明确为 unknown），不把任一时间称为券商实时数量证明。用户选定 PM Holdings 是非富途的记录来源，trust 仅指该记录来源完整且此次估值证据可核对。pm.holdings_quantity 质量项来自富途同步回执，不参与 non_futu 的数量判定。记录长期未变更不自动等于数量错误；也不得以本次读取时间改写记录更新时间。
- 仅对纳入且非零的行校验报价与必要 FX。CNY 现金/MMF 的 fixed_identity 单价为 1，不要求市场成交时刻或外部 FX，完整性由 Holdings 读取与数量、行结构证明。其余报价要求价格有限且大于零、来源明确、取数/缓存时间有效、缓存未过期且非 fallback；这些条件满足即可按现有 PM 报价缓存合同参与 complete，但只声明“取数/缓存有效”，不声称市场价实时。若上游有已核实语义的市场时刻，另存 market_as_of 并检查其有效性；缺少该字段时明示 market_as_of=unknown，不把 fetched_at 冒充市场时刻，也不单凭缺此字段降为 partial。已知市场时刻失效、价格或缓存失效才使相关估值 partial。外币现金/MMF 的 fixed_identity 单价同样不要求市场时刻，但必须核验必要 FX。PM 定价 owner 为所有需换汇行附独立 FX 来源、取数/缓存时间和 stale/fallback 状态；24 小时以内且非 fallback 才能称 FX fresh，不能仅凭正汇率或价格 fetched_at 判定。FX 失效、必要证据缺失或金额无法换算时该账户 partial/unavailable、逐行说明；CNY 恒等换算不要求 FX 外部证据。来源读取成功但零合格行不要求无关的 pm.prices/pm.fx artifact。HTTP 包装器不能再用全量账户级价格/FX artifact 覆盖 scoped freshness；all 请求继续原合同。
- OM PM client 验证响应 schema、账户、holdings_scope、snapshot、时间、账户状态、行来源及原始 broker 清单，再交给领域投影；不能只在 OM 过滤完整 PM 响应并继承旧 status/freshness。PM 服务端与 OM 客户端的版本兼容由请求字段及响应回显检测，失败时保留富途基线、情景 partial。
- 配置启用预检复用同一 scoped 请求，并展示本次原始切片中各账户 broker 原文、行数及 futu/non_futu/unknown 分类供确认；真实取值尚未在本设计阶段读取。成功且零合格行、无 unknown/unsupported 时显示 ready_empty/0 eligible，并为每个已配置账户写入可为空的批准集合。启用预览将排序后的账户→非富途原文集合纳入待发布 YAML、生成配置及 preview_sha256；apply 重新读取 PM 并要求集合与预览一致，变化则报 stale preview、禁止写入。config_validator 对存在的集合验证账户覆盖及无重复的非空字符串列表；旧配置即使 enabled=true 且缺集合仍可加载，但查询必须暂停 PM 补充、标 partial 并提示经现有预览/确认流程补齐，不能把缺集合解释为空集合。enabled=false 时保留但忽略既有集合，下次启用重新预览覆盖。读失败、unknown broker、scope 不匹配或必要质量不可证均拒绝启用。只在部署支持清单字段的 PM、实际券商归属经核对后才允许新开关启用；生产配置及部署另行授权。

### 富途报价、汇率与质量

- 用所请求账户富途股票及富途短期权底层标的去重形成报价集合，同一请求内对每个标的只取一次 OpenD 行情，同价用于富途现有股和富途期权假设指派新增股。非富途 PM 持仓按 PM scoped 证据估值；非富途期权终局估值不借用富途报价冒充券商完整基线。先用现有 quote route；无 watchlist binding 时取所请求账户已用于持仓读取的富途连接设置；端点冲突或身份不明则报价不可用，不任意挑第一个账户。
- 估值判定使用原始 last_price、update_time、sec_status、suspension、market_state 与 symbol/currency 一致性；不得仅凭开仓观察的 market_closed status 放行。价格必须有限且大于零，更新时间必须有效且不在未来，证券状态正常且未停牌。连续交易时，300 秒以内可视为 fresh；超过 300 秒但不超过 7 个日历日仅可作为标时的 partial 估值。闭市且最近价格不超过 7 个日历日也仅作 partial 估值；更旧或无效报价不传入领域 quote map，相关市值与分布标记不完整。300 秒用于交易时新鲜度，7 日是保守的失效上限，不新增运行配置；长假可显式不完整。
- 同次情景由 OM 应用层调用现有 fetch_market_exchange_rates 一次，取得一个腾讯或新浪观察；逐账户富途余额仍各自读取，但 context 接受应用层传入的该观察并跳过内部重复 FX 获取，现金和报价估值都使用同一观察及 source/observed_at。所有账户共享其质量结果；失败时共同标为 FX 缺失，不能从某账户上下文另挑可用汇率。CNY 恒等 1；USD/HKD 等仅在现有 24 小时质量门通过时换算。缺 FX 时不伪造 CNY 金额；非 CNY 现金和股票的相关结果 partial。PM 非富途资产的 CNY 市值、价格及 FX 由 PM scoped 证据负责，不用 OM 汇率重算。
- 构造 portfolio.valuation_evidence.v1 时，从每个实际来源的完整性、时间和 trust 推导 status/freshness；保留 Futu、PM、quote、FX、账本各自时间和 snapshot identity。完整富途基线与 PM 补充失败并存时，聚合 status=partial，PM 缺失作为单独原因；已纳入富途数据的 freshness/trust 仍按其自身证据判断，不把 PM unavailable 覆盖成聚合 unavailable，也不伪称 PM fresh/trusted。与此相反，富途完整性失败直接 unavailable。不会因为函数成功返回就设置 fresh/trusted；过期 quote 即使有 is_stale 标志也必须使整体 partial，因为领域 _quote_values 不检查该标志。

### 失败与状态

| 情况 | 行为 |
| --- | --- |
| 富途某账户身份不匹配、cash_balance_reliable 不为 true、position_snapshot_input 缺失或 errors 非空 | 整体 unavailable；不使用 PM 镜像补齐。 |
| 某标的报价或 FX 缺失、异常、过期 | 该金额未知；其余已知行可展示；整体 partial，不回退 PM 富途报价。 |
| PM 只因被排除的富途副本缺价 | scoped 响应不受影响；以真正纳入行的证据判定。 |
| PM 请求失败或返回旧版/错 scope | 保留富途基线，标记 partial 与 PM 补充缺失；不谎称完整。 |
| PM 原始 broker 清单缺失/不一致，或出现同账户未批准的新 non_futu 原文 | 本次整份 PM 补充不进入投影；保留富途/账本基线并标 partial，列出账户和新原文，提示重新预览确认。已批准名称的持仓行数变化不影响纳入。 |
| 旧 enabled=true 配置缺批准集合，或新账户没有对应集合 | 配置仍可加载；只读查询保留富途/账本基线，但不纳入 PM 补充并标 partial，提示重新预览确认。 |
| PM 成功返回零合格非富途行 | 有效零补充；若其余证据完整，仍可 complete。 |
| PM 行 broker/账户不明、非富途行估值缺失 | 排除不明行或保留金额未知；整体 partial，逐行说明。 |
| PM 原始外壳损坏、跨账户行或读取失败 | 对应账户 unavailable，组合保留富途基线但整体 partial；不能当作零补充。 |
| PM 非富途零数量或负数量非现金行 | 零行计数但不估值；负行按有符号市值纳入，无法可靠估值则 partial，不可在 broker 分类前丢弃。 |
| PM 报价只有有效取数/缓存时间、无市场时刻 | 可按现有 PM 报价缓存合同估值并明示 market_as_of=unknown；不声称市场实时，也不单凭缺市场时刻降为 partial。 |
| PM 已知市场时刻失效，或 FX 缺少独立来源/时刻、回退过期缓存 | 受影响估值 partial；FX 数值不能单独证明 fresh。 |
| 非富途短期权缺少同账户、券商的可证实起始现金或正股完整基线 | 保留指派事实与已知数量变化；相关终值未知，总分布 partial，富途 cash_coverage 不混入该期权。 |
| 富途账户现金为负，其他券商现金为正 | 按账户、券商保留两条有符号行；总资产与负债分别计算，净额仅用于摘要。 |
| 同一富途账户不同币种现金一正一负 | 各币种先保留原币种指派后正负值，再按同次 FX 折成 CNY 计算总资产和负债；跨币种净额仅作经济摘要。 |
| 富途报价闭市但在保守时间窗内 | 可显示带时间的估值，整体 partial；过窗不计价。 |

## 不采用的方案

- PM Holdings 完全替代富途账户仓位：PM 有富途股票、现金、MMF 副本且刷新时差不同，可能重复或覆盖账户事实。
- 开关关闭后仍调用 PM 取行情：增加了不必要的依赖，富途价也失去同源一致性。
- OM 事后过滤 PM 全量估值响应：PM 已为排除行询价，并已用它们决定 status/freshness。
- 把 PM 非富途现金直接并入富途 CSP 覆盖：跨券商可用性没有证据。
- 新建第二个 Holdings 开关、独立 Feishu 读链或新计算 owner：现有开关、PM API 与领域投影足以承载。

## 实现切片与验证

| Slice | 可独立观察的行为增量 | 覆盖信号 | 依赖 |
| --- | --- | --- | --- |
| A 富途基线与隔离投影 | 关闭时只读富途和全部券商短期权账本；完成 (account, broker, currency) 富途现金桶、富途期权原币种指派与 CNY 经济覆盖，非富途期权终值未知，不混入富途覆盖；同一 OpenD 报价、单次 FX、现金/MMF 去重、富途完整性标志与缺价/闭市质量；同步更正 Tool Gateway 来源标签、CLI 文案及只读 public facade。 | S1、S3、S4 的关闭态、S6 | 无 |
| B PM scoped 证据 | PM 旧 all 行为不变；新 non_futu 请求对只读完整原始切片先分类再转换/报价，沿用 broker 自由文本合同并排除富途/unknown，按来源区分恒等单价、报价缓存和独立 FX 质量，回显 scope、每账户完整 broker 原文/分类/行数与计数。 | S2、S5 | 无 |
| C 启用补充 | OM 只在开启时请求 B，独立校验 scope/原始 broker 清单与配置中按账户批准集合，再把 PM 非富途资产加到 A 的有符号分布；新值或坏清单则整份 PM 补充 partial；受非富途期权影响的券商终值仍未知，PM 账户级质量不当作券商完整基线；调整同一开关的预检、预览摘要、配置发布/回读、输出与文档。 | S2、S4 的开启态、S5、S6 | A、B |

验证以隔离 fixture 为主，不接触真实券商/PM/Feishu：PM 服务路由→服务→估值及 OM HTTP 客户端的双端契约测试；OM public Tool Gateway/CLI 与领域计算测试。Slice A 必须先覆盖 off 零 PM 请求、富途 100 CNY 现金 + 非富途 200 CNY Put 时富途资金缺口不变且非富途终值未知、非富途期权与富途股票同代码时不得借用富途报价、同一富途账户 USD 折合 -100/HKD 折合 +200 时资产 200/负债 100/净额 100、跨账户/币种经济净额为零缺口但分桶负债保留、全部券商指派数与仅富途资金文案及静态来源标签、Futu/MMF 去重、现金可靠性与仓位 errors、报价过期/闭市/异常、多账户同一 FX 与 FX 缺失。Slice B/C 再覆盖 on 富途三类副本及 moomoo/中英文别名排除、`Moomoo US` 自动排除、具体非富途名称须经预览确认才纳入、manual/泛称 fail closed、配置预览显示 broker 原文/行数/分类并将非富途集合绑定 preview/apply、已批准名称行数变化、预览后新增名称使 apply 拒绝、启用后出现未批准具体名称使全部 PM 补充 partial、PM 清单缺失/计数不合、旧 enabled=true 配置无清单仍能加载但补充 partial、新账户无集合、开关关闭不依赖批准集合、OM 归一化对账本身份消费者的影响、同符号跨券商、PM 非富途现金不增富途资金、PM 非富途期权影响桶仍 partial、零合格行与无关价格/FX artifact 缺失、未知 broker 行（含零数量）、负数量非现金行、排除富途行只有估值字段损坏与原始外壳损坏的不同结果、scoped 请求不写本地缓存、CNY 现金 fixed_identity 可 complete、普通报价无市场时刻可 complete 且 market_as_of=unknown、已知市场时刻失效为 partial、FX 过期回退/无独立时刻、旧版 PM、跨账户行、PM 失败与 snapshot/status 一致性。文档检查核对 docs/INDEX.md 引用及 git diff --check。开发、交付和运行环境升级均另行授权。

## 风险与待验证项

- R1（PM 服务 owner，Slice B）：PM 的质量 artifact 当前按账户/数据集汇总，scoped freshness 需要以实际纳入行的报价取数/缓存质量与独立外汇证据核验。无市场时刻的报价仍可按现有缓存合同参与 complete，但必须明示 market_as_of=unknown，不声称市场实时；FX 无来源/时间或过期回退必须 partial，不得声称排除富途副本后自动 trusted。
- R2（OM 报价 owner，Slice A）：节假日超过 7 日会使闭市估值不可用，这是保守失败；Improve Design 核对现有市场日历能力后可在同一文档内细化，不默认放宽。
- R3（OM 领域 owner，Slice A；PM 补充在 Slice C）：现有 starting_cash 同时服务分布与资金覆盖，现金又在计算总资产/负债前净额化；先以账户/券商/币种隔离核对 cash_coverage、account_breakdown、expiry_ladder、distribution/by_code 及负债消费者。PM 的账户级 Holdings 证据不足以证明非富途券商起始资产完整，不得用 PM 持仓缺行或 OM 富途 FX/报价补出假完整终值。
- R4（跨仓部署 owner，交付阶段）：PM scoped API 必须先于 OM 启用路径部署；未升级时 OM 明示 partial，运行环境变更不由本设计阶段执行。
- R5（OM 富途适配 owner，Slice A，待证据）：OpenD 若把已计入余额的 MMF 又返回为 FUND 仓位，现有 position_asset_type_unknown 可能使股票快照不完整。先取得隔离的 OpenD 行形态；证实后仅跳过与现金/MMF 明确同一资产的仓位并加回归例，不放松其他未知证券类型错误。
- R6（PM 数据源 owner，启用前）：实际非富途 broker 值尚未经只读核对。沿用 PM 的自由文本来源合同，不预置无数据依据的代码白名单；futu、moomoo、富途前缀归富途，空值、泛称和 manual 归 unknown。配置预览给出实际 broker 原文、行数和分类，经确认的非富途原文集合由现有配置发布链保存，查询遇新值则整个 PM 补充 partial。单个已批准 broker 被错误标成非富途仍可能误算，来源字段真实性需由目标数据预览与 PM 记录管理保证；本设计不能宣称真实清单已验证。
