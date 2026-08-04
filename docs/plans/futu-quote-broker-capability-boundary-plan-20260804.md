# Futu quote/broker capability boundary and measured shared-quote plan

- Revision: 2
- Prepared: 2026-08-04
- Status: plan only; this artifact does not authorize source implementation, runtime config writes, release, remote upgrade, service restart, OpenD restart, Incus mutation, broker write, notification, or Futu state migration
- Predecessor review: `docs/reviews/plan-review-20260804-161125.md`
- Related incident plan: `docs/plans/incus-opend-reclaim-remediation-plan-20260804.md`

## 1. Decision summary

Adopt the direction “generic market facts use a shared quote plane; account facts use per-account broker planes”, but implement it as a measured, capability-specific source boundary rather than as an immediate OpenD/HOME migration.

This revision makes nine binding decisions:

1. The primary goal of this work is to remove quote/trade client coupling and make endpoint use observable. Resource relief is a hypothesis to measure, not a promised outcome.
2. “Shared quote” means that the already-authoritative effective `symbols[].fetch` Futu bindings converge to one physical `(host, port)`. No `quote_owner`, `quote_account`, or second host/port authority is added.
3. New multi-account broker endpoint authority remains `account_settings.<account>.futu`. A sole Futu account may use the existing legacy `portfolio.futu`/fetch projection with an explicit compatibility marker; multiple Futu accounts may not share that fallback. Account names never select a quote endpoint.
4. Quote and broker SDK contexts become independently lazy and independently ready. A broker-only path must not construct `OpenQuoteContext`; a quote-only path must not construct `OpenSecTradeContext`.
5. Callers that need both account facts and market facts receive two explicit gateways. They may point to the same physical OpenD, but their authority and readiness remain distinct.
6. “交易分开” in this plan means per-account broker endpoint/identity and client capability. It does not mean separate listener processes or a new capture/apply state machine.
7. Trade-listener topology, per-account HOME/`Device.dat`, systemd OpenD topology, and Incus capacity remain separate work units with separate authorization.
8. The broker authority for one logical account is a binding set across all selected runtime configs: every Futu member must use the same physical endpoint and trade environment, while all configured account IDs form a required union with config/market provenance. Readiness must observe every required ID, not merely one candidate.
9. In auto-intake, quote routing/readiness is a lazy auxiliary dependency. Broker listener startup and `_process_payload()` ordering remain unchanged; quote failure degrades lifecycle timing/observation only. Only the explicit lifecycle reconciliation CLI may preflight quote and broker dependencies before its own apply phase.

The initial two-process topology may still be `lx@127.0.0.1:11111` serving the converged quote route plus lx broker facts and `sy@127.0.0.1:11112` serving sy broker facts. That is logical routing isolation, not OS-, credential-, or least-privilege isolation.

Revision 2 closes the three predecessor findings narrowly: PR-01 through the cross-config binding set and all-required identity check; PR-02 through the entrypoint/failure matrix that freezes current intake ordering; PR-03 by retaining the assigned-stock diagnostic override and specifying exact health/tool schema, documentation, and regression owners.

## 2. Evidence and corrected causal model

### Current source facts

- `_FutuAPIBackend._ensure_clients()` currently creates both `OpenQuoteContext` and `OpenSecTradeContext` on the first access to either capability.
- `_FutuAPIClient._quote()` and `_trade()` both call that coupled method.
- `build_ready_futu_gateway()` always runs `ensure_quote_ready()`, including for portfolio callers that only need balance or positions.
- `fetch_futu_portfolio_context()` therefore treats quote login as a prerequisite for account facts and constructs a quote client on the account endpoint.
- The direct trade listener and history backfill already construct only `OpenSecTradeContext`; they do not use the coupled gateway and are not a target for redesign in this work.
- `OpenDOptionPositionAdapter` is a real mixed caller: it reads broker positions, then uses quote trading days and market snapshots through one account-bound gateway.
- lifecycle timing and settlement observation are also mixed: account history/positions/cash are broker facts, while `request_trading_days` is a market-calendar query.
- account health currently runs the quote-oriented Futu doctor, including option-chain/snapshot checks, against each account endpoint. That would continue to activate quote work on a broker-only endpoint unless health is capability-aware.
- required-data planning already identifies physical fetch work by symbol, source, host, and port. That existing binding remains the quote authority.

### Supported and unsupported performance claims

Supported:

- OM currently has source-level paths that can create or use quote clients on account endpoints even when the primary operation is an account/broker read.
- Removing that coupling is independently useful for failure attribution: quote login failure must not block a broker read, and trade login failure must not block generic quote collection.

Not yet supported:

- That an idle OpenD with no OM quote client will release its large mmap working set.
- That moving generic quote requests away from `sy` will materially reduce the observed NVMe refault traffic.
- That shared HOME caused the incident or that independent HOME directories would reduce memory or IO.
- That two OpenD processes provide a security boundary merely because OM routes capabilities separately.

Therefore source completion and incident closure are separate outcomes. The source boundary can be correct while the host incident remains open.

## 3. Authority and capability contract

| Fact or operation | Capability | Sole endpoint authority | Failure rule |
|---|---|---|---|
| option chain, expiration discovery, snapshot, kline, event data, quote field doctor | `quote` | effective `symbols[].fetch.{source,host,port}` | preserve provider/binding; no account-endpoint fallback |
| market trading calendar used by a market-data flow | `quote` | unique effective Futu fetch binding for that runtime market | missing/conflicting binding is explicit unavailable |
| balance, funds, positions, order/deal lists, history, cash flow | `broker` | resolved account broker binding set: per-account Futu settings, or sole-account legacy projection | fail closed on cross-config drift, trade login, or any missing required identity |
| deal push and history backfill transport | `broker` | existing trade-intake source for the account | unchanged direct trade context and state machine |
| position quality evidence | `mixed` | broker gateway for positions; quote gateway for calendar/multiplier evidence | no silent reuse of broker endpoint for quote facts |
| lifecycle timing/settlement evidence | `mixed` | broker gateway for account receipts; quote gateway for calendar receipt | after intake apply, quote failure preserves the case and follows existing `needs_review`/incomplete-receipt semantics; it does not stop listener transport |
| OpenD readiness | typed `quote` or `broker` | same authority as the capability being checked | one capability cannot satisfy or block the other |

### Shared-route derivation

Add one pure, read-only resolver owned by the application configuration boundary, tentatively `src/application/futu_quote_routing.py`. It must reuse the existing template/watchlist resolution helpers and produce an inventory, not a new configuration source.

For every selected runtime config and market it records non-secret provenance:

```json
{
  "config_key": "us",
  "market": "US",
  "symbol": "NVDA",
  "source": "futu",
  "host": "127.0.0.1",
  "port": 11111
}
```

Rules:

1. Resolve profiles/templates before reading `fetch`; do not inspect only raw YAML/JSON rows.
2. Preserve non-Futu providers and symbol-specific overrides; they are not coerced into OpenD.
3. Apply the existing canonical Futu fetch defaults (`127.0.0.1:11111`) in one shared normalization helper, then normalize host case and port type. This extracts the semantics already used by required-data planning; it is not a new fallback and never derives an endpoint from an account.
4. For a market-level mixed caller, a canonical Futu quote route exists only when all applicable effective Futu bindings in that runtime market resolve to exactly one `(host, port)`.
5. A globally shared Futu quote route exists only when every selected market-level route resolves and all of them are the same physical endpoint.
6. Zero bindings, an invalid endpoint after canonical normalization, or more than one physical endpoint return structured `missing` or `conflict`; the quote operation fails closed and health reports the conflict. A mixed auto-intake caller contains that failure according to §8.4 instead of failing its broker transport.
7. There is no caller-local fallback to `11111`, the first symbol, the first account, or the broker endpoint.

Ordinary scheduled/application flows must use this derived authority. The existing read-only `option_positions_read(action=assigned-stock)` inputs `opend_host` and `opend_port` remain a supported, ephemeral diagnostic override rather than becoming equality assertions. With neither input, assigned-stock refresh uses the canonical fetch binding. With both, the explicit endpoint is used even when it differs from the canonical route. With only one, that coordinate overlays the canonical route; if the canonical route cannot supply the other coordinate, refresh returns structured unavailable. Any explicit use is labeled `quote_refresh.route_source=explicit_diagnostic_override`, is never persisted, is never used by maintenance/scheduled flows, and is excluded from shared-route acceptance windows. Other explicitly named low-level diagnostic/research probes may retain the same labeled override rule. Canonical routes use `route_source=canonical_fetch_binding`.

The local `config.us.json` and `config.hk.json` snapshots currently each show one Futu symbol-fetch endpoint at `127.0.0.1:11111`. That is local evidence only; production rendered configs must be inventoried again at the separately authorized pre-apply gate.

### Broker-binding derivation and compatibility

Add or consolidate pure account broker-binding resolvers at the existing account configuration boundary. The single-config resolver returns one member. A thin aggregate resolver, tentatively `resolve_account_broker_binding_sets(...)`, accepts the complete selected runtime-config sequence and returns one private `ResolvedAccountBrokerBindingSet` per logical account:

```text
account
host, port, trd_env
required_account_ids            # lossless set; masked outside the resolver/readiness boundary
members[]                       # config_key, market, authority_source, account_ids
compatibility_warnings[]
```

This is derived validation evidence, not a new persisted entity or configuration source. P0 and Gate F must call the aggregate resolver with the exact US/HK rendered configs selected for that deployment. A single-market runtime caller consumes the corresponding validated member, but a production apply cannot pass merely because its members validate independently.

Make that gate executable by extending the existing read-only `./om config validate` command with repeatable `--related-config-path <runtime.json>` for runtime-source validation. The existing `--config-path` remains the primary config and all current output fields retain their meanings; when related paths are supplied, add a non-secret `futu_routing_audit` containing config/market provenance, quote-route status, broker binding-set status, masked/count-only identities, and errors. Validate every file first, reject duplicate paths/markets, call the aggregate resolver once, and exit nonzero on any quote-route or broker-binding conflict. The option is invalid with `--source yaml`; it performs no OpenD/provider call and no write. This reuses the existing validation facade instead of adding a new tool or workflow. Its owners are `src/interfaces/cli/config_ops.py`, the application resolver, `tests/test_config_yaml.py`, and `docs/AGENT_WIKI.md`. Gate F records this command and its sanitized output.

The additive audit shape is fixed as:

```text
futu_routing_audit:
  schema_version: futu_routing_audit.v1
  ok: bool
  configs[]: {config_path: masked string, config_key: string|null, market: string}
  quote: {status: ok|missing|conflict, host: string|null, port: int|null, member_count: int}
  broker_accounts[]: {account, status, host, port, trd_env, required_account_id_count, masked_required_account_ids[], members[]}
  trade_intake_sources[]: {source_id, account, status, host, port, trd_env, required_account_id_count}
  errors[]: {code, scope, message}
  warnings[]: string
```

Members contain only config key, market, authority source, and masked/count-only identity evidence. On conflict the canonical host/port fields are null and conflicting endpoints appear only in scoped error details; raw account IDs and secrets never appear.

Rules:

1. A complete `account_settings.<account>.futu` binding is authoritative for that account.
2. Existing compatibility fields may fill a binding only when the selected runtime config contains exactly one `type=futu` account. Mark the result `authority_source=legacy_single_futu_projection` and report a compatibility warning.
3. With two or more Futu accounts, each account must have an explicit complete per-account binding. Missing host, port, account ID, or trade environment is an error; do not borrow `portfolio.futu` or a symbol fetch endpoint.
4. `resolve_futu_account_ids()` may retain its current legacy trade-mapping fallback only under the same sole-Futu-account rule. Multi-account ambiguity fails before client creation.
5. Account type `external_holdings` never receives a broker binding.
6. The resolver, not `infer_futu_portfolio_settings()` caller order, owns compatibility precedence. New broker callers consume the typed result.
7. For one logical account, absence from a selected config or presence there only as `external_holdings` contributes no broker member. Every config where the account is `type=futu` does contribute a member and provenance.
8. All contributing members must normalize to exactly the same `(host, port)` and `trd_env`; endpoint or environment drift is a structured conflict. Compatibility-source differences may warn but cannot change this equality rule.
9. Within each member, every losslessly normalized ID returned by `resolve_futu_account_ids()` is required. Across members, `required_account_ids` is the union, not the first, intersection, or an alternatives list. Different market-scoped ID sets are therefore allowed only when their complete union is verified; an empty set or lossy ID is an error.
10. Resolve the existing trade-intake sources during the same audit. For every enabled Futu source, `(host, port)` must equal the applicable account binding, and the source IDs attributed through its account mapping must equal that member's required ID set. The current direct listener/backfill uses `REAL` explicitly, so a trade-intake-enabled member must also resolve `trd_env=REAL`; `SIMULATE` is permitted only when direct trade intake for that member is disabled. A mismatch blocks validation rather than changing listener transport semantics in this work unit.
11. When two or more logical Futu accounts are selected, their normalized configured `(host, port)` values must be pairwise distinct; otherwise the intended per-account broker-plane topology is not present and routing audit fails. Because host aliases can still address one listener, P0/Gate F must additionally map each broker endpoint to a distinct listening OpenD PID. The canonical quote endpoint may equal one broker endpoint/PID, as in the initial lx topology, without merging the two capability authorities.

This source work does not rewrite config. Any production multi-Futu config that fails these rules blocks rollout and requires a separately authorized config correction. Current local generated snapshots demonstrate the legacy host/port projection shape but do not contain enough evidence to prove complete broker readiness; they are compatibility fixtures, not production authority.

### Broker identity readiness

Broker readiness is read-only and does not unlock trading. For the binding set (or its single-config projection) it requires:

1. a reachable OpenD and `program_status_type=READY`;
2. `trd_logined=true` from `get_global_state()` on the trade context;
3. `get_acc_list()` success;
4. lossless normalization of every returned `acc_id` and environment used for comparison; and
5. `required_account_ids` is a subset of the IDs observed in `get_acc_list()` rows for the requested `trd_env`.

Additional accounts on the same OpenD are not an error. A required ID missing entirely, present only under another environment, duplicated ambiguously after lossy normalization, or otherwise not comparable is an error. Readiness exposes only masked IDs and required/matched counts in logs and public health payloads. Read-only account queries do not require or imply trade unlock.

The Futu SDK documents `qot_logined` and `trd_logined` independently in global state and recommends confirming account identity with `get_acc_list()` before other trading calls:

- <https://openapi.futunn.com/futu-api-doc/en/quote/get-global-state.html>
- <https://openapi.futunn.com/futu-api-doc/en/trade/get-acc-list.html>

## 4. Scope and non-goals

### In scope

- independent lazy construction and closing of quote/trade SDK contexts;
- typed quote readiness and broker identity readiness;
- an explicit quote-binding inventory derived from existing config authority;
- deterministic single-config and aggregate account broker-binding resolvers with sole-account legacy compatibility;
- an additive, read-only multi-config audit on the existing `om config validate` facade;
- migration of all current gateway/direct-context callers into quote, broker, mixed, or operational categories;
- capability-aware doctor/watchdog/health reporting;
- deterministic unit/integration tests and read-only observability;
- a separately authorized post-deploy resource experiment.

### Out of scope

- changing `config.yaml`, generated runtime JSON, secrets, account IDs, Futu credentials, quote rights, or `auto_hold_quote_right`;
- introducing `quote_owner`, `opend_session`, a provider registry, a fallback graph, or a generalized data lane;
- splitting trade-intake into one process per account;
- changing inbox lifecycle, ledger transactions, event identity, outbox, notifications, push/backfill convergence, or broker-write behavior;
- changing OpenD install directories, HOME, `Device.dat`, login, 2FA, cache, mmap files, or service users;
- changing systemd/launchd OpenD units, startup sequence, restart policy, or Incus memory in the source work unit;
- claiming incident closure from tests or source delivery alone.

## 5. Gate order and independent authorization

| Gate | Scope | Entry condition | Exit evidence |
|---|---|---|---|
| P0 | read-only route/call/resource baseline | approved plan | binding inventory, caller inventory, matched baseline windows, go/no-go record |
| A | capability gateway and typed readiness | P0 route inventory is complete | focused gateway/watchdog tests; compatibility facade intact |
| B | caller migration, including mixed paths | A passes | every caller classified; no unintended SDK client creation in spies |
| C | repository verification | B passes | focused suites, lint/analyze, full tests, docs, clean diff check |
| D | source delivery | separately requested commit/push/merge | exact commit/ref; no VERSION/tag/release/apply |
| E | release | separately requested | VERSION-driven tag/Release evidence; no remote upgrade |
| F | remote apply and natural observation | separately requested production authorization | typed readiness, workload correctness, matched resource evidence |

Do not combine these gates by implication. P0 and F are read-only with respect to business data, but F may still require a separately authorized software upgrade. No gate authorizes an OpenD restart or HOME migration.

## 6. Gate P0 — baseline and go/no-go

### 6.1 Static caller inventory

Before editing, produce a checked inventory covering all of these current owners:

| Category | Current owners | Planned treatment |
|---|---|---|
| quote-only gateway | `opend_symbol_fetching.py`, `opend_symbol_chain_fetching.py`, `opend_market_snapshot_fetching.py`, `events/probe.py`, `events/source_futu.py`, `performance/evidence_collection.py`, `positions/assigned_stock_quotes.py`, `positions/maintenance.py`, `futu_gateway_pool.py` | explicit quote builder |
| broker-only gateway | `futu_portfolio_context.py`, `trades/futu_detail_lookup.py` | explicit broker builder with expected identity |
| mixed gateway | `infrastructure/quality/opend_position_adapter.py`, `trades/lifecycle_runtime.py`, `trades/settlement_observation.py`, their `auto_intake.py` and CLI composition roots | separate broker and quote gateways |
| direct quote | `futu_doctor.py`, `opend_watchdog.py`, `external_services.py` | typed quote/broker probe or quote builder; remove first-binding guesses |
| direct broker | `trades/push_listener.py`, `trades/history_backfill.py` | preserve behavior; regression-only coverage |

Re-run repository-wide searches for `OpenQuoteContext`, `OpenSecTradeContext`, `build_futu_gateway`, and `build_ready_futu_gateway` immediately before implementation. The table is a minimum inventory, not permission to ignore a newly discovered caller.

### 6.2 Runtime route inventory

Against the exact rendered US/HK production configs, record:

- every effective Futu fetch binding and its config/symbol provenance;
- the set of market-level endpoints and whether it converges globally;
- every Futu account broker endpoint, expected masked account identity, and trade environment;
- each broker binding member's authority source and any sole-account compatibility warning;
- for each logical account, the selected-config member list, endpoint/environment equality result, required-ID union count, and per-member masked ID provenance;
- pairwise distinct configured broker endpoints across logical Futu accounts and their read-only listening-PID mapping, while recording any intentional quote/broker physical overlap;
- every enabled direct trade-intake source's endpoint, mapped ID-set equality, and implicit-`REAL` compatibility with its account binding;
- any endpoint that serves both roles;
- quote entitlement/field and subscription-quota results for the proposed shared endpoint;
- conflicts or missing bindings without mutating config.

If the Futu quote bindings do not converge to one endpoint, stop the shared-route rollout. If any account binding set has endpoint/environment drift, an empty/lossy ID set, or cannot be built from all selected configs, stop broker/mixed-path rollout for that account and fail Gate F. The capability split may still proceed against deterministic fixtures as a correctness refactor, but no affected production route is eligible until the exact rendered config set passes the aggregate resolver. A config change or ownership decision requires a separate explicit plan/authorization.

### 6.3 Resource baseline

Use the measurement contract in the Incus remediation plan and add endpoint/PID attribution. Do not manually run a notification-producing tick or fabricate trading activity. Capture at least three matched natural windows containing the same scheduled workload class, plus one idle window:

- per OpenD PID: read bytes/sec, major faults/sec, RSS, PSS, and process age;
- cgroup: `memory.current`, `memory.events` deltas, memory PSI, and IO PSI;
- NVMe: read throughput, await, queue depth, and utilization;
- OM: pre-change static caller/current-log evidence and generic quote call destination; post-change one-time capability/client-creation records;
- correctness: scheduler result, required-data artifact status, broker health, trade-intake continuity, and notification count without triggering a send.

Before the post-change window, freeze the material-improvement threshold as:

```text
required_relative_drop = max(20%, 3 * baseline_relative_MAD)
```

where `baseline_relative_MAD` is the median absolute deviation divided by the median across the matched baseline windows. A resource claim is material only if the sy OpenD median read-rate or major-fault-rate falls by at least that threshold, the other does not regress by more than baseline variability, and host memory/IO PSI and NVMe await do not regress. Incident closure still uses the absolute steady-state criteria in the Incus remediation plan.

If a baseline median is zero, that metric cannot support a relative-drop claim; use the other non-zero direct metric and report the zero-baseline metric separately. If `required_relative_drop >= 100%`, the baseline is too unstable for this comparison and P0 must collect new matched windows rather than declaring success or failure.

If baseline evidence shows no OM quote activity on the sy endpoint, record `performance_hypothesis_not_supported`. Continue only with the independently justified capability-boundary work; do not represent it as the incident fix.

## 7. Gate A — gateway and readiness implementation

### 7.1 Independent SDK contexts

In `src/infrastructure/futu_gateway.py`:

1. Replace `_ensure_clients()` with `_ensure_quote_client()` and `_ensure_trade_client()`.
2. `_FutuAPIClient._quote()` calls only the quote initializer; `_trade()` calls only the trade initializer.
3. Add matching private accessors on `FutuGateway`; no method may obtain a tuple of both contexts.
4. `close()` closes only clients that were actually created, at most once each, and remains safe after partial construction failure.
5. Preserve error classification, retry behavior, option-chain cache behavior, host/port defaults, and existing method payloads.
6. Emit one structured INFO record only when each SDK client is created: event, capability, host, and port. Do not log credentials, unmasked account IDs, payloads, or query results.

### 7.2 Explicit builders

Provide these internal factories:

```python
build_ready_futu_quote_gateway(...)
build_ready_futu_broker_gateway(
    ...,
    expected_account_ids: Iterable[str],
    trd_env: str,
)
```

- The quote builder runs only quote readiness.
- The broker builder runs only broker login and complete required-account identity readiness; `expected_account_ids` is a required set, never an alternatives list.
- `build_ready_futu_gateway()` remains a compatibility facade with its current quote-ready semantics during this migration; it must not be silently redefined as broker or mixed.
- `build_futu_gateway()` remains lazy and capability-neutral.
- Do not add a generalized factory registry or public config enum.

### 7.3 Capability-aware watchdog and doctor

Make readiness explicit without breaking existing callers:

- `run_watchdog_check(required_capability="both")` keeps `both` as the compatibility default.
- `quote` checks READY plus `qot_logined` through a quote context.
- `broker` checks READY plus `trd_logined` through a trade context and requires the full non-empty expected-ID set to be present in matching-environment account-list rows.
- classification ignores the unrequested login flag; quote readiness cannot fail only because trade is logged out, and vice versa.
- `run_futu_doctor_checks()` accepts the same capability. Required option-chain/snapshot fields run only for `quote`.
- returned payloads add the requested capability and separate readiness result while preserving existing top-level compatibility fields.

Do not make watchdog/doctor start or restart OpenD as part of tests or plan execution. Existing `ensure` behavior remains opt-in and outside the source verification path.

## 8. Gate B — caller migration

### 8.1 Quote-only callers

Move every quote-only gateway caller and the thread-local required-data pool to `build_ready_futu_quote_gateway()`. Preserve each caller's explicit host/port, retry, cache, provider, and output behavior. The pool stays quote-specific; do not generalize it into a multi-capability pool.

“Preserve host/port” applies only after tracing it to the authority contract. In particular, `positions/assigned_stock_quotes.py` and the assignment-refresh path in `positions/maintenance.py` must stop deriving their default quote route from `infer_futu_portfolio_settings()`. Maintenance and ordinary calls resolve the symbol/market route from effective fetch bindings. The read-only assigned-stock tool keeps the explicit diagnostic override behavior defined in §3; it cannot persist or feed that endpoint into maintenance, tick, lifecycle, or health. Low-level event/performance diagnostics that intentionally accept an endpoint use the same route-source label.

`external_services._resolve_opend_endpoint_for_market()` must stop taking the first matching symbol. Replace its internal lookup with the canonical quote-route resolver. On missing/conflict it returns the existing unavailable outcome and does not try an account endpoint.

The tick watchdog requests `required_capability="quote"` for the endpoint already selected by the tick's fetch route. This does not change tick notification authority or rerun behavior.

### 8.2 Broker-only callers

- `fetch_futu_portfolio_context()` uses the broker builder with the member's complete required ID set and `trd_env`; balance and position reads must create no quote context.
- `trades/futu_detail_lookup.py` uses broker readiness before order/deal lookup, with the configured candidate IDs. Existing fail-closed enrichment behavior remains.
- Direct push listener and history backfill remain on their existing direct trade contexts. The config/binding audit must prove their resolved endpoint, mapped full ID set, and current implicit `REAL` environment equal the applicable binding before rollout. Add regression spies proving they never construct a quote context; do not change source selection, environment selection, state, inbox, checkpoint, or retry semantics.

All three consume the applicable member of the same resolved account broker binding set. Every account query uses the complete ID set required by that member; aggregate P0/F validation additionally proves that the account does not drift across selected configs. A sole-account legacy projection remains compatible and visible; a multi-Futu incomplete binding fails before any provider call.

### 8.3 Mixed position-quality path

Refactor `OpenDOptionPositionAdapter.fetch()` composition, not its domain result:

1. Resolve the account broker endpoint and the runtime market's canonical Futu quote endpoint before opening either client.
2. If either authority is invalid, return an incomplete snapshot with a typed binding/readiness error before making provider calls.
3. Use the broker gateway only for positions.
4. Use the quote gateway only for trading days and missing multiplier snapshots.
5. Close both independently, deduplicating close only if tests inject the same object; physical endpoint equality does not merge capability authority.
6. Preserve `OpenDOptionSnapshot`, account fingerprinting, freshness, multiplier completeness, and fail-closed result semantics.

No new persisted quality schema is required. Add non-secret broker/quote endpoint roles to diagnostic error context only if the existing public payload has an owning field; otherwise keep them in logs/tests rather than growing the contract.

### 8.4 Mixed lifecycle path

Keep the existing lifecycle state machine and split only provider dependencies:

- `ensure_lifecycle_timing_after_intake(..., quote_gateway=..., quote_dependency_error=...)` uses only the quote gateway for `get_trading_days_with_receipt()`. It must return before inspecting that dependency when there is no lifecycle case or an immutable timing policy already exists. A missing gateway is converted inside its existing failure boundary to `lifecycle_timing_policy_unavailable`, never leaked as a `None` dereference.
- `collect_broker_settlement_observation(..., broker_gateway=..., quote_gateway=..., quote_dependency_error=..., trd_env=...)` uses broker for history deals/orders, positions, and cash flows under the validated binding environment rather than a new caller-local default. Only the calendar receipt uses quote; an unavailable quote dependency produces an explicit incomplete calendar receipt with the existing receipt shape and no broker-endpoint fallback.
- `build_settlement_observation_collector()` accepts the non-empty allowed ID set and returns a dispatcher. For each case it reads the case's already-immutable `futu_account_id`, requires exact membership, and passes that ID to the case-local collector. `reconcile_due_lifecycle_cases_for_source()` must no longer require `len(account_ids)==1` or choose the first ID; it carries the complete source ID set, both gateways, and `trd_env`.
- `auto_intake` creates one lazy, capability-neutral broker gateway per source from that source's broker endpoint. It resolves the canonical quote binding without opening a client; when valid it creates a separate lazy `build_futu_gateway()` for quote use, and when invalid it retains `quote_gateway=None` plus a typed, non-secret dependency error. It must not call either ready builder during listener startup.
- Gateways remain owned by the source loop; do not share one non-thread-safe client object across source threads merely because the physical endpoint is the same.
- CLI `lifecycle reconcile-due` resolves both authorities and uses the ready quote/broker builders before entering its own domain apply logic. Static conflict or readiness failure exits both dry-run and write modes with zero lifecycle/ledger writes.

The mandatory entrypoint/failure matrix is:

| Entry point | Missing/conflicting quote binding | Transient quote login/API failure | Broker transport/readiness failure | Required ordering/result |
|---|---|---|---|---|
| source-loop startup | record typed degraded quote dependency; continue starting direct broker listener/backfill | no eager quote call, so it cannot block startup | preserve current listener authentication/reconnect/start-cancel behavior | no ledger/lifecycle write during startup |
| push, inbox retry, or backfill payload apply | run `_process_payload()` first; only a newly required timing bind becomes `needs_review` | same `lifecycle_timing_policy_unavailable -> needs_review` result | preserve current inbox/apply/retry failure path | exact order remains `durable inbox/current _process_payload -> optional lifecycle timing`; never roll back an applied trade event because quote failed |
| source-loop periodic due reconciliation | emit incomplete calendar receipt or the existing `last_lifecycle_due_error`; keep listener loop alive | same; retry on existing cadence | account receipts remain incomplete/error under existing reconciliation semantics; listener recovery remains owned by listener health | no stop-event, reconnect, or inbox reorder; any lifecycle transition/outbox creation keeps the existing state fingerprint, transition key, deduplication, and delivery authority |
| explicit `lifecycle reconcile-due` CLI, dry-run or write | fail preflight | fail readiness preflight | fail readiness preflight | return before domain apply; zero writes in either mode |
| healthcheck | quote capability error only | quote capability error only | affected broker capability/account-primary error only | legacy aggregate is the conjunction defined in §8.5; one capability never substitutes for another |

Every early return, listener construction failure, reconnect exhaustion, and normal shutdown must close whichever of the two gateways was constructed; deterministic tests cover partial construction and every existing `settlement_gateway.close()` branch. A quote gateway that was never constructed needs no close.

Preserve case identity, timing-policy immutability, receipt schema, transaction boundary, retry cadence, inbox state, outbox/notification authority, and current `needs_review` behavior. In particular, a quote-caused `needs_review` transition may create the same idempotent lifecycle outbox row it creates today; this work neither suppresses it nor sends it directly. Static quote conflict and transient quote failure share the same degraded lifecycle semantics in the running listener, but remain distinguishable by typed reason/error context. Neither may fall back to the broker endpoint or widen the broker listener's availability boundary.

### 8.5 Health projection

Update health composition to build two inventories:

- quote endpoints from effective Futu fetch bindings, checked once per physical endpoint with representative symbols;
- broker endpoints from validated Futu account binding members, checked per logical account with its complete required identity set.

If one endpoint has both roles, publish two capability results. Add checks named `opend_quote_readiness_<endpoint>` and `opend_broker_readiness_<account>_<endpoint>`, reusing the current dot/colon-to-underscore endpoint sanitizer and normalized lowercase account label. Their `value` contains `capability`, `host`, `port`, `accounts`, `ready`, `global_state`, and `telnet`; broker values additionally contain only `required_account_id_count`, `matched_account_id_count`, and `masked_required_account_ids`. A broker-only endpoint must not run option-chain/snapshot field checks.

Public compatibility is exact rather than implied:

1. Keep `_HEALTHCHECK_OUTPUT_CONTRACT.schema_version=healthcheck.output.v1`, existing `checks[].{name,status,message}`, `account_paths`, `summary`, and all existing field types. Add `checks[].value.capability`, `checks[].value.capabilities`, `checks[].value.required_account_id_count`, `checks[].value.matched_account_id_count`, and `checks[].value.masked_required_account_ids` to the contract; do not remove or rename old fields.
2. Keep each existing `opend_readiness_<endpoint>` compatibility check. Its status is the conjunction of every required typed quote/broker result for that physical endpoint; its existing `value.host`, `port`, `accounts`, `global_state`, and `telnet` remain, and `value.capabilities` is an additive map of role to status.
3. In the existing account-endpoint branch, keep the aggregate check named `opend_readiness`. Its status is `ok` only when every required typed capability in the current healthcheck config scope is `ok`; otherwise it is `error`. Existing consumers that read only this check therefore remain fail-closed.
4. Keep `account_paths[account].primary` keys and types. For `type=futu`, only that account's broker identity readiness contributes to `primary.ok`; a quote-only failure is visible in typed checks and the aggregate but does not falsely mark broker facts unavailable.
5. Retain `opend_readiness_global` only for its existing compatibility branch; project it from the same typed results rather than running a third probe. Do not synthesize the account-branch aggregate there merely to rename legacy output.
6. Compute `summary.ok`, `summary.critical_count`, `summary.warning_count`, and warning strings from the same pre-existing compatibility-check projection plus all unrelated checks. New typed child checks must not be counted a second time; one failed physical dependency therefore cannot inflate the legacy counts solely because its role detail was added.
7. Keep all IDs masked and do not expose a new unmasked identity field.

Owning public surfaces are `src/application/agent_tools/diagnostics.py`, `src/application/agent_tools/healthcheck_impl.py`, `src/application/agent_tools/positions.py`, the assigned-stock output-contract resolver, `docs/AGENT_WIKI.md`, and `docs/DEPLOY_LINUX_MAC.md`. The assigned-stock output keeps `option_positions_read.assigned_stock_output.v2`; add optional `quote_refresh.route_source` to its fact fields and update the two endpoint descriptions to say “read-only diagnostic override”. No `route_mode` or deprecation period is added because the current override behavior is retained.

## 9. Tests and verification

### Gateway/readiness tests

1. Quote builder creates quote context exactly once and never creates trade context.
2. Broker builder creates trade context exactly once and never creates quote context.
3. Quote failure closes only the constructed quote context; broker partial failure closes only trade.
4. Repeated close is safe and partial initialization does not leak a client.
5. Quote readiness checks READY and `qot_logined`, ignoring only unrequested trade login.
6. Broker readiness checks READY, `trd_logined`, every required ID, and environment, ignoring only unrequested quote login; two-required/one-observed fails.
7. Missing/ambiguous/lossy account identity, or a required ID present only under another environment, fails closed and public diagnostics mask IDs.
8. Compatibility `build_ready_futu_gateway()` remains quote-ready.
9. Retry and mapped 2FA/auth/rate/transient errors remain unchanged.
10. A sole Futu account can resolve the documented legacy projection with a warning; multiple Futu accounts cannot borrow shared legacy endpoint or identity fields.
11. An account binding set accepts a complete market-specific ID union, rejects US/HK endpoint or environment drift, preserves member provenance, and treats absent/external members as non-contributors.
12. An enabled direct trade-intake source passes only when its endpoint, mapped required-ID set, and implicit `REAL` environment match the applicable binding; mismatch fails validation without constructing a context.

### Route tests

1. Same effective endpoint across US/HK resolves one shared route with symbol/config provenance.
2. Different endpoint by market or symbol yields conflict; no first-row selection.
3. Missing host/port yields missing binding; defaults are used only where the existing canonical fetch contract explicitly supplies them.
4. Non-Futu providers and symbol overrides are preserved.
5. Template/profile-resolved fetch values, not raw authoring rows, determine identity.
6. Account broker endpoints never participate in quote-route resolution.
7. Broker binding resolution prefers complete per-account settings, permits legacy projection only for one Futu account, and rejects incomplete multi-Futu configs.
8. Cross-config aggregation requires endpoint/environment equality and a complete ID union; cover same-ID, different-ID union, union incomplete at readiness, endpoint mismatch, environment mismatch, absent account, and external-only member.
9. Two Futu accounts with the same broker endpoint fail topology validation; distinct broker endpoints pass, and quote overlap with exactly one broker endpoint remains valid.
10. `om config validate --related-config-path` preserves existing single-config output, rejects duplicate paths/markets and YAML mode, emits a sanitized audit, makes no provider call, and exits nonzero on aggregate conflict.

### Caller tests

1. sy portfolio balance/positions use sy broker binding and construct no quote client.
2. required-data, event, assigned-stock, and maintenance quote flows construct no trade client and retain their exact fetch binding.
3. assigned-stock without explicit endpoint uses the canonical quote route; full and partial `opend_host`/`opend_port` diagnostic overrides remain accepted and labeled, while maintenance never consumes them and no override is persisted.
4. position quality directs positions to broker and calendar/snapshot to quote; either failure remains incomplete.
5. lifecycle intake and due reconciliation direct account receipts to broker and calendar receipts to quote.
6. with quote binding conflict or quote login down, listener startup and broker payload apply continue; the exact `_process_payload -> lifecycle timing` order remains, an applicable case becomes `needs_review`, its existing idempotent outbox result is unchanged, and the listener does not reconnect or stop because of quote.
7. periodic due reconciliation records incomplete/error evidence and keeps the listener alive; broker failure retains existing listener/reconciliation recovery semantics.
8. explicit lifecycle reconcile CLI quote/broker preflight failure produces zero writes in dry-run and write modes.
9. a source with two required IDs reconciles each lifecycle case through its own immutable ID; an unconfigured/missing case ID fails case-locally, and no first-ID fallback or cross-account receipt is possible.
10. settlement broker queries use the validated `trd_env`; current enabled listener/backfill fixtures remain `REAL` byte/semantically compatible.
11. push listener and history backfill remain trade-only and their state/checkpoint tests are unchanged.
12. broker-only health does not request option chains or snapshots; quote health does not require trade login.
13. one physical endpoint serving both roles produces two typed readiness results; legacy per-endpoint and aggregate checks equal the documented conjunction, account primary uses broker only, and typed children do not inflate legacy summary counts/warnings.
14. health/assigned-stock metadata, output contracts, examples, and docs preserve their named compatibility fields and expose only additive typed route/capability fields.
15. no test opens a live OpenD, sends a notification, writes broker data, or mutates production runtime artifacts.

### Required commands

Run focused tests first, including at minimum:

```bash
./.venv/bin/python -m pytest -q \
  tests/test_futu_gateway_minimal.py \
  tests/test_futu_gateway_pool.py \
  tests/test_account_config.py \
  tests/test_config_yaml.py \
  tests/test_futu_portfolio_context.py \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_assigned_stock_quotes.py \
  tests/test_positions_maintenance.py \
  tests/quality/test_opend_position_adapter.py \
  tests/test_settlement_observation.py \
  tests/test_trades_lifecycle_runtime.py \
  tests/test_trades_account_mapping.py \
  tests/test_trades_auto_intake_cli.py \
  tests/test_trades_auto_intake_backfill.py \
  tests/test_trades_push_listener.py \
  tests/test_trades_history_backfill.py \
  tests/test_runtime_script_dependency_cleanup.py \
  tests/test_opend_watchdog_alerts.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py
```

Then run the repository's current analyze/lint and full-test gates from live project instructions, followed by:

```bash
git diff --check
```

If the full suite is too large for one host window, the implementation gate must record the exact split commands and demonstrate that their union covers the full collected test set; “focused tests only” is not completion for this operations-sensitive boundary.

## 10. Acceptance criteria

### Source acceptance

All must be true:

- every Futu context construction site is classified and tested;
- pure quote and pure broker paths create only their requested SDK context;
- mixed paths receive two explicit authorities and never silently reuse the broker endpoint for market facts;
- no new endpoint/config authority exists;
- the existing config-validation facade can deterministically audit the exact selected rendered-config set without provider calls or writes;
- sole-account broker compatibility is explicit, while each logical account's selected-config binding set has one endpoint/environment and a complete required-ID union; multiple logical Futu accounts have pairwise distinct broker endpoints;
- every required broker account identity is verified before account queries;
- every enabled direct listener/backfill source is proven equal to its account broker binding without changing its existing `REAL` transport/state behavior;
- quote failure cannot prevent listener startup or reorder/roll back broker intake; lifecycle degradation and CLI preflight follow the §8.4 matrix;
- multi-ID due reconciliation dispatches by each case's immutable Futu account ID and uses the binding environment, with no first-ID fallback;
- health reports quote and broker readiness independently while preserving the exact legacy projections and assigned-stock diagnostic overrides documented in §8.5;
- listener/ledger/inbox/outbox/state schemas and behavior are unchanged;
- focused, lint/analyze, full tests, and diff check pass.

### Separately authorized production acceptance

Use matched natural windows; do not manually trigger notification-producing work. All must be true:

- production rendered quote bindings resolve to the expected single endpoint;
- all selected rendered configs resolve one conflict-free broker binding set per Futu account, every required ID is observed in the configured environment, and different account broker endpoints map to different OpenD PIDs;
- generic Futu quote operations appear only at that endpoint;
- every per-account broker operation uses its configured endpoint and verified account identity;
- an account endpoint that is not the canonical quote endpoint records zero OM-created quote clients, except a separately identified legacy process that blocks completion;
- broker facts, required-data artifacts, lifecycle intake, and health remain complete/fresh under their existing contracts;
- no unexpected notification, ledger mutation, service restart, or OpenD restart occurs during observation;
- resource comparison is reported against the frozen threshold, even if it shows no improvement.

If functional routing passes but the material resource threshold fails, mark the architecture source change `accepted` and the host incident `not_closed`. Route next to the existing capacity/startup plan. Do not infer authorization for HOME migration, service splitting, memory increase, or an additional quote-only OpenD.

## 11. Rollback and failure containment

The source change has no config or database migration. Roll back by reverting the scoped source work unit and restoring callers to the compatibility quote-ready builder. Do not roll back by modifying generated configs, deleting state, restarting OpenD, or copying HOME data.

For a separately authorized remote apply:

1. preserve the previous release and service profile;
2. do not restart OpenD as part of the application upgrade unless separately authorized;
3. if broker identity/readiness regresses, roll back the OM release before changing Futu login/device state;
4. if quote routing regresses, roll back rather than silently falling back to an account endpoint;
5. verify ledger/outbox/inbox continuity read-only after rollback.

Because no state schema changes, mixed-version data migration is not required. Running old and new OM processes concurrently is still unsupported unless the existing service contract already permits it.

## 12. Deferred work and residual risks

Tracked separately, not implementation TODOs inside this plan:

- trade listener process isolation and any capture/apply protocol;
- per-account OpenD HOME/`Device.dat` migration and 2FA runbook;
- systemd OpenD dependency/startup sequencing beyond the completed Strategy Lab binding;
- Incus memory envelope change or host/container separation;
- a dedicated third quote-only OpenD;
- strong credential/OS least-privilege isolation;
- automatic quote failover across endpoints.

Residual risks after successful source implementation:

- OpenD may retain the same SecListDB/mmap working set without any OM quote client, so IO pressure may not improve.
- A single quote endpoint is a generic market-data availability dependency.
- Futu quote rights can be displaced by another terminal, and subscription quota is shared across connections on one OpenD. No silent endpoint failover is introduced: <https://openapi.futunn.com/futu-api-doc/en/qa/opend.html>, <https://openapi.futunn.com/futu-api-doc/en/quote/query-subscription.html>.
- The lx physical process may still combine quote and lx broker capabilities; this plan provides logical separation and diagnostic clarity, not strong isolation.
- Two broker-required OpenD working sets may still exceed the current cgroup envelope and require a separately measured capacity or host-isolation decision.

## 13. Implementation handoff checklist

Before coding:

- [ ] P0 caller inventory refreshed from current source.
- [ ] Effective production quote bindings inventoried read-only.
- [ ] Baseline windows and threshold frozen.
- [ ] No unresolved quote-route conflict for the in-scope runtime markets.
- [ ] Every selected-config account binding set passes endpoint/environment equality and required-ID union validation.

Before source delivery:

- [ ] All context constructors classified.
- [ ] Gateway/readiness, route, pure caller, mixed caller, and health tests pass.
- [ ] Lifecycle write semantics and trade-intake state machine show no diff outside dependency injection.
- [ ] Quote conflict/down tests prove broker listener startup and `_process_payload()` ordering remain intact; explicit CLI preflight tests prove zero writes.
- [ ] Agent-tool input/output metadata, health compatibility projections, and named docs match the public contract in §8.5.
- [ ] Public diagnostics mask account IDs and contain no secrets.
- [ ] Analyze/lint/full tests and `git diff --check` pass.
- [ ] Review confirms no config, VERSION, release, remote, service, Incus, or Futu-state mutation is bundled.

Before any production claim:

- [ ] Release and remote apply were separately authorized.
- [ ] `./om config validate --config-path <us-runtime.json> --market us --related-config-path <hk-runtime.json>` passed against the exact deployed rendered configs and its sanitized routing audit was retained.
- [ ] Read-only `healthcheck` ran once for every selected runtime config; the combined per-member broker checks cover the audit's complete required-ID union and every typed quote/broker result is retained.
- [ ] Upgrade did not restart OpenD unless separately authorized.
- [ ] Natural-window functional and resource evidence is complete.
- [ ] Architecture outcome and incident outcome are recorded separately.
