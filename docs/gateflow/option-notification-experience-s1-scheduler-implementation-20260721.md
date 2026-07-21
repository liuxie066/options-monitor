# Gateflow Slice 1 Implementation — Scheduler occurrence identity

- Gate：implementation slice 1
- Work unit：期权监控通知体验升级
- Base commit：`bb24f87b`
- Scope：scheduler targets、processed-target watermark、scheduler DTO round-trip、account outcome target map、runtime status

## Changed files

- `src/application/scan_scheduler.py`
- `domain/domain/tool_boundary.py`
- `domain/domain/engine/decision_engine.py`
- `src/application/tick_account_execution.py`
- `src/application/tick_scheduler_context.py`
- `src/application/agent_tools/operations_impl.py`
- `tests/test_scan_scheduler_notify_semantics.py`
- `tests/test_multi_tick_scheduler_application.py`
- `tests/test_multi_account_tick.py`
- `tests/test_domain_engine_batch2.py`
- `tests/test_agent_plugin_smoke.py`

## Implementation decisions

1. 现有 report targets 保持 `09:40 + 有效整点 + 15:50`；新增有效 `HH:30` candidate-check targets，明确排除 `09:30`，并复用 run window、breaks 和 gates。
2. `scan_targets = report_targets ∪ candidate_check_targets`；`is_notify_window_open` 只在未处理的 report target 为真。
3. 新增 `scheduled_scan_target_market`，保留 `scheduled_target_market` 表示 fixed report target；force/manual 两者均为空。
4. scheduler v1 normalization 显式输出 optional `scheduled_scan_target_market=None`，不升级 schema version；`SchedulerDecisionView` 保留 scan/fixed target。
5. 新增 `last_processed_scan_target_utc_by_account` 作为去重权威；旧 state 完全缺少该字段时才 fallback 到 legacy completion watermark。
6. writer 同时记录实际 completion time 和 exact processed target，并拒绝 target watermark 倒退。
7. account execution 不再提前写 scheduler state，也不吞写失败；只返回 `scheduled_scan_targets_by_account`，等待 Slice 4 在 durable outcome 后提交。
8. runtime `scheduler_status` additive 展示账户 processed-target watermark。

## State transitions and invariants

```text
legacy state without processed map -> use last_run as one-time compatibility seed
new state with processed map       -> only exact scheduled target controls dedupe
account pipeline outcome           -> return account -> target, no state write
future notification flow           -> durable outcome -> commit target -> provider send
```

- `last_run_utc_by_account` 仅表示实际完成时间。
- processed target 单调不减且账户隔离。
- `09:40` 晚至 `10:01` 完成时，processed target 仍是 `09:40`，因此 `10:00` 可 catch up。
- `15:30` 晚完成不吞 `15:50`。
- no-scan、force/manual 不制造 scheduled scan target。

## Validation

- Focused scheduler/domain/account/runtime tests：`144 passed in 1.05s`。
- Broader tick/scheduler/agent contract tests：`203 passed in 2.25s`。
- `python3.12 -m compileall -q domain/domain src/application`：pass。
- `python3.12 -m ruff check <changed files>`：All checks passed。
- `git diff --check`：pass。
- Domain import-boundary grep：无 `domain/domain -> src/scripts` 违规。

## Docs decision

Public tool output only gains an additive scheduler status field；用户文档在 Slice 5 统一更新，不在本 slice 重复描述内部状态。

## Residual risks and uncovered areas

- processed target 目前只由 account outcome 返回，尚未提交：covered by later approved Slice 4；在该 slice 前不会发布/部署中间版本。
- force/manual 仍保留旧 `is_notify_window_open=True` compatibility flag：covered by Slice 4 notification policy；scheduled targets 均为 `None`，不会被误记为 fixed batch。
- legacy seed 切换时可能保守重复/跳过最近 target：assigned to production rollout observation。

## Completion status

- Slice 1 implementation：pass
- Unclassified residual risks：0
- Current gate / next entry point：Slice 1 code review
