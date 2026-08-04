# Gateflow S1 Exact-Position Scope Amendment

- Gate: plan amendment before continuing S1 implementation
- Work unit: `required-data-multi-account-integrity`
- Slice: S1 — Required-data completion, receipt, seal, and gateway truth
- Artifact path: `docs/gateflow/required-data-multi-account-integrity/s1-exact-position-scope-amendment.md`
- Status: accepted after passing re-review
- Failed PlanReview artifact: `docs/reviews/plan-review-20260804-124418.md`
- Passing re-review artifact: `docs/reviews/plan-review-20260804-125807.md`

## Evidence

`_position_requirement_side_plans()` currently converts ready Close Advice
positions into side-wide minimum/maximum strike bounds. `_merge_same_side_plans()`
then merges those bounds with strategy ranges. This loses the original
expiration-to-strike relation. A position strike inside a strategy range leaves
no trace in the final debug plan, so post-write coverage can accept range edges
while omitting the held contract itself. Selecting the wider fetch bounds cannot
repair an interior-strike omission because the exact demand is no longer
representable.

The owning representation and merge functions live in
`src/application/required_data_planning.py`, which was not in the accepted S1
allowlist. Implementing only in the coverage evaluator would invent provenance
that the plan no longer contains.

The same adversarial pass found that expiration discovery accepts any non-empty
underlier and that successful row payloads do not consistently bind their raw
underlier code to the discovery identity. Those checks belong to S1 files already
allowed, but are recorded here so the amended acceptance signal is complete.

## Exact scope addition

- Add `src/application/required_data_planning.py` and
  `tests/test_required_data_fetch_planning.py` to S1.
- Add a required debug-contract field on `OptionSideFetchPlan`:
  `required_exact_strikes_by_expiration: dict[str, list[float]]`, with `{}` for
  strategy-only plans.
- Build the mapping from ready position requirements by exact option side and
  expiration. Keys are strict ISO dates; values are non-empty, positive finite,
  non-boolean, unique, deterministically sorted strike lists.
- Merge same-side plans by expiry-local set union. Do not flatten strikes across
  expirations and do not duplicate the mapping at request level; nested side
  plans already carry the exact identity.
- Require the field in the expected-contract validator. Its expiry keys must be
  a subset of the side plan expirations, every strike must lie within the fetch
  strike window, and top-level versus nested side-plan equality remains exact.
- Require CSV coverage to contain every declared exact strike in the matching
  side and expiration, using one small fixed numeric tolerance only for stable
  CSV float representation. Existing base-range coverage remains independently
  mandatory.
- Derive the expected Futu underlier code from the canonical plan symbol through
  the domain symbol identity owner. Require both discovery identity and raw
  provider payload to match it for `success_rows` and `success_empty`.
- Before lossy projection, validate every input whose `planning_status` is
  `ready`: option type must be supported, expiration must be a strict ISO date,
  and strike must be positive, finite, and non-boolean. One malformed ready item
  is a typed symbol planning failure; it must not be skipped while valid peers
  form a smaller plan. Explicit unavailable/non-ready items remain excluded by
  their existing typed status.
- Use one shared exact-strike identity helper in both typed and debug coverage:
  `math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9)`. Do not reuse the
  range-edge tolerance and do not scale the tolerance with strike value or
  window width.

## Evidence-critical multi-spec execution moved into S1

The failed PlanReview proved that a legal plan can contain multiple
`merged_specs`, while the canonical prefetch adapter currently flattens them
into one Cartesian provider request. S1 cannot require one child per exact
request and then manufacture those children from the aggregate plan.

- In-process S1 execution invokes each ordered `RequiredDataFetchSpec` exactly
  once through the existing gateway, using
  `build_fetch_request_from_spec`. It aggregates only the returned typed child
  payloads, saves/finalizes once, and never publishes a partial child result.
- Reuse the existing execution/merge owners in
  `required_data_fetching.py`; move or expose the existing deterministic child
  evidence aggregation now private to `required_data_steps.py` rather than
  adding a second aggregation implementation.
- Freeze child evidence v1 as an ordered list matching ordered
  `merged_requests`. Each item carries its zero-based request index, canonical
  planned-request SHA-256, canonical symbol and physical source/host/port
  binding, typed status/outcome, source observation time, and completion time.
  The coordinator may bind the planned request identity to the actual call it
  is about to execute, but it may not synthesize status, outcome, or timestamps.
- Receipt validation recomputes the ordered request hashes from the expected
  contract and rejects missing, duplicate, unexpected, reordered, wrong-symbol,
  wrong-binding, non-ok, or timestamp-invalid children. Aggregate status,
  identity, snapshots, RV, and times must still reconcile with those same
  children.
- Current authored and resolved runtime configs omit
  `runtime.prefetch.execution_mode`; `_resolve_execution_mode()` therefore uses
  its explicit `inprocess` default. Until S6 adds exact-spec JSON plumbing,
  subprocess execution remains supported for zero/one spec but a multi-spec
  plan returns a stable typed unsupported result before starting a subprocess,
  writing outputs, or publishing a receipt. It must not fall back to the
  Cartesian selector path. S6 removes this narrow temporary boundary and adds
  exact multi-spec subprocess execution.
- Do not change provider selectors, fetch widening, position requirement schema,
  receipt paths, manifest schema, retry behavior, scoped-union policy, spot
  memoization, or public/CLI exact-spec plumbing in S1.

The S1 expected-fetch-contract schema is newly introduced and unpublished in
this work unit. Its final v1 shape therefore requires this field; missing must not
silently normalize to `{}` because deleting it and recomputing the hash would
erase exact position demand.

## Success signal

- Position-only data missing its exact strike cannot receive a receipt.
- A strategy range whose endpoint rows exist still fails when an interior held
  strike is absent.
- Exact strikes for two expirations cannot satisfy one another.
- Strategy-only plans emit and validate an explicit empty mapping.
- Missing, malformed, non-finite, duplicate, unsorted, out-of-window, or
  top/nested-drifted exact-strike identity is rejected before provider/cache use.
- Any malformed ready position requirement fails before provider/cache use;
  valid siblings cannot hide it.
- Wrong-symbol discovery or raw underlier evidence produces zero receipt.
- A real two-spec in-process plan produces two request-bound child observations,
  one aggregate save/finalize, and one receipt. Wrong/duplicate/reordered child
  identity or any failed child produces zero receipt.
- A configured subprocess multi-spec plan produces typed unsupported with zero
  subprocess, output, payload, or receipt until S6 restores that mode.
- Existing exact-plan and coverage regressions remain green.

## Validation

- Planning tests prove expiry-local preservation through position-only and
  strategy-plus-position merges.
- Expected-contract tests cover required empty maps, malformed maps, window
  exclusion, and top/nested drift.
- Coverage/output tests cover position-only, interior, outside-range, and
  multi-expiration omissions plus wrong underlier evidence.
- In-process prefetch tests exercise a real two-spec call sequence and reject
  child hash/count/order/binding drift without synthetic child metadata.
- Subprocess tests prove the temporary multi-spec boundary occurs before
  execution or publication, while zero/one-spec compatibility remains intact.
- Typed and debug coverage are parameterized over the exact same within/outside
  `1e-9` boundary cases.
- Focused S1 suite and a timestamped DeepReview re-review must pass before the
  protected local S1 commit.

## Residual risk

- The provider still fetches range windows; an upstream chain that omits an
  interior contract is rejected after readback rather than selectively retried.
- Numeric tolerance covers serialization representation only and must not widen
  the economic strike identity.
- Subprocess multi-spec is explicitly unavailable during the protected S1
  commit. The current canonical config uses the in-process default; S6 must
  restore subprocess exact-spec execution before the aggregate work unit can be
  accepted or delivered.
- Per-position lot identity remains owned by the later Close Advice authority
  slice; S1 binds only the exact quote demand needed to prove market-data
  completeness.
