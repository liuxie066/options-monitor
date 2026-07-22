# Gateflow Implementation Artifact — S2 Lifecycle Routing

- **Gate**: implementation
- **Work unit**: `close-advice-bug-boundaries`
- **Slice**: S2 — lifecycle routing and single business date
- **Baseline**: accepted S1 commit `66f1296f`
- **Artifact path**: `docs/gateflow/close-advice-bug-boundaries/s2-implementation.md`
- **Status**: implementation and code-review loop complete; ready for accepted-slice commit

## Scope and changed files

- `src/application/close_advice_runner.py`
  - reads one run-level business date;
  - classifies each lot as `active`, `expiry_day`, `expired_open`, or `unknown` from canonical expiration;
  - sends only active positions into required-data planning, OpenD fallback, and event enrichment;
  - prevents quote DTE from replacing missing/malformed canonical expiration;
  - emits existing `not_evaluable` state/action semantics for every non-active lifecycle;
  - projects `position_lifecycle_state` into CSV output.
- `src/application/agent_tools/close_advice_read_impl.py`
  - exposes the lifecycle diagnostic through the public read surface.
- `src/application/agent_tools/analysis.py`
  - carries the lifecycle diagnostic into Close Advice snapshots.
- `docs/CLOSE_ADVICE_CONTRACT.md`
  - documents lifecycle classification, I/O eligibility, and the no-inference boundary.
- `docs/DEPENDENCY_GRAPH.md`
  - mechanically refreshes test import-edge counts required by the repository graph check; production dependency edges and cycle count are unchanged.
- `tests/test_close_advice_runner.py`
  - covers expiry-day, expired-open, unknown, active, quote-DTE rejection, I/O exclusion, public projection, and the single-date invariant;
  - freezes historical fixture dates whose prior behavior implicitly depended on wall-clock time.

No strategy threshold, tier priority, rank rule, action type, notification selector, config, state-write, or broker-facing module changed.

## Decisions and invariants

- Canonical expiration plus the one run-level business date owns lifecycle and DTE.
- Only `active` positions are quote/fetch/event eligible.
- `expiry_day` and `expired_open` have `quote_status=not_required`; `unknown` is a deterministic data gap.
- Existing local quotes are never consumed by a non-active row.
- Non-active rows use the existing `tier=not_evaluable`, `exit_state=not_evaluable`, and `close_action=not_evaluable` contract.
- No exercise, assignment, called-away, or settlement outcome is inferred.
- Active rows continue through the existing evaluator and fee/action paths unchanged.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/close_advice_s2 python3.12 -m pytest \
  tests/test_close_advice_runner.py \
  tests/test_close_advice_domain.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  -q -p no:cacheprovider
```

Final focused result:

```text
179 passed, 2 warnings
```

Both warnings are existing legacy notification-renderer deprecation warnings. `git diff --check` also passed.

Aggregate offline validation performed before the accepted-slice commit because Git index approval was temporarily unavailable:

```text
dependency graph check: 2 passed
compileall domain src: pass
full pytest: 3002 passed, 10 skipped, 6 warnings
```

The first full-suite attempt identified only the stale generated dependency graph. After running the repository generator, the full suite passed; the graph diff changes test import counts only.

## Docs decision

Updated `docs/CLOSE_ADVICE_CONTRACT.md` because lifecycle state and I/O eligibility are public diagnostic contracts. No PRD, config, VERSION, CHANGELOG, or notification documentation changed.

## Residual risks

- **Existing reconciliation ownership**: expired-open lots require ledger/broker reconciliation; this slice only prevents invalid advice work.
- **Evidence preservation**: malformed expiration text remains visible in diagnostic output while DTE stays null.
- **Aggregate review**: aggregate adversarial review and PR review still run after the accepted S2 commit; full tests are already green.

No residual risk is unclassified.
