# Strategy Lab System Design

## 设计目标

Strategy Lab 是产品内的线上策略实验模块，不是本地分析脚本，也不是单次 replay。它的系统目标是：

- 从线上运行数据构建冻结实验数据集。
- 在冻结 dataset 上评估 baseline 和 candidate/grid。
- 输出可复现的实验结论和推荐等级。
- 不影响真实交易、持仓、账本和生产策略配置。

## 边界

Strategy Lab 只拥有以下职责：

- `dataset collect`：采集并冻结历史样本。
- `experiment run`：执行策略实验。
- `report render`：生成实验报告和用户回执。
- `current inspect`：查看最新实验状态。

Strategy Lab 不拥有：

- 运行状态诊断。
- 单 symbol 过滤原因排查。
- 单次扫描 replay。
- 交易写入。
- 持仓写入。
- 生产策略配置修改。

## 模块划分

目标模块结构：

```text
src/application/strategy_lab/
  dataset_contracts.py      # Dataset schema and validation
  dataset_collect.py        # Runtime evidence collection
  experiment_contracts.py   # Experiment request/result contracts
  experiment_engine.py      # Deterministic evaluation engine
  preflight.py              # Data quality and sample-size gates
  recommendation.py         # Recommendation policy
  experiment_report.py      # Markdown and structured experiment report rendering
  storage.py                # Runtime-root dataset/result persistence
  service.py                # Application facade
```

`evidence_loader.py`、`simulator.py` 和 `run_replay_backtest` 可以作为 MVP 的内部确定性计算组件复用，但不能成为 Strategy Lab 的 public contract。对外契约只暴露 dataset、experiment、preflight、recommendation 和 current pointer；后续替换内部引擎时不应改变这些外部契约。

## 数据流

```text
User / Feishu / CLI
  -> strategy-lab experiment request
  -> DatasetResolver / DatasetCollect
      -> existing dataset, or collect from runtime root
  -> Preflight
      -> not_evaluable when sample/data quality is insufficient
  -> ExperimentEngine
      -> baseline evaluation
      -> candidate/grid evaluation
  -> RecommendationPolicy
      -> reject | watch | shadow | review
  -> ReportRenderer
  -> Storage + Audit
  -> User receipt
```

## Runtime Root

所有 Strategy Lab 运行产物属于 runtime root，而不是 release checkout。

默认路径：

```text
$OM_RUNTIME_ROOT/output_shared/strategy_lab/datasets/
$OM_RUNTIME_ROOT/output_shared/strategy_lab/experiments/
$OM_RUNTIME_ROOT/output_shared/strategy_lab/reports/
$OM_RUNTIME_ROOT/output_shared/state/current/strategy_lab.current.json
```

如果没有配置 `OM_RUNTIME_ROOT`，开发环境可以回退到 repo root。生产环境必须通过 setup/doctor 提示 runtime root 是否正确。

## Dataset Contract

Dataset 是 Strategy Lab 的事实输入。

建议 schema：

```json
{
  "schema_version": "strategy_lab_dataset.v1",
  "dataset_id": "us_sy_sell_put_20260301_20260524_xxx",
  "created_at": "2026-05-24T00:00:00Z",
  "scope": {
    "market": "us",
    "account": "sy",
    "strategy_type": "sell_put",
    "start_date": "2026-03-01",
    "end_date": "2026-05-24"
  },
  "sources": {
    "runs": [],
    "trade_events": [],
    "position_lots": [],
    "historical_snapshots": []
  },
  "candidates": [],
  "rejects": [],
  "outcomes": [],
  "capital_snapshots": [],
  "market_snapshots": [],
  "warnings": []
}
```

Dataset 生成规则：

- 必须记录来源路径、run_id、时间范围和采集时间。
- 必须包含全部候选和拒绝样本，避免幸存者偏差。
- 必须将生命周期结果和候选样本关联。
- 缺失字段保留为空并进入 warnings，不允许推断成事实。
- Dataset 生成后不再就地修改；修正数据生成新的 dataset。

当前实现：

- CLI：`om strategy-lab dataset collect`
- Agent：`strategy_lab_dataset_collect`
- 默认 dry-run；`--confirm` / `confirm=true` 写入 `output_shared/strategy_lab/datasets/<dataset_id>.json`。

## Preflight

Preflight 是 Strategy Lab 的强制入口。

最小检查：

- `candidate_count >= min_candidate_sample`
- `outcome_count >= min_outcome_sample`
- 覆盖交易日数满足最低要求。
- 至少覆盖一个完整到期周期；Sell Put/Sell Call 推荐覆盖多个周期。
- baseline 和 candidate 都有可比较样本。
- 资金占用字段可计算。
- 关键 outcome 字段可计算。
- 历史行情 snapshot 覆盖候选标的和时间范围。

Preflight 输出：

```json
{
  "status": "evaluable",
  "sample": {
    "candidate_count": 120,
    "outcome_count": 45,
    "reject_count": 300
  },
  "warnings": []
}
```

或：

```json
{
  "status": "not_evaluable",
  "reason": "outcome_sample_below_minimum",
  "missing": ["close_or_expiry_outcomes"],
  "next_actions": ["extend_date_window", "collect_more_runs"]
}
```

当 Preflight 返回 `not_evaluable` 时，ExperimentEngine 不输出策略优劣结论。

当前实现的最小 gate：

- `candidate_count >= min_candidate_sample`
- `outcome_count >= min_outcome_sample`
- `trace_count + reject_count >= min_trace_or_reject_sample`
- 资金占用字段缺失进入 warnings，不伪造事实。

## Experiment Engine

ExperimentEngine 在同一 dataset 上运行 baseline 和 candidate/grid。

输入：

- dataset id。
- baseline policy。
- candidate policy 或 candidate grid。
- capital model。
- execution assumptions。
- risk constraints。

输出：

- baseline metrics。
- candidate metrics。
- lift / delta。
- risk changes。
- data-quality warnings。
- recommendation input facts。

策略评估不能读取 live broker 状态。需要行情时读取 dataset 内冻结的 market snapshots。

当前实现：

- CLI：`om strategy-lab experiment`
- Agent：`strategy_lab_experiment`
- 可传 `dataset_id` 读取冻结 dataset；未传时会先采集临时 dataset。
- 默认 dry-run；确认后写 result/report/current pointer。

## Metrics

MVP 指标：

- `net_cash_inflow`
- `realized_pnl`
- `locked_cash`
- `locked_cash_days`
- `return_per_locked_cash_day`
- `assignment_rate`
- `strike_breach_rate`
- `worst_trade_pnl`
- `tail_loss_scenario`
- `concentration_by_symbol`
- `concentration_by_expiry`
- `sample_size`
- `confidence_level`

暂不输出：

- Sharpe ratio。
- Sortino ratio。
- Calmar ratio。

原因：这些指标需要可信权益曲线和统一估值口径。没有这层数据时输出会制造伪精确。

## Recommendation Policy

推荐结果：

- `not_evaluable`
- `reject`
- `watch`
- `shadow`
- `review`

推荐原则：

- 样本不足只能是 `not_evaluable`。
- 风险明显变差不能进入 `shadow`。
- 资金效率提升但样本偏少只能是 `watch`。
- 资金效率提升、风险不变差、样本质量达标，才能是 `shadow`。
- `review` 只表示值得人工评审，不表示自动上线。

## Public Interfaces

目标 CLI：

```bash
./om strategy-lab dataset collect \
  --config-key us \
  --account sy \
  --strategy-type sell_put \
  --start-date 2026-03-01 \
  --end-date 2026-05-24 \
  --confirm

./om strategy-lab experiment \
  --dataset-id us_sy_sell_put_20260301_20260524_xxx \
  --candidate-grid-path configs/strategy_lab/sell_put_grid.json \
  --confirm

./om strategy-lab current
```

目标 Agent tool：

```text
strategy_lab_dataset_collect
strategy_lab_experiment
strategy_lab_current
```

飞书输入：

```text
评估 sy 的 sell put 策略，最近三个月
```

飞书回执：

```text
Strategy Lab：可评估
账号：sy
策略：Sell Put
窗口：2026-03-01 至 2026-05-24

结论：建议 shadow
原因：候选参数单位资金收益提升，assignment 风险未变差，样本质量达标。

报告：strategy_lab/reports/<experiment_id>.md
```

## Write Safety

默认 dry-run：

- collect 默认只预览将采集哪些数据。
- experiment 默认只预览 dataset 和参数。

写入需要：

- `--confirm` 或已授权的后台任务。
- Agent tool 写入还需要 `OM_AGENT_ENABLE_WRITE_TOOLS=true`。

允许写：

- Strategy Lab dataset/result/report/current pointer。

禁止写：

- runtime config。
- strategy config。
- trade events。
- position lots。
- notifications。
- broker state。

## 与其他模块的关系

| 模块 | 关系 |
|---|---|
| Doctor / Healthcheck | 负责系统问题诊断，不进入 Strategy Lab 结论 |
| Candidate Explain | 负责单 symbol 过滤解释 |
| Diagnostic Replay | 负责证据复盘和规则差异排查，是诊断能力，不是 Strategy Lab 产品入口 |
| Research | 负责证据打包，不生成策略推荐 |
| Ledger | 提供 trade_events / position_lots / outcome 事实 |
| Runtime Status | 提供运行产物定位和健康上下文 |

## 测试策略

单测：

- dataset schema round-trip。
- dataset collect source selection。
- preflight not_evaluable。
- preflight evaluable。
- baseline/candidate comparison。
- recommendation policy。
- storage writes stay under runtime root。
- write safety guard。

集成测试：

- 用 fake runtime root 构造多 run 样本。
- collect dataset。
- run experiment。
- inspect current pointer。
- 验证报告不包含伪标准风险指标。

回归测试：

- Strategy Lab 不把 reject log 当作 candidate。
- Strategy Lab 不在样本不足时输出 `reject/shadow/review`。
- Strategy Lab 不写交易、持仓、配置和通知。

## 迁移计划

1. 文档纠偏：明确 Strategy Lab 产品目标，Replay 不再作为实验室主能力。
2. 新增 dataset contract 和 storage。
3. 新增 preflight。
4. 新增 experiment engine MVP。
5. 新增 CLI / agent tool。
6. 飞书接入自然语言入口。
7. 保持 replay 只在 diagnostic 边界内使用；不要恢复 `strategy-lab replay` 或 `strategy_lab` agent tool 作为 Strategy Lab 用户入口。
