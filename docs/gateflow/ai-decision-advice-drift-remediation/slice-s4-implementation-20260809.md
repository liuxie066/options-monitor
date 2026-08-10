# Gateflow Slice S4 Implementation

- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S4 - Advice schema, exact scopes, fact validation and reuse`
- Base checkpoint: `8320cabe feat(ai-advice): accept drift remediation S3`
- Status: implementation complete; pending slice DeepReview

## Implemented contract

- Replaced the legacy portfolio-context binding with the exact six-field v1
  binding contract: five semantic hashes plus `external_evidence_run_id`.
- Added projections, the PM portfolio distribution and the frozen fact registry
  to the model data input. The old generic `portfolio` key is no longer sent.
- Derived the exact account decision scopes from the frozen candidate pool and
  validated candidate, projection, portfolio, option-position, coverage,
  evidence and gap registry membership before any model call can be accepted.
- Required the output scope set and cardinality to match exactly: one Sell Put
  scope when applicable and one Covered Call scope per candidate symbol.
  Missing, duplicate and extra scopes are structural `incomplete_output`
  failures and are never synthesized by OM.
- Enforced reference namespaces, frozen-registry membership and per-scope
  boundaries. Unknown, cross-scope or wrong-namespace facts demote only the
  affected decision to `needs_review`.
- Centralized deterministic action ceilings in the validator:
  - `keep` requires fresh/trusted PM context, complete prepared option context,
    complete projections, complete account evidence coverage, baseline and
    projection facts, and every required coverage fact;
  - `switch` requires a legal in-scope candidate, baseline and selected
    candidate facts, plus an internal risk fact or usable external evidence;
  - `defer` requires direct internal risk or usable external evidence; missing
    or incomplete search coverage alone is not risk support;
  - `needs_review` requires a registered gap, usable external conflict fact or
    multiple internal risk facts forming an explicit trade-off.
- Changed incomplete coverage demotion from `defer` to `needs_review` and kept
  Covered Call switches restricted to the same underlying symbol.
- Kept the structural repair to one retry under the original account deadline.
  A second incomplete scope result becomes exactly
  `unavailable: incomplete_output`.
- Replaced enumerable account hashes with 96-bit `secrets`-generated run-local
  references. Reused records are rebuilt from a whitelist, get a new reference
  and do not copy provider audit payloads or expose the prior reference.
- Reuse now requires the exact new binding shape, equality of all five semantic
  hashes, and matching provider/model/schema/prompt versions. A changed evidence
  run ID alone remains reusable when semantic evidence and coverage are
  unchanged.
- Updated the Advice prompt fragments to describe the fact registry, exact
  scopes, action support and current evidence reference namespace.

## Focused validation evidence

```text
python3 -m pytest -q \
  tests/test_ai_decision_advice_advice.py \
  tests/test_ai_decision_advice_validation.py
43 passed in 0.42s

./.venv/bin/ruff check \
  src/application/ai_decision_advice/validation.py \
  src/application/ai_decision_advice/advice.py \
  src/application/ai_decision_advice/advice_store.py \
  tests/test_ai_decision_advice_validation.py \
  tests/test_ai_decision_advice_advice.py
All checks passed

python3 -m py_compile <S4 source and test files>
passed

git diff --check
passed
```

Coverage includes all four actions, SP account scope, CC per-symbol scopes,
same-symbol and cross-symbol switches, exact scope omission/duplication/addition,
new and legacy bindings, random and reused references, internal-only risk
support, unusable external evidence, PM/option/projection/coverage ceilings,
wrong namespaces, unknown/cross-scope facts and the shared repair deadline.

Expanded AI Decision Advice validation:

```text
python3 -m pytest -q tests/test_ai_decision_advice_*.py
166 passed, 3 failed
```

The three failures are the unchanged S6-owned typed-handoff gap described
below; no S4 validator, store, prompt or model-call test fails.

## Residual boundary

- Production orchestration still calls the removed legacy S3 input signature.
  That known three-test failure remains assigned to S6; S4 does not restore a
  fallback or read legacy holdings files.
- Collector/evidence authority changes remain S5-owned.
- Daily Brief and notification integration remain S6-owned.
