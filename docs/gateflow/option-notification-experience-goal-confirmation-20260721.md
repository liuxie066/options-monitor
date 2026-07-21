# Gateflow Goal Confirmation — 期权监控通知体验升级

- Gate：goal confirmation / accepted plan
- 日期：2026-07-21
- Work unit：期权监控固定计划点报告、新增候选通知、最近成功快照查询与资金展示升级
- 产品真源：`docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md`
- 实施计划：`docs/plans/option-notification-experience-implementation-plan-20260721.md`
- 最终 plan review：`docs/reviews/plan-review-20260721-181258.md`
- 用户确认：2026-07-21 确认使用 Gateflow 完成升级，并确认创建独立工作分支继续

## 目标与动机

在保留一套 canonical 策略扫描和现有 10 分钟唤醒的前提下，恢复可预期的固定计划点完整报告；在有效半点发现未送达的新普通候选时立即发送完整候选通知；用户可随时读取最近一次成功扫描快照。修复 pipeline failure 覆盖 successful current 并被误报为正常无候选的问题。

## 成功信号

- `09:40`、有效整点和 `15:50` 成功扫描后发送完整报告，无候选也发送；`09:30` 不扫描。
- 有效半点成功扫描发现新候选时立即发送；固定点与新增候选同时成立时只发送完整报告。
- 非目标点不运行 pipeline，但允许精确恢复已持久化、未确认的 delivery envelope。
- pipeline failure 不覆盖最近成功快照，也不渲染为“正常无候选”。
- 消息展示现金总额、可用于期权开仓资金和容量，不展示总资产。
- 查询只读最近成功快照；用户消息不泄漏 revision、digest、pointer 或内部枚举。

## 锁定边界

- 候选身份：`账户 + 市场 + canonical 标的 + 策略族`。
- 默认“期权监控”查询聚合全部启用账户和市场。
- force/manual 只更新 successful current，不伪装 scheduled target，不主动推进普通 fixed/candidate delivery 状态。
- 不新增第二 scanner、timer、sender、database、ranking authority 或 broker fetch 路径。

## 直接代码证据

- `multi_account_tick.py` 的 no-scan 分支在 notification flow 前返回，阻断 delivery-only 恢复。
- `tick_account_execution.py` 在 pipeline 后立即 mark scheduler，且异常被吞掉。
- `scan_scheduler.py` 使用实际完成时间作为 target 去重水位，可能吞掉后续计划点。
- `tool_boundary.py` 与 `SchedulerDecisionView` 尚未传递 `scheduled_scan_target_market`。
- 当前 notification flow 在 `should_notify is False` 时跳过账户，无法形成统一成功快照和固定计划点发送。

## 非目标与不过度设计说明

本 work unit 不修改候选过滤、排名、Close Advice、账本、自动交易或 broker 权威；不引入独立 outbox 服务。最小安全方案是在现有 scheduler state、Daily Brief repository、notification flow、renderer 和 agent/CLI 读取面上做窄扩展。

## 执行与生产审批边界

本地代码、测试、review、draft PR 可按 Gateflow 自动推进。生产配置修改、v1 -> v2 生产状态迁移、真实发送 canary、远端服务升级分别需要明确审批；远端容量证据必须在 production rollout 前完成。

## Residual risks

- legacy scheduler 首次切换的 seed 可能保守跳过或重复最近一次 target：归类为 production rollout observation。
- `15:50` pipeline 太晚完成可能错过 `16:00` recovery slot：归类为 rollout capacity gate。
- revision 增长与 retention：归类为后续 work unit，只有实际容量指标超预算时启动。
- provider 窗口结束仍未确认的消息会过期而不跨日补发：归类为已接受产品风险，由审计暴露。

## Completion status

- Goal confirmation：pass
- Plan review：pass-with-risks；无未解决 material finding
- Current gate / next entry point：accepted plan commit
