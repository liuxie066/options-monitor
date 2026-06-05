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

- Sell Put
- Covered Call
- Combo Yield

当前状态：

- Sell Put / Covered Call 已完成本轮 `insurance_underwriting` 语义重构。
- Combo Yield 产品上属于平行开仓策略，运行编排已从 Sell Put 模块迁出到独立模块；当前 runtime key 为 `combo_yield`，详细策略已按价格边界、融资经济性、call 参与质量和执行质量重构，不继承 Sell Put / Covered Call 的 underwriting RV、event 或 gate。

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
- Combo Yield 使用独立的组合腿结构、组合收益、组合风险和组合排序，不再作为 Sell Put overlay 扩展。

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
- Close Advice 是持仓管理能力，不是开仓策略；其中 `short_vol` thesis 是持仓/平仓语义，本轮不重命名。

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
- `src/application/agent_tool_runtime_status.py`

### 4. 配置与控制面

定义：负责用户意图、系统默认值、runtime 快照、账户/标的管理，以及 CLI / Agent / Assistant 入口。

包含模块：

- `config.yaml` authoring config
- generated runtime config
- account / symbol management
- CLI
- Agent tools
- Assistant / inbound
- settings / setup / service operations

配置链路：

```text
system defaults
  + config.yaml
  -> build runtime config
  -> validate
  -> tick / scan / close advice / assistant
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

Feishu / Assistant
  -> inbound
  -> assistant perception / reasoning / action
  -> tool_execution
  -> application use cases
```

边界：

- CLI / Agent / Assistant 是入口和控制面，不拥有业务规则。
- Agent 工具默认应优先读现有证据；写路径必须受 preview / confirm / env gate 控制。
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

定义：读取历史运行证据，评估策略质量、参数假设和 replay readiness。

包含模块：

- Research Archive
- Shadow Replay
- Strategy Quality Analysis
- Parameter Advice Gate

证据链路：

```text
output_runs / required_data / candidate trace / reject logs / marks / outcomes
  -> dataset
  -> analysis
  -> advisory evidence
```

边界：

- 研究与复盘只产出建议和证据，不直接修改生产配置。
- 参数建议必须基于 replay / trace / outcome 证据，而不是只看最终候选 CSV。
- 在线生产监控和离线策略研究保持分离。

主要实现位置：

- `src/application/research/`
- `src/application/shadow_replay/`
- `docs/SHADOW_REPLAY_RUNBOOK.md`
- `docs/OPPORTUNITY_QUALITY.md`

## 横向支撑能力

横向支撑能力被多个产品域共享，但不单独构成产品域。

| 支撑能力 | 定义 | 典型消费者 |
|---|---|---|
| 行情与 required data | 期权链、quote、DTE、IV、delta、multiplier 等开仓/平仓输入 | 开仓机会监控、Close Advice、Research |
| Event Risk | 财报、除权等事件快照和状态 | 开仓机会监控、Close Advice |
| Portfolio / Ledger Context | 现金、正股、锁定股数、position lots、risk view | 开仓机会监控、持仓管理 |
| Candidate Trace / Run Evidence | 拒绝原因、排序证据、运行状态、审计事件 | Agent 工具、Research、排障 |
| Storage | SQLite、`output_runs`、`output_shared`、reports、state | 全部产品域 |
| External Adapters | OpenD/Futu、Feishu、exchange rate、subprocess | 运行与通知、数据采集、Assistant |
| Domain Rules | 确定性策略、账本、通知、调度和 schema 决策 | Application use cases |

## 当前实现与目标差距

当前已对齐：

- Sell Put / Covered Call 的开仓语义已经从 `short_vol` 转为 `insurance_underwriting`。
- 开仓配置不再接受 `strategy=short_vol`。
- Combo Yield 已有独立开仓编排模块，不再由 `sell_put_steps.py` 拥有组合收益的 trace、summary 和 alert 决策。
- Close Advice 和持仓侧保留 `short_vol` thesis 命名。
- Research / Shadow Replay 与生产执行保持分离。

当前未完全对齐：

- Combo Yield 仍有部分内部模块/函数名沿用 legacy `yield_enhancement`。
- Close Advice 侧仍保留 legacy `yield_enhancement` 持仓退出适配。

下一步目标：

```text
opening strategies
  -> sell_put
  -> covered_call
  -> combo_yield

position management
  -> ledger / lifecycle / close_advice / reports
```

Combo Yield 详细开仓策略和 runtime key 已对齐；剩余差距是部分内部模块名、历史 artifact 读取和持仓退出适配仍保留 legacy `yield_enhancement`。
