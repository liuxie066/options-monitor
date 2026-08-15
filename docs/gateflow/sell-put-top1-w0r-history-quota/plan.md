# Gateflow Plan — Sell Put Top1 W0R History K-Line Quota Boundary

- Gate: `plan`
- Work unit: `sell-put-top1-w0r-history-quota`
- Branch: `feat/sell-put-top1-w0r-history-quota`
- Base: `origin/main@0da901b30cd26242636b9ec967b8aa281f61937c`
- Goal contract: `docs/gateflow/sell-put-top1-w0r-history-quota/goal-confirmation.md`
- Design source: `docs/performance/sell-put-top1-capability-preflight-20260814.md`
- External protocol source: Futu API v10.9 history quota and historical K-line documentation
- Artifact path: `docs/gateflow/sell-put-top1-w0r-history-quota/plan.md`
- Current gate: accepted plan; implementation S1 pending

## 1. Goal, motivation, and completion signal

Implement one behavior-complete source capability: a strict read-only quota fact at the existing Futu gateway boundary plus a dedicated `history_kline` endpoint in the existing OpenD rate-limit configuration owner.

Completion means focused tests, Ruff, dependency-graph check, and `git diff --check` pass; Kimi code, aggregate, and PR DeepReviews have no unresolved accepted finding; and documentation states that the source seam is green but live W0R remains no-go.

## 2. Non-goals and fixed boundary

- No W5 runner, provider orchestration, historical close read, quota decision policy, caller, persistence, receipt schema, hashing, observed timestamp, retry, dedupe, or terminal publication.
- No account fee-plan, calendar, observation, terms-capacity, cadence, or real OpenD evidence.
- No generic provider protocol, registry, repository, factory, service, CLI, Agent tool, timer, migration, or production config edit.
- No claim that W0R, W5, or a real experiment is complete.

## 3. Goal alignment and direct evidence

| Planned decision | Confirmed signal | Direct evidence |
|---|---|---|
| Add `FutuGateway.get_history_kl_quota()` | validated read-only quota fact | SDK method exists; project gateway has zero matching method |
| Strictly validate counts and detail rows | malformed/provider errors fail closed | current gateway maps exceptions through `_raise_mapped()`; SDK response is an external trust boundary |
| Add `OpenDFetchLimits.history_kline` | dedicated endpoint config | current resolver owns three endpoint limits but omits historical K-lines |
| Keep existing fetch/discovery kwargs unchanged | no production candidate-path change | those kwargs are consumed by current tick/candidate fetchers and do not perform W5 history reads |
| Update current capability status only | source green/live unknown, W0R no-go | preflight separates SDK, project, and live evidence |

The official provider maximum is 60 first-page historical K-line requests per 30 seconds; later pages are exempt. The default uses `max_calls=60`, `window_sec=30.0`, and the existing snapshot-style `max_wait_sec=30.0`. Scheduling headroom and production coexistence remain future runner/readiness responsibilities, not invented here.

## 4. Affected files and ownership

Production:

- `src/infrastructure/futu_gateway.py` — external SDK protocol adaptation and error mapping.
- `src/application/opend_fetch_config.py` — OpenD endpoint rate-limit defaults/resolution.

Tests/docs:

- `tests/test_futu_gateway_minimal.py` — fake quote-provider contract tests.
- `tests/test_opend_batch_config.py` — default and canonical override resolution.
- `tests/test_global_liquidity_filters.py` — extend the existing config-validator acceptance fixture to include the canonical endpoint.
- `docs/performance/sell-put-top1-capability-preflight-20260814.md` — append/update current source capability without changing live status.
- Gateflow/review artifacts under `docs/gateflow/sell-put-top1-w0r-history-quota/` and `docs/reviews/`.
- Dependency graph artifacts only if the existing generator reports a real change.

## 5. Public contract and protocol handling

Add a zero-argument public gateway method:

```text
get_history_kl_quota() -> {
  used_quota: int,
  remain_quota: int,
  detail_list: [{code: str, request_time: str}, ...]
}
```

Rules:

1. Always call the SDK with `get_detail=True`; callers cannot weaken the fact to aggregate-only data.
2. Require an SDK tuple `(ret, payload)`, `ret == 0`, and a three-item payload `(used_quota, remain_quota, detail_list)`.
3. Counts must be non-boolean integral values greater than or equal to zero.
4. `detail_list` must be a list/tuple of dictionaries with non-empty trimmed `code` and `request_time`; normalize code to uppercase, omit `name`, and require `request_time` to parse and round-trip exactly as `%Y-%m-%d %H:%M:%S` using the already imported stdlib `time` module.
5. Duplicate codes or a detail count different from `used_quota` make the fact unusable. Sort normalized details by `(code, request_time)` for deterministic downstream hashing.
6. Any violation or provider error is raised through `_raise_mapped(action="get_history_kl_quota")`; no partial/default quota fact is returned.

This is an in-memory gateway response, not a new stored receipt. A future W5 runner owns observation time, compact persistence, hashing, authorization policy, and any additional binding.

## 6. Rate-limit configuration contract

- Add canonical endpoint key `history_kline`; do not add speculative aliases.
- Add `OpenDFetchLimits.history_kline` and include it in `as_config()`.
- Resolve its canonical config under `runtime.opend_rate_limits.history_kline` with defaults `60 / 30s / 30s wait`.
- Do not add history fields to `fetch_kwargs()`, `discovery_kwargs()`, or `OPEND_FETCH_KWARG_KEYS`; those are existing candidate-fetch call signatures.
- `OpenDFetchLimits.from_flat_kwargs()` constructs the history field from defaults only; the future W5 caller must use the canonical config resolver rather than expanding legacy flat kwargs.
- The existing config validator automatically accepts the new canonical key through `OPEND_RATE_LIMIT_ENDPOINT_KEYS`; one existing acceptance fixture will prove it.

No state-machine or storage change exists in this slice.

## 7. Implementation slice S1 — strict quota and limiter boundary

Objective: make the project-side history quota/limiter seam usable and fail closed, without adding a consumer.

Allowed changes are exactly those in §§4–6.

Required assertions:

- success calls `get_detail=True`, strips `name`, canonicalizes/sorts detail facts, and returns exact keys;
- SDK nonzero return maps to `FutuGatewayError`;
- missing/wrong payload shape, missing detail fields, malformed or impossible request timestamps, boolean/non-integral/negative counts, duplicate codes, and count/detail mismatch all fail closed;
- default resolver returns `history_kline={max_calls:60, window_sec:30.0, max_wait_sec:30.0}`;
- canonical override round-trips and config validation accepts `history_kline`;
- existing three endpoints and existing fetch/discovery kwargs remain unchanged;
- no test imports/starts OpenD or writes runtime state.

Validation commands:

```bash
<options-monitor-venv>/bin/python -m pytest -q -p no:cacheprovider tests/test_futu_gateway_minimal.py tests/test_opend_batch_config.py tests/test_global_liquidity_filters.py
<options-monitor-venv>/bin/python -m ruff check src/infrastructure/futu_gateway.py src/application/opend_fetch_config.py tests/test_futu_gateway_minimal.py tests/test_opend_batch_config.py tests/test_global_liquidity_filters.py
<options-monitor-venv>/bin/python scripts/generate_dependency_graph.py --check
git diff --check
```

Expected outcome: all focused checks pass and the capability document reports only `SDK/project source green; live unknown`.

Stop condition: if implementation requires a caller, stored receipt schema, production configuration, live OpenD evidence, or a policy for deciding sufficient remaining quota, return to goal confirmation instead of expanding scope.

## 8. Documentation decision, risks, and completion report

Update the capability preflight because its current source-status claim would otherwise be stale. Do not update operator/product docs because there is no public command or runtime behavior.

Classified residual risks:

- Live quota receipt and OpenD availability: assigned to a separately authorized W0R live probe.
- Quota sufficiency policy, unique-owner demand calculation, receipt persistence, and provider retry/dedupe: assigned to the remaining W5 runner.
- Coordination with production historical-K-line traffic and bounded runtime: assigned to remaining W5/W7 readiness work.
- Account fee-plan, calendar, exact-expiration close/terms, observation, and capacity evidence: assigned to their later W0R work units.

Completion report must list changed files, validation evidence, review finding status, draft PR URL, and these residual owners. It must state that no OpenD/provider/account call, production config write, release, deployment, or real experiment occurred.
