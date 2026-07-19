# Plan Review — trade-intake-async-auth-preflight

- Gate: plan review
- Decision: changes required

## Findings

### PR-1 — High — cancelled construction must not enter the ordinary retry loop

If sibling auth sets the shared stop event, `start()` cancellation cannot be represented as a generic `RuntimeError`, because `_run_listener_source_loop` would catch it as retryable, write reconnecting status, and potentially wait. Add a dedicated `TradeIntakeStartCancelled` exception and return cleanly when the stop event is already set.

### PR-2 — High — temporary log handler must be removed on every return path

Multiple listeners and retries make handler leakage multiplicative. The handler registration/removal must be inside `try/finally`, and tests must assert no handler remains after success, auth, exception, and cancellation.

### PR-3 — Medium — abandoned constructor threads must not accumulate after generic timeout

The plan should not introduce a construction timeout that retries while the prior SDK thread is still alive. Wait indefinitely for success/auth/cancellation; the SDK owns transport retries during construction. Only auth or coordinated process shutdown may abandon the daemon thread.

### PR-4 — Medium — log classification must be scoped to trade-context initialization

Filter warning records to messages containing both `init connect fail` and `OpenSecTradeContext` before applying the classifier. Otherwise unrelated Futu warnings in the same process could terminate intake.

## Lenses

Architecture: pass after fixes; SDK adaptation remains in push-listener ownership. Recovery: direct auth is terminal, transport remains SDK-retryable, sibling shutdown is cancellable. Simplicity: one worker thread, one handler, two small exceptions; no subprocess or private SDK monkeypatching.
