# Gateflow Fix Artifact — Slice 1 Code Review

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `fix`
- Slice: `slice-1`
- Review artifact: `docs/reviews/code-review-20260812-083924.md`
- Re-review artifact: `docs/reviews/code-review-20260812-084107.md`
- Status: fixes accepted by code re-review
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/slice-1-review-fix.md`

## Finding decision and fix

### CR-S1-01 — accepted — fixed

The original helper performed fallback independently for every reject and then sorted all selected values together.
That allowed a generic top-level reason from one reject to outrank a concrete nested reason from another reject.

The helper now keeps three separate deterministic tiers across the full contract scope:

1. `metric_value.reason_codes` first non-empty code;
2. `metric_value.reason_code`;
3. canonical top-level reject reason.

It chooses lexicographically only inside the first non-empty tier. A parameterized regression uses both reject orders
and proves `term_matched_rv_unavailable` wins over a sibling top-level `input_missing` while a specific strategy-owned
cause remains protected.

Final status: `已修复`.

### CR-S1-02 — accepted — fixed

The re-review found that multiple contract scopes for the same symbol/mode were still first-contract-wins. The
projection now aggregates all concrete unresolved contract reasons by `(symbol, mode)` and selects the
lexicographically first code from the full set before replacing a generic strategy reason. A parameterized regression
reverses contract scope order and proves the result remains `option_multiplier_conflict`.

Final status: `已修复`.

## Validation

```text
/Volumes/liuxie的硬盘/workspace/options-monitor/.venv/bin/python -m pytest -q tests/test_scan_volume_gate_min_zero.py tests/test_candidate_scanning_evidence.py tests/test_opening_candidate_snapshot.py
```

Result after both fixes: `39 passed`.

## Docs decision

No public docs change; only implementation/test and Gateflow review artifacts changed.

## Residual risks

- Slice 2 report consumption remains `covered by later approved slice`.
- Broader cross-path regression remains `covered by aggregate DeepReview`.
- Future definitive calculation reasons remain `assigned to later work unit`.

## Completion status

Accepted findings CR-S1-01 and CR-S1-02 are fixed. Final re-review
`docs/reviews/code-review-20260812-084308.md` concluded `pass`. Current gate / next entry point:
`accepted slice commit`.
