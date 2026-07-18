# Gateflow Implementation Artifact — Option Performance Refactor S10

- **Gate**: implementation
- **Work unit**: `option-performance-refactor`
- **Slice**: S10 — Shadow Reconciliation, Cutover, Docs and Legacy Isolation
- **Created at**: 2026-07-18 01:08:41 UTC
- **Status**: implementation-complete; awaiting code review
- **Artifact path**: `docs/gateflow/option-performance-refactor-S10-implementation-20260718-010841.md`

## Objective and Completion Signal

Implemented the approved final migration slice: old/new reconciliation distinguishes exact equality from expected semantic deltas, historical replay and missing-evidence behavior have explicit gates, legacy source consumers are held to an exact path allowlist, public docs make v1 primary, and rollback/removal boundaries are executable without data migration rollback.

## Scope and Decisions

### Pure reconciliation layer

Added `src/application/performance/reconciliation.py` as a pure validation module, not a second report pipeline.

- `reconcile_legacy_monthly_report` compares:
  - legacy `premium_received_gross` to v1 `activity.premium_collected_gross` by native currency;
  - legacy option-only trade cash (`net_cashflow_gross - assignment_stock_net_cashflow_gross`) to v1 `cash.option_trade_cash_gross`;
  - option realized gross by close-event/allocation identity, excluding assigned-stock sale PnL from the option equality check;
  - opened/closed contract quantities separately from money.
- Expected deltas are explicit classifications, never arbitrary tolerance:
  - actual fee or incomplete fee coverage;
  - effective-time v1 FX versus legacy static FX;
  - opening/end valuation plus assigned-stock lifecycle versus legacy option realized;
  - Asia/Shanghai versus legacy UTC month attribution;
  - intentional removal of generic legacy return rates.
- `assess_replay_determinism` compares canonical JSON plus SHA-256 hashes.
- `assess_report_coverage` rejects missing evidence encoded as zero, missing FX encoded as CNY, invalid partial/not-observed envelopes, and gross/net evidence erasure.

### Proven-scope zero semantics

The accepted plan required:

```text
no events in a proven scope => observed zero
scope cannot be proven => not_observed
```

Implemented this at the service/public-config boundary:

- `build_option_period_performance(..., scope_proven=True)` recursively converts additive amount envelopes from `not_observed` to observed zero while leaving partial metrics untouched.
- The public Agent/CLI path proves scope only from configured accounts. A requested configured account, or the configured aggregate scope, is proven; an arbitrary unconfigured account remains unproven.
- Tests cover configured aggregate/account scopes, unconfigured accounts, and direct empty-ledger proven/unproven reports.

### Legacy consumer zero-check

Added an exact allowlist for every Python source path under `src/` containing `monthly_income_report`, `net_income_cny`, or `realized_return_rate`.

Allowed ownership classes:

1. deprecated adapter/rollback boundaries;
2. deprecated compatibility projections;
3. candidate/strategy-domain naming outside historical performance reporting.

The scanner fails on both unowned new paths and stale allowlist entries. The scanner implementation file itself is excluded from the consumer inventory.

### Legacy adapter dependency-cycle finalization

Full-suite validation exposed a stale production module cycle:

```text
src.application.ledger.queries <-> src.application.ledger.read_model
```

The cycle came from the deprecated monthly adapter importing `assigned_stock_event_log` back from `queries`. Finalized the adapter boundary so `queries.position_monthly_income_report` loads assigned-stock events through the legal API owner and injects them into the read-model builder. The read model no longer imports queries or probes repository capability. Generated dependency graph artifacts were refreshed and now report zero production module cycles.

### Stale smoke-test dependency

Full public-tool smoke coverage had a stale helper that monkeypatched `analysis.get_exchange_rates`, removed during S8. The helper now patches only the still-owning positions tool. The legacy monthly tool smoke test passes without weakening production behavior.

## Proven-Dependency Scope Admissions

The approved S10 file list allowed additional files only when a direct dependency was proven. The following additions were required and are included in this artifact before the accepted commit:

| File | Evidence / reason |
|---|---|
| `src/application/agent_tools/materialization_impl.py` | runtime config is the owner that can prove configured account scope before calling the service |
| `tests/test_option_performance_agent_tool.py` | validates configured aggregate/account scope and unconfigured account non-proof |
| `tests/test_agent_plugin_smoke.py` | removes stale S8 monkeypatch target discovered by S9/S10 integration validation |
| `src/application/ledger/queries.py` | legal public owner must inject assigned-stock events into deprecated read-model adapter |
| `src/application/ledger/read_model.py` | removes reverse import and accepts injected assigned-stock events |
| `docs/OPTION_PERFORMANCE_DESIGN.md` | authoritative design status was stale at S6 |
| `docs/DEPENDENCY_GRAPH.md`, `docs/dependency_graph.mmd` | generated architecture artifacts became stale after the new module and cycle fix |

No production config, notification, Feishu, broker-facing, or runtime-state write path was changed.

## Changed Files

Production/application:

- `src/application/performance/reconciliation.py` (new)
- `src/application/performance/service.py`
- `src/application/agent_tools/materialization_impl.py`
- `src/application/ledger/queries.py`
- `src/application/ledger/read_model.py`

Tests:

- `tests/test_performance_reconciliation.py` (new)
- `tests/test_option_performance_agent_tool.py`
- `tests/test_agent_plugin_smoke.py`

Docs/generated architecture:

- `docs/migrations/OPTION_PERFORMANCE_V1_MIGRATION.md`
- `docs/AGENT_WIKI.md`
- `README.md`
- `docs/OPTION_PERFORMANCE_DESIGN.md`
- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`

## Validation

### S1-S10 focused merge suite

```text
766 passed, 10 skipped
```

This suite covered all approved slice validation files through S10, including ledger economics, valuation/FX evidence, assigned stock, capital, public Agent/CLI, analysis/Assistant/Copilot, both portfolio bridges, reconciliation, positions reporting, smoke and research.

After the dependency-cycle fix and final account-scope regression, the directly touched focused tests also passed:

```text
18 passed  # reconciliation + option performance Agent tool
38 passed  # positions reporting + assigned-stock queries (dependency-graph check excluded until regeneration)
```

Dependency graph:

```text
[OK] dependency graph generated; production_modules=465 cycles=0
[OK] dependency graph current; production_modules=465 cycles=0
```

Repository Ruff:

```text
python3 -m ruff check .
All checks passed!
```

Full pytest was executed once without exclusions:

```text
2588 passed, 10 skipped, 20 failed
```

Failure classification:

- 18 failures are environment-only: four CLI/entrypoint test files invoke missing `.venv/bin/python`; this worktree has no `.venv`.
- 1 failure was the dependency graph cycle/staleness caused by the accepted S5 adapter edge plus the new module; fixed in this slice and the generated check now passes.
- 1 failure is in the unrelated dirty Feishu ACK work: `tests/test_inbound_feishu_ws.py::test_feishu_ws_delegates_to_inbound_and_replies` expects the deprecated monthly tool while that parallel test/code change now routes to `option_performance_report`. S10 did not modify or stage that file.

A repository-wide rerun excluding only the four missing-`.venv` test files and the one unrelated dirty Feishu assertion passed:

```text
2575 passed, 10 skipped, 1 deselected
```

Legacy reference scanner:

```text
status=pass
unowned=[]
stale_allowlist=[]
25 exact allowed paths
```

`git diff --check` passed for all S10-owned tracked paths; untracked new Python/test files pass `py_compile`, Ruff, and pytest and will be rechecked after explicit staging.

## Docs Decision

Updated all required public/migration documentation:

- README now advertises MTD/YTD/month/year primary CLI and Agent examples and labels legacy commands deprecated.
- Agent Wiki documents activity/cash/PnL namespace selection, proven-zero semantics, primary bridges, and rollback surfaces.
- Migration doc contains exact versus expected-delta matrix, exact consumer allowlist, replay/coverage gates, rollback steps, and a later versioned removal gate.
- Authoritative design slice status now reflects S7-S10 implementation.
- Generated dependency graph is current.

No `CHANGELOG.md` or `VERSION` change belongs to S10; both have unrelated pre-existing workspace edits and remain excluded.

## Residual Risks and Uncovered Areas

| Risk / area | Classification |
|---|---|
| Legacy UTC vs v1 Asia/Shanghai boundary samples require source-fact inspection to explain exact native mismatches | documented expected-delta classification; aggregate deepreview must confirm the fail-closed policy is sufficient |
| Full pytest cannot be literally green without a worktree `.venv` | environment issue; all non-environment tests passed in the adjusted repository-wide run |
| One Feishu WS test fails in unrelated dirty work because its expected tool name is stale relative to its parallel code | owned by the separate Feishu ACK work unit; S10 files do not modify/stage it |
| Legacy adapter physical removal and version bump | explicitly assigned to a later versioned work unit after the documented removal gate |

No unclassified residual risk remains.

## Completion Status

- **Implementation**: pass
- **Blocking open questions**: none
- **Current gate / next entry point**: S10 code review using `deepreview`
