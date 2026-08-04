# Gateflow S1 Scope Amendment

- Gate: plan amendment before S1 implementation completion
- Work unit: `required-data-multi-account-integrity`
- Slice: S1 — Required-data completion, receipt, seal, and gateway truth
- Artifact path: `docs/gateflow/required-data-multi-account-integrity/s1-scope-amendment.md`
- Status: accepted (`pass-with-risks`)
- PlanReview artifact: `docs/reviews/plan-review-20260804-111459.md`

## Evidence

`ThreadLocalFutuGatewayPool.close_current_thread()` closes and removes worker-local
gateways, then calls `mark_success()`. The prefetch coordinator invokes this method
as worker cleanup. Consequently cleanup is observationally indistinguishable from
a typed successful provider result: an error payload can record one failure and a
later cleanup success, while an artifact failure after a valid provider result can
record two successes.

The S1 invariant already requires gateway health changes only after typed result
inspection. The owning implementation is
`src/infrastructure/futu_gateway_pool.py`, which was not in the original S1
allowlist.

## Exact scope addition

- Add `src/infrastructure/futu_gateway_pool.py` and
  `tests/test_futu_gateway_pool.py` to S1.
- Replace the cleanup call to `mark_success()` with a direct reset of the
  thread-local consecutive-connection-failure counter after all local gateways
  have been closed.
- Do not change endpoint keys, gateway construction, close thresholds, retries,
  registry behavior, or public method signatures.
- Add a regression proving cleanup closes and clears state without invoking the
  typed provider-success method.

## Success signal

- Provider typed failure: exactly one failure and zero success observations.
- Provider complete result followed by artifact failure: exactly one success and
  zero failure observations.
- Worker cleanup remains idempotent and closes every unique local gateway.

## Residual risk

- Broader gateway lifecycle or retry redesign remains out of scope.
- `PrefetchCoordinator` cannot guarantee one cleanup task per executor thread;
  the enclosing flow still invokes `close_registered()` after shutdown. This
  pre-existing executor-affinity limitation is tracked outside S1 and does not
  weaken the typed provider-outcome acceptance signal above.
