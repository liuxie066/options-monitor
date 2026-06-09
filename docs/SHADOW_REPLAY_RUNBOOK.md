# Shadow Replay Runbook

Shadow Replay 的目标不是重新跑一次扫描，而是把某次扫描当时的候选全集保存下来，之后持续采样这些合约的价格路径，最后比较 accepted / rejected / post-filtered 样本的路径风险和结果。

当前产品分层固定为：

```text
Research = 证据基础设施
Shadow Replay = 反事实复盘引擎
Strategy Lab = 策略进化产品入口
```

因此，本文是底层复盘引擎手册。面向策略参数自我进化的上层 PRD 和技术方案见 [Strategy Lab Design](STRATEGY_LAB_DESIGN.md)。Strategy Lab 当前已提供 update、只读 readiness、experiment、advisory proposal 和 llm-context 入口；Shadow Replay 继续作为它的反事实 evaluator 和 dataset / mark / outcome 生命周期引擎，而不是被删除。`strategy-lab update` 现在包装本文的 latest scanned run dataset build、status / run-data-plan：默认 dry-run，显式 `--build-dataset --write` 才构建本地 dataset，显式 `--write` 才执行本地 collect / settle。远端持续记录通过 `./om service render --include-strategy-lab-recorder` 显式启用，生成低频 timer 维护 dataset、mark path 和 outcome facts；默认部署不会开启。

Strategy Lab 会按 strategy domain adapter 区分 Sell Put、Covered Call 和 Combo Yield。统一的是 evidence / readiness / experiment / scorecard / proposal workflow；分开的是决策单元、目标函数、参数空间、硬约束和 proposal target。Sell Put / Covered Call 可以先复用单腿 candidate-impact；Combo Yield 必须按 `strategy_group_id` / legs 形成 group-level decision instance，不能被拆成彼此独立的单腿参数实验。

核心原则：复盘需要时间路径。OpenD 可以在采样时提供当前报价，但不能在几天后恢复当时没有保存的历史 option mark。要避免数据不够，必须在 dataset 建好后持续收集 mark。

## 数据模型

一个可复盘 dataset 至少需要四类证据：

- `candidate_snapshots.jsonl`：当时进入评估的候选全集，必须包含 accepted 和 rejected / post-filtered 样本。
- `filter_decisions.jsonl`：为什么被拒、在哪个 stage/rule 被拒。
- `mark_path_snapshots.jsonl`：之后每个观察点的合约 mark、spot、bid/ask/mid、路径 PnL。
- `outcome_facts.jsonl`：从 mark path 或到期 spot 推导的结果。

`required_data/parsed/*_required_data.csv` 只是 mark 的报价来源，不是 replay 结果本身。真正的复盘路径要写进 replay dataset 的 `mark_path_snapshots.jsonl`。

## Opportunity Quality

Shadow Replay 的分析结论使用 [Opportunity Quality](OPPORTUNITY_QUALITY.md) 口径。它不把单次 PnL 直接等同于好机会或坏机会，而是先按 `insurance_underwriting` / `return_first` 策略口径判断当时 accept/reject 的证据是否足够支持人工复盘。历史 `short_vol` 样本会归入当前承保口径。

`decision_quality` 标签包括 `good_accept`、`bad_accept`、`good_reject`、`bad_reject` 和 `inconclusive`。样本不足、缺少 outcome、缺少策略口径或关键字段不足时必须输出 `inconclusive`，不能生成策略结论。`review_readiness` 是新的人工策略复盘 readiness 入口；现有 `parameter_advice_gate` 字段保留为兼容字段，不输出具体参数数值。

`review_readiness.status=ready_for_manual_strategy_review` 只表示可以进入人工策略复盘；仍然不能自动改配置。旧 `parameter_advice_gate.status=ready_for_parameter_review` 与它兼容映射。常见 blocker 的处理口径：

| blocker | 含义 | 处理 |
|---|---|---|
| `sample_size_below_min_sample` | 样本数低于人工评审阈值 | 继续积累样本或显式降低本次人工评审阈值 |
| `strategy_profile_breakdown_missing` | 样本缺少 `insurance_underwriting` / `return_first` 等策略口径 | 先修证据字段，不做参数结论 |
| `bad_decision_signal_missing` | 没有可解释的坏接受或坏拒绝信号 | 暂不需要策略调整讨论，继续观察 |
| `inconclusive_rate_too_high` | `inconclusive` 比例过高 | 补 mark / outcome / 策略口径，再复盘 |

## 执行模型

不要把完整 replay 放进 tick 主路径。tick 的职责是产生扫描证据和 required_data cache；Shadow Replay 的职责是离线采样、离线 settle、离线 analyze。

推荐模型：

1. 扫描完成后，用 run artifact 建 dataset。
2. 在盘中、收盘后、到期日或固定间隔，运行 `collect-marks` 采样一次。
3. 样本足够后运行 `analyze`，人工评审分桶表现。
4. 人工决定是否调整策略参数；replay 不自动修改 runtime config。

远端部署时，推荐用 Strategy Lab recorder 代替手工调度这些维护动作：

```bash
./om service render \
  --target systemd \
  --runtime-root /var/lib/options-monitor \
  --include-strategy-lab-recorder \
  --strategy-lab-recorder-source opend
```

生成的 build timer 默认按 latest scanned run id 幂等构建 dataset；sample timer 采样 mark path；settle timer 维护 `outcome_facts.jsonl`。这些 timer 只写本地 replay artifact、required-data / OpenD cache / rate-limit state 和 receipt，不运行 Strategy Lab experiment/proposal，也不改生产配置、交易状态或通知。

## 候选影响对比

`candidate-impact` 用已有扫描证据做候选影响对比，用来回答“如果当时承保参数换成这一组，会新增/移除哪些候选”。旧 `parameter-backtest` 命令保留为兼容入口。它不是重新扫描市场，也不会用 OpenD 事后恢复当时没有保存的期权链；它也不判断哪组参数最优。

```bash
cat > params.json <<'JSON'
{
  "baseline": "production",
  "variants": [
    {
      "name": "iv_rv_1_10",
      "insurance_underwriting": {
        "min_iv_rv_ratio": 1.10,
        "min_iv_minus_rv": 0.05,
        "min_abs_delta": 0.15,
        "max_abs_delta": 0.30,
        "min_dte": 20,
        "max_dte": 60
      }
    }
  ]
}
JSON

./om research shadow-replay candidate-impact \
  --profile-path /var/lib/options-monitor/service.profile.json \
  --start-date 2026-06-01 \
  --end-date 2026-06-02 \
  --account lx \
  --market hk \
  --params params.json \
  --min-sample 30
```

用户常用入口是 `candidate-impact-report`：它复用同一套对比逻辑，一次写出 JSON 和 Markdown，不自动生成参数、不修改 runtime config、不发送通知。旧 `parameter-report` 命令保留为兼容入口。

```bash
./om research shadow-replay candidate-impact-report \
  --runtime-root /var/lib/options-monitor \
  --start-date 2026-06-03 \
  --end-date 2026-06-03 \
  --account lx \
  --account sy \
  --market us \
  --params-dir /var/lib/options-monitor/output_shared/research/shadow_replay/backtests/<params-dir> \
  --min-sample 30
```

`candidate-impact-report` 默认写入 `output_shared/research/shadow_replay/backtests/candidate-impact-report-<market>-<date-window>-<timestamp>/result.<market>.json|md`；旧 `parameter-report` 默认目录仍保留 `parameter-report-*` 前缀。也可以用 `--output-dir` 指定精确目录。

也可以对已建立的 dataset 回测，并输出 Markdown 报告：

```bash
./om research shadow-replay candidate-impact \
  --dataset output_shared/research/shadow_replay/datasets/<dataset-id> \
  --params params.json \
  --format markdown \
  --output candidate-impact.md
```

参数文件只允许调整 `insurance_underwriting` 的 `min_iv_rv_ratio`、`min_iv_minus_rv`、`min_abs_delta`、`max_abs_delta`、`min_dte`、`max_dte`、`min_annualized_return`。历史 `short_vol` 样本会映射到当前承保参数口径。事件风险、spread、流动性、集中度、合约身份、交易状态和通知都不是可调参数；如果 variant 触碰这些安全边界，结果会保留拒绝原因而不是放行。

结果里的关键字段：

- `coverage.strict_backtest_allowed`：指定日期窗口是否真的有扫描 artifacts。没有指定起始日数据时不会静默补数。
- `universe_scope=observed_run_universe`：只评估历史 artifacts 中出现过的合约。
- `data_mode=filter_only/path_only/closed_replay`：是否已有路径和 outcome，可以支持到什么级别的结论。`filter_only` 只能回答“候选数量会怎么变”，不能回答收益、回撤或是否应改生产参数。
- `evidence_quality.field_coverage`：参数文件实际引用字段的覆盖率，例如 `dte`、`abs_delta`、`iv_rv_ratio`、`iv_minus_rv`、`annualized_return`；字段不足时先修证据，不把结果解释成参数过严。
- `gates.candidate_impact`：候选影响层 gate。扫描证据、样本量和参数字段完整样本达到下限时，可以输出 filter-only 候选影响；如果仍有部分字段缺失，计数应按 lower bound 解读。
- `gates.production_recommendation`：生产推荐层 gate。只有 `closed_replay` 可进入人工评审；即使通过，也不会自动修改 runtime config。
- `candidate_impact`：候选影响摘要，包括 baseline 接受数、每组 variant 的新增/移除数量，以及新增候选数最多的 variant。
- `baseline`：生产实际观察结果。
- `variants`：每个参数组的 accepted/rejected、新增/移除候选、拒绝原因、安全边界原因和 outcome/insurance 指标。
- `recommendation`：只给下一步 gate。`ready_for_live_shadow_candidate_review` 表示候选影响可进入人工评审，不表示已经完成 live shadow；生产改参仍取决于 `gates.production_recommendation`。

## 建立 Dataset

```bash
DATASET_ID=us-<run-id>
DATASET=output_shared/research/shadow_replay/datasets/$DATASET_ID

./om research shadow-replay build --run-id <run-id> --dataset-id "$DATASET_ID"
```

## 远端证据归档

远端 runtime 空间有限时，把原始 run 证据镜像到本地归档，再从本地归档生成 Shadow Replay dataset。默认归档根目录：

```text
output_shared/research/remote_archive/prod/
  manifests/
  output_runs/
  output_shared/research/
  output_shared/required_data/
  logs/
```

先预览，再写入：

```bash
./om research archive pull --remote prod --ssh-target deploy@example --since-days 7
./om research archive pull --remote prod --ssh-target deploy@example --since-days 7 --write
./om research archive pull --remote prod --ssh-target deploy@example --require-replay-evidence --write
./om research archive verify --remote prod
./om research archive inventory --remote prod
```

`pull` 使用 `rsync` 增量同步，不在远端打 tar 包；默认 dry-run，显式 `--write` 才写本地归档和 manifest。`--require-replay-evidence` 会在源端只选择含候选 CSV、reject log 或 `candidate_filter_trace.jsonl` 的 run，避免把 scheduler skip / tick 心跳当 replay 样本。没有 SSH 时，也可以用已挂载 runtime 根目录：

```bash
./om research archive pull --remote prod --source-root /Volumes/prod-runtime --run-id <run-id> --write
```

从已验证归档生成本地 dataset：

```bash
./om research archive build-datasets --remote prod --market us
./om research archive build-datasets --remote prod --market us --write
```

`build-datasets --market us|hk --write` 会按归档 run 的候选/reject 文件名推断市场并过滤样本；不传 `--market` 才会保留所有市场。dataset build 默认会尝试读取每个归档 run 内的 `required_data/parsed/*_required_data.csv`，给 dataset 生成第一批本地 `mark_path_snapshots.jsonl`。这是 scan-time mark，不等于最终 outcome；后续仍要用 `run-data-plan` / `collect-marks --source opend` 追加路径采样，再由 `settle --write` 产出 `outcome_facts.jsonl`。如需只构建候选/拒绝样本，可加 `--no-mark-from-run-required-data`。

远端清理必须独立执行。默认只预览远端 `service cleanup`；加 `--confirm` 前会读取本地 `output_shared/research/remote_archive/prod/manifests/inventory.latest.json`，确认远端计划删除的每个 run 都已经在本地归档中 verified：

```bash
./om research archive prune-remote --remote prod --ssh-target deploy@example --keep-days 3 --keep-count 30
./om research archive prune-remote --remote prod --ssh-target deploy@example --keep-days 3 --keep-count 30 --confirm
```

归档不同步 secrets、runtime config、SQLite、trade events、locks 或 broker-facing state。需要分析交易状态时，应另走脱敏导出，不把生产写路径混进 replay 归档。

生产 runtime 已有 `service.profile.json` 时，优先用 profile 解析线上证据路径，避免手拼 runtime 目录：

```bash
PROFILE=/var/lib/options-monitor/service.profile.json
DATASET_ID=us-<run-id>

./om research shadow-replay build \
  --profile-path "$PROFILE" \
  --latest-scanned-run \
  --dataset-id "$DATASET_ID"

./om research shadow-replay status \
  --profile-path "$PROFILE" \
  --min-sample 30 \
  --min-mark-points 2 \
  --mark-stale-hours 24

./om research shadow-replay run-data-plan \
  --profile-path "$PROFILE" \
  --min-sample 30 \
  --min-mark-points 2
```

`--profile-path` 只解析 `output_runs`、dataset root、required-data root 和 receipt root；不会改 runtime config、交易状态、Feishu 或 broker-facing 数据。`--latest-scanned-run` 会从 profile/runtime 的 `output_runs` 中按 mtime 找最新包含候选、reject log 或 `candidate_filter_trace.jsonl` 的 run；如果要复盘指定 run，用 `--run-id <run-id>` 或 `--run-dir <path>`。

查看所有本地 dataset 是否已经能复盘：

```bash
./om research shadow-replay status --min-sample 30 --min-mark-points 2 --mark-stale-hours 24
```

`status` / `list` 不采样、不结算、不写 dataset。它会给每个 dataset 输出 `candidate_snapshot_count`、`has_rejected_universe`、`last_mark_at`、`usable_mark_path_snapshot_count`、`outcome_fact_count`、`sampling` 和 `next_suggested_action`。顶层 `data_plan` 只列出可执行的数据维护动作：`collect_marks` / `settle`；证据已经足够人工复盘的 dataset 会进入 `review_queue`，并给出显式 `analyze` 命令。`--min-mark-points` 用来避免只有单点 mark 就误以为路径证据充足；`--mark-stale-hours` 用来标记长时间未更新的 mark。

独立执行当前数据计划：

```bash
./om research shadow-replay run-data-plan --min-sample 30 --min-mark-points 2
./om research shadow-replay run-data-plan --min-sample 30 --min-mark-points 2 --source local --write
./om research shadow-replay run-data-plan --min-sample 30 --min-mark-points 2 --source opend --write --max-datasets 3
```

默认 dry-run，只返回会执行的动作，不写 receipt。显式 `--write` 时才执行 `collect_marks` / `settle`，并写 receipt 到 `output_shared/research/shadow_replay/receipts/`。`--action` 只能限制为数据维护动作，例如 `--action settle`；它不接受 `analyze`，人工复盘仍从 `analyze` 命令进入。

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

操作顺序：

1. 先看 `status.data_plan`，优先处理 `priority=high` 的数据维护 dataset。
2. 想批量处理时先跑 `run-data-plan` dry-run，确认动作和 dataset。
3. `action=collect_marks` 时先用本地 required data；如果缺报价，再显式用 OpenD。
4. `action=settle` 时说明路径证据已经够进入 outcome 推导。
5. `review_queue` 里的 `next_suggested_action=analyze` 只表示证据已经可供人工复盘；不要把 `data_plan` 当策略建议，也不要通过 `run-data-plan` 执行分析。

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
- `insurance_metrics`：把 Sell Put / Covered Call 当作承保组合复盘，重点看收取保费、赔付/回补成本、loss ratio、保费/资本占用、接货/被叫走比例，以及路径最大浮亏/保费。
- `outcome_by_bucket`：DTE、Delta、IV/RV、Spread、集中度各区间的表现。

## Status 解释

| status / reason | 含义 | 处理 |
|---|---|---|
| `not_ready / candidate_universe_missing` | 没有候选全集 | 重新指定 `run-id` / `run-dir` / candidate path |
| `not_ready / candidate_snapshot_count_below_min_sample` | 样本数不足 | 多积累 run 或降低人工评审阈值 |
| `not_ready / parameter_fields_missing` | 候选影响对比样本缺少实际可调字段，不能可靠比较 variants | 让新扫描 trace/reject evidence 写入 `dte`、`delta/abs_delta`、`iv_rv_ratio`、`iv_minus_rv` 后重跑 |
| `evidence_incomplete / rejected_universe_missing` | 只有最终候选，缺被拒样本 | 检查 `candidate_filter_trace.jsonl` / reject log |
| `ready_for_sampling / mark_path_snapshots_missing` | 没有路径采样 | 跑 `collect-marks --source local` 或 `--source opend` |
| `ready_for_sampling / usable_mark_path_snapshots_missing` | 有 mark 但没有可用报价 | 检查 bid/ask/mid/spot，必要时用 OpenD 重新采样 |
| `ready_for_settlement / outcome_facts_missing` | 有路径但未结算 outcome | 跑 `settle --write` 或 `collect-marks --write --settle` |
| `ready_for_settlement / outcome_facts_incomplete` | 部分合约 outcome 缺失 | 补跑 `settle --write` 或继续采样 |
| `needs_human_review / shadow_replay_ready_for_manual_review` | 证据够人工评审 | 看 bucket 和 accepted/rejected 对比 |

## 边界

- 不自动执行；需要人工命令或独立低优先级调度。
- 不跟随 tick 主链路同步执行；未来如自动化，应作为 post-tick / after-market job 消费 tick artifacts。
- `status` / `list` 只读本地 dataset，`data_plan` 只生成数据维护建议命令，不执行命令；`review_queue` 只提示人工复盘入口。
- `run-data-plan` 默认 dry-run 且不写 receipt；显式 `--write` 才执行数据维护动作并写本地 receipt。它不执行 `analyze`，仍不是 tick、不是通知、不是策略推荐。
- `--source local` 只读 required_data cache，显式 `--write` 时只写 replay dataset。
- `--source opend --write` 会读取 OpenD、刷新本地 required_data cache、写 replay dataset，并维护本地 OpenD 限流状态和 option-chain cache；不带 `--write` 时使用临时目录做预览，不持久化这些文件。
- 不写 Feishu、不写 broker、不写 trade state、不写 runtime config、不发送通知。
