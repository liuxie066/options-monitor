# 回执发送面与消息风暴风险清单

- Work unit: `lifecycle-receipt-batching`
- Review base: `origin/main@51275d59`
- Evidence scope: current `src/application`, `src/interfaces`, `scripts`, tests and accepted Gateflow implementation
- Safety: 本清单为代码与测试审计；未发送真实通知，未读取或写入生产 ledger

## 结论

本次发现的高风险活动路径只有 lifecycle 数据核对回执：原实现按 source/account 每五秒逐行领取，一次核对产生 24 行时可以形成 24 次外部发送。该路径已改为同路由持久化批次，并删除可直接逐行发送的 legacy dispatcher。

其他回执要么已经在一次业务运行内聚合、要么是一项操作一个 durable outbox、要么只是内部证据对象；当前没有第二条“数据一行一条外部消息”的活动路径。普通 trade-intake sender 虽仍保留兼容实现，但当前 runtime 没有调用者；若将来重新启用，必须重新做批量与幂等评审。

## 风险矩阵

| 回执面 | 当前外部发送单位 | 批量/风暴风险 | 当前控制与结论 |
|---|---|---|---|
| Lifecycle reconciliation receipt | 同 provider/channel/target 的 durable delivery batch | 改造前高；改造后低 | 10 秒 quiet、最久 60 秒、同路由 60 秒一次、24 members 不拆包、稳定 batch key、unknown/accepted 冻结；进程级单 owner |
| Lifecycle CLI one-shot dispatch | 一次显式确认命令最多尝试一个 batch | 低 | applied 只允许全局语义，使用 canonical enabled-account allow-set；account-scoped applied 被拒绝 |
| Auto-close / expired-position maintenance receipt | 每账户、每次 maintenance run 一条汇总 | 中低、按账户有界 fan-out | 一个账户内所有 position decision 已汇总；receipt identity/state 防重复。多账户运行仍可能一账户一条，但不是逐 position 风暴 |
| Assistant upgrade final receipt | 每个 upgrade operation 一个 durable outbox | 低 | operation/outbox claim，Feishu UUID 或 WeChat idempotency key 稳定；内部重试承载同一逻辑回执 |
| Ordinary trade-intake receipt sender | 当前无 production caller | dormant；若重新接线则高 | `auto_intake` callback 只声明 `outbox_managed`，不会调用兼容 sender。重新启用逐 deal sender 必须另开 work unit |
| Settlement source receipt / migration receipt | SQLite/JSON 内部证据与审计对象 | 无外部消息风险 | 名称为 receipt，但没有 notification adapter/provider call |
| Scheduled notification `local_receipt_id` 等 transport receipt | 已发送通知的 transport evidence | 无新增发送风险 | 只是 delivery result 字段，不是另一条消息 |

## Lifecycle 改造后的硬边界

- 逐案件 outbox row 只证明通知意图，不再拥有 provider call。
- 唯一活动 sender 是 `LifecycleReceiptBatchDispatcher -> dispatch_notification_batch_once()`。
- `_run_listener_source_loop()` 不包含 lifecycle sender；legacy `dispatch_notifications_once()` 已删除且不再导出。
- provider I/O 发生在 SQLite transaction 与 `process_lock` 之外。
- 一个 receipt 在同一事务中结算 batch 与全部 members。
- `accepted`、异常/歧义、stale `send_started` 不自动重发。
- 单进程内只有一个 owner；生产升级前仍须以 service topology 证明只有一个 active process。

## 后续监控建议

- 观察 `lifecycle_delivery.dispatcher.last_result`、`batch_status_counts`、`oldest_unknown_batch`、`unbound_eligible_count` 与 `messages_avoided`。
- 若 auto-close 账户数显著增长，单独评估“跨账户一条 maintenance digest”；当前无需把不同业务回执强行并入通用消息总线。
- 任何重新引入 ordinary trade-intake sender 或新的 row-loop notification call site，都应以“外部调用次数是否随数据行数线性增长”为 Gateflow/DeepReview 必查项。
