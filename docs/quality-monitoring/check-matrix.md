# 运行与数据质量检查矩阵

- **状态**：已确认；本地实现映射完成，生产证据待 Phase 5
- **日期**：2026-07-26
- **总设计**：[architecture.md](architecture.md)
- **HTTP/API 契约**：[api-contract.md](api-contract.md)
- **实现映射**：[OM](om-check-implementation.md) · [PM](pm-check-implementation.md) · [Quality Hub](hub-check-implementation.md)
- **状态 Payload Schema**：[quality_status.v1.schema.json](quality_status.v1.schema.json)
- **实施计划**：[implementation-plan.md](implementation-plan.md)

本矩阵共 46 个正式检查 ID：运行检查 10 个、OM 数据检查 11 个、PM 数据检查 17 个、Hub 检查 8 个。每个 ID 必须在对应实现映射中拥有唯一实现边界和确定性验证证据；Phase 5 再补充真实生产运行证据，不以生产 canary 替代本地契约测试。

## 1. 使用规则

本矩阵是 V1 检查集合和门禁语义的规范。实现可以拆分或合并 I/O，但不得：

- 合并不同账户或不同数据集的结论；
- 用旧证据替代缺失的必要证据；
- 将 `unavailable` 降格为普通 warning；
- 用 Hub 重新实现 OM/PM 业务计算；
- 在检查失败时自动修改业务数据。

统一结论：

| 检查结果 | 默认严重度 | 数据/运行结论 | 门禁 |
|---|---|---|---|
| `pass` | `info` | 保持或恢复 trusted/healthy | 无 |
| `warn` | `warning` | partial/degraded | 仅明确允许的消费者可继续 |
| `fail` 且有确定性反证 | `blocking` | untrusted/unhealthy | 阻断相关消费者 |
| `unknown` 或必要证据缺失 | `blocking` | unavailable/unknown | 正式消费者 fail closed |

## 2. 运行检查

| ID | Owner | Scope | 检查与证据 | 触发/频率 | 通过条件 | 异常结论 | 受影响消费者 |
|---|---|---|---|---|---|---|---|
| `RT-OM-001` | OM | service | OM 主服务/systemd 状态、进程 exit、runtime artifact | Host Watchdog 5 分钟 | service active，最近进程无失败 | inactive/failed=`unhealthy/blocking`；不可查询=`unknown` | OM 全部正式消费者 |
| `RT-OM-002` | OM | listener + account | listener stage、heartbeat、last push/backfill、last error | OM 15 分钟；任务后立即 | heartbeat ≤ 配置间隔 2 倍 | 2–5 倍=`degraded`；>5 倍=`unhealthy` | trade intake、option positions |
| `RT-OM-003` | OM | timer + market | systemd timer、预期触发、最近成功回执、交易日历 | Watchdog 5 分钟 | 预期任务在 deadline+15 分钟内成功 | 明确失败立即 unhealthy；超期 unhealthy | 对应市场数据集 |
| `RT-OM-004` | OM | OpenD | 连接、账户/环境、权威查询返回、refresh evidence | 日终/差异复查/人工 | 完整成功且账户环境匹配 | 失败=`unavailable/blocking`，不得回退旧缓存 | 需要 OpenD 的对账 |
| `RT-PM-001` | PM | API/service | PM service systemd、HTTP self health | Watchdog 5 分钟 | service active 且接口可用 | `unhealthy` 或 `unknown` | PM 全部正式消费者 |
| `RT-PM-002` | PM | timer + account | morning/evening timer、job exit、最近成功 snapshot | Watchdog 5 分钟 | deadline+15 分钟内成功 | 明确失败立即 unhealthy；超期 unhealthy | 对应账户 holdings/cash/MMF |
| `RT-PM-003` | PM | OpenD | OpenD 连接、账户查询、refresh evidence、限频/超时 | PM 早晚同步/人工 | 查询成功且 completeness=complete | `unavailable/blocking` | PM snapshot 数据集 |
| `RT-HUB-001` | Hub | service | Hub service、SQLite、拉取 scheduler | Watchdog 5 分钟 | service/DB/scheduler 正常 | unhealthy；OM/PM 本地门禁继续工作 | 统一视图、跨服务告警 |
| `RT-HUB-002` | Hub | dispatcher | outbox backlog、最近投递、最近错误 | 每次投递/Hub 5 分钟 | 无失败积压 | 三次失败=`unhealthy` | 飞书通知，不改变业务数据状态 |
| `RT-EXT-001` | external | instance | Hub outbound dead-man heartbeat | Hub 每 5 分钟 | 15 分钟内收到 heartbeat | `[基础设施失联]` | 整机/网络/Hub 可用性 |

## 3. OM 数据质量检查

| ID | Dataset | Scope | 权威证据 | 通过条件 | 宽限/恢复 | 异常结论 | `blocked_consumers` |
|---|---|---|---|---|---|---|---|
| `OM-INT-001` | `om.trade_intake` | account + source | inbox state、checkpoint、audit、listener heartbeat | 无超期 pending；checkpoint 单调；audit 可读 | 普通成交 5 分钟 | 超期 pending=`untrusted/blocking`；证据不可读=`unavailable` | option positions、lifecycle、持仓型建议 |
| `OM-INT-002` | `om.trade_intake` | account + source | failed/unresolved rows、last error、retry evidence | 无未解决 failed | 修复后重新 intake reconciliation | unresolved=`untrusted/blocking` | 同上 |
| `OM-INT-003` | `om.trade_intake` | broker deal | broker deal identity、normalized economic fields、local event | OpenD 查询窗口完整且每条成交有唯一终态 | 盘中 5 分钟 | broker 有/local 无=`untrusted`；窗口不完整=`unavailable` | option positions、历史成交报告 |
| `OM-LED-001` | `om.ledger_projection` | account | 全量 `trade_events`、deterministic replay、materialized `position_lots` | full replay == materialized projection | 无；确定性检查 | 不等=`untrusted/blocking` | option positions、lifecycle、close advice |
| `OM-LED-002` | `om.ledger_projection` | account | duplicate broker identity、event/lot conservation diagnostics | 无冲突重复；数量守恒 | 无 | 冲突或不守恒=`untrusted/blocking` | 同上 |
| `OM-POS-001` | `om.option_positions` | account + market | OpenD option snapshot metadata | `refresh_cache=True` 成功、账户正确、snapshot complete | 查询失败可重试只读 | 不完整=`unavailable/blocking` | OpenD convergence、持仓型消费者 |
| `OM-POS-002` | `om.option_positions` | account + contract | materialized/replay positions、OpenD normalized positions | 身份、方向、数量、乘数一致 | 首次、1 分钟、5 分钟；三次或 5 分钟后阻断 | transient 不告警；persistent=`untrusted/blocking` | 持仓报告、lifecycle、close advice |
| `OM-LCY-001` | `om.lifecycle` | account + lifecycle case | trade events、assignment/exercise/expiry evidence、reconciliation | 已有完整终态，或仍在合法期限 | 下个市场交易日首次深对账 +2 小时 | 合法期内 pending；超期 stale=`partial/untrusted` | 生命周期报告、相关建议 |
| `OM-LCY-002` | `om.lifecycle` | account + lifecycle case | company action/transfer/manual adjustment evidence | 已明确分类和处理边界 | 人工 review 后重新验证 | `external_adjustment_pending_review`=`unavailable/blocking` | 受影响生命周期/持仓 |
| `OM-LCY-003` | `om.lifecycle_history` | account + historical period | migration evidence、legacy cases | 所需历史证据完整 | 无实时故障提醒；单独统计 | `legacy_evidence_gap`=`partial/untrusted` | 受影响历史报告 |
| `OM-HSYNC-001` | `om.stock_refresh_intent` | account | stock deal intent、queue/audit、PM sync result/high water | intent 最终得到 PM 成功 result | PM 既有 debounce/retry + scheduled fallback | 超时/失败=`degraded`；PM 数据状态由 PM producer 决定 | 正股刷新时效视图 |

## 4. PM 数据质量检查

| ID | Dataset | Scope | 权威证据 | 通过条件 | 宽限/恢复 | 异常结论 | `blocked_consumers` |
|---|---|---|---|---|---|---|---|
| `PM-ACC-001` | `pm.account_mapping` | account | configured acc_id、OpenD account list、trd_env/market/currency | acc_id 显式、存在、唯一、REAL；账户间不重复 | 无 | 缺失/冲突=`unavailable/blocking` | 对应账户全部同步、NAV |
| `PM-SRC-001` | `pm.futu_snapshot` | account | OpenD refresh result、pagination、query time、snapshot hash | 查询/分页完整，非异常空快照 | 只读有限重试 | 不完整=`unavailable/blocking`；不得写入 | holdings/cash/MMF |
| `PM-SRC-002` | `pm.futu_snapshot` | account | source_currency、configured currency、normalized currency | OpenD CNH 与配置一致；证据保留 CNH | 无 | 不一致=`unavailable/blocking` | cash/MMF/NAV |
| `PM-POS-001` | `pm.holdings_quantity` | account + security | OpenD STOCK/ETF snapshot、PM holdings read | 规范化身份和数量精确一致 | 写后立即 +30 秒重读 | persistent mismatch=`untrusted/blocking` | NAV、持仓报告、组合消费者 |
| `PM-POS-002` | `pm.holdings_quantity` | account | security classification、side、empty snapshot guard | 非零 position 分类完整；无 short/unknown；空快照经保护 | 无 | 分类/方向/异常空快照=`unavailable/blocking` | holdings sync、NAV |
| `PM-COST-001` | `pm.cost_basis` | account + security | OpenD average_cost、PM avg_cost | Decimal 按 PM 存储精度后相等 | 写后立即 +30 秒重读 | mismatch=`untrusted/blocking` | 成本/盈亏报告；不单独阻断 NAV |
| `PM-CASH-001` | `pm.securities_cash` | account | `accinfo.cash`、source currency、PM `CNY-CASH` | `cash` 明确存在；Decimal 金额一致 | 写后立即 +30 秒重读 | 缺失=`unavailable`；不一致=`untrusted` | cash_like、NAV |
| `PM-CASH-002` | `pm.securities_cash` | account | selected OpenD source field | source_field 必须为 `cash` | 无 | 使用 available_funds/withdraw_cash/power=`untrusted/blocking` | cash sync、NAV |
| `PM-MMF-001` | `pm.fund_mmf` | account | `accinfo.fund_assets`、PM `CNY-MMF` | 字段存在；0 与缺失区分；金额一致 | 写后立即 +30 秒重读 | 缺失=`unavailable`；不一致=`untrusted` | cash_like、NAV |
| `PM-CASHLIKE-001` | `pm.cash_like_assets` | account | securities_cash、fund_mmf dataset status | 两项均 trusted | 依赖恢复后重算状态 | 一项异常=`partial`；正式 NAV 仍阻断 | NAV、流动性报告 |
| `PM-SYNC-001` | `pm.futu_sync` | account + run | write_stage、positions result、cash/MMF result、exception | 所有阶段成功 | 无 | 明确失败/partial_write_possible=`untrusted/blocking` | 对应写入阶段的数据集 |
| `PM-SYNC-002` | `pm.futu_sync` | account + run | sync_run_id、source_snapshot_id、各阶段 receipt | positions/cash/MMF 关联同一 source snapshot | 无 | snapshot generation 不同=`untrusted/blocking` | NAV、正式报告 |
| `PM-SYNC-003` | `pm.futu_sync` | account + run | 写后 repository read 与原 snapshot | 所有目标字段一致 | 首次 transient；30 秒只读复查 | persistent=`untrusted/blocking` | 对应数据集消费者 |
| `PM-PRICE-001` | `pm.prices` | account + holding | quote source、quote time、fallback、missing list | 每个必要持仓有符合 PM policy 的新鲜价格 | PM 既有 price policy | missing/stale 超界=`unavailable/untrusted`；合法 fallback 可 partial | NAV、日报 |
| `PM-FX-001` | `pm.fx` | currency + fact time | FX source、fact time、evidence ID | 非 CNY 估值存在对应事实时间汇率 | 无当前汇率回填 | 缺失=`unavailable/blocking` | NAV、历史/业绩报告 |
| `PM-NAV-001` | `pm.nav` | account + nav date | holdings/cash/MMF/prices/FX status、valuation evidence、finality | 所有 required dataset trusted 且 finality=final | 无 | 上游异常或 finality 缺失=`untrusted/unavailable` | 正式 NAV、日报、业绩报告 |
| `PM-NAV-002` | `pm.nav_history` | account + date | duplicate audit、accuracy/receipt | 无重复；精度/证据检查通过 | 无 | 重复或证据冲突=`untrusted/blocking` | 历史 NAV、业绩报告 |

## 5. Hub、契约和告警检查

| ID | Owner | Scope | 检查 | 通过条件 | 异常结论 |
|---|---|---|---|---|---|
| `HUB-CON-001` | Hub | producer | JSON Schema 与 schema_version | 当前或前一兼容版本，完整校验通过 | `incompatible/unknown`；对应 producer unavailable |
| `HUB-PULL-001` | Hub | producer | HTTP、auth、observed_at、artifact freshness | 只读拉取成功，状态未过期 | 拉取失败/过期=`unknown/unavailable` |
| `HUB-DEP-001` | Hub | dataset + consumer | blocked_by、required dependency、usable_for | 只传播显式依赖 | 依赖缺失 fail closed；不得跨账户污染 |
| `HUB-INC-001` | Hub | incident fingerprint | state transition、occurrence、ack/recovery | new/persistent/acknowledged/recovered 合法迁移 | 非法迁移拒绝；不得人工恢复 |
| `HUB-ALT-001` | Hub | notification | fingerprint、notification_id、outbox | 状态变化只生成一次有效通知 | 持久重试；三次失败 delivery unhealthy |
| `HUB-MNT-001` | Hub | maintenance scope | window、actor、reason、start/end | 有界且范围明确 | 只抑制范围内重复通知，不改变状态/门禁 |
| `HUB-SEC-001` | Hub/producer | endpoint | bind address、token scope、log redaction | loopback/受控内网、独立只读 token、无敏感日志 | fail closed + security warning/blocking |
| `HUB-RET-001` | Hub/producer | artifacts | rotation、retention、permissions | 30/400 天策略执行，目录权限正确 | degraded；证据丢失影响审计时 unavailable |

## 6. 消费者门禁矩阵

| Consumer | Required datasets | `partial` | `untrusted/unavailable` |
|---|---|---|---|
| PM 内部展示 | 对应展示数据集 | 显示状态和 as_of | 允许显示旧值，但显著标记不可用/不可信 |
| PM 正式 NAV | holdings_quantity、securities_cash、fund_mmf、prices、fx、nav finality | 阻断 | 阻断 |
| PM 成本/盈亏报告 | holdings_quantity、cost_basis、prices、fx | 带警告仅用于内部草稿 | 正式发布阻断 |
| PM 日报/业绩报告 | 对应 NAV/估值数据集 | 可生成带警告草稿 | 正式发布阻断 |
| OM 普通候选扫描 | candidate/source 自身证据 | 按既有策略 | 不受无关持仓异常影响 |
| OM 持仓报告 | trade_intake、ledger_projection、option_positions | 带警告内部展示 | 正式发布阻断 |
| OM lifecycle/close advice | option_positions、lifecycle、相关市场证据 | 不生成确定性建议 | 阻断 |
| 组合级 PM/OM 报告 | 显式声明的 PM/OM dependencies | 按最严格依赖处理 | 阻断并输出 blocked_by |

## 7. 固定回归场景

以下场景必须进入确定性测试集：

1. OM 11 条 stale 生命周期状态；
2. OM 漏一条 broker 成交；
3. OM full replay 与 materialized projection 不一致；
4. OpenD snapshot/分页不完整；
5. PM acc_id 缺失、重复或环境错误；
6. PM positions 成功、cash/MMF 阶段失败；
7. `cash` 缺失但 `power` 存在，必须 unavailable 而非回退；
8. `fund_assets=0` 与字段缺失；
9. PM 写后首次不一致后恢复；
10. PM 写后 30 秒仍不一致；
11. listener/timer/service 停止；
12. OM 盘中 transient 在 5 分钟内收敛；
13. Hub Schema 不兼容、拉取超时、outbox 失败；
14. incident acknowledge 不改变门禁，重新验证后恢复。
