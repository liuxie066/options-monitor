# Gateflow S1 Implementation — Owner-aware capture routing

- Gate: `implementation S1`
- Work unit: `hk-combo-capture-failure-notification`
- Accepted plan commit: `6e0d8964`
- Status: implementation complete; pending code review

## Implemented scope

- Canonicalized capture statuses at the snapshot-owning boundary and routed only
  `put` / `call` to the opening snapshot, `combo_yield/variant=sp_lc` to the
  Combo snapshot, and `combo_yield/variant=cc_lp` to the CC+LP snapshot.
- Built expected scopes from the same resolved watchlist traversal used by the
  pipeline, then applied unexpected, duplicate, missing, invalid-status and
  quote-binding validation independently per owner.
- Retained the per-symbol frozen quote identity invariant across owners before
  reducing owner-local statuses.
- Added a shared owner-local status reducer covering completed-empty,
  unavailable, partial, market-closed and not-applicable outcomes.
- Routed pairs independently by variant. Existing SP+LC rows without a variant
  are treated as the sole legacy compatibility case; unknown explicit variants
  fail closed.
- Preserved the CC+LP producer's `not_applicable` status and reason instead of
  overwriting it with `completed`; malformed CC+LP summary status/results become
  a failed capture through the existing scan-failure path.
- Added Combo capture evidence to `FrozenRequiredDataUnavailable`, including the
  canonical variant and frozen quote binding.

## Tests added or strengthened

- The real `run_watchlist_pipeline_default()` sealing path now proves:
  - opening + SP+LC snapshots are sealed independently;
  - a legacy variant-less SP+LC pair survives routing;
  - CC+LP `not_applicable` seals only the CC+LP snapshot;
  - all-completed empty, all-failed, mixed, all-not-applicable and missing-scope
    aggregates retain their distinct semantics;
  - invalid modes/variants, unexpected scopes, duplicate scopes and invalid pair
    variants fail before a terminal snapshot is written;
  - cross-owner quote conflicts downgrade every completed owner for that symbol.
- Symbol monitoring regressions prove CC+LP `not_applicable` survives the
  producer callback and frozen required-data failure emits a Combo failed status
  with `variant=sp_lc`.

## Validation

- `python3.12 -m pytest -q -p no:cacheprovider tests/test_pipeline_capture_status_routing.py tests/test_symbol_monitoring_fetch_spec_merge.py tests/test_opening_candidate_snapshot.py tests/test_combo_yield_candidate_snapshot.py tests/test_cc_lp_candidate_snapshot.py`: `52 passed`.
- Broader pipeline regression command covering capture routing, watchlist,
  runtime paths, context contracts, processor boundaries and snapshot tests:
  `129 passed`.
- `python -m py_compile` for both changed production modules: pass.
- `git diff --check`: pass.

The repository `.venv` does not contain `pytest`; validation therefore used the
project's documented `python3.12 -m pytest -p no:cacheprovider` fallback.

## Docs and safety decision

- No public schema, CLI, config, notification wording or operator workflow
  changed; only Gateflow artifacts are updated.
- No runtime data, provider, broker, service, production config, notification,
  release or deployment action was invoked.
- Pre-existing unrelated dirty files remain untouched and unstaged.

## Residual risks

- S1 removes the 10:00 capture-routing crash but does not yet make 09:40-style
  prefetch failures eligible for bounded fixed-failure delivery; that is S2.
- Scheduler processed-target retry and OpenD timeout behavior remain explicitly
  outside this work unit.

## Next gate

`code review S1` using DeepReview.
