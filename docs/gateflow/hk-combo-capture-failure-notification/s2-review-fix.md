# Gateflow S2 Review Fix — Typed corrupt-artifact failure

- Gate: `fix S2`
- Work unit: `hk-combo-capture-failure-notification`
- Initial review: `docs/reviews/code-review-20260810-120101.md`
- Status: fix complete; pending S2 re-review

## Finding decision

### DR-S2-01 — accepted — fixed

The source-owner boundary decoded existing `receipt.json` and `payload.json`
bytes, but its public exception translator did not include `UnicodeError`.
Invalid UTF-8 could therefore escape `PositionAdviceAccountSourceError`, bypass
the `account_run` typed mapping and terminate the multi-account tick instead of
isolating the affected account.

The owner helper now translates `UnicodeError` through the same
`PositionAdviceAccountSourceError` boundary as malformed JSON, hash conflicts,
stale data and filesystem errors. The source-owner conflict matrix now includes
invalid UTF-8 and wrong-broker cases. The existing tick-level conflict regression
proves any typed owner failure prevents prefetch and account execution and yields
`should_notify=False`.

## Validation target

- Run the source-owner conflict matrix.
- Re-run the complete S2 focused suite.
- Re-run Ruff, Python compilation and `git diff --check`.
- Perform a fresh DeepReview of the corrected S2 chain.
