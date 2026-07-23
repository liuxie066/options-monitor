# Gateflow Aggregate Validation — Close Evidence Collection

- Gate: aggregate validation
- Work unit: `close-evidence-collection`
- Reviewed commits: `ad5912bd`, `3fe0f3cf`, `a08e2012`
- Artifact path: `docs/gateflow/close-evidence-collection/aggregate-validation.md`
- Completion status: validation and aggregate deepreview complete; ready for aggregate commit

## Aggregate scope

- Close run discovery and strict dataset capture orchestration.
- CLI public contract and result/safety semantics.
- systemd/launchd recorder wiring and service profile observability.
- Operator documentation and generated dependency graph.
- No strategy policy, notification, authored production config, trade, Feishu or broker-facing change.

## Validation evidence

Initial full suite found only the expected stale generated dependency graph after new test imports:

```text
1 failed, 3048 passed, 10 skipped, 6 warnings
failure: docs/DEPENDENCY_GRAPH.md stale
```

The repository generator refreshed `docs/DEPENDENCY_GRAPH.md`; production import edges remained unchanged and architecture guards stayed clean:

```text
[OK] dependency graph generated; production_modules=481 cycles=0
[OK] dependency graph current; production_modules=481 cycles=0
```

Release metadata remains valid for the current unreleased branch base:

```text
[OK] release metadata valid for 1.4.12
```

Final full Python 3.12 suite:

```text
3049 passed, 10 skipped, 6 warnings in 61.03s
```

Warnings are pre-existing Legacy Tick renderer deprecations outside this work unit.

## Docs decision

- Public flag and automatic recorder behavior are documented in the existing Tool Reference, Shadow Replay runbook and Linux/Mac deployment guide.
- Generated dependency graph is refreshed because the repository guard requires it.

## Residual risks and uncovered areas

- Real production artifact cadence/shape: `covered by later approved rollout` canary after merge/release authorization.
- 6h sampling completeness: `assigned to later work unit` S5 readiness evaluation.
- Active-run partial-write/collision recovery: `requiring new issue or explicit user decision` only if canary/runtime evidence shows material recurrence.
- Strategy promotion: `requiring explicit user decision`; explicitly outside this work unit.

## Stop condition

All local aggregate quality gates and deepreview pass; no accepted unresolved finding remains.
