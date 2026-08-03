# Gateflow S2 Review Fix — Account-scoped History Backfill

- Work unit: `lifecycle-single-source-of-truth`
- Gate: `code review -> fix`
- Slice: `S2`
- Date: 2026-08-04
- Review artifact: `docs/reviews/code-review-20260804-003745.md`
- Status: fixed and re-reviewed
- Artifact path: `docs/gateflow/lifecycle-single-source-of-truth/s2-review-fix.md`

## Finding decision

### S2-01 — accepted — 已修复

The two lifecycle-discovery phases now contribute to the top-level backfill completion contract.

- `lifecycle_discovery_complete` is true only when both before and after phase results are successful.
- The value is exposed in diagnostics and included in `out["ok"]`.
- A lifecycle-only failure produces the stable top-level error `lifecycle_discovery_incomplete`, allowing the listener status projection to retain an observable `last_backfill_error`.
- Existing error precedence is preserved: incomplete history and durable Inbox failures remain authoritative before lifecycle discovery failure.
- History payload processing still runs when lifecycle account scope is incomplete; unmapped payloads retain the existing unresolved/audit path.

### S2-02 — accepted — 已修复

The phase result keeps the existing `lifecycle_discovery_result.v2` schema discriminator.

- `accounts` and `account_results` remain additive fields.
- Existing union summary and compatibility fields remain at the same top-level paths.
- Durable audit events continue to wrap the phase payload under `result`.
- The single-account regression now asserts the v2 marker before and after discovery.

## Regression coverage

The incomplete-scope regression now includes an unmapped history payload and asserts:

- both lifecycle phases make zero discovery calls and retain typed scope evidence;
- payload receipt and identity-review audit phases still occur;
- the payload remains unresolved;
- top-level `ok` is false, `error` is `lifecycle_discovery_incomplete`, and diagnostics expose incomplete lifecycle discovery.

The single-account regression asserts that diagnostics and the durable audit envelope retain `lifecycle_discovery_result.v2` while exposing additive account evidence.

## Validation

```bash
PYTHONPYCACHEPREFIX=/tmp/om_lifecycle_s2_rereview3 python3.12 -m pytest -q \
  tests/test_trades_auto_intake_backfill.py
```

Result: `14 passed in 1.12s`.

`python3.12 -m ruff check src/application/trades/backfill.py tests/test_trades_auto_intake_backfill.py`: pass.

`git diff --check`: pass.

## Docs decision

No additional operator documentation is required; the existing S2 docs already state the fail-closed account-scope behavior.

## Residual risks

- History checkpoint advancement remains tied to history-query and durable-Inbox completeness. Lifecycle discovery is an independent idempotent ledger scan on every backfill cycle, so a lifecycle-only failure does not rewind successfully processed history windows.
- Per-account create-only discovery remains retryable without cross-account rollback.

## Completion status

S2-01 and S2-02 are fixed; accepted re-review artifact: `docs/reviews/code-review-20260804-004321.md`.
