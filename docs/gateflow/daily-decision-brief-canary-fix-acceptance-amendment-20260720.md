# Gateflow Plan Amendment — Stable HK Daily Brief Canary Identity Acceptance

- **Work unit**: `daily-decision-brief-canary-correction`
- **Date**: 2026-07-20
- **Decision owner**: CEO/product
- **Status**: accepted; final plan re-review passed
- **Supersedes**: only the live-production contract-specific bullets in section 12.5 of `daily-decision-brief-canary-fix-plan-20260720.md`
- **Does not supersede**: deterministic fixture expectations in sections 5.2 and 10
- **Initial review**: `docs/reviews/plan-review-20260720-134335.md` (`fail`; four findings)
- **Final re-review**: `docs/reviews/plan-review-20260720-134503.md` (`pass`)

## 1. Reason for amendment

The v1.3.2 and v1.3.3 production-root no-send Canaries proved that live artifact membership is volatile: P450 was present in the exact run's canonical labeled artifact, so its presence was not raw fallback. A live rule that hard-codes P450 as rejected confuses a deterministic fixture fact with a changing production input fact.

The safety property is not a particular strike. The safety property is provenance:

- candidate-derived outputs may use only identities present in the exact run/account canonical labeled artifacts;
- identities present only in raw artifacts must not enter candidate-derived outputs or rendered messages.

## 2. Scope

This amendment changes only production Canary acceptance and closeout evaluation. It does not change:

- application code, candidate ranking, labeling, capacity, underwriting, event policy, or renderer behavior;
- artifact schemas, Daily Brief schema, revision allocation, semantic digest, delivery keys, or pointers;
- deterministic conflict fixtures or their P430/P440/P450 expected values;
- the separate requirement for explicit user authorization before real sending.

## 3. Exact-run identity sets

For one immutable `run_id` and one account, read only the run-scoped account directory and construct:

```text
L = normalized contract_symbol identities from *_sell_put_candidates_labeled.csv
R = normalized contract_symbol identities from *_sell_put_candidates.csv
U = R - L
C = normalized contract_symbol identities from brief.candidates.sell_put
A = normalized contract_symbol identities from Sell Put actions with action_type=open_candidate
```

Normalization is:

```text
trim(contract_symbol).upper()
```

An empty identity in `C` or `A` fails closed. Build `L` as an identity-to-core-fields map, where core fields are symbol normalized through the existing `domain.domain.symbol_identity.canonical_symbol()`, ISO expiration, and numeric strike normalized through decimal value equality. Exact duplicate labeled rows may deduplicate. If one normalized identity maps to conflicting core fields, the labeled authority is malformed and the Canary fails closed. Every row in `C` and `A` must match the unique labeled core fields when those fields are present. Numeric normalization must treat equivalent forms such as `450` and `450.0` as equal.

Sets are scoped by exact run and account. They must not be built from mutable `current` paths or unioned across accounts or runs.

Raw artifact reads are audit-only and occur after the production run outputs are frozen. Raw rows must never be passed to Daily Brief assembly, ranking, candidate/action/event builders, or renderers. Any reusable audit helper belongs in test/operations evidence code, not the normal `daily_decision_brief_service.py` candidate-loading path.

Non-candidate actions—position close, observe, blocked, or invalidated lifecycle actions—are outside `A` because their authority is position/close-advice state rather than candidate CSVs.

## 4. Acceptance invariants

The Sell Put production Canary authority check passes only when:

```text
C subset-of L
A subset-of L
(C union A) intersect U = empty
```

And all of these checks pass:

1. Every Sell Put candidate and Sell Put `open_candidate` action has a source path ending in `_sell_put_candidates_labeled.csv`.
2. Every identity in `U` is absent from `candidates.sell_put`, Sell Put `open_candidate` actions, and candidate-derived events carrying a contract identity. Renderer checks apply to candidate/action/event recommendation sections. Explicitly labeled rejection or provenance diagnostics may mention a rejected identity without making it actionable.
3. A valid labeled artifact does not require a raw counterpart; absent raw artifacts yield an empty `R`/`U`, not a Canary failure.
4. Candidate rows with missing/invalid identity, conflicting labeled core fields, or core fields inconsistent with the unique labeled row cannot enter candidates or candidate-derived actions.
5. Capacity, candidate/action/summary, four-surface, renderer-integrity, no-send, config, delivery-pointer, and scheduler-notify checks remain unchanged.
6. Real sending remains blocked until the amended plan review passes, the saved exact-run evidence is re-evaluated under this standard, and the user separately authorizes sending.

## 5. Fixture boundary

P430/P440/P450 remain mandatory fixed-fixture values:

- labeled: P430 and P440;
- raw: P430, P440, plus attractive P450 raw-only;
- expected: P430/P440 may enter; P450 is absent from normalized brief and renderer.

This fixture proves the implementation rejects a known raw-only counterexample. It is not a claim that live production P450 must always be rejected.

## 6. Evidence reuse

A new market-data scan is not required solely because the acceptance oracle changed. The immutable v1.3.3 evidence may be re-evaluated if it contains:

- exact run/account raw and labeled artifacts;
- canonical and run-scoped briefs;
- exact-revision CLI and Agent Tool outputs;
- prepared audit plus render limits and message digest;
- config, delivery-pointer, and scheduler-notify before/after evidence.

The original audit report must remain unchanged. Before re-evaluation, generate a sorted SHA-256 manifest covering every consumed raw/labeled CSV, canonical and run-scoped brief, CLI and Agent output, prepared metrics, diff, renderer replay input, and config/delivery/scheduler safety snapshot. The derived amended-acceptance report records the manifest SHA-256, original audit SHA-256, release version, run ID, account scope, and amended standard version. Missing files, manifest drift, or evidence-generation mixing fails closed.

## 7. Stop conditions

Keep the real-send gate blocked if any of these occur:

- `C` or `A` is not a subset of `L`;
- `(C union A) intersect U` is non-empty;
- any `U` identity appears in candidate-derived normalized or rendered recommendation output;
- identity is empty, cannot be normalized, maps to conflicting labeled core fields, or disagrees with the unique labeled row;
- raw rows enter any normal runtime candidate/ranking/render path;
- evidence mixes run IDs, accounts, revisions, mutable `current` state, or does not match its sorted SHA-256 manifest;
- any existing four-surface, renderer, capacity, no-send, config, pointer, or scheduler-notify safety check fails.

## 8. Intended gate transition

```text
amendment drafted
-> planreview
-> accepted amendment
-> immutable v1.3.3 evidence re-evaluation
-> final closeout pass
-> real send remains pending separate explicit user authorization
```
