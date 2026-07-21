# Candidate Event Risk Monitoring — Slice C Implementation

- **Work unit**: `candidate-event-risk-monitoring`
- **Slice**: C — Material diff and user rendering
- **Gate**: implementation
- **Date**: 2026-07-21
- **Base commit**: `4576a1c1`
- **Artifact path**: `docs/gateflow/candidate-event-risk-monitoring-slice-c-implementation-20260721.md`
- **Status**: implementation complete; ready for code review

## Scope

Implemented the approved event materiality and Chinese user projection through the existing Daily Decision Brief diff/renderer/repository/notification lifecycle. No sender, scheduler, pointer, state machine, receipt, config key, or CLI was added.

## Changed files

- `domain/domain/daily_decision_event_risk.py`
- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_event_risk.py`
- `tests/test_daily_decision_brief_domain.py`
- `tests/test_daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_scenarios.py`
- `tests/test_daily_decision_brief_agent_tool.py`

## Decisions

- Added the six approved candidate event material changes: added, date changed, entered expiry window, evidence degraded, evidence recovered, and same-chain removal.
- Event transitions apply only to stable opening actions where current or previous state is active P0/P1.
- Old v1.3.5 briefs without `event_risk` do not fabricate an evidence-recovered notification during rollout.
- Freshness/cache metadata is excluded from transition comparison.
- Unanchored date correction is accepted only for one future occurrence per series on both sides; multiple unanchored dates or mixed anchored/unanchored evidence is conflict/unknown.
- An occurrence that elapsed before the current market trading date is never treated as a date correction; the next recurrence is a new event.
- Removal requires the same non-empty evidence chain and is detected even when a later previously known event remains.
- Every rendered candidate receives a plain-Chinese event line; internal provider/status/reason enums are not rendered.
- Event change summaries name the concrete candidate contract and the current expiration relation; the renderer still sends the full current snapshot.
- The existing agent-tool test expectation was corrected because `2026-07-20T20:00:00+00:00` is expired on July 21, 2026, so effective actionability is `planning_only`.

## Validation

```text
./.venv/bin/python -m pytest tests/test_daily_decision_brief_*.py tests/test_daily_decision_event_risk.py -q
138 passed

./.venv/bin/python -m ruff check <all Slice C touched Python files>
All checks passed

git diff --check
passed
```

Manual projection check produced:

```text
较上一轮：NVDA 08-21 $160 Put 财报日期调整至 8 月 5 日，现在早于当前 Put 到期日。
...
预计 8 月 5 日发布财报，早于当前 Put 到期日；执行前需要重新确认事件窗口和报价。
```

## Uncovered areas

- Release docs/version/dependency graph and full repository validation remain for approved Slice D.
- Live notification delivery was not invoked; existing no-send unit/integration lifecycle tests cover pointer reuse.

## Residual risks

- Provider event semantics are limited to the evidence coverage supplied by Slice A; classification: **covered by current design contract**, no new action.
- Full-suite/release checks are pending; classification: **covered by later approved Slice D/final validation**.

## Completion status

Slice C implementation is complete and ready for the required Deepreview code-review gate.
