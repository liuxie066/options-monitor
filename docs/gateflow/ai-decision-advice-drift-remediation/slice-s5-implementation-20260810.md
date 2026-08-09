# Gateflow Slice S5 Implementation

- Work unit: `ai-decision-advice-drift-remediation`
- Slice: `S5 - Anonymous observation and authoritative evidence collection`
- Base checkpoint: `9b6941be feat(ai-advice): accept drift remediation S4`
- Status: implementation complete; slice DeepReview complete

## Implemented contract

- Split identity snapshot audit hash from semantic identity hash so observation
  timestamps do not invalidate still-current evidence.
- Added a market-partitioned anonymous observation snapshot with a private lock,
  exact allowlisted schema, atomic replacement and corruption fail-closed behavior.
  It contains no account labels, quantities or position provenance.
- Reworked the managed collector to read the anonymous observation set, fall back
  only to configured symbols when absent, order same-priority symbols by historical
  attempt time and preserve explicit full/incremental search modes.
- Built identities from the existing OpenD infrastructure gateway in 200-code
  snapshot batches. Missing names fall back to `get_stock_basicinfo` using only an
  explicit code list; partial successful batches remain usable.
- Enforced exact result cardinality, per-symbol completed native web-search audit,
  HTTPS URL normalization, native citation/source intersection and exact symbol
  binding. Provider text, raw responses, search queries, call IDs and account
  provenance are not persisted.
- Implemented two-batch maximum concurrency under one five-minute monotonic
  deadline. Batch-local provider failures do not discard other completed batches.
- Added full/incremental evidence state transitions, stable refs, semantic snapshot
  hashes, retained prior success after failed refresh, strict active-ref replay and
  actual evidence-as-of calculation.
- Required top-level DeepSeek Responses status `completed` before any extracted
  output can become evidence.
- Removed the collector from the public `./om` parser. The opt-in managed service
  invokes a narrow internal argv/environment wrapper whose business logic remains
  in the application runner.
- Updated the evidence output prompt and operator documentation to match the
  internal-only refresh boundary.

## Review-driven scope exception

The first rereview proved that the existing domain fallback was not connected in
production. `src/infrastructure/futu_gateway.py` and its focused test were added to
the S5 scope as the smallest correct-owner change: they expose only OpenD
`get_stock_basicinfo(market, code_list)` and do not move identity policy into the
adapter.

## Focused validation evidence

```text
python3 -m pytest -q \
  tests/test_ai_decision_advice_identity.py \
  tests/test_ai_decision_advice_collector.py \
  tests/test_ai_decision_advice_evidence_store.py \
  tests/test_ai_decision_advice_collector_cli.py \
  tests/test_deepseek_responses.py \
  tests/test_service_deploy.py \
  tests/test_futu_gateway_minimal.py
251 passed in 1.47s

ruff: passed
py_compile: passed
git diff --check: passed
```

Expanded AI Decision Advice validation remains `197 passed, 3 failed`. The same
three failures are the approved S6-owned typed authority handoff; no S5 collector,
identity, evidence, adapter or service test fails.

## Residual boundary

- Live DeepSeek native-source availability remains a release-readiness canary.
  Missing bindable native sources deliberately produces no trusted evidence.
- No live provider call, systemd installation, public refresh command, release or
  deployment is part of S5.
- Daily Brief orchestration and typed PM/option authority handoff remain S6-owned.
