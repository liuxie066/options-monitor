# Shadow Replay Runbook

Shadow Replay 的目标不是重新跑一次扫描，而是把某次扫描当时的候选全集保存下来，之后持续采样这些合约的价格路径，最后比较 accepted / rejected / post-filtered 样本的路径风险和结果。

核心原则：复盘需要时间路径。OpenD 可以在采样时提供当前报价，但不能在几天后恢复当时没有保存的历史 option mark。要避免数据不够，必须在 dataset 建好后持续收集 mark。

## 数据模型

一个可复盘 dataset 至少需要四类证据：

- `candidate_snapshots.jsonl`：当时进入评估的候选全集，必须包含 accepted 和 rejected / post-filtered 样本。
- `filter_decisions.jsonl`：为什么被拒、在哪个 stage/rule 被拒。
- `mark_path_snapshots.jsonl`：之后每个观察点的合约 mark、spot、bid/ask/mid、路径 PnL。
- `outcome_facts.jsonl`：从 mark path 或到期 spot 推导的结果。

`required_data/parsed/*_required_data.csv` 只是 mark 的报价来源，不是 replay 结果本身。真正的复盘路径要写进 replay dataset 的 `mark_path_snapshots.jsonl`。

## 执行模型

不要把完整 replay 放进 tick 主路径。tick 的职责是产生扫描证据和 required_data cache；Shadow Replay 的职责是离线采样、离线 settle、离线 analyze。

推荐模型：

1. 扫描完成后，用 run artifact 建 dataset。
2. 在盘中、收盘后、到期日或固定间隔，运行 `collect-marks` 采样一次。
3. 样本足够后运行 `analyze`，人工评审分桶表现。
4. 人工决定是否调整策略参数；replay 不自动修改 runtime config。

## 建立 Dataset

```bash
DATASET_ID=us-<run-id>
DATASET=output_shared/research/shadow_replay/datasets/$DATASET_ID

./om research shadow-replay build --run-id <run-id> --dataset-id "$DATASET_ID"
```

先检查 readiness：

```bash
./om research collect --config-key us --scope candidate --run-id <run-id> --output json --no-write-outputs --shadow-replay-min-sample 30
```

看 `candidate_evidence.shadow_replay.summary`：

- `candidate_snapshot_count` 是否足够。
- `counterfactual_candidate_count` 是否大于 0。
- `reason` 是否还停在 `rejected_universe_missing`、`mark_path_snapshots_missing` 或 `outcome_facts_missing`。

## 采样 Mark Path

如果本地 `output_shared/required_data` 已经有当前报价，直接采样：

```bash
./om research shadow-replay collect-marks \
  --dataset "$DATASET" \
  --source local \
  --required-data-root output_shared/required_data \
  --write
```

如果需要用 OpenD 拉当前报价后再采样：

```bash
./om research shadow-replay collect-marks \
  --dataset "$DATASET" \
  --source opend \
  --required-data-root output_shared/required_data \
  --opend-host 127.0.0.1 \
  --opend-port 11111 \
  --limit-expirations 8 \
  --write
```

`--source opend --write` 会按 dataset 中的 symbol、option type 和 expiration 生成 OpenD 请求，刷新本地 required_data cache，然后追加这一刻的 mark；OpenD 拉取过程也会维护本地限流状态，默认会使用本地 option-chain cache。它不会运行扫描、不会发通知、不会写交易状态、不会改配置。不带 `--write` 时只做预览，OpenD fetch 使用临时目录，不持久化 required_data、replay mark、限流状态或链缓存。

建议采样点：

- T0：扫描后尽快采一次，锁住初始 mark。
- 每个交易日收盘后：用于路径风险和最大浮亏。
- 价格剧烈波动后：用于 stress path。
- 到期日或到期后：用于到期 outcome。

## 单独 Mark / Settle

如果只想用本地 required_data 生成 mark：

```bash
./om research shadow-replay mark --dataset "$DATASET" --required-data-root output_shared/required_data --write
```

如果 mark 已经存在，只补 outcome：

```bash
./om research shadow-replay settle --dataset "$DATASET" --write
```

`collect-marks --write` 默认只追加 mark path，不重算 outcome。需要采样后立即重算时显式加 `--settle`，或者单独运行上面的 `settle --write`。

## 分析

```bash
./om research shadow-replay analyze --dataset "$DATASET" --min-sample 30
```

重点看：

- `summary.status`：只有 `needs_human_review` 才说明证据足够进入人工评审。
- `outcome_coverage`：有多少 candidate instrument 被 mark、被 usable mark、被 outcome 覆盖。
- `path_risk.by_status`：accepted / rejected 的最大浮亏和路径样本数量。
- `outcome_stats.by_status`：accepted 与 rejected 的 PnL、胜率、损失次数。
- `outcome_by_bucket`：DTE、Delta、IV/RV、Spread、集中度各区间的表现。

## Status 解释

| status / reason | 含义 | 处理 |
|---|---|---|
| `not_ready / candidate_universe_missing` | 没有候选全集 | 重新指定 `run-id` / `run-dir` / candidate path |
| `not_ready / candidate_snapshot_count_below_min_sample` | 样本数不足 | 多积累 run 或降低人工评审阈值 |
| `evidence_incomplete / rejected_universe_missing` | 只有最终候选，缺被拒样本 | 检查 `candidate_filter_trace.jsonl` / reject log |
| `not_ready / mark_path_snapshots_missing` | 没有路径采样 | 跑 `collect-marks --source local` 或 `--source opend` |
| `not_ready / usable_mark_path_snapshots_missing` | 有 mark 但没有可用报价 | 检查 bid/ask/mid/spot，必要时用 OpenD 重新采样 |
| `not_ready / outcome_facts_missing` | 有路径但未结算 outcome | 跑 `settle --write` 或 `collect-marks --write --settle` |
| `needs_human_review / shadow_replay_ready_for_manual_review` | 证据够人工评审 | 看 bucket 和 accepted/rejected 对比 |

## 边界

- 不自动执行；需要人工命令或独立低优先级调度。
- 不跟随 tick 主链路同步执行；未来如自动化，应作为 post-tick / after-market job 消费 tick artifacts。
- `--source local` 只读 required_data cache，显式 `--write` 时只写 replay dataset。
- `--source opend --write` 会读取 OpenD、刷新本地 required_data cache、写 replay dataset，并维护本地 OpenD 限流状态和 option-chain cache；不带 `--write` 时使用临时目录做预览，不持久化这些文件。
- 不写 Feishu、不写 broker、不写 trade state、不写 runtime config、不发送通知。
