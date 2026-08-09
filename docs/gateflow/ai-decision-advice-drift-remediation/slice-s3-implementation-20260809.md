# Gateflow Slice S3 Implementation

- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S3 - Deterministic model inputs, option aggregation, and one-contract projections`
- Base checkpoint: `77f1bbd9 feat(ai-advice): accept drift remediation S2`
- Status: implementation complete; pending slice DeepReview

## Implemented contract

- Replaced legacy holdings/file inference with typed prepared PM distribution and prepared option-position inputs. Candidate, PM, option, evidence, projection, and fact-registry content now have independent deterministic hashes.
- Serialized only model-safe PM facts: validation status, observed time, quality gaps, per-symbol weights, per-currency weights, and cash/MMF weight. Absolute portfolio values, quantities, mapped PM account identifiers, and source payloads remain private calculation inputs.
- Validated the complete prepared option-position payload before use. Any invalid row, authority mismatch, or unavailable prepared context makes the whole option-position input unavailable instead of silently dropping rows or treating them as zero.
- Aggregated positive open contracts by economic identity: symbol, option type, side, strike, expiry, and multiplier. Every valid open position contributes to account-level direction/type and expiry concentration; only candidate symbols expose contract detail to the model.
- Added deterministic one-contract projections for every accepted candidate:
  - Sell Put assignment exposure uses strike, multiplier, same-run prepared OpenD FX, and prepared PM CNY total.
  - Covered Call call-away fraction uses contract multiplier and current PM shares for the symbol.
  - Both modes expose same-obligation counts, long-call/long-put counts, exact-expiry counts, and the separate +/-7-day expiry band using actual open contract quantities.
- Missing strike, multiplier, FX, PM total, or Covered Call shares produces an explicit projection gap and a `needs_review` ceiling. No multiplier, exchange rate, portfolio total, or share count is guessed.
- Simplified combination labels are emitted only when the current ledger's formal identity contract validates. Unverified grouping metadata is ignored and no structure is inferred from coincident strikes or expiries.
- Built a deterministic fact registry with candidate, projection, portfolio, position, coverage, evidence, and gap facts. Model-visible records use stable fact references and contain no source paths, private record/group identifiers, absolute holdings, premium cash values, notes, or account identifiers.
- Preserved legal empty states: zero candidates, zero portfolio assets, and zero open option positions remain distinct from unavailable or invalid inputs.

## Validation evidence

```text
python3 -m pytest -q \
  tests/test_ai_decision_advice_contexts.py \
  tests/test_ai_decision_advice_projection.py \
  tests/test_prepared_option_positions_context.py
26 passed in 0.47s

python3 -m py_compile \
  src/application/ai_decision_advice/contexts.py \
  src/application/ai_decision_advice/projection.py \
  src/application/prepared_option_positions_context.py \
  tests/test_ai_decision_advice_contexts.py \
  tests/test_ai_decision_advice_projection.py \
  tests/test_prepared_option_positions_context.py
passed

python3 -m ruff check \
  src/application/ai_decision_advice/contexts.py \
  src/application/ai_decision_advice/projection.py \
  src/application/prepared_option_positions_context.py \
  tests/test_ai_decision_advice_contexts.py \
  tests/test_ai_decision_advice_projection.py \
  tests/test_prepared_option_positions_context.py
All checks passed

git diff --check
passed
```

Coverage includes USD/HKD/CNY Sell Put projections, Covered Call share fractions, actual-contract aggregation, same-obligation and long-leg counts, exact and +/-7-day expiry buckets, missing-factor gaps, unavailable-versus-zero distinctions, multi-account rejection, verified/unverified combination identities, deterministic hashes, evidence references, and recursive privacy checks.

## Documentation decision

No public command, configuration key, artifact location, or operator workflow changes in this slice. The accepted design and Gateflow plan already define these deterministic input and projection contracts, so no additional public documentation change is required before review.

## Residual boundary

- Advice record/schema consumers and prompt-binding validation move to S4; this slice only produces the new deterministic bindings.
- Production orchestration still invokes the legacy context path until the typed handoff is wired in S6.
- The current formal ledger identity schema validates SP+LC. No formal CC+LP identity exists yet, so S3 safely emits no CC+LP label rather than inferring one; formal CC+LP identity remains a separate ledger-contract work unit.
- The PM producer's row-level CNY unit is still not expressed in its vendored OpenAPI. That accepted cross-repository contract gap remains assigned to the separate portfolio-management work unit recorded in S2.
