# 行权价距离与离线对照

本入口只解释显式提供的合成或脱敏离线样本。它不更新候选快照、筛选、排序、容量、张数、通知或生产配置，也不采集行情、账户或持仓。普通 CSP、CC、Wheel Put、Wheel Call 分别使用实验参数。

```bash
./om research strike-distance \
  --input tests/fixtures/strike_distance_synthetic.json \
  --threshold csp=0.03 --threshold cc=0.05
```

结果是 stdout 上的 CLI JSON envelope；`data.markdown` 是中文并列展示，`observations` 是逐行对照，`summaries` 按策略、币种分别汇总。省略 `--threshold` 只展示信息，没有默认实验门槛。`0.03` 表示 3%，只是示例，不是建议。

## 口径

设现价 S、行权价 K、已扣完整卖出估算费用的每张净权利金 N、标准合约乘数 M：

| 指标 | Put | Call |
| --- | --- | --- |
| 裸行权价距离 | `(S-K)/S` | `(K-S)/S` |
| 短期权腿盈亏平衡价格 | `K-N/M` | `K+N/M` |
| 含净权利金距离 | `(S-(K-N/M))/S`，净接货折价 | `((K+N/M)-S)/S`，短 Call 腿盈亏平衡上行距离 |

正裸距离为 OTM、零为 ATM、负值为 ITM。Call 净距离**不是 CC 持股的下跌保护或整个持仓保本距离**；不能替代历史持股成本、成本底线、被叫走后的总收益。权利金不会改变行权价或被指派条件。这些数字均不是精确指派概率。

计算复用 Candidate Engine 的距离与盈亏平衡算术。正式开仓指标的字段、未舍入内部计算、舍入规则保持不变。研究输入 `net_income` 是既有每张净金额，不在应用层另算费用；保留 `fee_basis`、`fee_schedule_version` 和货币。零和负净权利金是可描述的样本，不因此声称符合正式候选条件。

`strike_distance_pct` 与净距离统一保留 10 位小数，百分比最多显示 8 位小数。阈值最多 10 位小数；距离等于阈值保留。判断只用裸距离，不用净距离替换。

## 输入及缺失状态

顶层：`schema_version: 1`、`source_kind: synthetic|offline_redacted`、明确带时区的回放时钟 `as_of`、`rows` 数组。参见上面完整合成 fixture。每行必须有唯一 `candidate_id`、`strategy: csp|cc|wheel_put|wheel_call`、匹配的 `option_type: put|call`、规范 `symbol`、`contract_symbol` 和正整数 `rank`。原始行按原顺序保存在 `baseline_rows`；这是传入样本的副本，不是重新认证的生产候选。

- 裸距离需要有限且为正的 `spot` / `strike`、`market: US|HK`、ISO 日期 `expiration`、带时区的 `spot_observed_at_utc`。现价在回放时钟之前 0–300 秒内有效，恰好 300 秒有效。过期、到期当日或缺失到期日不可用；DTE 按市场本地自然日重算，不采用输入猜测值。历史样本以其明确回放时钟检查，不冒充今日现价。
- 净距离另外需要 `option_standard_type: STANDARD`、一致的 `multiplier` / `chain_multiplier` / `snapshot_multiplier`、对应市场的 `currency: USD|HKD`、有限 `net_income`、费用口径/版本、0–300 秒内的 `snapshot_received_at_utc`。不默认 100，不把成本底线当成现价，不回退缓存。净指标不完整时仍可显示有证据的裸距离。
- `contracts` 必须显式提供非负整数才能汇总实验金额。它表示本次实验拟比较的张数，不能从 `max_new_contracts` 自动填入。缺失或非法时金额不可用，原字段仍保留。候选是替代方案，逐行金额之和不能解释成同时可执行的资金配置。
- `metrics.status/reason`、`net_status/net_reason` 分别说明缺失；`decision` 为 `not_requested`、`unavailable`、`would_exclude` 或 `retain`。不可判定不是保留。
- `forgone_net_premium` 是假设排除行的每张净权利金乘实验张数，以原币十进制字符串输出；负净额保持负值。汇总 `known_forgone_net_premium` 只是已知小计，`missing_premium_candidate_ids` / `unassessed_candidate_ids` 非空时 `status=partial`，不能把小计当完整机会损失。未设阈值则 `not_requested`。
- 输入由调用者提供，并不因 `source_kind` 或 `status=ok` 就证明来自完整、正确的券商事实。不得用账户凭据或未脱敏生产账本充当输入。

## 波动缓冲边界

本版本 `volatility_buffer` 固定为 `unavailable`，理由 `rv_quality_dependencies_629_630_631`。2026-10-16 核对基线 `5c74cb84` 时，[#629](https://github.com/liuxie066/options-monitor/issues/629)、[#630](https://github.com/liuxie066/options-monitor/issues/630)、[#631](https://github.com/liuxie066/options-monitor/issues/631) 尚无合入修复，分别涉及日历覆盖、证券身份及未知日历类型。即使样本声称 RV 正常，也不据此计算风险建议。

本入口没有采用 IV 或 RV，没有年化转换或“同期波动倍数”。未来启用前需要在 RV owner 验证修复、来源、完整性及期限匹配，明确 252 交易日年化与剩余交易 session，或者另行论证 IV 与自然日到期口径；不可直接把自然日 DTE 代入 RV 的交易日基准。无需为本实验重复修复这些 issue。

## 旧过滤为何退役

main 中的 [`e2ec1690`](https://github.com/liuxie066/options-monitor/commit/e2ec16900357209efaa6c74869322fb0ec8c0518)（2026-06-05）删除 `min_otm_pct` 并在配置校验中拒绝它，改用现价与可接受接货/卖出价格边界。该提交的架构说明把普通 CSP/CC 转为围绕价格边界的承保语义，区分硬筛与排序。未找到可核验的专门 PR 讨论或阈值收益实证，因此不能说旧过滤已被回测证明无效。本功能不恢复退役配置。

当前普通 CSP/CC 的 Delta 仅展示；候选 owner 已计算 Put 裸距离、净接货折价和 Call 距现价距离，排序已有相关 tie-break。Wheel 默认 Delta 和生命周期/容量边界不同，实验不更改或重复实现它们。细则见 [候选合同](candidate_strategy.md) 与 [Wheel 合同](WHEEL_STRATEGY_PRD.md)。

## 后续评估

取得授权且完整的历史报价、成交/费用、持仓与企业行动数据后，才可在相同决策时间、资金预算和可成交假设下做基线与阈值对照，分别报告 CSP/CC 与 Wheel 各分支：

1. 期权净现金流与费用，加上被指派股票的已实现/未实现盈亏，采用一致的标记时点；
2. 实际占用资金与占用天数，而非只加最大候选手数；
3. 组合回撤、指派率及其样本区间；
4. 放弃的权利金、未参与的上涨/反弹与替代交易机会损失，避免重复计算；
5. 样本缺失、不可成交、提前指派、拆股/分红和到期持股残值的处理，避免幸存者偏差与未来信息。

本合成工具只做阈值切片和放弃净权利金核算，不能完成上述收益回测。没有合规历史数据时，不能证明收益改善、推荐最佳阈值或宣称生产已生效。下一项产品决策是是否取得这些数据开展评估，以及经验证后是否将距离信息接入日常报告；任何默认硬过滤需要独立决策。

该子命令在 CLI 环境装配前即跳过 runtime root、用户 instance record 和 env 文件发现，即使进程有 `OM_ENV_FILE` 也不会加载它。样本如果显式标记 `underlier_observation_status` 非 `ready`，保留失败原因并将距离标为不可用；不能以新鲜时间戳覆盖已知取数失败。
