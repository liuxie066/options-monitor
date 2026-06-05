# Opportunity Quality

Opportunity Quality 是 Shadow Replay、扫描质量复盘和参数建议共用的判定口径。它回答的不是“这张合约后来赚没赚钱”，而是“系统当时接受或拒绝这张机会是否合理”。

## 目标

Shadow Replay 的目标是离线评估扫描质量：

```text
扫描证据 -> 路径证据 -> 结果证据 -> 决策质量标签 -> 参数假设
```

它不重新扫描、不自动调参、不修改 runtime config、不写交易状态、不发送通知。参数建议只能作为可审查假设输出，不能直接执行。

## 分层

| 层级 | 目的 |
|---|---|
| 事前质量 | 判断开仓当时的收益是否补偿策略所要求的风险 |
| 事后路径 | 判断后续路径和结果是否验证或否定当时判断 |
| 决策质量 | 判断系统当时 accept/reject 是否合理 |
| 参数假设 | 在证据充分时提出可审查、dry-run-only 的参数调整方向 |

## 策略口径

`insurance_underwriting` 和 `return_first` 必须分开评价。历史 `short_vol` 样本归入当前承保口径。

### insurance_underwriting

`insurance_underwriting` 是承保评估器。好机会的核心不是单次盈利，而是保费是否足够补偿 IV/RV、事件、流动性、现金或持股覆盖和 strike 边界风险。

事前质量重点：

- IV/RV 和 IV-RV 边际是否足够
- 事件风险是否可评估且可接受
- 流动性和价差是否可交易
- 现金或持股覆盖是否满足开仓前提
- 收益补偿是否足够

一个 `insurance_underwriting` 候选最终赚钱，不自动说明它是好机会；如果开仓时 IV/RV 不足、事件不可评估或执行质量差，它仍可能是坏承保。一个 `insurance_underwriting` 候选最终亏损，也不自动说明它是坏机会；如果风险在预算内且保费补偿合理，亏损可能只是已接受风险兑现。

### return_first

`return_first` 是收益筛选器。它主要判断 DTE、strike、年化收益、单笔净收入、流动性、现金或持股覆盖是否满足收益目标。它不系统性评价完整承保风险，所以不能用 `insurance_underwriting` 的承保标准直接判定 `return_first` 样本。

## 决策质量标签

| 标签 | 含义 |
|---|---|
| `good_accept` | 系统接受，事前质量合格，事后没有否定该判断 |
| `bad_accept` | 系统接受，但事前风险补偿不足，或事后暴露出模型本该拦住的风险 |
| `good_reject` | 系统拒绝，拒绝理由与风险或收益质量一致 |
| `bad_reject` | 系统拒绝，但证据显示可能错过了符合策略口径的机会 |
| `inconclusive` | 样本、mark path、outcome、策略口径或关键字段不足 |

标签不能只由 PnL 决定。

## 参数建议规则

参数建议只能从决策质量标签派生：

- `bad_accept` 多：优先考虑收紧风险阈值。
- `bad_reject` 多：考虑放宽过严阈值。
- `good_reject` 多：拒绝规则有效，不应因为错过表面收益而放宽。
- `inconclusive` 多：先补数据，不建议调参。

Shadow Replay 先输出 `parameter_advice_gate`，只判断当前证据是否允许进入参数讨论阶段。它不会输出具体参数数值，也不会修改配置。

每条参数建议至少包含：

- `strategy_profile`
- 样本量
- 触发标签
- 涉及参数
- 建议方向
- 预期改善
- 代价
- 置信度
- `shadow_dry_run_only=true`

## 最小验收样例

1. `insurance_underwriting` 候选最终赚钱，但开仓时 IV/RV 不足，不能标 `good_accept`。
2. `insurance_underwriting` 候选最终亏损，但风险在预算内且保费补偿合理，不自动标 `bad_accept`。
3. rejected 候选后来赚钱，但当时事件风险不可评估，不能标 `bad_reject`。
4. `return_first` 样本不能用 `insurance_underwriting` 的 IV/RV 失败直接判坏。
5. 样本不足时只能输出 `inconclusive`，不能生成参数建议。
