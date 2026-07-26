# Quality Hub 检查实现映射

- **状态**：代码与本地验证完成；生产 onboarding 待 Phase 5
- **日期**：2026-07-26
- **规范来源**：[check-matrix.md](check-matrix.md)

Hub 检查分为两类：`RT-*` 和 `HUB-DEP-001` 是持续状态检查，会出现在 V1 payload 或 incident；其余 `HUB-*` 是在契约读取、控制写入、通知或 retention 所有权边界强制执行的事务不变量，不为凑 ID 再造第二套运行状态。

## 持续运行检查

| ID | 实现入口 | 确定性证据 | 结论边界 |
|---|---|---|---|
| `RT-HUB-001` | `AggregateService.health/_runtime_checks`；`HubScheduler._refresh_watchdog` | API lifespan、persisted runtime、stale scheduler、Watchdog artifact tests | service/SQLite/poll scheduler/Watchdog 任一必要证据失败即 fail/degraded |
| `RT-HUB-002` | `AggregateService._runtime_checks`、`SQLiteRepository.outbox_health`、dispatcher loop | 1/5/15 分钟重试、三次失败、无 dispatcher 且有 pending alert 测试 | 重试中 warn；三次失败或无法投递 pending alert 时 fail |
| `RT-EXT-001` | `DeadmanClient`、`HubScheduler._heartbeat_deadman`、`AggregateService._runtime_checks` | exact safe payload、adapter state、失败不泄密测试 | 未 onboard 不伪造 pass；onboard 后失败为 blocking |

## 契约、拉取和依赖

| ID | 实现入口 | 确定性证据 | 输出 |
|---|---|---|---|
| `HUB-CON-001` | `contracts.validator.validate_quality_status` | Draft 2020-12、unknown version、enum、trusted evidence、vendor hash tests | 不兼容 producer 标为 `incompatible`，不猜测解析 |
| `HUB-PULL-001` | `ProducerClient.fetch/_result_from_payload`、`HubScheduler.run_once` | auth/timeout/HTTP/Schema/identity/stale/clock-skew/ETag/304 tests | poll state 和 pull-owned incident；旧 snapshot 只供诊断展示 |
| `HUB-DEP-001` | `DependencyService.evaluate` | 缺 required dataset、account/market scope、PM→OM 隔离 tests | `hub.consumer.*` dataset 中的真实 check ID、`blocked_by` 和 fail-closed status |

## Incident、通知和控制写入

| ID | 实现入口 | 确定性证据 | 强制语义 |
|---|---|---|---|
| `HUB-INC-001` | `IncidentService`、`SQLiteRepository.reconcile_incidents/acknowledge_incident` | fingerprint 去重、ack 保留、只经成功重验证 recovery、非法 ack tests | state machine 与 audit event；人工不能直接恢复 |
| `HUB-ALT-001` | `NotificationService`、`SQLiteRepository.notification_outbox`、`FeishuDispatcher` | stable notification ID、dedup、recovery supersede、重试和 restart tests | 状态变化一次有效通知；持久 outbox，不改变业务数据状态 |
| `HUB-MNT-001` | `MaintenanceService`、operator API | 空/无 service/无界 scope 拒绝，幂等与 audit，范围外不抑制 tests | 只抑制匹配范围通知，不改变 incident 或 gate |

## 安全和保留

| ID | 实现入口 | 确定性证据 | 强制语义 |
|---|---|---|---|
| `HUB-SEC-001` | `Settings.validate`、API auth/error envelope、producer/deadman clients | loopback、独立 token、read/operator 分权、safe error、deadman URL tests | 配置不完整或权限复用时启动 fail closed；公开输出不含 token/业务数据/内部路径 |
| `HUB-RET-001` | `SQLiteRepository.apply_retention`、systemd renderer | normal 30 天、blocking/control 400 天、active incident、unsent/failed outbox preservation tests | retention 只删满足期限且未受保护的历史控制记录 |

## 本地质量基线

- Hub 完整 pytest：`66 passed`；
- Ruff、compileall、`git diff --check`：通过；
- 使用 commit time `SOURCE_DATE_EPOCH=1785064563` 连续隔离构建两次，
  wheel SHA 一致；全新 venv install/import 与 packaged Schema：通过；
- Hub commit：`b7f2ca94735b5f5209eca1ab3f6d615d1f8826ab`；
- Hub version：`0.2.0`；
- candidate wheel SHA-256：`aac156209f8ad40434603be4a6a732e436d94af77e08f125d82bee86cde1ba35`。

真实 producer pull、Watchdog、飞书 incident/recovery、external missed-heartbeat、retention canary 和 rollback 属于 Phase 5，不能由本地测试替代。
