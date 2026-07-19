# Gateflow Hotfix Plan — trade-intake-async-auth-preflight

- Gate: plan
- Base: `origin/main` at v1.2.416 (`415c8a45`)
- Production evidence: v1.2.416 canary failed; trade-intake is intentionally stopped.
- Status: proposed

## Goal

Detect OpenD phone-verification failure before the Futu SDK's synchronous `OpenSecTradeContext` constructor can trap the application forever, then preserve the v1.2.416 exit-78/systemd contract.

## Revised evidence

- Futu `OpenContextBase.__init__` loops forever around `_init_connect_sync()` while auto-reconnect is enabled.
- `OpenSecTradeContext` does not expose `is_async_connect`; therefore application code never reaches `listener.check_health()` during phone verification.
- An async `OpenQuoteContext` experiment emitted the auth warning but its synchronous query/close also blocked, so quote preflight is not a safe owning boundary.
- The SDK emits the direct trade-context failure through the `FTConsoleLog` warning stream before retrying.

## Behavioral contract

1. Construct the real trade context on a daemon worker thread while the caller remains able to observe cancellation and SDK warning records.
2. Attach a temporary logging handler to the SDK's `FTConsoleLog` logger during construction and feed warning text into the existing `classify_watchdog_result()`.
3. `OPEND_NEEDS_PHONE_VERIFY` raises the existing `TradeIntakeAuthRequired` immediately; main returns 78 and the process exits, terminating the daemon constructor thread.
4. Successful construction transfers the context/handler to the listener and preserves normal push behavior.
5. A shared multi-source stop event cancels sibling construction waits so source coordinator shutdown is bounded.
6. Constructor exceptions other than auth remain retryable under the existing capped application backoff. There is no construction timeout/retry while a prior constructor worker is alive.
7. Do not create a quote context, parse journald/files, monkeypatch Futu internals, or call `os._exit`.

## Slice 1 — cancellable construction monitor

Files:
- `src/application/trades/push_listener.py`
- `src/application/trades/auto_intake.py`
- `tests/test_trades_push_listener.py`
- `tests/test_trades_auto_intake_restart_policy.py`

Changes:
- make `OpenDTradePushListener.start()` accept an optional cancellation event;
- run `_build_default_context()` on a daemon thread and return its result through a queue;
- temporarily capture only `init connect fail` records for `OpenSecTradeContext`, classify them, and remove the handler in `finally` on every path;
- check cancellation while waiting and raise a dedicated `TradeIntakeStartCancelled`;
- pass the shared source-loop stop event into `listener.start()`.

Tests:
- blocking constructor emits phone-verification warning and start raises terminal auth within a bounded time;
- successful constructor remains compatible;
- cancellation exits a blocked construction wait;
- retryable constructor exception remains retryable;
- multi-source sibling cancellation remains bounded.

## Slice 2 — release/rollout verification

No application behavior expansion. After review and merge:
- release v1.2.417;
- upgrade production;
- explicitly render/install the generated unit because update reconciliation has not refreshed unit content in prior rollouts;
- start trade-intake once;
- prove exit status 78, `RestartPreventExitStatus=78`, no restart, blocked status artifact, and stable journal growth.

## Validation

- focused listener and restart-policy tests;
- full trade-intake/watchdog/service-deploy regression bundle;
- full pytest and smoke before release;
- production canary after explicit unit installation.

## Residual risks

- SDK logger name/level could change in a future futu-api version: dependency-version-owned, with regression test around the current adapter contract.
- daemon constructor threads are intentionally abandoned only when the whole process is about to exit or coordinated shutdown is underway; they must never accumulate during ordinary retry loops.
