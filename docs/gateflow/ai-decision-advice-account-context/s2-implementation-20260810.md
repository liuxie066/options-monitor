# Gateflow Implementation — S2 Account-Isolated Integration Proof

- Gate: `implementation`
- Work unit: `ai-decision-advice-account-context`
- Slice: `S2`
- Branch: `fix/ai-decision-advice-account-context`
- Accepted plan commit: `aca09b53`
- Previous slice commit: `12ab398f`
- Status: `accepted; deepreview found no unresolved material issues; ready for slice commit`
- DeepReview: `docs/reviews/code-review-20260810-112736.md`

## Scope

S2 adds integration proof only. No production module, Candidate Engine rule, projection formula, config schema,
persistence schema, prompt, collector, notification, release, deployment, or runtime state was changed.

Changed tests and contract text:

- `tests/test_prepared_portfolio_distribution.py`
- `tests/test_prepared_option_positions_context.py`
- `docs/AI_DECISION_ADVICE_DESIGN.md`
- `docs/gateflow/ai-decision-advice-account-context/goal-confirmation-20260810.md`

## Clarified source boundary

1. Sell Put and Covered Call Candidate Engine capacity denominators continue to use the account's configured Futu
   or holdings operational context.
2. PM is the strategic portfolio provider for AI Decision Advice only.
3. Within AI Decision Advice, the deterministic one-contract projection uses PM total portfolio value for Sell Put
   exposure and PM held shares for Covered Call call-away fraction.
4. The Advice projection does not mutate, re-rank, replace, or feed back into the sealed candidate snapshot.

## Integration proof

1. **Futu operational + PM strategic**
   - The account config retains `portfolio.source=futu`.
   - Advice PM preparation requests only the account's explicit `holdings_account` mapping.
   - The frozen Advice distribution and one-contract projection use the returned PM facts.
   - The candidate snapshot remains value-equivalent before and after freezing.
2. **One physical SQLite, two logical accounts**
   - One SQLite ledger holds nonzero `lx` and `sy` positions.
   - One preparation call performs one ledger generation read and one FX observation.
   - Strict loading produces account-bound contexts with only the current account's open contracts.
   - Frozen summaries, candidate contracts, exact/near-expiry counts, obligation counts, projections, and evidence
     symbol scopes contain no other-account facts.
   - Both account contexts bind the same coherent ledger-generation hash.
3. **Mismatch isolation**
   - A wrong `lx` config hash is rejected by the strict loader.
   - The independent `sy` prepared context remains loadable and retains its own contract total.

## Test evidence

- Focused prepared-source tests: `27 passed`.
- Expanded prepared PM, prepared option, Advice contexts, orchestration, projection, and Daily Brief notification
  flow: `105 passed`.
- Ruff on both changed test files: pass.
- `git diff --check`: pass.

## Production-code result

The integration tests exposed no production defect. The only initial failure was an incorrect test arithmetic
expectation: `95 * 100 * 7.2 / 100000 = 0.684`. The expected value was corrected; no production implementation was
changed.

The slice review also replaced fixed current-era FX/expiry values and an unrealistic `NVDA/CNY` fixture with durable
future expiries and a realistic USD candidate backed by prepared FX. These were test-quality corrections, not product
changes.
