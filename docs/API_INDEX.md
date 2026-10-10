# 共享能力与 API 索引

新增功能涉及共享数据或计算时，先按业务词查本页，再读对应入口、契约及直接调用者。这里只记录可复用入口和责任边界；参数、类型、公式、TTL、舍入及错误码由源码和契约维护。

本页覆盖当前核心共享能力，不是全部函数或 HTTP endpoint 清单。Python 入口供项目内部开发；人工操作用 `./om`，结构化操作用 `./om-agent`。完整 Tool schema、权限和副作用从现有 `./om-agent spec` 查询，参见 [Tool Reference](TOOL_REFERENCE.md)。运行环境采用哪个实现，仍须按 [Docs Index](INDEX.md) 核验实际版本和证据。

## 如何复用

1. 确定业务场景、账户/券商/市场范围、事实时间及用途；“都用了汇率”不代表当前容量与历史记账可以共用一个报价。
2. 选择入口：正式 tick 消费已封存的同批次事实；独立查询由请求的编排层取一次事实；领域计算接收已验证输入。纯计算入口不能替代取数、完整性或时效校验。
3. 跟踪入口的直接调用者和共享测试，保留来源、身份、质量及不可用原因。调用成功不代表业务数据完整；缺失、过期、部分结果和有效零结果不能互换。
4. 已有能力缺字段时，先在责任 owner 扩展，再让消费者复用；确认现有入口不能承担该职责后才新增。不要在 renderer、CLI、Tool 或策略适配器中另写取数、缓存、时效、回退或公式。

## 数据、身份与快照

下表入口状态均为**现行**。联网查询、缓存写入和封存写入分别标注；只读本地快照不代表它仍满足决策时效。

| 能力 / 检索词 | 推荐源码入口 | 使用边界与事实归属 | 契约与共享测试 |
| --- | --- | --- | --- |
| 标的身份、alias、symbol、市场/币种 | `domain/domain/symbol_identity.py::resolve_symbol_identity`、`domain/domain/symbol_identity.py::canonical_symbol` | 纯身份解析；新入口复用显式别名与 canonical symbol，不在下游宽松猜测或持久化别名。 | [标的规范](GUARDRAILS.md#c-symbol-canonicalization-rule)；`tests/test_symbol_normalization_contract.py`、`tests/test_trade_symbol_identity.py` |
| 合约数量、multiplier、股数、strike identity | `domain/domain/trade_contract_identity.py::contract_share_quantity`、`domain/domain/trade_contract_identity.py::require_option_multiplier` | 纯校验/数量换算；使用已证明的合约乘数，缺失不能默认成 100。身份与数量规则不由报表重建。 | [订单需求](ORDER_DOMAIN_MODEL_PRD.md)；`tests/test_trade_contract_identity.py` |
| 配置、defaults、层叠、有效 runtime | `src/application/layered_config.py::build_layered_runtime_config_from_user_config`、`src/application/config_loader.py::load_config` | 构建与加载复用现有 owner；YAML 经支持的 build 生成 runtime JSON。scheduled 加载可能写验证缓存，不能当作无副作用 reader。 | [配置合同](../CONFIGS.md)；`tests/test_config_loader_validation_cache.py`、`tests/test_account_config.py` |
| 行情、期权链、required-data、批次快照 | `src/application/required_data_snapshot.py::resolve_frozen_required_data_csv_bytes_batch`、`src/application/required_data_snapshot.py::resolve_frozen_required_data_csv_bytes` | 只读 owner 校验的 manifest/blob；批量消费先解析一次。取数/封存由既有 prefetch 与 snapshot owner 承担，消费者不能失败后自行抓取另一份报价。 | [Required Data](REQUIRED_DATA_STORAGE_DESIGN.md)；`tests/test_required_data_snapshot.py`、`tests/test_required_data_coverage.py` |
| 当前汇率、FX、CNY、display/capacity | `src/infrastructure/exchange_rates.py::current_exchange_rate_snapshot`、`src/infrastructure/exchange_rates.py::project_exchange_rate_snapshot`、`src/infrastructure/exchange_rates.py::rates_for_purpose` | 独立请求取一次快照，再按用途投影并重验资格。取快照会联网、默认写缓存；`write_cache=False` 关闭缓存写入。投影不重新取价；报价时效/休市政策由 FX owner 负责。 | [当前 FX](EXCHANGE_RATE_FACT_DESIGN.md)；`tests/test_current_exchange_rate_snapshot.py`、`tests/test_unified_fx_consumers.py` |
| tick 汇率、run_id、同批次同价 | `src/application/current_fx_run.py::load_run_fx_snapshot`；编排写入口 `src/application/current_fx_run.py::seal_run_fx_snapshot` | worker 读同一封存快照/hash；编排层封存一次。损坏、冲突或不可用不能由消费者补抓覆写；晚到消费仍重验用途资格。 | [同批次事实](EXCHANGE_RATE_FACT_DESIGN.md#同批次事实)；`tests/test_current_fx_run.py` |
| 券商资金/持仓、cash snapshot、portfolio context | `src/application/prepared_portfolio_context.py::load_prepared_portfolio_context`；独立请求 `src/application/portfolio_context_service.py::load_account_portfolio_context` | 正式 worker 读取绑定 run/account/config 的 prepared context；独立加载可能查询 OpenD 并默认写账户缓存。账户现金和持仓来源不能由 PM 镜像补成可信；现金资格复用同模块的 `src/application/portfolio_context_service.py::cash_snapshot_is_usable`。 | [资金与持仓边界](EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md)；`tests/test_prepared_portfolio_context.py`、`tests/test_futu_portfolio_context.py` |
| 历史汇率、事件现金、cash_conversion | `src/application/cash_conversion.py::attach_trade_event_cash_conversions`、`domain/domain/performance/cash_conversion.py::select_cash_fx_rate` | 事件写入路径组装已验证的事件时点转换证据；持久化仍经 ledger owner。历史报告消费已落库证据，缺失走现有 preview/backfill，不拿当前 FX 回写历史。 | [历史 FX](EXCHANGE_RATE_FACT_DESIGN.md#历史现金与绩效)、[绩效](OPTION_PERFORMANCE_DESIGN.md)；`tests/test_cash_conversion_at_write.py`、`tests/test_cash_conversion_backfill.py` |
| PM、非富途资产、valuation evidence | `src/application/portfolio_assignment_scenario.py::read_portfolio_valuation_evidence`；客户端 `src/infrastructure/portfolio_management_client.py::PortfolioManagementClient` | 联网读 PM 并经客户端验证 schema、scope、账户与来源证据；非富途补充使用 scoped 响应，不用 OM 的现价/FX 重算 PM 已估值事实。连接复用 `src/application/portfolio_management.py::resolve_portfolio_management_client`。 | [资产情景](PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md)；`contracts/portfolio-management/v1.openapi.json`、`tests/test_portfolio_management_client.py`、`tests/test_portfolio_management_contract_vendor.py` |

## 计算与决策

下表领域入口为**现行纯计算**；应用入口负责组装证据。金额单位、乘数、价格 tick、费用来源、时间窗口、舍入及缺失值处理沿用该 owner 的契约，不在索引复制数值或公式。

| 能力 / 检索词 | 推荐源码入口 | 使用边界与计算归属 | 契约与共享测试 |
| --- | --- | --- | --- |
| 单腿开仓、candidate、收益、filter/rank | `domain/domain/engine/candidate_engine.py::calculate_opening_candidate_metrics`、`domain/domain/engine/candidate_engine.py::evaluate_opening_candidate_policy`、`domain/domain/engine/candidate_engine.py::rank_candidate_rows` | 规范化 OpenD 输入进入同一开仓计算、策略判定与排序；bid/ask、乘数、匹配波动率、价格取整和缺失语义保持原合同。组合策略仍进入其既有策略 owner。 | [候选策略](candidate_strategy.md)、[策略架构](STRATEGY_ARCHITECTURE.md)；`tests/test_candidate_engine_phase2_contract.py`、`tests/test_candidate_engine_parity.py` |
| 费用、fee、开仓估算 | `domain/domain/fee_calc.py::estimate_futu_option_sell_fee` | 候选阶段统一费用估算及版本/来源证据；不能拿候选估算冒充成交后的实际费用。成交和绩效复用持久化 fee provenance。 | [候选策略](candidate_strategy.md)、[绩效](OPTION_PERFORMANCE_DESIGN.md)；`tests/test_fee_calc.py` |
| CSP 现金、CC 覆盖、担保、capacity | `domain/domain/risk_capacity.py::compute_sell_put_cash_capacity`、`domain/domain/risk_capacity.py::compute_sell_call_share_capacity`、`domain/domain/risk_capacity.py::evaluate_cash_snapshot` | 从账户现金/持仓、账本占用和有效 FX 计算能力；用途及可用判定不能由余额为正推断。多策略共享占用继续经现有分配 owner，不能各自承诺同一份资金/股票。 | [策略架构](STRATEGY_ARCHITECTURE.md)、[资金边界](EXTERNAL_HOLDINGS_ACCOUNT_RETIREMENT_DESIGN.md)；`tests/test_risk_capacity.py`、`tests/test_cash_secured_utils.py` |
| 仓位、集中度、Position Sizing、候选指派 | 应用 `src/application/short_vol_risk_context.py::build_portfolio_risk_context`；领域 `domain/domain/short_vol_assessment.py::portfolio_concentration_fields` | 组装账户证据后复用 `domain/domain/portfolio_assignment_scenario.py::project_non_option_assignment_assets`。当前、已有指派及一份候选共享冻结价格与有符号现金/股数；这是展示口径，不自行新增开仓门槛。 | [Position Sizing](PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md)；`tests/test_assignment_position_sizing.py`、`tests/test_position_sizing_flow.py` |
| 指派后资产分布、assignment scenario | 应用 `src/application/portfolio_assignment_scenario.py::query_portfolio_assignment_scenario`；领域 `domain/domain/portfolio_assignment_scenario.py::project_assignment_scenario` | 应用只读账本并查询富途/PM/报价/FX，领域做同一资产投影；完整性、估值和资金覆盖分别保留证据。情景计算不写入实际指派或持仓。 | [资产情景](PORTFOLIO_ASSIGNMENT_SCENARIO_DESIGN.md)；`tests/test_portfolio_assignment_application.py`、`tests/test_portfolio_assignment_scenario.py` |
| 期权绩效、cashflow、胜率、资本时间、period | 公共应用 `src/application/performance/__init__.py::build_option_period_performance`；领域 `domain/domain/performance/weighted_reducer.py::reduce_option_performance`、`domain/domain/performance/period.py::normalize_performance_period` | 应用读取一份 canonical ledger 事件快照，领域统一归约。策略只作归属/分组；原币指标和历史 CNY 现金证据不可在 renderer 再汇算。 | [绩效合同](OPTION_PERFORMANCE_DESIGN.md)；`tests/test_performance_service_v2.py`、`tests/test_performance_weighted_reducer.py`、`tests/test_performance_period.py` |
| 平仓建议、Close Advice、剩余收益 | `domain/domain/close_advice.py::evaluate_close_advice`；生成报告 `src/application/close_advice_runner.py::run_close_advice` | 领域负责政策；runner 组装输入并写报告。查询已有建议使用 `close_advice_read` Tool，不能为回答一次查询运行报告生成或另算评分。 | [平仓合同](CLOSE_ADVICE_CONTRACT.md)；`tests/test_strict_close_advice.py`、`tests/test_close_advice_required_data.py` |

## 账本、简报与策略工作流

这些入口状态均为**现行**。读取、领域投影和写入仍是不同动作；模块存在公共函数不代表可以绕过工作流直接写生产数据。

| 能力 / 检索词 | 推荐源码入口 | 使用边界与责任归属 | 契约与共享测试 |
| --- | --- | --- | --- |
| 期权持仓、lot、trade_events、ledger | 非 ledger 模块统一使用 `src/application/ledger/api.py`；读取例如 `src/application/ledger/api.py::trade_event_log`、`src/application/ledger/api.py::list_position_lot_snapshots`、`src/application/ledger/api.py::list_open_short_assignment_rows` | `trade_events -> projection -> position_lots` 为本地事实链；外部模块不直接导入 ledger 内部 repository/writer。该 facade 同时导出受控写入能力，不能把整个模块标成只读；写入经既有 trades/positions 工作流。 | [Ledger](LEDGER_ARCHITECTURE.md)；`tests/test_ledger_module_facades.py`、`tests/test_ledger_sqlite_workflows.py` |
| 决策简报、通知正文、Daily Brief、preview | `src/application/daily_decision_brief_service.py::assemble_daily_decision_brief`、`src/application/daily_decision_brief_repository.py::read_latest_daily_decision_brief`、`src/application/daily_decision_brief_renderer.py::build_daily_brief_user_view` | service 组装结构化批次证据，repository 管理版本/状态，renderer 投影正文。查询/preview 消费持久化简报，不重新扫描；真实发送和送达确认留在既有 delivery owner。 | [通知合同](OPTION_NOTIFICATION_EXPERIENCE_PRD.md)；`tests/test_daily_decision_brief_service.py`、`tests/test_daily_decision_brief_agent_tool.py`、`tests/test_daily_decision_brief_renderer.py` |
| Wheel、共享现金/股票、预留、确认复验 | `src/application/wheel/capacity.py::build_shared_cash_capacity_fact`、`src/application/wheel/capacity.py::build_shared_coverage_facts`；生命周期操作 `src/application/wheel/workflows.py` | 复用账户物理现金、账本占用、active intent 与本批次 FX；确认沿用现有复验，不能为 Wheel 再建取数/容量政策。工作流包含持久写入，按具体操作的 preview/confirm 边界执行。 | [Wheel](WHEEL_STRATEGY_PRD.md)；`tests/test_wheel_workflows.py`、`tests/test_risk_capacity.py`、`tests/test_unified_fx_consumers.py` |

## 兼容、退役与尚未实现

| 状态 | 入口 / 材料 | 新功能去向 |
| --- | --- | --- |
| 兼容，只修不增 | `src/application/notify_symbols.py` 的旧正文格式、旧 `python -m src.application.*` 人工入口 | 正文进入 Daily Brief；人工入口使用 `./om`，工具使用 `./om-agent`。完整冻结范围见 [Compatibility Freeze](COMPATIBILITY_FREEZE.md)。 |
| 兼容，迁移用途 | ledger migration/backfill 及 `ledger.api` 中相应 re-export | 仅承担旧事实 inventory/preview/apply/verify；日常能力扩展现有 commands/queries，通过 application facade 访问。 |
| 已退役 | Removed: `scripts/send_if_needed*.py` | 不作为通知入口；当前入口见 [Agent Handbook](AGENT_WIKI.md)。 |
| 批准目标与实现仍有差异 | [订单领域设计 §4.3](ORDER_DOMAIN_MODEL_DESIGN.md) 中的扁平身份目标 | 当前 `PositionLot` 仍含 `contract_key`；新增调用不能把设计目标当成已提供的 API，也不能擦除已批准目标。 |

## 维护与验证

- 修改共享入口、事实来源或契约时，同步对应 owner 文档、共享测试及本页这一行；新增业务词补在现有能力行，避免再写平行手册。公式、schema 和政策值只在其 owner 维护。
- 源码符号写成 `module.py::symbol`（表中均使用真实仓库路径），文件/测试使用完整仓库相对路径，文档使用本地 Markdown 链接。现有 guardrails 检查 living-doc 路径、明确命名的模块符号（定义或显式导入）及本地 Markdown 文档目标；从源码静态解析，不导入或执行业务模块。staged 模式读取 Git index 的文档与源码。
- 本地运行 `./.venv/bin/python scripts/guardrails_check.py --check-doc-wording`；同一检查已接入 commit hook 与 CI。门禁确认入口引用存在，业务复用是否正确由调用链、契约及相应测试证明。仓库已有 import boundaries 继续约束层间访问。

本页不定义新的权限：生产写入、发送、发布和升级按 [AGENTS](../AGENTS.md) 与各自受控流程执行。
