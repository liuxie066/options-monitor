# 产品架构

本文是 `options-monitor` 的产品架构权威说明。它定义产品域、模块责任和模块间依赖关系；具体技术分层见 [ARCHITECTURE.md](ARCHITECTURE.md)，具体开仓策略细节见 [STRATEGY_ARCHITECTURE.md](STRATEGY_ARCHITECTURE.md)。

## 架构原则

产品架构按用户要解决的问题划分，不按代码目录划分。

系统固定为 5 个产品域：

1. 开仓机会监控
2. 持仓管理
3. 运行与通知
4. 配置与控制面
5. 研究与复盘

数据、证据、外部适配、存储和 domain 规则是横向支撑能力，不作为独立产品域。

```text
配置与控制面
  -> 运行与通知
    -> 开仓机会监控
    -> 持仓管理
  -> 研究与复盘

横向支撑能力
  -> 行情 / RV / event risk / portfolio context / ledger context
  -> candidate trace / output_runs evidence / SQLite / reports
  -> OpenD/Futu / Feishu / exchange-rate adapters
  -> deterministic domain rules
```

## 产品域

### 1. 开仓机会监控

定义：回答“现在是否值得开仓、开什么、哪个候选最优”。

包含策略：

- Cash-Secured Put (CSP)
- Covered Call (CC)
- Combo Yield

当前状态：

- CSP / CC 已完成本轮 `insurance_underwriting` 语义重构。
- Combo Yield 产品上属于平行开仓策略，运行编排已从 CSP 模块迁出到独立模块；当前 runtime key 为 `combo_yield`。当前仅支持 `same_expiry_pair`：Funding Put 复用完整 CSP underwriting 结果，再配对同到期、可由 Put 净收入覆盖成本的 Long Call。成本约束统一为 `min_net_credit_retention`，排序以 retention 优先、跨标的以 Funding Put 期间非年化净收益主导；候选写入独立的 run/account 级 sealed snapshot，消费者从 CSV 切换为快照。

标准生命周期：

```text
召回 universe
  -> 硬筛
  -> 排序
  -> 输出解释 / trace
```

边界：

- 开仓机会监控只推荐候选，不记录真实成交，不修改持仓账本。
- 排序用于推荐最优候选，不能替代硬风险阈值。
- Combo Yield 使用独立的组合腿结构、组合资金关系和组合排序，不再作为 CSP overlay 扩展；仅支持 `same_expiry_pair` 同期两腿，不把不同期限的两腿压成单一组合年化或 scenario 指标。正式排序唯一存在于 domain / seal 时，Daily Brief 只读快照结果。
- 推荐身份 `candidate_pair_id` 与真实成交意图 `pair_intent_id` 分离；没有显式 intent 时只记录单腿，不猜测持仓关系。

主要实现位置：

- `src/application/pipeline_runtime.py`
- `src/application/pipeline_watchlist.py`
- `src/application/symbol_monitoring.py`
- `src/application/sell_put_steps.py`
- `src/application/sell_call_steps.py`
- `src/application/combo_yield_steps.py`
- `domain/domain/insurance_underwriting.py`
- `domain/domain/engine/candidate_engine.py`

### 2. 持仓管理

定义：回答“已有仓位是否应该继续持有、关闭、接受指派、被行权或进入生命周期处理”。

包含模块：

- Trade Intake
- Ledger / Position Lots
- Position Lifecycle
- Close Advice
- Position / Income Reports

核心事实模型：

```text
trade_events
  -> projection
  -> position_lots
  -> position context / close advice / reports
```

边界：

- Ledger / projection 是持仓事实来源。
- Feishu `option_positions` 不是 source of truth。
- Close Advice 是持仓管理能力，不是开仓策略。它只对 short put/call
  判断严格止盈平仓，不比较新候选，不生成 roll、replace 或 reallocate。
- 除 `close` 外的可评估持仓统一为 `hold`；证据不全为 `not_evaluable`。

主要实现位置：

- `src/application/ledger/api.py`
- `domain/domain/ledger/projection.py`
- `src/application/positions/`
- `src/application/trades/`
- `src/application/close_advice_runner.py`
- `domain/domain/close_advice.py`

### 3. 运行与通知

定义：负责生产运行、调度、多账户执行、运行证据写入和通知交付。

包含模块：

- Tick Scheduler
- Multi-account Tick
- Account Run
- Required-data / Event Prefetch
- Notification Preparation / Delivery
- Runtime Status / Healthcheck

运行链路：

```text
tick
  -> config freshness / schedule guard / OpenD guard
  -> per-account account_run
      -> expired position maintenance
      -> required_data prefetch
      -> event prefetch
      -> scan pipeline
      -> close advice
  -> notification flow
  -> run state / audit
```

边界：

- 运行域负责编排，不拥有策略判断。
- 通知是结果消费方，不反向影响扫描和 Close Advice 的决策。
- 有副作用的外部通知、服务变更和持仓写入必须走显式权限边界。

主要实现位置：

- `src/application/multi_account_tick.py`
- `src/application/account_run.py`
- `src/application/tick_account_execution.py`
- `src/application/tick_notification_flow.py`
- `src/application/healthcheck.py`
- `src/application/agent_tools/runtime_status_impl.py`

### 4. 配置与控制面

定义：负责用户意图、系统默认值、runtime 快照、账户/标的管理，以及 CLI / Tool Gateway / Inbound Assistant 入口。

包含模块：

- `config.yaml` authoring config
- generated runtime config
- account / symbol management
- CLI
- Tool Gateway tools
- Inbound
- settings / setup / service operations

配置链路：

```text
system defaults
  + config.yaml
  -> build runtime config
  -> validate
  -> tick / scan / close advice / inbound
```

入口链路：

```text
./om
  -> src.interfaces.cli.main
  -> application use cases

./om-agent
  -> src.interfaces.agent.cli
  -> tool_execution
  -> application use cases

Feishu / WeChat / Inbound
  -> channel adapter
  -> explicit command or permission response -> deterministic Control
  -> all other text -> Copilot Service -> Host -> Agent
  -> canonical application tools and use cases
```

边界：

- CLI / Tool Gateway / Inbound Assistant 是入口和控制面，不拥有业务规则。
- Tool Gateway 工具默认应优先读现有证据；写路径必须受 preview / confirm / env gate 控制。
- 配置默认值应集中在配置层，不应散落到策略实现里形成第二套控制面。

主要实现位置：

- `src/application/config_defaults.py`
- `src/application/config_yaml.py`
- `src/application/config_validator.py`
- `src/interfaces/cli/`
- `src/interfaces/agent/`
- `src/application/agent_tool_registry.py`
- `src/application/assistant/`
- `src/application/inbound/`

### 5. 研究与复盘

定义：读取历史运行证据，形成可复盘 dataset，并在证据足够时评估策略质量、参数假设和策略进化建议。

包含模块：

- Research Archive
- Shadow Replay
- Strategy Lab
- Strategy Quality / Readiness Gates

证据链路：

```text
output_runs / required_data / sealed candidate snapshot / candidate trace / marks / outcomes
  -> Research archive / evidence bundle
  -> Shadow Replay dataset
  -> readiness / candidate-impact
  -> Strategy Lab Recipe research / hidden validation / receipt (under implementation)
  -> human review / shadow rollout / manual promotion
```

边界：

- 研究与复盘只产出建议和证据，不直接修改生产配置。
- 参数假设讨论必须基于 replay / snapshot / trace / outcome 证据，而不是只看终态候选。
- Research 是证据基础设施；Shadow Replay 是反事实复盘引擎；Strategy Lab 是策略进化产品入口。
- Shadow Replay 直接提供 dataset、mark、outcome、候选影响和探索性复盘；Strategy Lab 不包装这些维护入口。
- Strategy Lab 当前只实现了根级 history-K readiness、普通 HK / `lx` context 和三表 Store 基础；Recipe、preview、20 日研究、10 日隐藏验证和回执尚未完成。
- Formal Strategy Lab 不声称绝对最优；只有冻结合同、完整事实和确定性评价满足门槛时才形成可采用建议。
- 在线生产监控和离线策略研究保持分离。

主要实现位置：

- `src/application/research/`
- `src/application/shadow_replay/`
- `src/application/strategy_lab/`、`src/infrastructure/strategy_lab/`
- `docs/STRATEGY_LAB_DESIGN.md`
- `docs/SHADOW_REPLAY_RUNBOOK.md`
- `docs/OPPORTUNITY_QUALITY.md`

## 横向支撑能力

横向支撑能力被多个产品域共享，但不单独构成产品域。

| 支撑能力 | 定义 | 典型消费者 |
|---|---|---|
| 行情与 required data | 期权链、quote、DTE、IV、delta、multiplier 等开仓/平仓输入 | 开仓机会监控、Close Advice、Research |
| Event Risk | 财报、除权等事件快照和状态 | 开仓机会监控、Research |
| Portfolio / Ledger Context | 现金、正股、锁定股数、position lots、risk view | 开仓机会监控、持仓管理 |
| Candidate Trace / Run Evidence | 开仓候选拒绝原因、排序证据、运行状态、审计事件 | Tool Gateway 工具、Research、排障 |
| Close Report Evidence | 严格持仓决策、封存输入绑定、report manifest 和运行审计 | Daily Brief、Close Replay、人工查询 |
| Storage | SQLite、`output_runs`、`output_shared`、reports、state | 全部产品域 |
| External Adapters | OpenD/Futu、Feishu、exchange rate、subprocess | 运行与通知、数据采集、Inbound |
| Domain Rules | 确定性策略、账本、通知、调度和 schema 决策 | Application use cases |

## 当前实现与目标差距

当前已对齐：

- CSP / CC 的开仓语义已经从 `short_vol` 转为 `insurance_underwriting`。
- 开仓配置不再接受 `strategy=short_vol`。
- Combo Yield 已有独立开仓编排模块，不再由 `sell_put_steps.py` 拥有组合收益的 trace、summary 和 alert 决策；Funding Put 仍通过显式依赖复用 CSP underwriting。
- 已有两腿（含历史错期组合）可用精确 lot id 原子登记 `pair_intent_id` 和共享 `strategy_group_id`，不做启发式匹配。
- Close Advice 已收敛为固定 `strict_profit_capture.v1`，不读取 `short_vol` thesis、事件、delta 或集中度。
- Research / Shadow Replay 与生产执行保持分离。

下一步目标：

```text
opening strategies
  -> sell_put
  -> covered_call
  -> combo_yield

position management
  -> ledger / lifecycle / close_advice / reports
```

Combo Yield 详细开仓策略、authoring/runtime key、内部模块和函数名已对齐。旧 `yield_enhancement` 只存在于历史 ledger/artifact 的只读解释边界；活动配置、策略和账本写路径均不再接受或生成旧字段。
