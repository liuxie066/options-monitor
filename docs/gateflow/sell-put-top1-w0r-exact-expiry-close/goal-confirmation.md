# Gateflow Goal Confirmation — Sell Put Top1 W0R Exact-Expiration Close Boundary

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w0r-exact-expiry-close`
- Confirmed by user: 2026-08-15
- Branch: `feat/sell-put-top1-w0r-exact-expiry-close`
- Base: `origin/main@813ec6f8021148ff6d152ff4ee4f5c39e36897fc`
- Artifact path: `docs/gateflow/sell-put-top1-w0r-exact-expiry-close/goal-confirmation.md`
- Decision: accepted

## Goal and motivation

Add the smallest read-only project boundary that can fetch one underlier's unadjusted daily close on one exact expiration date for a future Sell Put Top1 research runner. The boundary must bind the result to the requested Futu code and expiration and reject incomplete, ambiguous, or malformed provider responses.

The installed Futu SDK already supports `request_history_kline()` with `K_DAY`, `AuType.NONE`, selected fields, and pagination. The project currently exposes only a permissive generic wrapper whose production consumer requests QFQ history; reusing or tightening that path would either leave the research fact unvalidated or risk changing existing behavior.

## Success signals

- `FutuGateway` exposes one dedicated exact-expiration close method without changing the existing generic history method.
- The method forces `start=end=expiration`, `K_DAY`, `NONE`, and only `time_key/close`, then returns either one compact validated fact or explicit successful absence.
- Wrong tuple shape/return code, incomplete pagination, malformed rows, multiple rows, code/date mismatch, or non-positive/non-finite close fail closed through the existing gateway error boundary.
- Fake-provider tests prove the request and failure contract without importing, starting, or connecting to OpenD.
- The capability preflight records project source capability green and live evidence unknown while keeping overall `W0R runtime_no_go`.

## Scope boundary and non-goals

This work unit does not implement:

- the W5 runner, research-close receipt schema, persistence, hashing, dedupe, retry, scheduling, deadline policy, or publication;
- `stock_owner` to Futu-code orchestration, unavailable-reason classification, calendar checks, terms capture, observation capacity, or fee-plan evidence;
- a provider interface, registry, repository, service, CLI, Agent tool, timer, migration, or production configuration;
- a live OpenD call, account query, release, deployment, notification, trade, or ledger write.

## Direct code evidence

- `src/infrastructure/futu_gateway.py::request_history_kline()` accepts permissive response shapes and returns raw data.
- `src/application/short_vol_metrics.py::_fetch_qfq_history_rows()` consumes that generic method with `autype=QFQ`; its behavior must remain unchanged.
- `src/application/strategy_lab/top1/research.py` already requires future close receipts to identify `opend_history_kline`, `K_DAY`, `NONE`, and `close`.
- The installed SDK returns `(ret, DataFrame, page_req_key)` and supports selected fields and `max_count`.
- `docs/performance/sell-put-top1-capability-preflight-20260814.md` currently marks the unadjusted exact-expiration close boundary red/unknown.

## Parsimony decision

Add one gateway method and its fake-provider check. Reuse `_normalize_history_kline_kwargs()` and `_raise_mapped()`; do not add another client abstraction or stored receipt. The future W5 runner remains responsible for converting domain symbol identity, deciding when a missing close is terminal, and wrapping the fact in the already designed research receipt.

## Blocking open questions

None. The user confirmed this exact work-unit boundary.

## Next gate

`plan`
