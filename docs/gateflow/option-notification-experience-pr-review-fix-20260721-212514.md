# Gateflow PR Review Fix — Option Notification Experience

## Gate

- Work unit: option notification experience
- Gate: PR review fix
- Source review: `docs/reviews/pr-109-review-20260721-210448.md`
- Status: implemented, awaiting PR re-review
- Artifact path: `docs/gateflow/option-notification-experience-pr-review-fix-20260721-212514.md`

## Accepted Findings and Fixes

### PR109-01 — 同日后续半点新增候选静默漏报

- Decision: accepted
- Root cause: v2 state 每日只有一个 `candidate_delivery` 槽，已确认 envelope 遇到不同 delivery key 时仍被 `_resolve_prepared_envelope()` 保留。
- Fix: candidate delivery 分支仅在旧 envelope 为 `confirmed` 且新 delivery key 不同时滚动到新 envelope；pending exact retry、ambiguous freeze、fixed reports 均保持原合同。
- Regression coverage:
  - repository：NVDA 第一批 prepare/confirm，AMD 第二批 prepare/retry/confirm，最终 alerted 集合同时保留两者；
  - notification flow：两个连续半点真实走 prepare/send/confirm，第二条只包含 AMD。

### PR109-02 — 策略步骤异常被伪装为正常无候选

- Decision: accepted
- Root cause: Sell Put / Combo Yield 异常会清空 artifacts 并继续；Covered Call 异常会被 symbol processor 上层吞并。pipeline 仍可能返回 0，而 assembler 无法区分“真实空结果”和“异常后空结果”。
- Fix:
  - 新增极窄 run-scoped `strategy_scan_failures.jsonl`，由 strategy orchestration owner 对 Sell Put、Covered Call、Combo Yield exception 写入结构化 family/symbol/error evidence；
  - 三个 family 均 fail closed 清理本轮候选 artifacts，避免 stale candidate 泄漏；
  - assembler 读取 failure artifact，直接丢弃对应 family 的全部候选行、标为 unavailable，并写 `strategy_step_failed` data gap；
  - 若存在 strategy failure 且没有任何可靠候选行，brief 进入 blocked，固定点发送 failure report，半点保持静默；
  - 若其他 family 仍有可靠候选，保留候选并标记 degraded，消息明确提示“某策略扫描异常，本轮结果不完整”；
  - 不将 strategy execution failure 混入 `candidate_filter_trace`，避免改变 reject-log fallback 与候选拒绝统计语义。
- Regression coverage:
  - 三个 family 的 exception 均写 failure artifact 并清理 stale output；
  - failure + 全空候选阻塞，不再输出正常无候选；
  - Sell Put failure + Covered Call 可用时保留候选、degraded 并显示用户提示；
  - controlled empty 且无 failure artifact 仍是正常 authoritative empty。

## Changed Files

- `src/application/daily_decision_brief_repository.py`
- `src/application/strategy_scan_failures.py`
- `src/application/symbol_monitoring.py`
- `src/application/sell_call_steps.py`
- `src/application/pipeline_symbol.py`
- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_repository_v2.py`
- `tests/test_daily_decision_brief_notification_flow.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_symbol_monitoring_fetch_spec_merge.py`
- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`

## Validation

- Daily Brief + candidate trace + symbol monitoring focused suite: `197 passed`
- Scheduler / multi-tick focused suite: `100 passed`
- Full repository: `2952 passed, 10 skipped`
- `python3.12 -m ruff check .`: pass
- `python3.12 -m compileall -q src domain scripts`: pass
- Dependency graph regenerated/check: `478 production modules`, `0 production cycles`, boundary pass
- US example YAML config validation: `ok=true`
- HK example YAML config validation: `ok=true`
- `git diff --check`: pass

## Docs Decision

- Public command/config/notification schedule contracts are unchanged.
- Generated dependency artifacts were refreshed for the new internal failure-artifact module.
- The new run-scoped artifact and semantics are documented in this durable Gateflow fix artifact; no operator command or production rollout documentation changes are required in this gate.

## Residual Risks

- GitHub PR metadata/checks have not yet been refreshed in this local gate because the initial GitHub API read was blocked by local credential/network access; classification: fixed in current slice (retry before final push/draft-PR-pass).
- Production enablement, v1 pointer migration, real send, release, remote upgrade, and normal-schedule observation remain outside this work unit execution authorization; classification: assigned to later work unit with explicit production approval.
- JSONL append relies on the existing per-line append pattern used by run artifacts; no cross-process transactional index is added because each symbol writes one independent line and the current pipeline uses one report directory per run/account; classification: fixed in current slice by deterministic reader/filtering and full regression coverage.

## Completion Status

- PR review fix: complete
- Next entry point: PR re-review
