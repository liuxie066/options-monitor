# 策略复盘与参数学习

这套能力是离线、证据优先的复盘入口。它不会重新扫描、不会发通知、不会写 Feishu，也不会自动修改生产配置。

## 目标

长期证据源建议拆成四类：

- 候选快照：每次扫描看到的所有候选及预测收益
- 过滤 / 拒绝决策：每个候选被哪个规则接受或拒绝
- 生命周期结果：是否成交、是否平仓、是否 roll、最终实际收益
- mark-to-market 路径：持有过程中用于计算最大回撤的价格序列

`strategy_replay_analyze` 接受的是把这些证据连接后的复盘视图。视图中每个候选至少包含：

- `symbol`：标的
- `mode` / `strategy` / `side`：`put`、`call`、`sell_put` 或 `sell_call`
- `contract_symbol`：合约标识，可选但建议保留
- `expiration`
- `dte`
- `delta`
- `predicted_return`：候选当时的预测收益；也兼容扫描输出里的 `annualized_net_return_on_cash_basis` / `annualized_net_premium_return`
- `actual_return`：最终实际收益
- `max_drawdown`：持有过程最大回撤，正负数均可，分析时按损失幅度处理
- `close_triggered`：是否触发平仓
- `roll_triggered`：是否触发 roll
- `accepted`：是否通过候选过滤 / 是否被选择
- `filter_reason`：被拒绝或风险过滤的原因，可用 `;` 分隔多个原因

关键原则：记录全部候选和被拒绝候选，不只记录通知或实际成交的候选。否则会有幸存者偏差，无法回答过滤条件是否真的有价值。

## 分析入口

Agent 工具：

```bash
./om-agent run --tool strategy_replay_analyze --input-json '{"replay_path":"output/reports/strategy_replay.csv","min_sample":5}'
```

统一 CLI：

```bash
./om strategy-replay analyze --replay-path output/reports/strategy_replay.csv --min-sample 5
```

也可以传 JSON / JSONL。JSON 顶层支持数组，或对象里的 `rows` / `records` / `candidates`。

## 自动回答的问题

`strategy_replay_analyze` 输出四组核心结论：

- `dte_effectiveness`：按 DTE 分桶统计胜率、平均实际收益、平均最大回撤、平仓/roll 触发率、风险调整收益
- `delta_effectiveness`：按 `abs(delta)` 分桶统计同样指标
- `symbol_risk_return`：按标的汇总，标出“收益高但回撤差”的标的
- `filter_value`：按过滤条件汇总被过滤候选的后验结果，估算过滤条件是否避免了低收益或高风险候选

输出里的 `dry_run_config_suggestions` 只是影子建议，用于人工审阅或后续 dry-run 对比，不会自动写入配置。

其中 `best_ranges` 按风险调整收益排序，`best_win_rate_ranges` 按胜率排序；DTE 通常看前者，Delta 胜率问题优先看后者。

## 样本护栏

默认 `min_sample=5`。低于样本数的桶会标记为 `low_sample`，完全没有实际收益的桶会标记为 `missing_outcomes`。

过滤条件要有被拒绝候选的后验结果才能评估价值。如果只有通过候选，没有 reject/shadow outcome，工具会返回 warning，而不是硬猜过滤条件有效。
