# Gateflow Plan — trade-intake-auth-aware-restart

- Gate: plan
- Work unit: `trade-intake-auth-aware-restart`
- Base: `origin/main` at `0bb5b690`
- Status: proposed

## Goal

Stop the production trade-intake log/reconnect storm when the OpenD trade context reports a phone-verification requirement, while preserving automatic recovery for ordinary transport failures.

## Evidence

- Production on 2026-07-19 repeatedly logs `需要手机验证码`, `RemoteClose`, and `Context status bad` from one long-running trade-intake PID.
- `OpenDTradePushListener.start()` creates and starts the Futu trade context but exposes no health observation, so the SDK can reconnect internally forever without raising into the application loop.
- `_run_listener_source_loop()` only applies reconnect policy to Python exceptions; it cannot distinguish terminal authentication failure from retryable disconnects.
- generated trade-intake systemd service uses `Restart=always` and has no `RestartPreventExitStatus`.
- `src.infrastructure.opend_watchdog.classify_watchdog_result()` already owns OpenD error classification, including `OPEND_NEEDS_PHONE_VERIFY`.

## Behavioral contract

1. The listener observes the already-created trade context's own global state. It must not open a quote context or use quote readiness as evidence of trade authentication.
2. A state/error classified as `OPEND_NEEDS_PHONE_VERIFY` is terminal for this process invocation.
3. Terminal auth produces stable exit status `78` (`EX_CONFIG` semantics) and a status artifact with `status=blocked`, `stage=auth_required`, and the classifier code/message.
4. Generated trade-intake systemd service keeps `Restart=always` for retryable exits but adds `RestartPreventExitStatus=78`.
5. Ordinary listener exceptions remain retryable with bounded exponential backoff: configured `reconnect_sec` is the floor, 60 seconds is the cap, and a successful listener start resets the delay.
6. In multi-source mode, an auth-required result from any source stops sibling source loops and becomes the process exit status.
7. Trade writes, idempotency, receipts, account mapping, and backfill semantics do not change.

## Implementation slices

### Slice 1 — listener-owned trade-auth observation

Owned files:

- `src/application/trades/push_listener.py`
- `tests/test_trades_push_listener.py`

Changes:

- Add a small listener health observation method that calls `get_global_state()` on the existing `OpenSecTradeContext`.
- Normalize Futu `(ret, data)` responses, pass state/error separately to `classify_watchdog_result()`, and preserve its code/message on the terminal exception.
- Raise a dedicated `TradeIntakeAuthRequired` exception only for `OPEND_NEEDS_PHONE_VERIFY`.
- Treat other non-OK responses/exceptions as retryable listener errors.
- Do not classify `OPEND_TRD_NOT_LOGINED` alone as terminal because it can be transient and lacks explicit human-action evidence.

Validation:

- healthy READY/trade-logged-in state;
- phone-verification response raises terminal exception;
- generic disconnect/error remains retryable;
- no new quote context is created.

### Slice 2 — process exit and multi-source propagation

Owned files:

- `src/application/trades/auto_intake.py`
- focused `tests/test_trades_auto_intake_*.py` or a new narrowly named test module

Changes:

- Define/export stable exit code `78` at the trade-intake application boundary.
- Poll listener health immediately after start and then every five seconds, independently of the 60-second heartbeat/backfill status cadence.
- Catch terminal auth separately, close the context, write blocked status, and return `78` without sleeping/retrying.
- Apply capped exponential backoff only to retryable exceptions and reset only after the first healthy trade-context observation.
- Wrap multi-source thread targets with a result queue; the coordinator observes completions while threads are alive, sets the shared stop event immediately on `78`, then joins and returns `78`.

Validation:

- single-source auth returns 78 and performs no retry sleep;
- retryable failure backs off and can recover;
- delay cap is 60 seconds;
- multi-source auth stops siblings and propagates 78;
- status artifact contains stable classifier details.

### Slice 3 — generated systemd lifecycle policy

Owned files:

- `src/application/service_deploy.py`
- `tests/test_service_deploy.py`
- public operations documentation only if the generated unit contract is documented there

Changes:

- Extend the systemd renderer with an optional list of restart-prevent exit statuses.
- Set `[78]` only for `options-monitor-trade-intake.service`.
- Preserve existing unit output for every other service.

Validation:

- trade-intake unit contains `Restart=always` and `RestartPreventExitStatus=78`;
- unrelated long-running and one-shot units do not gain the directive;
- existing service renderer tests continue to pass.

## Validation ladder

1. Focused listener and auto-intake tests.
2. Focused service-deploy tests.
3. Import/lint/static checks used by the repository for changed modules.
4. Broader trade-intake and service-deploy suites.
5. `git diff --check`.
6. Aggregate deepreview against `origin/main`.

## Rollout boundary

This work unit ends at a Draft PR. Merge, release, production unit installation, service stop/start, and production authentication actions require separate CEO authorization.

## Residual risks

- The Futu SDK may expose a phone-verification message only through asynchronous logging and not through `get_global_state()`. Tests can prove our boundary but production canary must confirm the actual SDK response. Owner: production rollout.
- A terminal exit intentionally leaves trade intake stopped until the operator completes authentication and starts/restarts it. Owner: operations runbook/rollout communication.
- Other persistent authentication modes not classified as `OPEND_NEEDS_PHONE_VERIFY` remain retryable. Owner: later evidence-driven work unit; do not broaden terminal classification speculatively.
