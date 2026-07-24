# Strategy Lab Design

本文固化 Strategy Lab 的产品和技术设计。当前状态是：`update`、`readiness`、`experiment`、`proposal` 和 `llm-context` 已作为第一批可执行入口落地。`update` 是 evidence lifecycle facade：默认 dry-run；显式 `--build-dataset --write` 时从 latest scanned run 构建本地 replay dataset，显式 `--write` 时才代理执行 Shadow Replay 的本地 collect / settle data-plan。

## 定位

Strategy Lab 是 `options-monitor` 的策略进化产品入口。完整形态下，它基于线上扫描留下的事实数据，自动生成参数实验，评估不同参数或策略 profile 的历史候选影响和后续表现，输出 dry-run 优化建议，并由人决定是否进入 shadow rollout 或生产调整。

三层关系固定为：

```text
Research = 证据基础设施
Shadow Replay = 反事实复盘引擎
Strategy Lab = 策略进化产品入口
```

Research 和 Shadow Replay 不删除。它们降级为底层能力：负责证据收集、归档、dataset 生命周期、mark/outcome 维护、readiness 和候选影响计算。Strategy Lab 负责把这些能力组织成面向策略优化的问题回答。

Strategy Lab 不是单一的 option 参数优化器。它必须同时容纳当前三类开仓策略：

```text
Sell Put = 单腿 short put 决策
Covered Call = 持股 + short call 决策
Combo Yield = 多腿组合决策
```

因此，Strategy Lab 的稳定形态是：

```text
Strategy Lab Core
  -> evidence / readiness / experiment / scorecard / proposal / llm context

Strategy Domain Adapters
  -> Sell Put Adapter
  -> Covered Call Adapter
  -> Combo Yield Adapter
```

核心 pipeline 保持通用，策略差异必须隔离在 domain adapter 里，避免把 Combo Yield 硬塞进单腿 `insurance_underwriting` 参数模型。

架构调整结论：

| 策略 | 决策单元 | 当前实验支持 | 不能共用的部分 |
|---|---|---|---|
| Sell Put | 一张 short put candidate | readiness、单腿 hypothesis、candidate-impact、scorecard、proposal | assignment / cash efficiency / downside stress 指标 |
| Covered Call | 一张 short call candidate + 持股覆盖上下文 | readiness、单腿 hypothesis、candidate-impact、scorecard、proposal | covered share、cost basis、call-away / missed upside 指标 |
| Combo Yield | 一组 legs 组成的组合 | pair-row 归一化、group identity / leg role readiness、group outcome evaluator 和 blocker | 不复用单腿 hypothesis；不能输出单腿化 patch |

所以实验室不是三套产品，也不是一套参数模型。它是一条统一的证据和实验工作流，下面挂三个策略域适配器。Sell Put / Covered Call 第一阶段共享单腿 replay 能力；Combo Yield 走组合级 observed-universe 实验，避免用错误的单腿模型给出伪精确建议。

## 目标

MVP 必须能回答：

```text
当前过滤掉最多机会的参数是不是合理？
如果不合理，在当前证据、目标函数和安全约束下建议调到多少？
```

扩展问题：

- 当前 DTE、IV/RV、IV-RV spread、annualized return 参数是否过严或过松；Delta 只做观测和分桶。
- 某个参数调整后，会新增或移除哪些候选。
- 新增候选后续 mark path / outcome 是否更好。
- 被过滤掉的候选里是否有明显 bad reject。
- 当前策略 profile 是否适合当前市场 regime。
- Sell Put、Covered Call、Combo Yield 是否应使用不同目标函数和参数空间。
- 是否建议进入 shadow rollout，而不是直接改生产配置。

## 非目标

Strategy Lab 不做：

- 不预测股价。
- 不自动下单。
- 不自动修改 `config.yaml`、`config.us.json`、`config.hk.json` 或 runtime config。
- 不发送生产通知。
- 不写 ledger、trade events、option position 或 broker-facing state。
- 不把 LLM 当成最终裁判。
- 不绕过样本量、字段覆盖率、mark path、outcome facts 和 holdout 检查。
- 不用 OpenD 事后重建当时没有保存的历史 option chain。

## 用户 Pipeline

用户感知的 pipeline 收敛为三步：

```text
update evidence -> run experiment -> review proposal
```

目标 CLI：

```bash
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab update --latest --write
./om research strategy-lab experiment --market us --account lx --auto
./om research strategy-lab proposal --experiment <experiment-id>
```

当前已实现的入口是 update、readiness、experiment、proposal 和 llm-context：

```bash
./om research strategy-lab update --latest
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab update --latest --build-dataset --include-close-decisions --write
./om research strategy-lab readiness --dataset <dataset> --min-sample 30
./om research strategy-lab readiness --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30
./om research strategy-lab experiment --dataset <dataset> --min-sample 30 --auto
./om research strategy-lab experiment --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30 --auto
./om research strategy-lab proposal --experiment <experiment-json>
```

这些入口只读取已有 Shadow Replay dataset 或显式写本地 research artifact。`update --build-dataset --write` 可以从 latest scanned run 构建本地 replay dataset；再加 `--include-close-decisions` 时，会独立选择 latest non-empty Close Advice run 并构建 close-decision facet。该 flag 必须与 `--build-dataset` 一起使用，且不能与显式 `--dataset-id` 组合。未显式传 `--dataset-id` 时，candidate dataset id 默认使用 latest run id，目标 dataset 已存在就跳过，避免覆盖之后积累的 mark path / outcome evidence。`update --write` 可以代理本地 mark / settle data-plan；`readiness` 把 candidate / leg / group / close-decision evidence 归一成可审计 decision instance，并输出 Strategy Lab 能走到哪一层；`experiment` 生成受控 hypotheses，复用 Shadow Replay evaluator 做反事实评估，并输出轻量 scorecard；`proposal` 从 experiment artifact 生成 advisory-only dry-run patch 和 Markdown。它们不会应用生产 patch 或修改生产状态；operator 对 `update` 显式传入 `--write` 时，写入仍只限本地 replay artifact、required-data / provider cache 和 receipt。

内部 pipeline 不被删除：

```text
capture / archive / build dataset
  -> collect marks
  -> settle outcomes
  -> readiness
  -> generate hypotheses
  -> evaluate variants
  -> scorecard
  -> proposal
  -> llm context
```

缩短的是用户操作链路，不是事实形成链路。未来表现数据需要时间，不能跳过 mark path 和 outcome facts。

## 功能清单

### 1. Evidence Update

职责：

- 自动发现新 run。
- 自动 build Shadow Replay dataset。
- 判断哪些 dataset 需要追加 mark。
- 判断哪些 dataset 可以 settle。
- 输出数据缺口和下一步动作。
- 显式 `--write` 时只写本地 research / replay artifact。

边界：

- 默认 dry-run。
- 不挂 tick 主链路。
- 不发通知。
- 不写生产配置或交易状态。
- 使用 OpenD 采样必须显式选择相关 source / write 参数。

### 2. Dataset Readiness Gate

Strategy Lab readiness 比现有人工 review readiness 更严格。它必须判断实验能走到哪一层：

- `filter_only`：只能比较候选集合变化。
- `path_only`：可以比较路径风险，但 outcome 还不足。
- `closed_replay`：可以进入参数建议和 shadow rollout 讨论。

检查项：

- candidate universe 是否存在。
- strategy family 是否可识别。
- 单腿 candidate 和组合 decision instance 是否能区分。
- rejected / post-filtered / ranked-below 样本是否存在。
- 参数字段覆盖率是否足够。
- Combo Yield 是否具备 group-level identity、leg identity 和 leg role。

分策略触达条件：

| 策略 | 进入 readiness 的最低证据 | 进入 experiment 的最低证据 | 进入 proposal 的最低证据 |
|---|---|---|---|
| Sell Put | option identity、underlying、strike、expiry、option_type、side、DTE、Delta、IV/RV 或缺失原因、annualized return、接受/拒绝状态 | 参数字段覆盖足够，accepted/rejected/post-filtered universe 可比较，样本量达到 `min_sample` | mark path 或 outcome facts 足够支撑风险/收益判断 |
| Covered Call | Sell Put 单腿字段 + 持股覆盖、covered quantity 或缺失原因、strike/cost basis 关系、call-away 语义 | 参数字段覆盖足够，且覆盖能力不是未知；否则只能做 filter-only 对比 | mark path 或 outcome facts 足够，且 missed upside / call-away 相关字段可解释 |
| Combo Yield | `strategy_group_id`、leg identity、`leg_role`、同 symbol / expiry / multiplier 的 legs、组合净权利金或缺失原因 | group-level evidence 足够时进入 observed-universe evaluator | 只能输出组合级 advisory 和 data gap，不输出单腿化生产 patch |

当前已落地的 readiness 输出包括：

- `summary.status`：`not_ready`、`partial_ready`、`ready_for_experiment`、`ready_for_proposal`。
- `summary.data_mode`：`filter_only`、`path_only`、`closed_replay`。
- `decision_instances.summary`：按 strategy family 和 blocker 汇总 `decision_instance`。
- `readiness.domain_readiness`：分别给出 Sell Put、Covered Call、Combo Yield 的样本数、ready 状态和支持范围。
- `shadow_replay.review_readiness` / `outcome_coverage`：复用底层 Shadow Replay 分析结果。

当前 readiness 的边界：

- Sell Put / Covered Call 支持单腿 `decision_instance` readiness。
- Combo Yield 支持 group identity / leg role readiness、blocker 识别和 observed-universe group evaluator。
- Combo Yield 第一版只输出组合级实验建议，不输出生产配置 patch。
- mark path 是否有足够观察点。
- outcome facts 是否足够。
- 样本量是否达到下限。
- 单一 symbol / 日期 / account / market 是否贡献过大。
- 是否有 holdout window。
- `inconclusive` 比例是否过高。

数据不够时停止实验，不生成优化建议。

### 3. Decision Instance

Strategy Lab 的实验对象不是原始 candidate row，而是 `decision_instance`。

单腿策略通常是一张 candidate 对应一个 decision instance：

```text
Sell Put candidate -> sell_put decision_instance
Covered Call candidate -> covered_call decision_instance
```

Combo Yield 必须按组合处理，一组 legs 对应一个 decision instance：

```text
Combo Yield group -> combo_yield decision_instance
  -> put leg
  -> short call leg
  -> long call leg
```

目标结构：

```json
{
  "decision_id": "string",
  "strategy_family": "sell_put | covered_call | combo_yield",
  "strategy_profile": "insurance_underwriting | return_first | combo_yield",
  "strategy_group_id": "string | null",
  "candidate_ids": ["string"],
  "legs": [],
  "config_snapshot": {},
  "portfolio_context": {},
  "market_context": {},
  "outcome_facts": {}
}
```

这样 Scorecard 和 Proposal 可以按策略域解释结果，而不是把所有策略都压成同一种 option row。

### 4. Strategy Domain Adapters

每个 strategy domain adapter 负责自己的：

- `candidate_universe`
- `decision_instance_builder`
- `tunable_parameters`
- `hard_constraints`
- `hypothesis_generator`
- `replay_method`
- `scorecard_metrics`
- `proposal_target`

统一 core 只认这些抽象，不直接理解策略细节：

```text
decision_instance -> hypotheses -> evaluation -> scorecard -> proposal
```

策略细节必须留在 adapter：

- 哪些参数可调。
- 哪些约束永远不能被实验 variant 放松。
- 哪些指标才代表这个策略的真实质量。
- dry-run patch 应该写向哪个配置命名空间。
- 什么情况下只能输出 data gap，而不能输出参数建议。

#### Sell Put Adapter

可调参数：

- DTE
- IV/RV
- IV/RV 历史百分位（仅离线实验）
- IV-RV
- annualized return

Delta 和集中度只作为观测、分桶和结果解释字段，不作为默认 underwriting 参数 variant。
历史百分位按 symbol、option type、DTE bucket 使用严格更早 run 计算；历史不足时回退绝对 IV/RV 底线。

硬约束：

- event risk fail-closed
- liquidity hard floor
- instrument identity
- cash-secured capacity

核心指标：

- assignment rate
- downside stress loss
- max adverse excursion
- cash efficiency
- tail loss
- premium per capital at risk

第一阶段 dry-run patch target：

- `sell_put.insurance_underwriting.*`
- `sell_put.return_first.*`

是否能建议 adjustment notional cap，取决于 dataset 是否保存了账户现金、担保占用和 rejected reason；否则只允许列为缺失证据。

#### Covered Call Adapter

可调参数：

- DTE
- IV/RV
- IV/RV 历史百分位（仅离线实验）
- IV-RV
- annualized return

Delta 只作为观测字段；call-away rate 和 missed upside 是结果指标，不是默认拒绝条件。
历史百分位样本门槛是实验可靠性条件，不是生产 underwriting 配置。

硬约束：

- covered share availability
- cost-basis floor
- liquidity hard floor
- event risk policy

核心指标：

- call-away rate
- upside missed opportunity
- premium vs opportunity cost
- holding coverage
- max favorable excursion missed
- right-tail opportunity cost to premium

第一阶段 dry-run patch target：

- `covered_call.insurance_underwriting.*`
- `covered_call.return_first.*`

Covered Call 的建议不能只看 option row。缺少持仓覆盖、成本线或可卖股数时，只能做候选影响对比，不能输出生产参数建议。

#### Combo Yield Adapter

Combo Yield 不按单张 option 独立优化，必须按 `strategy_group_id` / leg group 处理。

需要证据：

- group-level candidate identity
- leg-level candidate identity
- `leg_role`
- group net credit / debit
- group payoff
- group max loss / max gain
- funding efficiency
- upside participation
- leg-level slippage

核心指标：

- combo total PnL
- leg slippage
- downside protection / funding quality
- upside participation retained
- net capital at risk
- group-level drawdown
- payoff shape stability

MVP 对 Combo Yield 的要求是把生产的一行 Put+Call pair 归一为同一 `strategy_group_id` 下的一条 short funding Put 和一条 long participation Call，严格验证组合身份，并按同步 mark 与两腿 outcome 汇总组合收益和最差路径。没有完整组合 outcome 时输出 `not_evaluable`，不生成参数 variants 或最佳方案。

### 5. Hypothesis Generation

第一版只为 Sell Put / Covered Call 生成受控参数 variants，并输出兼容现有 Shadow Replay `ParameterSet` 的结构。Combo Yield 不复用单腿 `ParameterSet`，只执行 group-level outcome evaluation。

MVP 可调参数：

- `min_dte`
- `max_dte`
- `min_iv_rv_ratio`
- `min_iv_minus_rv`
- `min_annualized_return`

这些参数只适用于 Sell Put / Covered Call 的单腿 underwriting 实验。Combo Yield 使用单独的 group-level experiment schema。

不可调安全边界：

- event risk
- spread / liquidity hard floor
- instrument identity
- cash / covered-share capacity
- trade state
- notification behavior
- broker-facing state

第一版以单参数和小范围组合为主，不做大规模搜索或强化学习。

参数搜索边界：

- 同一次 experiment 内只比较同一 strategy family 的 variants，不把 Sell Put、Covered Call、Combo Yield 相互排名。
- 生成 variants 时必须保留 baseline；任何建议都要说明新增候选、移除候选和反例。
- 不能为了提高候选数放松 hard constraints，例如 event fail-closed、流动性地板、持仓覆盖、组合 identity。
- 样本不足时，输出 blocker 优先于输出 patch。

### 6. Counterfactual Evaluation

Evaluator 复用 Shadow Replay 的 candidate-impact 能力：

```text
production observed baseline
  vs
generated parameter variants
```

它只在 `observed_run_universe` 内评估，不重新扫描市场。输出包括：

- strategy family。
- decision instance count。
- baseline accepted / rejected。
- 每个 variant 的新增候选、移除候选。
- 拒绝原因。
- safety violation reason。
- safety-rejected 数量；正常保留安全门禁不算 variant violation。
- outcome / path / insurance metrics。
- evidence mode 和 gate status。
- IV/RV 历史样本数、百分位和绝对底线回退模式。

Sell Put / Covered Call 第一版复用 candidate-impact evaluator。Combo Yield 第一版使用 group-level outcome evaluator，不生成参数 variants，也不把多腿组合拆成单腿候选去排名。

历史百分位 variant 保留生产绝对 IV/RV 底线，只增加相对历史门槛，以便单独识别历史百分位的影响。历史样本只使用同一 symbol、option type、DTE bucket 的严格更早 run，同一 run 不进入自身历史。没有候选达到历史样本门槛时，该 variant 只能报告绝对底线回退结果，不能进入最佳 variant 比较。min_iv_rv_percentile 和 min_iv_rv_history_samples 是离线实验参数，Proposal 不得把它们转换成生产 dry-run patch。

Strategy Lab 另行输出 Sell Put / Covered Call 的排序对照：保留生产 CSV 观测顺序，并计算“strike 意愿价格边际优先、去重后承保补偿其次”的排序。去重补偿分数不包含单笔净收入，波动率补偿取 IV/RV edge 与 IV-RV edge 的较弱项；净收入只作最后 tie-break。实验同时输出四格对照：固定 IV/RV / 历史百分位 IV/RV × 生产观测顺序 / 去重排序。四格结果复用同一 observed universe、mark 和 lifecycle outcome；证据不足时明确输出 not_evaluable，不得宣称收益、回撤或 CVaR 改善，也不修改 runtime config。

评价结果必须按策略域分开解释：

- Sell Put 的“新增候选”可能增加接货义务，不能只看保费增加。
- Covered Call 的“新增候选”可能增加被叫走或错失上涨的机会成本，必须结合持仓上下文。
- Combo Yield 的“新增组合”必须看组合净权利金、call 参与质量和 legs 的执行质量，不能只看 put leg 收益率。

### 7. Scorecard

Scorecard 不使用综合黑盒分数。候选数量只用于影响审阅；选择 variant 必须满足严格 outcome dominance：相对生产 baseline，所有可比结果指标均不差，且至少一项更好。

输出必须同时包含：

- risk-adjusted return
- annualized return
- win rate
- max adverse excursion
- max drawdown
- assignment / call-away rate
- tail loss
- missed opportunity
- liquidity cost
- concentration risk
- sample confidence
- blocker / limitation

Scorecard 必须按 strategy family 输出 domain-specific 指标：

- Sell Put 关注 assignment、cash efficiency、downside stress。
- Covered Call 关注 call-away、right-tail opportunity cost、holding coverage。
- Combo Yield 关注 group payoff、leg slippage、funding quality、upside participation。

第一阶段 scorecard 分层：

| 层级 | 用途 | 当前状态 |
|---|---|---|
| candidate-impact scorecard | 样本量、accepted/rejected 变化和 safety violation | 已落地；不产生 `best_variant` |
| outcome scorecard | 资本收益、最差路径、经验 CVaR、生命周期收益 | Sell Put / Covered Call 已接入严格 dominance；证据不足输出 `not_evaluable` |
| family scorecard | Sell Put / Covered Call / Combo Yield 的策略语义指标 | Put/Call 分开比较；Combo 缺 group outcome 时不选择 variant |
| promotion scorecard | 是否进入 shadow rollout | 目标能力，必须等 outcome facts 和 holdout 足够 |

Strategy Lab 只有在唯一 variant 严格支配生产 baseline 时才说：

```text
在当前 observed universe、当前安全约束和当前 outcome 样本下，
该 variant 在全部可比结果指标上不差于 baseline，且至少一项更好。
```

### 8. Strategy Proposal

Proposal 输出 advisory-only 建议：

- 推荐参数。
- dry-run patch。
- 证据摘要。
- 影响范围。
- 反例。
- 风险和限制。
- 置信度。
- 是否建议进入 shadow rollout。

Proposal 必须带 `strategy_family`，并且不同策略写入不同 dry-run patch target。Sell Put / Covered Call 只有在 `closed_replay`、真实 outcome readiness 和 `best_variant_basis=strict_outcome_dominance` 同时成立时才输出 dry-run patch；`filter_only` / `path_only` 只能输出证据缺口和下一步动作。Combo Yield 缺 group outcome 时只能输出组合级 data-gap advisory，不能输出推荐 variant 或单腿参数 patch。

它不能修改生产配置。

Proposal 输出规则：

| 策略 | 可以输出 | 不能输出 |
|---|---|---|
| Sell Put | 参数 dry-run patch、影响范围、风险、反例、shadow rollout 建议 | 自动改配置、绕过 cash / event / liquidity hard gate |
| Covered Call | 参数 dry-run patch、持仓覆盖相关限制、missed upside 风险 | 在缺少持仓上下文时给生产建议 |
| Combo Yield | group evidence readiness、group evaluator 摘要、data-gap proposal、后续 outcome 输入需求 | 单腿化参数 patch、把 put leg 结果当组合最优 |

示例结构：

```json
{
  "status": "shadow_rollout_recommended",
  "runtime_config_write_allowed": false,
  "dry_run_patch": {
    "sell_put.insurance_underwriting.min_iv_rv_ratio": 1.25
  },
  "confidence": "medium",
  "limitations": ["earnings_week_sample_insufficient"]
}
```

### 9. LLM Context

LLM 是策略研究助理，不是 optimizer。

LLM 可以：

- 解释 scorecard。
- 挑战样本偏差。
- 写策略 memo。
- 提出下一轮 hypothesis 草案。
- 生成人可读的实验总结。

LLM 不可以：

- 直接改配置。
- 直接影响扫描结果。
- 直接发通知。
- 直接写交易状态。
- 绕过 readiness gate。
- 直接声称参数最优。

系统只生成脱敏 `llm_context.json`，不在生产链路调用在线 AI。

LLM context 必须保留 strategy family 边界。允许 LLM 对同一 experiment 做解释和质疑，但不能把 Sell Put 的好参数迁移成 Covered Call 或 Combo Yield 的建议，也不能把 Combo Yield 的 group blocker 翻译成单腿参数 patch。

## 技术架构

模块规划和当前实现：

```text
src/application/strategy_lab/
  __init__.py        # implemented
  evidence.py        # implemented
  update.py          # implemented
  readiness.py       # implemented
  decisions.py       # implemented
  hypotheses.py      # implemented
  experiment.py      # implemented
  combo_evaluator.py # implemented
  evaluator.py       # folded into experiment.py for single-leg MVP
  scorecard.py       # folded into experiment.py for MVP
  proposal.py        # implemented
  llm_context.py     # implemented
  reporting.py       # target
  workflow.py        # target
  domains/           # implemented
    __init__.py      # implemented
    base.py          # implemented
    sell_put.py      # implemented
    covered_call.py  # implemented
    combo_yield.py   # implemented
```

职责：

- `evidence.py`：读取 Shadow Replay dataset / run window，统一证据加载。
- `update.py`：包装 latest scanned run dataset build、Shadow Replay status / run-data-plan，收敛本地 dataset build / mark / settle evidence lifecycle。
- `readiness.py`：Strategy Lab 专用 readiness gate。
- `decisions.py`：把 candidate / leg / group evidence 归一成 `decision_instance`。
- `domains/`：定义不同策略域的 adapter、可调参数、硬约束、scorecard 指标和 proposal target。
- `hypotheses.py`：按 strategy domain 生成参数 variants；Sell Put / Covered Call 可输出兼容 `src.application.shadow_replay.parameter_sets.ParameterSet`。
- `combo_evaluator.py`：按 `strategy_group_id` 聚合 Combo Yield legs，验证组合身份，并计算组合级收益、同步最差路径和 outcome scorecard。
- `experiment.py`：调用 `run_shadow_replay_candidate_impact`，不复制 candidate-impact 逻辑；同时接入 Combo Yield group evaluator；MVP 内含 outcome-gated scorecard。
- `evaluator.py`：后续如果 experiment 变复杂，再从 `experiment.py` 拆出 evaluator。
- `scorecard.py`：后续如果评分逻辑变复杂，再从 `experiment.py` 拆出 scorecard。
- `proposal.py`：生成 dry-run strategy proposal。
- `llm_context.py`：生成 LLM 输入，不调用在线 AI。
- `reporting.py`：渲染 JSON / Markdown。
- `workflow.py`：串起 `update`、`experiment`、`proposal`。

## 产物目录

Strategy Lab 写入本地 research artifact：

```text
output_shared/research/strategy_lab/
  experiments/
    <experiment-id>/
      manifest.json
      readiness.json
      decision_instances.jsonl
      hypotheses.json
      evaluations.json
      scorecard.json
      proposal.json
      proposal.md
      llm_context.json
  receipts/
```

artifact 命名必须能区分策略域：

- `strategy_family`
- `strategy_profile`
- `strategy_group_id`，仅组合策略需要
- `domain_readiness`
- `proposal_target`

所有 artifact 必须带安全声明：

```json
{
  "offline_only": true,
  "writes_runtime_config": false,
  "writes_trade_state": false,
  "sends_notifications": false,
  "runtime_config_write_allowed": false
}
```

## 当前 CLI 与目标接口

当前可执行子命令：

```bash
./om research strategy-lab readiness --dataset <dataset> --min-sample 30
./om research strategy-lab readiness --dataset <dataset> --min-sample 30 --output output_shared/research/strategy_lab/readiness.json
./om research strategy-lab readiness --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30
./om research strategy-lab update --latest
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab update --latest --build-dataset --include-close-decisions --write
./om research strategy-lab update --latest --write
./om research strategy-lab experiment --dataset <dataset> --min-sample 30 --auto
./om research strategy-lab experiment --dataset <dataset> --min-sample 30 --auto --output output_shared/research/strategy_lab/experiment.json
./om research strategy-lab experiment --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30 --auto
./om research strategy-lab proposal --experiment output_shared/research/strategy_lab/experiment.json
./om research strategy-lab proposal --experiment output_shared/research/strategy_lab/experiment.json --output output_shared/research/strategy_lab/proposal.json --markdown-output output_shared/research/strategy_lab/proposal.md
./om research strategy-lab llm-context --experiment output_shared/research/strategy_lab/experiment.json --proposal output_shared/research/strategy_lab/proposal.json
./om research strategy-lab llm-context --experiment output_shared/research/strategy_lab/experiment.json --proposal output_shared/research/strategy_lab/proposal.json --output output_shared/research/strategy_lab/llm_context.json
```

这些命令只读 dataset、scanned-run window 或本地 experiment / proposal artifact；只有显式 `--output` / `--markdown-output` / `--write` 时写本地 artifact。`update --build-dataset --write` 只写本地 replay dataset；`--include-close-decisions` 显式加入 close facet；`update --write` 只执行本地 replay data-plan / receipt。`update` 返回 `research.strategy-lab.update`；`readiness` 返回 `research.strategy-lab.readiness`；`experiment` 返回 `research.strategy-lab.experiment`；`proposal` 返回 `research.strategy-lab.proposal`；`llm-context` 返回 `research.strategy-lab.llm-context`。

### `update`

目标：

- 收敛现有 `shadow-replay build/status/run-data-plan/collect-marks/settle` 的用户操作。
- 默认 dry-run。
- 显式 `--write` 时只维护本地 research / replay artifact。

目标命令：

```bash
./om research strategy-lab update --latest
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab update --latest --build-dataset --include-close-decisions --write
./om research strategy-lab update --latest --write
```

当前已实现：

```bash
./om research strategy-lab update --latest
./om research strategy-lab update --latest --build-dataset --write
./om research strategy-lab update --latest --build-dataset --include-close-decisions --write
./om research strategy-lab update --latest --write
./om research strategy-lab update --max-datasets 3 --source local
./om research strategy-lab update --max-datasets 3 --source opend --write
```

当前 `update` 复用 Shadow Replay latest scanned run build、`status` / `run-data-plan`。默认 dry-run，只返回会执行的 dataset build 或 `collect_marks` / `settle` 本地数据维护动作、ready queue 和 next action。显式 `--build-dataset --write` 时先从 latest scanned run 写本地 replay dataset，再进入 data-plan；加 `--include-close-decisions` 时另选 latest non-empty Close Advice run，构建可选 close facet。该 flag 要求 `--build-dataset` 且拒绝显式 `--dataset-id`。未显式传 `--dataset-id` 时默认使用所选 run id 作为 dataset id，已存在则返回 `dataset_build_reason=dataset_already_exists` 并跳过，不覆盖已有路径数据。显式 `--write` 时才执行已有 Shadow Replay data-plan，并可写本地 receipt；`--source opend --write` 仍只允许写 required-data cache、OpenD cache/rate-limit state、Shadow Replay dataset 和 receipt。它不执行 `analyze`，不生成参数建议，不修改 runtime config、交易状态、通知、Feishu 或 broker-facing state。

远端持续记录需要显式渲染，默认不启用：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --config-yaml /var/lib/options-monitor/config.yaml \
  --markets us hk \
  --accounts lx sy \
  --include-strategy-lab-recorder \
  --output-dir /tmp/options-monitor-service
```

它把 `update` 拆成三个低频 timer：candidate/close-aware dataset build、mark
sampler、outcome settler。build unit 固定使用
`--build-dataset --include-close-decisions --write --source local`。这个
recorder 只是 evidence lifecycle 的服务化入口，不是 experiment runner，也不会应用
proposal。

### `readiness`

目标：

- 对 dataset 或 date window 运行 Strategy Lab readiness。
- 输出 `not_ready`、`partial_ready`、`ready_for_experiment`、`ready_for_proposal`。

目标命令：

```bash
./om research strategy-lab readiness --dataset <dataset>
./om research strategy-lab readiness --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD>
```

当前已实现：

```bash
./om research strategy-lab readiness --dataset <dataset> --min-sample 30
./om research strategy-lab readiness --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30
```

当前支持已有 dataset 输入，也支持通过 `--start-date` / `--end-date` / `--market` / `--account` 聚合 scanned-run window。

### `experiment`

目标：

- 自动生成 hypotheses。
- 调用 candidate-impact evaluator。
- 产出 scorecard。

目标命令：

```bash
./om research strategy-lab experiment --dataset <dataset> --auto
./om research strategy-lab experiment --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --auto
```

当前已实现：

```bash
./om research strategy-lab experiment --dataset <dataset> --min-sample 30 --auto
./om research strategy-lab experiment --market us --account lx --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --min-sample 30 --auto
```

当前支持已有 dataset 输入，也支持通过 `--start-date` / `--end-date` / `--market` / `--account` 直接聚合 scanned-run window。它会生成 `strategy_lab_hypotheses.v1`，输出 Sell Put / Covered Call 的受控 `ParameterSet` variants；Combo Yield 不进入单腿 `ParameterSet`，而是在 `group_experiments.combo_yield` 输出 `strategy_lab_combo_yield_group_experiment.v1`。随后它调用 Shadow Replay candidate-impact evaluator，并输出 `strategy_lab_experiment.v1` scorecard。无 outcome 时 scorecard 只报告候选变化；只有唯一 variant 满足严格 outcome dominance 时才产生 `best_variant`。

### `proposal`

目标：

- 从 experiment artifact 生成 dry-run proposal 和 Markdown。

目标命令：

```bash
./om research strategy-lab proposal --experiment <experiment-id>
```

当前已实现：

```bash
./om research strategy-lab proposal --experiment <experiment-json>
./om research strategy-lab proposal --experiment <experiment-json> --output <proposal-json> --markdown-output <proposal-md>
```

当前 `--experiment` 支持 experiment JSON 文件，或包含 `experiment.json` / `strategy_lab_experiment.json` 的目录。输出是 `strategy_lab_proposal.v1`，包含 `dry_run_patch`、evidence summary、impact、counterexamples、risks、limitations、可选 Combo Yield `group_advisory` 和 Markdown。它不会应用 patch，也不会修改 runtime config；单腿 dry-run patch 需要严格 outcome dominance，旧 candidate-count artifact 不可生成 patch，Combo Yield 当前只输出 data-gap advisory。

### `llm-context`

目标：

- 为本地 Codex / LLM 分析生成脱敏上下文。
- 不调用在线 AI。

目标命令：

```bash
./om research strategy-lab llm-context --experiment <experiment-id>
```

当前已实现：

```bash
./om research strategy-lab llm-context --experiment <experiment-json>
./om research strategy-lab llm-context --experiment <experiment-json> --proposal <proposal-json> --output <llm-context-json>
```

当前 `--experiment` / `--proposal` 支持 JSON 文件、包含标准 artifact 名称的目录，或应用层 inline payload。输出是 `strategy_lab_llm_context.v1`，包含 allowed tasks、forbidden actions、strategy family boundaries、实验摘要、proposal 摘要、analysis prompts、redaction receipt 和 safety payload。它会脱敏 secret / token / webhook / password / cookie / authorization / api key 类字段；不会调用在线 AI，不会应用 dry-run patch，也不会把 LLM 输出写回生产配置。

## 实施顺序

已落地：

1. 新增 `strategy_lab` 包和 CLI skeleton。
2. 实现 `evidence`，读取已有 Shadow Replay dataset。
3. 实现 `decisions`，把现有单腿 candidate 转为 Sell Put / Covered Call `decision_instance`，并识别 Combo Yield group blocker。
4. 实现 `readiness`，复用现有 Shadow Replay dataset，并增加 strategy family / group identity 检查。
5. Shadow Replay capture 保留 Combo Yield 的 `strategy_group_id` 和 `leg_role`。
6. 实现 Sell Put / Covered Call / Combo Yield domain adapters。
7. 实现 `hypotheses`，自动生成 Sell Put / Covered Call 的 DTE / IV-RV / annualized return variants；Delta 仅观察。
8. 实现 `experiment`，复用 `run_shadow_replay_candidate_impact`。
9. 实现 outcome-gated `scorecard`，候选数量仅用于影响审阅，严格 outcome dominance 才产生 `best_variant`。
10. 实现 `proposal`，输出 advisory-only dry-run patch 和 Markdown。
11. 实现 `llm_context`，输出脱敏本地 LLM context，不调用在线 AI。
12. 实现 `update`，收敛 Shadow Replay status / run-data-plan 的 dataset mark / settle 生命周期入口。
13. 扩展 `update --build-dataset --write`，从 latest scanned run 构建本地 replay dataset 后继续进入 data-plan。
14. 更新 README / Shadow Replay Runbook / Tool Reference / 架构文档，让 Strategy Lab 成为上层产品入口，Shadow Replay 保持底层引擎。
15. 实现 Combo Yield group-level observed-universe evaluator；缺少组合 outcome 时不生成最佳 variant 或单腿化生产 patch。

下一步：

1. 扩展 `update` 支持 archive build-datasets。
2. 在积累完整组合 outcome 后，扩展 Combo Yield group evaluator 的 holdout 和 payoff-shape 对照。

## 验收标准

MVP 通过条件：

已满足：

- 能读取已有 Shadow Replay dataset。
- 能判断数据只支持 `filter_only`、`path_only` 还是 `closed_replay`。
- 能把 Sell Put / Covered Call candidate 转成 `decision_instance`。
- 能识别 Combo Yield group evidence，并在 group evidence 不足时给出 blocker。
- 能按 strategy family 自动生成 DTE / IV-RV / annualized return 参数 variants；Delta 不进入参数实验。
- 能复用 candidate-impact evaluator 比较 variants。
- 能给每个 variant 输出轻量 scorecard。
- 能按 Sell Put / Covered Call 输出不同 scorecard 指标。
- 能输出 advisory-only dry-run proposal。
- 能生成脱敏 LLM context。
- 能以默认 dry-run 的 `update` 汇总并代理执行本地 dataset mark / settle data-plan。
- 能通过 `update --build-dataset --write` 自动 build latest scanned run dataset。
- 能通过 `readiness --start-date/--end-date/--market/--account` 直接聚合 scanned-run window。
- 能通过 `experiment --start-date/--end-date/--market/--account` 直接聚合 scanned-run window。
- 能按组合身份对 Combo Yield 做 observed-universe group evaluation；缺 outcome 时不选择最佳 variant。
- 能明确列出 blocker、限制和置信度相关前置条件。
- 不能修改生产配置。
- 不能写交易状态。
- 不能发送通知。

MVP 外后续增强：

- promotion scorecard 仍需等待更多 outcome facts / holdout evidence，不能作为当前 MVP 的完成条件。

## 演进方向

第一阶段只做 Strategy Lab，不抽象成大平台。等第二个优化目标真正落地时，再提炼公共框架。

长期产品心智：

```text
Research
  -> Labs
      -> Strategy Lab
      -> Close Lab
      -> Portfolio Lab
      -> Notification Lab
```

当前 close-decision facet 仍属于 Shadow Replay 的 evidence/analysis 能力；
`Close Lab` 只是未来产品化方向，不代表已经上线第二个 Lab，也不授权自动改变生产
Close policy。

未来每个 Lab 作为一个 decision domain：

- `decision_type`
- `candidate_universe`
- `tunable_parameters`
- `hard_constraints`
- `objective_function`
- `replay_method`
- `scorecard_metrics`
- `proposal_target`
- `promotion_policy`

公共组件只有在 Strategy Lab 和第二个 Lab 之间出现稳定重复后，才沉淀为 `decision_lab` 框架。
