# Gateflow Implementation Artifact — Slice 1

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `implementation`
- Slice: `slice-1` — candidate evidence classification and concrete sealed reason
- Base commit: `8a3520f0`
- Status: accepted after code review/fix/re-review
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/slice-1-implementation.md`
- Reviews: `docs/reviews/code-review-20260812-083924.md`,
  `docs/reviews/code-review-20260812-084107.md`, `docs/reviews/code-review-20260812-084308.md`

## Objective and outcome

Project the proven deterministic `net_premium_non_positive` calculation outcome as an existing canonical policy
rejection while keeping genuinely unevaluable inputs fail closed. When a sealed contract scope contains a concrete
diagnostic, expose it instead of a generic strategy-level gap reason.

Implemented outcome:

- opening-ready `net_premium_non_positive` uses top-level `policy_rejected` and retains the exact calculation code in
  `metric_value.reason_code`;
- unknown opening-ready calculation failures continue to use `input_invalid`;
- non-ready contracts retain their existing canonical reason;
- scope evidence counts the deterministic case as policy rejection with zero unresolved eligibility;
- `candidate_universe_summary()` derives a deterministic contract diagnostic and replaces only generic strategy gap
  reasons, preserving specific strategy-owned causes.

## Changed files

- `src/application/candidate_scanning.py`
- `src/application/opening_candidate_snapshot.py`
- `tests/test_scan_volume_gate_min_zero.py`
- `tests/test_opening_candidate_snapshot.py`
- this artifact

`tests/test_candidate_scanning_evidence.py` was validated but required no change.

## Decisions and invariants

- The definitive-reason set is private and contains only `net_premium_non_positive`.
- The closed Candidate Engine reject vocabulary is unchanged.
- No message-text, sign, market, or option-side inference is used.
- Contract detail precedence is `metric_value.reason_codes[0]`, then `metric_value.reason_code`, then the canonical
  top-level reject; multiple diagnostics resolve lexicographically for deterministic output.
- A contract cause can replace `partial_data`, `data_unavailable`, or another generic strategy state, but cannot
  overwrite a specific cause such as `covered_call_portfolio_context_unavailable`.
- Snapshot data, seal, hashes, schema, candidate ranking, and strategy policy are unchanged.

## Validation

Command:

```text
./.venv/bin/python -m pytest -q tests/test_scan_volume_gate_min_zero.py tests/test_candidate_scanning_evidence.py tests/test_opening_candidate_snapshot.py
```

Initial implementation result: `36 passed`. Final result after accepted review fixes: `39 passed`.

Assertions include:

- US Sell Put and HK Covered Call with standard multiplier `100` and fee-negative premium validate as canonical
  `policy_rejected`, nested `net_premium_non_positive`, `policy_rejected_count=1`, unresolved count `0`, and
  `completed/no_candidate`;
- multiplier conflict remains `input_invalid` and unresolved;
- term-matched RV remains unavailable and its concrete reason reaches universe summary;
- a specific strategy-owned unavailability reason is not overwritten;
- existing mixed/accepted/no-candidate snapshot regressions still pass.

`git diff --check`: passed.

## Docs decision

No public docs change. This is an internal evidence-classification correction with no command, config, or schema
change; Gateflow artifacts provide the audit trail.

## Residual risks and uncovered areas

- Future deterministic calculation reasons: `assigned to later work unit`; they remain fail closed until proven.
- Daily Brief consumption, CC+LP requirement, prefetch success, specific Chinese warning, and AI fallback copy:
  `covered by later approved Slice 2`.
- Production replay: `assigned to separately authorized release/upgrade verification`.

## Completion status

Slice 1 implementation and review loop are accepted. Current gate / next entry point: `accepted slice commit`.
