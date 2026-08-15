# Gateflow Plan — Sell Put Top1 W0R Exact-Expiration Close Boundary

- Gate: `plan`
- Work unit: `sell-put-top1-w0r-exact-expiry-close`
- Branch: `feat/sell-put-top1-w0r-exact-expiry-close`
- Base: `origin/main@813ec6f8021148ff6d152ff4ee4f5c39e36897fc`
- Goal contract: `docs/gateflow/sell-put-top1-w0r-exact-expiry-close/goal-confirmation.md`
- Design source: `docs/performance/sell-put-top1-capability-preflight-20260814.md`
- External protocol source: installed Futu API v10.9 `request_history_kline()` implementation and contract
- Artifact path: `docs/gateflow/sell-put-top1-w0r-exact-expiry-close/plan.md`
- Current gate: accepted plan; implementation S1 pending

## 1. Goal, motivation, and completion signal

Implement one behavior-complete read boundary: ask OpenD for exactly one underlier's unadjusted daily close on exactly one expiration date, then return only a code/date-bound positive close fact.

Completion means focused fake-provider tests, adjacent history/research regression, Ruff, dependency-graph check, and `git diff --check` pass; Kimi code, aggregate, and PR DeepReviews have no unresolved accepted finding; and documentation states that only project source capability is green while live W0R remains no-go.

## 2. Fixed boundary and non-goals

- No W5 runner, receipt construction/storage, observed timestamp, hashing, dedupe, retry, quota decision, rate-limit use, scheduler, deadline, or publication.
- No calendar, terms, observation, account fee-plan, production config, or real provider evidence.
- No symbol registry or gateway-side domain-symbol conversion. The future runner must call the existing `futu_underlier_code()` owner before this method and rebind the result to its domain `stock_owner`.
- No change to generic `request_history_kline()` or its QFQ consumer.
- No provider protocol/interface, repository, service, CLI, Agent tool, migration, release, deployment, or live experiment.

## 3. Goal alignment and direct evidence

| Planned decision | Confirmed signal | Direct evidence |
|---|---|---|
| Add a dedicated gateway method | exact validated source fact | existing generic method returns permissive raw data and has a QFQ production caller |
| Force one date and unadjusted daily bars | fact matches research semantics | pure evaluator requires `K_DAY`, `NONE`, `close` |
| Bind code/date and reject pagination/duplicates | no ambiguous close can enter research | SDK returns provider code, `time_key`, and a continuation key |
| Return compact in-memory fact or `None` | no premature receipt/storage design | future W5 runner owns receipt status, timing, and persistence |
| Update source capability only | no false readiness claim | no live OpenD call or runtime capacity evidence is authorized |

## 4. Affected files and ownership

Production:

- `src/infrastructure/futu_gateway.py` — exact SDK request, response validation, normalization, and existing error mapping.

Tests/docs:

- `tests/test_futu_gateway_minimal.py` — fake quote-provider success, absence, and failure contract.
- `docs/performance/sell-put-top1-capability-preflight-20260814.md` — update only this source capability and preserve live/overall status.
- Gateflow/review artifacts under `docs/gateflow/sell-put-top1-w0r-exact-expiry-close/` and `docs/reviews/`.
- Dependency graph artifacts only if the existing check proves a real import-graph change.

## 5. Public gateway contract

Add one method:

```text
get_exact_expiration_close(*, code: str, expiration: str)
  -> {code: str, expiration: str, close: float} | None
```

Input rules:

1. `code` must be non-empty text; trim and normalize it to uppercase Futu wire text. The gateway does not translate aliases or domain symbols.
2. `expiration` must be exact canonical `YYYY-MM-DD` text, round-trip through the stdlib calendar parser, and represent a real date.
3. Input validation occurs before quote-client acquisition. Invalid input is mapped with action `get_exact_expiration_close` and cannot connect to OpenD.

Request rules:

1. Call the quote SDK directly through the existing quote-client owner, not through the permissive generic method.
2. Reuse `_normalize_history_kline_kwargs()` to send:
   - `code=<normalized code>`;
   - `start=end=<expiration>`;
   - `ktype=K_DAY`;
   - `autype=NONE`;
   - `fields=[time_key, close]`;
   - `max_count=2` and initial `page_req_key=None`.
3. `max_count=2` is only a cardinality guard: zero rows is absence, one row is usable after validation, and two rows or any continuation proves ambiguity/incompleteness. It is not a pagination loop.

Provider response rules:

1. Require an exact three-item tuple `(ret, data, page_req_key)`.
2. `ret` must be a non-boolean integral zero. A nonzero provider return raises using its payload; malformed return values fail closed.
3. A continuation key must be `None`, empty text, or empty bytes. Any non-empty key means the exact-date result is incomplete and fails closed.
4. Require SDK DataFrame-like data with callable `to_dict(orient="records")` and a `columns` collection containing `code`, `time_key`, and `close`; allow provider extras such as `name`. Plain list/tuple/dictionary data is malformed, including an empty list. Do not reuse `_FutuAPIClient._rows()` because it silently drops malformed entries.
5. `to_dict(orient="records")` must return a list and every materialized row must be a dictionary. Only a correctly shaped frame with zero rows returns `None`; missing columns/data, malformed materialization, or more than one row fails closed.
6. The sole row's `code` must be non-empty text whose trimmed uppercase form equals the requested normalized code.
7. `time_key` must be canonical provider text in either `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS` form, must round-trip exactly, and its calendar date must equal `expiration`. These are the only accepted daily-bar representations; arbitrary suffixes and whitespace fail closed.
8. `close` must be a non-boolean real number that is finite and greater than zero. Numeric strings, zero, negatives, NaN, and infinities fail closed.
9. Drop provider-only fields such as `name`; return exactly normalized `code`, requested `expiration`, and `float(close)`.
10. Every provider-shape/value violation is raised through `_raise_mapped(action="get_exact_expiration_close")`; no partial/default fact is returned.

`None` means only “the provider successfully returned a complete, valid empty result.” It does not mean holiday, delayed availability, permanent absence, or a research outcome. The future runner owns that classification and its deadline.

## 6. Implementation slice S1 — strict exact-expiration close source

Objective: make the source fact usable and fail closed without adding a consumer.

Allowed changes are exactly those in §§4–5.

Required assertions:

- success proves exact date range, `K_DAY`, `NONE`, selected `time_key/close`, `max_count=2`, initial page, code/date binding, provider-field dropping, and compact return;
- a complete empty DataFrame with all required columns returns `None`;
- invalid code/date fails before provider acquisition;
- wrong tuple length, boolean/non-integral/nonzero ret, non-empty continuation, missing data/columns, plain list/tuple data, malformed `to_dict`, non-list materialization, non-dictionary row, multiple rows, code mismatch, malformed/noncanonical/wrong date, and invalid close all fail closed;
- provider 2FA/auth/rate-limit/transient errors retain the existing mapped gateway class rather than becoming usable data;
- generic `request_history_kline()` behavior and the QFQ short-vol path remain unchanged;
- no test imports/starts OpenD or writes runtime state.

Focused validation:

```bash
<options-monitor-venv>/bin/python -m pytest -q -p no:cacheprovider tests/test_futu_gateway_minimal.py
<options-monitor-venv>/bin/python -m pytest -q -p no:cacheprovider tests/test_short_vol_metrics.py tests/test_strategy_lab_top1_research.py
<options-monitor-venv>/bin/python -m ruff check src/infrastructure/futu_gateway.py tests/test_futu_gateway_minimal.py
<options-monitor-venv>/bin/python scripts/generate_dependency_graph.py --check
git diff --check
```

Stop condition: if implementation requires a runtime caller, receipt schema, stored artifact, production configuration, live OpenD evidence, or a policy for timing/absence, return to goal confirmation instead of expanding scope.

## 7. Documentation decision, risks, and completion report

Update the capability preflight because its source-status claim would otherwise be stale. Do not update operator/product docs because there is no public command or runtime behavior.

Residual owners:

- Domain symbol to Futu-code conversion, call timing, quota/rate-limit use, retries, deadline, absence reasons, receipt construction/sealing, dedupe, and publication: remaining W5 runner.
- Live exact-expiration receipt and capacity/duration evidence: separately authorized W0R live probe.
- Account fee-plan, calendar, terms, observation, and overall readiness: their remaining W0R/W7 work units.

Completion report must list changed files, validation evidence, review finding status, and draft PR URL. It must state that no OpenD/provider/account call, production config write, release, deployment, or real experiment occurred.
