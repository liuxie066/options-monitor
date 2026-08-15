# Gateflow Goal Confirmation — Sell Put Top1 W0R History K-Line Quota Boundary

- Gate: `goal confirmation`
- Work unit: `sell-put-top1-w0r-history-quota`
- Confirmed by user: 2026-08-15
- Branch: `feat/sell-put-top1-w0r-history-quota`
- Base: `origin/main@0da901b30cd26242636b9ec967b8aa281f61937c`
- Artifact path: `docs/gateflow/sell-put-top1-w0r-history-quota/goal-confirmation.md`
- Decision: accepted

## Goal and motivation

Add the smallest read-only project boundary needed for a future W5 runner to inspect Futu historical K-line quota and resolve a dedicated `history_kline` rate limit before any historical close call. Malformed provider facts must fail closed.

The installed Futu SDK already exposes `OpenQuoteContext.get_history_kl_quota(get_detail=True)`, but the project gateway has no equivalent method and `OpenDFetchLimits` has no `history_kline` endpoint. The current W0R preflight therefore records this source capability as red.

## Success signals

- `FutuGateway` calls `get_history_kl_quota(get_detail=True)` and returns only validated, normalized quota facts.
- Non-integral, boolean, negative, missing, malformed, or provider-error quota responses are mapped through the existing gateway error boundary and never become usable facts.
- `OpenDFetchLimits` resolves a dedicated `history_kline` endpoint using Futu's documented 60 first-page requests per 30 seconds limit, without changing existing candidate-fetch kwargs.
- Focused fake-provider tests prove success and failure behavior without importing or connecting to OpenD.
- The capability preflight records `SDK/project source green; live unknown` for this narrow seam while keeping overall `W0R runtime_no_go`.

## Scope boundary and non-goals

This work unit does not implement:

- the W5 provider runner, exact-expiration close collector, receipt persistence, dedupe, authorization, or research publication;
- calendar, observation, exact-expiration terms capacity, account fee-plan evidence, or a real provider receipt;
- a CLI, Agent tool, timer, service, database/schema, provider interface, registry, factory, or workflow framework;
- production configuration changes, OpenD startup/calls, account queries, release, deployment, notification, trading, or ledger writes.

## Direct code evidence

- `src/infrastructure/futu_gateway.py` exposes `request_history_kline()` but has no `get_history_kl_quota()` project seam.
- `src/application/opend_fetch_config.py` resolves only `option_chain`, `market_snapshot`, and `option_expiration`.
- `docs/performance/sell-put-top1-capability-preflight-20260814.md` marks the history K-line quota boundary `red / unknown` and keeps provider-dependent research forbidden.
- Futu's v10.9 API contract returns `(used_quota, remain_quota, detail_list)` and documents a 60-request/30-second first-page limit for historical K-lines.

## Parsimony decision

Reuse the existing `FutuGateway` error mapping and `OpenDEndpointRateLimit` resolver. Add no stored receipt schema or caller because no approved caller exists yet. Drop the provider-only security name from the normalized detail facts; future authorization needs the canonical code and request time, not duplicated display data.

## Blocking open questions

None. The user confirmed this exact work-unit boundary.

## Next gate

`plan`
