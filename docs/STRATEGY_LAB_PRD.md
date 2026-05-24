# Strategy Lab PRD

## 一句话目标

Strategy Lab 是 OM 的线上策略实验系统。它使用冻结的历史样本评估当前策略和候选策略，在不影响真实交易的前提下，给出是否值得调整策略的证据化建议。

## 背景

历史 `strategy_lab` 实现更接近单次证据复盘：读取候选 CSV、reject log、trace 和历史行情 snapshot，然后按两套筛选规则比较输出。这类能力适合排查规则和证据链，但不能回答策略实验的核心问题，也不应作为 Strategy Lab 的公开产品入口：

- 当前策略过去一段时间表现如何？
- 候选参数是否比当前策略更好？
- 更好是否来自过拟合？
- 是否值得进入 shadow 或人工评审？

因此 Strategy Lab 的产品边界需要从“重跑一次策略证据”调整为“基于历史样本做策略评估和策略改进建议”。

## 用户与场景

主要用户是 OM 使用者本人，目标是在不直接修改生产策略的情况下，判断策略是否需要调整。

核心场景：

- 评估 `sy` 账号最近三个月 Sell Put 策略是否有效。
- 比较当前 Sell Put 参数和更保守/更激进的一组候选参数。
- 判断某个候选策略是否值得进入 shadow 观察。
- 当样本不足时，明确知道缺什么数据，以及需要从线上采集哪些样本。

## 产品目标

Strategy Lab 只回答一个核心问题：

> 在给定历史样本、资金约束和风险约束下，当前策略是否值得调整？如果值得，哪个候选策略更优，是否可以进入 shadow 或 review？

必须做到：

- 支持线上运行，不依赖本地 Mac 分析。
- 自动从线上运行产物和账本中采集实验样本。
- 将实验输入冻结成可复现 dataset。
- 同时评估 baseline 和 candidate/grid。
- 样本不足时返回 `not_evaluable`，不输出伪策略结论。
- 只输出建议，不自动改生产配置、不写交易、不改持仓。

## 非目标

Strategy Lab 不负责：

- 单次扫描 replay。
- “为什么没有候选”的系统诊断。
- candidate/reject/trace 路径排查。
- OpenD、配置、账本、服务健康检查。
- 直接修改 `config.yaml` 或 runtime config。
- 直接写 `trade_events` / `position_lots`。
- 自动下单、自动调仓、自动切换策略。
- 由 LLM 生成收益、风险、持仓、账本事实。

这些能力分别归属：

- Doctor / Healthcheck：系统和运行状态诊断。
- Candidate Explain：候选为什么被过滤或排序异常。
- Diagnostic Replay：证据复盘和规则差异排查。
- Research：证据打包和人工分析交接。

## 核心概念

### Dataset

冻结的实验数据集。它是 Strategy Lab 的唯一事实输入，来自线上系统已有数据和显式采集结果。

最小内容：

- 实验范围：market、account、strategy_type、start_date、end_date。
- 历史 scan runs。
- 所有候选样本。
- 所有拒绝样本和过滤原因。
- trade_events。
- position_lots / lifecycle outcome。
- close / expiry / assignment 结果。
- 历史行情 snapshot。
- 资金占用和现金可用快照。

Dataset 一旦生成，不随线上文件变化而改变。后续实验必须引用 dataset id。

### Experiment

一次策略实验。它包含：

- baseline：当前线上策略参数。
- candidate：一组候选策略参数，或一个参数 grid。
- capital model：资金占用模型。
- execution assumptions：成交价格假设和滑点/保守价假设。
- risk constraints：最大资金占用、集中度、assignment、tail loss 等约束。

### Evaluation

基于同一个 dataset 对 baseline 和 candidate/grid 做可比评估。

输出指标至少包括：

- 样本量。
- 净流入。
- realized PnL。
- locked cash / locked cash days。
- 单位资金收益。
- assignment rate。
- strike breach rate。
- worst trade PnL。
- tail loss scenario。
- 标的集中度。
- 到期日集中度。
- 数据质量和置信度。

没有可信权益曲线前，不输出标准 Sharpe、Sortino、Calmar。需要这类指标时，必须先建立可信权益曲线和估值口径。

### Recommendation

Strategy Lab 只输出以下结论：

- `not_evaluable`：样本或数据质量不足，不能评估。
- `reject`：证据不支持调整策略。
- `watch`：继续观察，暂不进入 shadow。
- `shadow`：建议进入影子观察，不改生产策略。
- `review`：值得人工评审，确认风险预算后再决定是否上线。

## 主要流程

### 1. 用户发起实验

CLI：

```bash
./om strategy-lab dataset collect \
  --config-key us \
  --account sy \
  --strategy-type sell_put

./om strategy-lab experiment \
  --dataset-id <dataset_id>

./om strategy-lab current
```

飞书：

```text
评估 sy 的 sell put 策略，时间范围最近三个月
```

### 2. 系统采集并冻结 Dataset

系统从线上 runtime root 读取历史样本，生成 dataset：

```text
output_shared/strategy_lab/datasets/<dataset_id>.json
```

Dataset collect 是写操作，需要确认或由已授权的后台任务执行。
默认 dry-run；确认后只写 Strategy Lab dataset 文件。

### 3. 数据质量 Preflight

Preflight 判断实验是否可评估：

- 样本数是否达到最低要求。
- 是否覆盖足够多的交易日和到期周期。
- 是否有候选、拒绝、成交、平仓/到期结果。
- 是否有必要的历史行情和资金占用字段。
- 是否存在明显数据断层。

不满足时返回 `not_evaluable`，并说明缺失项和补样建议。

### 4. 执行实验

对 baseline 和 candidate/grid 在同一 dataset 上运行评估。

Strategy Lab 可以调用确定性策略引擎，但不能调用会改变线上状态的链路。

### 5. 输出报告和建议

报告写入：

```text
output_shared/strategy_lab/experiments/<experiment_id>.json
output_shared/strategy_lab/reports/<experiment_id>.md
output_shared/state/current/strategy_lab.current.json
```

这些文件只属于 Strategy Lab 自己的运行产物，不改变线上策略、账本或交易。

飞书回执只展示产品结论：

- 是否可评估。
- baseline 表现。
- candidate 表现。
- 主要风险。
- 建议结论。
- 下一步动作。

## 权限和安全

Strategy Lab 允许的写入：

- Strategy Lab dataset。
- Strategy Lab experiment result。
- Strategy Lab report。
- current pointer。
- audit record。

Strategy Lab 禁止的写入：

- 生产策略配置。
- `trade_events`。
- `position_lots`。
- Feishu 多维表。
- broker / OpenD 交易状态。

任何将候选策略推进到 shadow 或生产的动作，都必须是独立流程，且需要 preview + confirm。

## MVP 验收标准

- 用户可以用账号、策略、时间窗口发起实验。
- 系统可以从线上 runtime root 采集并冻结 dataset。
- Dataset 不足时返回 `not_evaluable`，不输出策略优劣结论。
- Dataset 足够时输出 baseline 与至少一个 candidate 的对比。
- 报告能说明收益、资金占用、风险、样本质量和建议等级。
- Strategy Lab 不暴露单次 replay 作为产品主入口。
- 测试覆盖 dataset collect、preflight、experiment evaluation、recommendation、安全写边界。

## 后续扩展

- 参数 grid 搜索。
- train / validation / holdout 切分。
- shadow 观察期管理。
- 策略版本库。
- 人工评审记录。
- 可信权益曲线和标准风险指标。
