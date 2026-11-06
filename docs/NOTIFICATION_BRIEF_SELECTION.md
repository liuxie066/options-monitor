# 通知简报条数与配置迁移

本文说明 Daily Brief 账户展示额度的迁移。配置事实源与快照生成流程见[配置契约](../CONFIGS.md)，通知通道配置见[配置指南](../CONFIGURATION_GUIDE.md#通知)。修改 authoring 配置后须按现有配置流程重新生成市场运行快照；不要手改生成的 JSON。

## 账户展示额度

```yaml
notifications:
  daily_brief:
    max_items_per_account: 5
```

`notifications.daily_brief.max_items_per_account` 默认 `5`，只接受 `0..20` 的整数；布尔、负数、超过 `20`、浮点数、`null` 和字符串均非法。

同一账户在同一市场的 A/B/C 各部分共享这个展示额度，默认合计最多 `5` 条，不是每个部分分别 `5` 条。市场分别计算：同一账户的 US 简报使用 US 的额度，HK 简报使用 HK 的额度，不互相占用。

`0` 只取消账户这一层的条数上限。整篇简报仍受 `42` 条总预算及 `12,000` 字符正文上限约束，也仍须通过各自的通知资格与 action 生成规则，因此不保证全部源记录都会展示。

## 选择顺序

- A：已通过各自 owner 资格判断的已有持仓平仓/移仓。已证实且早于下一固定报告的业务截止点优先；其余平仓队列按剩余最高年化升序、净兑现比例降序、最后可交易时间升序、稳定身份排序，移仓队列按首位合格目标的增量年化降序、行权价调整比例降序、延期天数升序、同币种净收取降序、稳定身份排序。两队列从平仓开始交替，空队列不保留名额。同旧仓的平仓与移仓合成互斥备选卡，不能同时执行；同旧合约和同目标的多个 lot 继续合卡。
- B：可执行开仓机会按已封存 rank 轮转；同 rank 顺序为 CSP、CC、组合增强、Wheel。diff 变化不改 rank，也不重新评分或选合约。
- C：时间/事件提醒及当前无法增加手数的候选、Wheel 状态，排在可执行动作之后。没有已证实的具体时间时标注“时间待核实”；不把时间未知当零天，也不据此要求平仓。
- 普通 hold 不占卡，汇总数量；不可评估持仓不占卡，进入缺口摘要。省略的动作/提醒有数量说明，持仓省略项同时列出标的与合约。

排序只读取封存数据。业务截止点不使用报价有效期代替；当前持仓生产数据没有封存最后可交易时刻或指派截止时刻时，保持未知，不从到期日猜出具体时刻。下一固定报告时间由 scheduler 的固定报告计划提供，排除中间候选检查和发送截止点。净收取总额不跨币种比较，也不优先于增量年化等经济指标；聚合后的 lot 总现金不作为排序依据。

## 旧行为与归属

旧逻辑存在多个独立的限制，不能把旧字段当成新的统一展示额度：

| 原有配置或限制 | 迁移前的作用 | 迁移后的归属 |
|---|---|---|
| `close_advice.max_items_per_account`，默认 `5` | 决定可进入平仓通知的条目与平仓 action 生成，并参与平仓展示切片 | 继续归平仓通知资格与平仓 action 生成所有；统一展示额度由 `notifications.daily_brief.max_items_per_account` 管理 |
| 持仓动作（含 roll）默认 `5`，各开仓策略候选分别默认 `3` | 持仓动作受 `max_actions_per_priority` 限制，开仓候选受 `max_candidates_per_strategy` 限制，账户总展示量可能超过 `5` | A/B/C 共同使用账户展示额度；`max_candidates_per_strategy` 仍限制各策略候选源，不保证各有展示名额 |
| `notifications.daily_brief.max_actions_per_priority` | 原有 action 展示限制 | 已弃用；仍接受并验证，选取不再读取该键 |

`close_advice.max_items_per_account` 不是单纯的历史展示字段，不能直接删除或自动搬到新位置。它继续决定平仓通知资格与平仓 action 生成；提高新的展示额度，不会把被平仓资格或 action 生成规则排除的条目重新加入简报。

## 迁移旧配置

在 `config.yaml` 中用 `notifications.daily_brief.max_items_per_account` 表达所需的账户展示额度。省略时使用默认 `5`；需要取消账户层上限时设为整数 `0`，整体条数与正文上限仍生效。

删除已不再分配名额的 `notifications.daily_brief.max_actions_per_priority`。迁移期间可以暂时保留它，但它仍严格接受 `1..20` 的整数：布尔、`0`、负数、超过 `20`、浮点数、`null` 和字符串照常验证失败，即使已配置新的有效额度也不例外。旧值不会自动迁移成新值；同时配置两者时，账户展示额度只取新键。

系统默认配置和配置示例已使用新键，不再自动加入旧键。验证仍含旧键的配置时会给出弃用与迁移提示；重新验证不含旧键的配置不会产生该提示。已有运行快照可能保留旧键，需要从更新后的 authoring 配置重新生成。

按原有业务需要保留 `close_advice.max_items_per_account`，不要为了改变简报展示条数而顺带改变平仓通知资格或平仓 action 数量。

配置实现由 [`config_defaults.py`](../src/application/config_defaults.py) 与 [`config_validator.py`](../src/application/config_validator.py) 维护；账户展示选取由 [`daily_decision_brief_renderer.py`](../src/application/daily_decision_brief_renderer.py) 维护。平仓资格与 action 生成的依据分别在 [`close_advice.py`](../domain/domain/close_advice.py) 与 [`daily_decision_brief_service.py`](../src/application/daily_decision_brief_service.py)。
