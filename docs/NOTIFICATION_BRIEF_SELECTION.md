# 通知简报条数与配置迁移

本文说明 Daily Brief 的独立分区额度与旧配置迁移。配置事实源与快照生成流程见[配置契约](../CONFIGS.md)，通知通道配置见[配置指南](../CONFIGURATION_GUIDE.md#通知)。修改 authoring 配置后须按现有配置流程重新生成市场运行快照；不要手改生成的 JSON。

## 独立分区与整份额度

```yaml
notifications:
  daily_brief:
    max_total_items: 40
    max_close_items: 5
    max_roll_up_items: 5
    max_roll_down_items: 5
    max_candidates_per_strategy: 3
    max_rejection_reasons: 5
```

| `notifications.daily_brief` 下的配置 | 默认值 | 严格整数范围与作用 |
|---|---|---|
| `max_total_items` | `40` | `1..40`，单份报告完整展开的决策卡总上限 |
| `max_close_items` | `5` | `0..20`，平仓建议分区 |
| `max_roll_up_items` | `5` | `0..20`，合格 CC 上移建议分区 |
| `max_roll_down_items` | `5` | `0..20`，合格 Put 下移建议分区 |
| `max_candidates_per_strategy` | `3` | `1..20`，各开仓策略独立的候选展示上限，固定报告与合格候选增量共用 |
| `max_rejection_reasons` | `5` | 沿用 `1..20` 校验及既有行为，本轮不扩展拒绝原因展示策略 |

这些字段均拒绝布尔、浮点数、`null`、字符串和范围外整数。三个持仓分区中的 `0` 只取消该分区条数上限；它不关闭报告，也不取消整份和渠道大小限制。整份总额与开仓候选额度不接受 `0`。

额度按账户、市场和本次逻辑报告独立计算。三个持仓分区彼此不借名额，平仓少于五条不会让下移多出名额；开仓不消耗持仓分区额度。没有单账户 A/B/C 合计五条的限制。同一账户 US 与 HK 分别出报告；本轮未实现跨账户组合或合并。

分区占用以入选建议计数，整份上限以完整卡片计数。同旧仓的平仓和移仓只有分别在各自分区入选后才能合成一张互斥备选卡：各占所属分区一条，整份只占一张。只入选一项时仅显示该项，不能借合卡带入落选备选。默认三个持仓分区可入选最多十五条建议，各策略开仓仍分别最多三条；合卡和最终大小限制会使完整卡数更少。

整份内容继续受 `12,000` 字符正文上限及各通道最终内容／请求体大小限制。额度省略与数据缺口分别说明；普通 hold 汇总、不占卡，不可评估持仓进入缺口摘要。完整源记录保留不等于全部逐条展示。

## 选择顺序与固定报告

- A：已通过领域 owner 资格判断的平仓、CC 上移、Put 下移建议，分别排序和限额。已证实且早于下一固定报告的业务截止点优先；平仓随后按剩余最高年化升序、净兑现比例降序、最后可交易时间升序、稳定身份排序。移仓随后按首位合格目标的增量年化降序、对应行权价调整比例降序、延期天数升序、同币种净收取降序、稳定身份排序。同旧合约和同目标的多个 lot 继续合卡。
- 整份或最终大小不足时，A 先于 B、B 先于 C；A 先保留已证实的紧迫业务截止项，其余按平仓、上移、下移轮转，空分区跳过、备选合卡不重复计数。不保证每区至少一条。
- B：可执行开仓机会按已封存 rank 轮转；同 rank 顺序为 CSP、CC、组合增强、Wheel。变化不改 rank、不突破配额，也不重新评分或选合约。
- C：时间／事件提醒及当前无法增加手数的候选、Wheel 状态，排在可执行动作之后。没有已证实的具体时间时标注“时间待核实”；不把未知时间当零天，也不据此要求平仓。

排序只读取封存数据。业务截止点不使用报价有效期代替；没有封存最后可交易时刻或指派截止时刻时保持未知，不从到期日猜出具体时刻。净收取不跨币种比较，也不优先于增量年化等经济指标；聚合 lot 总现金不作为排序依据。Wheel 的合约乘数、指派交割费用、覆盖等缺口继续保留在受保护摘要中，即使对应卡片被省略。

每个有效固定批次都应报告，包括内容无变化和来源完整的真实空态。正常空态明确说明本轮无合格建议，动作计数为零；来源缺失不能伪装成零。展示额度只影响正文展开，不能取消或推迟固定批次；候选增量仍须满足既有通知资格。故障恢复按既有流程处理，未确认送达的批次不能记为成功。

## 旧键与 Close Advice 归属

| 配置 | 现行边界 |
|---|---|
| `notifications.daily_brief.max_items_per_account` | 已弃用且忽略；继续严格校验整数 `0..20`，不再限制任何分区或整份报告 |
| `notifications.daily_brief.max_actions_per_priority` | 已弃用且忽略；继续严格校验整数 `1..20` |
| `close_advice.max_items_per_account` | 保留给独立 Close Advice 入口，原有非负整数与 `0` 不限语义保留；不再限制 Daily Brief 来源、通知资格或平仓 action 生成 |

Daily Brief 从全部领域合格平仓行建立来源资格与 action，再由 `notifications.daily_brief.max_close_items` 独立选择展示。`close_advice.max_items_per_account` 保留，不自动删除或搬值；配置验证会说明其独立入口作用域。平仓策略、门槛和移仓有效性仍由原领域 owner 判断。

## 显式迁移旧配置

先按所需展示量设置新分区键与总额键。旧的混合限额不能表达三个独立限额，因此不复制旧值、不据此生成新值；新键缺省时采用默认各五条、整份四十条。新旧键并存时仅新键分配名额，但旧键仍须通过严格校验，即使新键有效也不例外。默认展示可能从旧混合五条增加到三个持仓分区各五条，消息可能变长。

沿用现有迁移入口，在目标实例预览：

```bash
./om config migrate-switches --config-yaml <path> --runtime-root <root>
```

核对 `changes` 中两个旧通知展示键的路径及原值，连同该命令原有的其它开关迁移、目标文件和哈希。根层和市场层旧键均会删除，已经配置的新键与 `close_advice.max_items_per_account` 保留。非法旧值报 `CONFIG_MIGRATION_CONFLICT`，不会猜测其含义或写入源文件。配置加载不会自动迁移。

使用同一命令加 `--apply --confirm`、`--expected-source-sha256 <before_sha256>`、`--expected-preview-sha256 <preview_sha256>` 才发布；沿用既有事务备份 YAML、重建已配置市场与 Bot 快照并回读。源或预览变化会拒绝旧哈希，需重新预览。再次迁移不重复删除已移除字段。该命令也可能迁移其它历史开关，须一起核对预览；详细边界见[配置开关迁移](../CONFIGURATION_GUIDE.md#配置开关迁移)。

系统默认配置和示例不再加入旧通知展示键。保留旧键会产生弃用与新键指引；移除后不再产生这类弃用提示。已准备的历史通知 envelope 按原冻结内容重试，不用新预算改写。

配置由 [`config_defaults.py`](../src/application/config_defaults.py)、[`config_validator.py`](../src/application/config_validator.py) 维护，显式迁移由 [`config_switch_migration.py`](../src/application/config_switch_migration.py) 维护。展示选择由 [`daily_decision_brief_renderer.py`](../src/application/daily_decision_brief_renderer.py) 维护，完整平仓来源与 action 组装由 [`daily_decision_brief_service.py`](../src/application/daily_decision_brief_service.py) 维护，策略资格由 [`close_advice.py`](../domain/domain/close_advice.py) 所属领域 owner 判断。
