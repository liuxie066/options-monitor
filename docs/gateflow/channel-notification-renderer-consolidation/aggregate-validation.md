# Gateflow Aggregate Validation — Channel Notification Renderer Consolidation

## Gate

- Work unit: `channel-notification-renderer-consolidation`
- Gate: `aggregate deepreview`
- Branch: `plan/channel-notification-renderer-consolidation`
- Base: `cd3d6c3d38d7250017d152822d664950d42a578b`
- Reviewed accepted slices: Slice 1 through Slice 4
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/aggregate-validation.md`

## Scope

The aggregate validation covers the Phase A local implementation accepted in Slices 1-4:

- Daily Brief as the only scheduled ordinary-notification renderer;
- manual/force ordinary notification no-send boundary;
- trigger-scoped idempotency and terminal multi-market failure;
- Compact Tick compatibility-only public artifacts and deprecated aliases;
- System Notice shell shared by OpenD and delivery failures;
- Receipt shell shared by trade and maintenance receipts;
- unchanged P0 Feishu Post / single-md-node / 28 KiB preflight and WeChat Markdown identity contracts.

It does not authorize production config mutation, real provider delivery, release/deploy, or the hard-paused Phase C/Slice 6 cleanup.

## Validation Evidence

### Planned aggregate matrices

- Daily Brief, config, trigger, idempotency, CLI: `154 passed`.
- System Notice and Receipt shells: `56 passed`.
- Compact/Legacy compatibility and public Agent tools: `148 passed`; `5` expected `DeprecationWarning`s.
- Broad multi-tick and transport regressions: `96 passed`; `4` expected `DeprecationWarning`s.

### Configuration and static checks

- US/HK example YAML validation: passed.
- US/HK example YAML build with `--dry-run`: passed.
- Planned Ruff matrix: passed.
- `python3.12 -m compileall -q domain src tests`: passed.
- `git diff --check`: passed.

### Full-suite diagnostic run

A full suite with the known baseline close-advice assertion deselected produced:

```text
2910 passed, 10 skipped, 1 deselected, 21 failed
```

Failure classification:

- `18` failures are independent-worktree environment failures: those tests invoke the repository-local `.venv/bin/python`, which is absent in this worktree. They do not identify an implementation regression, but the aggregate fix gate must provide a temporary ignored link to the existing environment and rerun them.
- `3` failures are real deterministic quality-gate failures and are accepted aggregate findings below.

### Known baseline debt

`tests/test_close_advice_runner.py::test_close_advice_text_can_drive_account_message_without_opening_candidates` remains deselected. Its expected legacy header already disagreed with base `cd3d6c3d`; the current Daily Brief output is intentional. The accepted plan assigns this Legacy bridge assertion to the hard-paused Phase C/Slice 6 and forbids an opportunistic aggregate fix.

## Accepted Aggregate Findings

1. Generated dependency graph artifacts are stale. `scripts/generate_dependency_graph.py --check` reports both `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` as stale.
2. `tests/test_domain_engine_batch4.py` still requires removed Compact/Legacy candidate preparation and ranking entrypoints from `tick_notification_flow.py`, contradicting the accepted Daily Brief authority chain.

Focused reproduction:

```text
3 failed
```

The failures are:

- `tests/test_dependency_graph_generator.py::test_dependency_graph_generator_check_passes`
- `tests/test_domain_engine_batch4.py::test_main_uses_notify_dispatch_gate_entrypoint_batch4`
- `tests/test_domain_engine_batch4.py::test_main_orchestrator_guard_batch4_no_legacy_rule_reflow`

## Residual Risks / Classification

| Risk | Classification / owner |
|---|---|
| Repository-local `.venv` is absent in the independent worktree | fixed in current aggregate fix loop by an ignored temporary link and full-suite rerun |
| Legacy close-advice assertion remains stale | covered by later approved but hard-paused Phase C/Slice 6; no current implementation change |
| Real Feishu/WeChat rendering and provider acceptance are not exercised | assigned to the separately authorized compatibility release/canary gate |
| Phase C physical Legacy removal and strict old-key rejection are not implemented | requires explicit CEO decision after compatibility-release evidence |

No residual risk is unclassified.

## Completion Status / Next Entry Point

- Aggregate validation: complete with two accepted medium findings.
- Current gate: `aggregate deepreview`.
- Next entry point after review artifact: `fix`.

## Fix-Gate Validation Addendum

After the two accepted findings were fixed:

- direct finding reproduction: `3 passed`;
- dependency graph and complete Batch-4 architecture tests: `8 passed`;
- full repository with only the accepted baseline Legacy assertion deselected: `2931 passed, 10 skipped, 1 deselected`;
- four planned matrices remained `154`, `56`, `148`, and `96` passed;
- US/HK validate/build dry-run, Ruff, compileall, dependency graph `--check`, and `git diff --check`: passed.

The previous 18 environment failures were eliminated by an untracked temporary virtual-environment symlink; they are no longer residual uncertainty.
