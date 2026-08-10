# Gateflow Plan Review Fix — HK Combo Capture / Failure Notification

- Gate: `plan review fix`
- Work unit: `hk-combo-capture-failure-notification`
- Review artifact: `docs/reviews/plan-review-20260810-111309.md`
- Status: fixed; pending adversarial re-review

## Finding decisions

| Finding | Decision | Plan change |
|---|---|---|
| PR-01 | accepted | Missing/empty pair variant is now the explicit legacy `sp_lc` compatibility case; unknown explicit variants still fail closed, and the real pair shape is a required test. |
| PR-02 | accepted | The plan now requires CC+LP summary-to-capture mapping so `not_applicable` and its reason survive the producer boundary. |
| PR-03 | accepted | Quote binding remains a per-symbol, cross-owner frozen-fact invariant before owner partitioning. |
| PR-04 | accepted | Removed receipt-path threading and the proposed `AccountRunRequest` field. One source-owner helper now owns locate/publish/validate/reuse for normal and recovery paths; recovery may publish only from the immutable current-run prepared context. |
| PR-05 | accepted | Daily Brief service and notification preparation tests are mandatory and must consume the early-owner artifact, not a manually assembled authority object. |

## Preserved boundaries

- No snapshot, receipt, CLI, config, notification, or scheduler schema change.
- No relaxation of current-run identity, freshness, or fixed-failure authority checks.
- No OpenD retry/timeout, scheduler processed-target, release, deployment, or production mutation work.
- No edit to the existing dirty `docs/DEPENDENCY_GRAPH.md`; the existing dependency direction remains unchanged.

## Re-review evidence requested

The re-review must confirm that:

1. legacy SP+LC pairs cannot be silently dropped;
2. CC+LP `not_applicable` is observable before the reducer;
3. cross-owner quote identity remains fail closed;
4. recovery has one owner-local idempotent receipt contract without request-layer leakage;
5. validation proves actual `fixed_failure` preparation in no-send mode.
