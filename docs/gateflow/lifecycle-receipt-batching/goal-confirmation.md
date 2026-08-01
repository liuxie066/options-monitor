# Gateflow Goal Confirmation — lifecycle receipt batching

- **Recorded at**: 2026-08-01T21:12:55Z
- **Work unit**: `lifecycle-receipt-batching`
- **Branch**: `codex/lifecycle-receipt-batching`
- **Base**: `origin/main@51275d59` (`v1.8.5`)
- **User confirmation**: 已确认按 Gateflow 实现并自动推进后续 gates。

## Goal

保留每个 lifecycle case/transition 一行不可变通知意图作为事实与审计真源，但把真实外部投递从“逐行发送”改成“同一路由持久化批次发送”，避免数据核对一次产生多条回执时形成消息风暴。

## Confirmed delivery contract

1. 相同 provider/channel/target 的 `lx`、`sy` lifecycle 回执允许进入同一批次。
2. 聚合静默窗口为 10 秒，最久等待 60 秒。
3. 同一路由最多每 60 秒开始一次真实发送；显式失败重试也计入该预算。
4. 单成员批次保持现有单条回执格式。
5. 多成员批次最多展示 12 个代表项，剩余只显示数量；一个批次永不因展示上限自动拆包。
6. 一个 provider receipt 必须在一个 SQLite 事务内结算批次及全部成员。
7. 显式 pre-acceptance failure 最多尝试 3 次，并复用稳定 transport idempotency key。
8. `accepted`、ambiguous/exception、或 stale `send_started` 批次必须冻结，不得自动重试。
9. 旧 `suppressed`、`confirmed` 行不得因迁移或 dispatcher 切换重新激活。

## Success signals

- 用历史风暴形态的 24 行 fixture（`lx=15`、`sy=9`、同一路由）验证只调用一次 sender。
- 成功回执后，批次和全部 24 个逐案意图原子变为 `confirmed`。
- 多成员内容确定性展示最多 12 项并报告剩余数量，但 24 个成员全部被结算。
- 失败重试始终使用同一批次 ID/transport key，第三次显式失败后停止。
- 模糊结果与 stale send-started 全批冻结；manual reconcile/resend 明确按批次操作。
- 现有 suppressed/confirmed migration fixture 保持原状态且没有 batch binding。
- focused tests、相关自动 intake/CLI tests、full pytest、Ruff、dependency graph check 和 `git diff --check` 全部通过。

## Scope

- SQLite lifecycle notification outbox schema/repository。
- lifecycle batch planner、claim/send/complete/recovery/manual reconcile 状态机。
- lifecycle batch renderer、route fingerprint 和 transport idempotency key。
- auto-intake 进程级全局 dispatcher ownership。
- lifecycle receipt CLI/diagnostics、测试与相应文档。

## Non-goals and safety boundary

- 不改变 lifecycle 事实判断、allocation、close reason 或 migration decision。
- 不改 Daily Brief、自动平仓、交易 intake 普通回执或 assistant reply 的投递模型。
- 不建设通用消息总线、分布式锁或跨主机调度系统。
- 不修改生产配置，不发送真实通知，不写生产 ledger，不发布 Release，不升级或部署远端。
- Draft PR 前的提交和推送仅按 Gateflow gate 执行；不 merge、不 approve、不 mark ready、不请求 reviewer。

## First-principles boundary

逐案 outbox row 证明“哪一个 case transition 需要通知”；delivery batch 证明“哪一次外部调用承载了哪些逐案意图”。外部 provider receipt 属于后者，不能继续由每个逐案 row 各自声称一次发送。

## Blocking questions

无。若实现发现当前 runtime 存在同一进程之外的并发 dispatcher，或通知目标实际按账户动态变化且无法在创建批次前可靠解析，则停止 implementation 并重新进入 plan review。
