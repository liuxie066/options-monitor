# Gateflow Slice S2 Implementation

- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S2 - Account-scoped prepared PM distribution and Tick soft dependency`
- Base checkpoint: `1ca4987d feat(ai-advice): accept drift remediation S1`
- Status: implementation complete; pending slice DeepReview

## Implemented contract

- Added `prepared_portfolio_distribution.v1` as an immutable run/account state envelope with separate `authority`, `payload`, and `integrity` sections.
- Bound every artifact to the run id, lowercase OM account, exact mapped PM account, effective provider, account-config hash, fetch time, validation state, and payload hash.
- Added strict normalization for PM asset rows. Only code, normalized asset type, currency, quantity, and CNY value survive into the prepared payload; upstream ratios and totals are ignored.
- Recomputed total value, per-code weights, per-currency weights, and cash/MMF weight locally. A fresh/trusted empty asset list remains a valid ready-zero portfolio.
- Mapped `fresh + trusted` to ready, stale trusted or partial trust to degraded, and unknown/untrusted/unavailable or any protocol/transport failure to unavailable. Unavailable payloads never retain asset rows.
- Published explicit unavailable artifacts for disabled Advice, provider `none`, and PM failures. PM is not used as a fallback for the existing Futu/holdings operational portfolio context.
- Added write-once adoption: an already valid same-run artifact is loaded without another PM read; an invalid existing artifact fails soft and is never overwritten.
- Added recovery semantics: `prefetch_done=True` loads only the exact run/account artifact. Missing, corrupt, or misbound artifacts become in-memory `recovery_artifact_unavailable`; recovery never invokes PM.
- Added typed distribution authority and privacy-safe status metrics to `TickAccountExecutionOutcome`. Metrics contain only OM account, provider, status, reason, and artifact hash; they contain no mapped PM account or absolute values.
- Kept PM preparation outside Candidate Engine gates. PM failure neither removes the scanning account nor changes the existing option-context hard boundary.

## Validation evidence

```text
python3 -m pytest -q \
  tests/test_prepared_portfolio_distribution.py \
  tests/test_tick_account_execution_barrier.py \
  tests/test_multi_account_tick.py
70 passed in 0.86s

python3 -m py_compile \
  src/application/prepared_portfolio_distribution.py \
  src/application/tick_account_execution.py \
  src/application/multi_account_tick.py
passed

python3 -m ruff check \
  src/application/prepared_portfolio_distribution.py \
  src/application/tick_account_execution.py \
  src/application/multi_account_tick.py \
  tests/test_prepared_portfolio_distribution.py \
  tests/test_tick_account_execution_barrier.py
All checks passed

git diff --check
passed
```

Coverage includes provider none/disabled, exact PM mapping, fresh trusted, ready zero, stale/partial, unknown/untrusted, wrong account, foreign breakdown, non-finite values, response errors, transport failure, multi-account isolation, one read/account, write-once adoption, payload/artifact tamper, initial-to-recovery identity, recovery missing/corrupt, and Candidate Engine continuation under PM failure.

The combined accepted S1 + S2 focused suite also passed: `191 passed in 1.55s`.

## Residual boundary

- The current PM producer establishes `by_asset[].value = market_value_cny`, but its vendored OpenAPI still does not express that row-level unit. This accepted cross-repository contract gap remains assigned to a later portfolio-management work unit.
- Notification/Advice consumption of the typed object is intentionally deferred to S6; S2 exposes the verified authority on the Tick outcome but does not add a path-based reread.
