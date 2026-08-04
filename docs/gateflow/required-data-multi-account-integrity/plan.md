# Gateflow Plan — Required Data and Multi-Account Integrity

- Work unit: `required-data-multi-account-integrity`
- Gate: `plan`
- Date: 2026-08-04
- Status: accepted after PlanReview re-review (`pass-with-risks`)
- Branch: `fix/required-data-multi-account-integrity`
- Base: `origin/main@ed2531e9`
- Goal artifact: `docs/gateflow/required-data-multi-account-integrity/goal-confirmation.md`
- Failed PlanReview: `docs/reviews/plan-review-20260804-100017.md`
- Accepted re-review: `docs/reviews/plan-review-20260804-102627.md`
- Artifact path: `docs/gateflow/required-data-multi-account-integrity/plan.md`

## Goal and success signals

The canonical multi-account tick must publish and consume one coherent run-scoped set of required-data,
configuration, position, FX, and event facts. A strict subset, wrong account, changed generation, failed provider,
or mismatched fetch contract must remain a typed failure and must not acquire success authority.

The work unit is complete only when the seven confirmed problems have deterministic regressions and these signals
hold:

1. option snapshot and required-RV completeness are postconditions of every fresh, cached, subprocess, and
   success-empty publication path;
2. `scheduler --run-if-due` cannot execute the legacy account-blind pipeline path or touch runtime state first;
3. each account child reads canonical config bytes atomically published inside its own run workspace;
4. Close Advice planning, pipeline position use, and output provenance consume one trusted ledger observation;
5. every option context is exact-account, including a valid explicit empty context for a trusted zero-position account;
6. cross-account prefetch respects requested scope, observes spot once per physical binding/day, and executes exact
   `RequiredDataFetchSpec` relations;
7. FX and event facts are observed once at the run boundary, terminally reused on re-entry, and provider failures
   never look like successful facts or successful gateway health.

## Non-goals and safety boundary

- Do not change strategy scoring, thresholds, ranking, notification wording, or notification authority.
- Do not add a provider, database schema, distributed lock, generic snapshot framework, cache daemon, background
  service, or account-specific OpenD route.
- Do not modify authored/generated production config, credentials, remote runtime artifacts, Feishu, broker, or
  ledger business data.
- Do not send real notifications, deploy, release, merge, approve, or mark a draft PR ready.
- Do not fold in PR 132 or unrelated local changes.
- Defer candidate-CSV consumer TOCTOU, manual multiplier drift outside canonical prefetch, failure-budget telemetry,
  receipt-scan complexity, path-risk dead-code cleanup, and future multi-binding output filename redesign.

## Design alignment and first-principles judgment

There is no user-supplied design document. This plan preserves the barrier ownership established by
`docs/plans/run-scoped-required-data-snapshot-fix-plan-20260728.md` and narrowly extends the Close Advice frozen
authority from `docs/plans/daily-brief-frozen-snapshot-close-advice-fix-plan-20260729.md` to the complete option
position context.

The shared invariant is: a success declaration is valid only after the exact consumer-visible bytes have been
proven to satisfy the declared input identity. The repair therefore stays at existing owners:

- provider result owns requested/returned completeness;
- receipt publication owns final artifact coverage;
- seal owns expected-plan matching and terminal per-symbol status;
- scheduler CLI owns removal of its shadow execution surface;
- run workspace owns child config bytes;
- ledger repository owns coherent multi-account reads;
- prepared-context loader owns exact bytes and no-fallback consumption;
- parent tick owns run-level FX and event observations.

No process-wide lock can repair a consumer that reopens mutable input. The plan instead uses one narrow SQLite read
transaction per shared ledger group, immutable run artifacts, and strict manifest validation at consumption and
immediately before output promotion.

## Contracts and state machines

### 1. Required-data expected contract, finalizer, receipt, and seal

Before any fetch, the parent builds a canonical `expected_fetch_contract` per physical symbol binding. It contains:

- normalized symbol;
- exact ordered `RequiredDataFetchSpec` debug payloads;
- source, host, and port physical binding;
- whether RV is required;
- the stable coverage policy used by `required_data_frame_covers_fetch_plan`;
- a canonical SHA-256 included in the global plan identity.

Operational execution metadata such as retry counters, batch budgets, and execution mode remains recorded in the
receipt with its own canonical hash, but is not falsely claimed to be plan-owned policy. Seal exact-compares the
plan-owned fetch contract and physical binding, and validates the operational-policy payload/hash for self-integrity.

`MarketSnapshotFetchResult` exposes requested, returned, missing, and unexpected code sets/counts plus `complete`.
Unexpected codes are discarded. After all fallback requests, any missing requested code yields
`SNAPSHOT_COVERAGE_INCOMPLETE`; a fully recovered fallback may return `ok` while retaining diagnostics.

A single internal finalizer handles four modes:

1. **fresh**: validate typed payload and binding -> save raw/CSV once -> apply existing multiplier normalization ->
   fresh CSV read -> exact contract coverage -> adopt or publish receipt;
2. **cached**: never call `save_outputs`; validate existing raw payload status/outcome/binding -> apply only the
   existing multiplier normalization if required -> fresh CSV read -> exact coverage -> adopt or publish receipt;
3. **subprocess**: treat subprocess raw/CSV as a fresh candidate, then execute the same raw/binding/readback/coverage/
   receipt postconditions without a second save;
4. **success-empty**: require valid empty-discovery raw evidence, expected contract/binding, and an empty CSV with
   canonical headers. RV is `not_applicable_no_contracts` and no RV provider call occurs.

Receipt publication is idempotent recovery, not blind recreation. After coverage, the finalizer resolves any
existing receipt for the same run, bytes, and expected contract. An exact match is adopted; otherwise the current
symbol becomes a typed integrity failure. Stable observation/completion timestamps come from frozen raw/discovery
evidence, never a new wall clock on cached re-entry.

The seal always publishes a terminal manifest when the global plan itself is valid. Each symbol is `ready` or typed
`failed`; aggregate status is `complete`, `partial`, or `failed`. Receipt/fetch-contract/binding mismatch affects that
symbol, not unrelated symbols. Only malformed/self-inconsistent global plan authority prevents terminal manifest
publication.

Gateway pool health changes only after typed provider result inspection: exception, `meta.status=error`, incomplete
snapshot, or required-RV failure calls `mark_failure`; a complete typed provider result calls `mark_success` once.
Cache-only paths do not mutate gateway health.

### 2. Scheduler execution authority

- Keep `--run-if-due` parseable for compatibility, but fail closed with structured code `UNSUPPORTED_OPERATION` and
  a `tick`/`tick-cron` hint before runtime-root resolution, config/state loading, adapter creation, or subprocess use.
- Python `run_scheduler(run_if_due=True)` fails before `_resolve_base` and `_resolve_state_path`.
- Decision and mark operations remain supported and account-scoped; the legacy subprocess branch/imports are removed.

### 3. Canonical run config bytes

The tick parent serializes each effective account config once with one canonical JSON helper and publishes it before
`prepare_portfolio_contexts()` or any other account subprocess. It retains exact bytes, path, and
`account_config_sha256`; those values travel through prepared-worker and AccountRun requests, and no later consumer
recomputes a different representation.

State transition before any child call:

`canonical bytes -> write-once/adopt <run>/accounts/<account>/state/config.override.json -> write-once/adopt
compatibility artifact from the same bytes -> verify both hashes -> any prepared worker/child start`.

An existing same-run artifact with identical bytes is adopted; different bytes are a typed conflict and are never
overwritten. Any write/archive/hash failure blocks that account before prepared portfolio work, pipeline, or Close
Advice. The canonical tick never writes `output_accounts/<account>/state/config.override.json`; historical files are
preserved but never authoritative.

### 4. Exact-account prepared option context

`validate_option_positions_context_account(context, expected_account)` is the single raw-context postcondition:

- `filters.account` must be present and equal the normalized expected account;
- every `open_positions_min` row must have the same account;
- zero rows are valid only when the surrounding trusted snapshot explicitly represents that account.

Cached, shared, and fresh-direct account-scoped paths validate before adapters/defaults can erase evidence. Manual
account-less/global compatibility remains unchanged. Prepared mode is a separate early branch outside broad fallback
handling: it loads one manifest, validates run/account/workspace/path/hash/authority, parses exactly those validated
bytes, and never calls cached/shared/direct/global-ledger or FX loaders.

Each account has one immutable prepared payload plus a ready/unavailable manifest. Ready authority binds:

- run id, exact account, normalized broker, and canonical account-config SHA;
- `ledger_binding_id = sha256(canonical resolved SQLite path)` without exposing the path;
- ledger group generation, decision fingerprint, stable position-state hash;
- prepared payload path/hash and exact context status;
- run-level FX observation identity/status;
- shared time authority (`position_observed_at_utc`, `business_date`, `lifecycle_now_ms`).

Unavailable authority carries a typed reason and never permits live fallback. Context publication uses existing atomic
state helpers. The mutable compatibility `option_positions_context.json` may still be produced for old readers, but
canonical prepared consumers and Close Advice never reopen it as authority.

### 5. Coherent ledger group and Close Advice plan v2

Accounts are grouped by exact resolved ledger path. `SQLiteOptionPositionsRepository` gains a narrow
`read_decision_state_rows_many(accounts)` API that opens one connection and one SQLite `BEGIN`, invokes the existing
row reader for every normalized account, and returns the group mapping before commit. `decision_state_snapshots(...)`
builds per-account snapshots from supplied rows while preserving the public single-account API.

At barrier start the parent captures one `position_observed_at_utc`, `business_date`, and `lifecycle_now_ms` and uses
them for every account in the run. Each ready snapshot must satisfy:

- expected schema and fingerprint schema;
- expected normalized account and `scope_for(account)` portfolio scope;
- `snapshot_status == trusted` and `actionable is True`;
- valid event/stored/reprojected SHA-256 fields and decision fingerprint;
- `validate_position_fact_snapshot_contract(snapshot) == ()`.

A batch read/global projection failure makes the entire ledger group unavailable. A local account contract defect
after a trusted group read may isolate that account, but must not be interpreted as zero positions. Trusted zero-lot
accounts are explicitly ready.

`ledger_generation_sha256` is computed from the canonical group batch authority. `position_state_sha256` is computed
from a closed canonical list sorted by normalized account, symbol, option type, side, expiration, strike, multiplier,
lot/record identity, contracts/quantity, status, lifecycle allocation, assigned-stock identity, and combo membership.
It excludes only the separately bound shared time fields and presentation-derived values.

If a declared strategy requires global path risk, the parent derives a broker-scoped, row-free risk aggregate from
the same group rows and binds it into prepared payloads. `not_required` is explicit for current strategies that do not
request it. Prepared mode never calls `load_global_option_positions_risk_context`; required aggregate absence is a
typed account failure.

Ledger-group re-entry is all-or-none:

- if the complete expected account-manifest set exists, every entry and payload must validate and share group/FX/time
  identities; the whole group is reused with zero ledger reads;
- if any expected entry is missing, tampered, or cross-generation, no member is started and no account is rebuilt from
  a new ledger read during terminal re-entry;
- before any manifest has been published, a first-attempt build may read and publish the whole group; once partial
  publication exists, recovery is fail-closed rather than mixing generations.

Position requirements may be derived provisionally from the trusted batch to build the global fetch plan. After the
required-data manifest seals, Close Advice plan v2 becomes the account consumption authority. Its envelope binds the
sealed required-data identity, account authority hash, and prepared context hash. Stable lot-local `requirement_id`
does not include account-wide hashes; an optional `requirement_snapshot_id` binds `requirement_id` to the authority
hash for this run.

Frozen `run_close_advice` receives the prepared manifest, loads exact payload bytes through the strict loader, and
validates plan v2 before any quote read. Immediately before output `os.replace`, it revalidates the prepared manifest,
payload bytes/hash, plan binding, and required-data identity. Mismatch yields typed integrity failure, invalidates old
success authority, and blocks notification. The report manifest records prepared-manifest and payload hashes. Manual/
non-frozen account-less behavior remains compatible.

### 6. Exact fetch execution and spot tri-state

Opening demand from base config is filtered to the union of requested account scopes before merge. Explicit Close
Advice position additions remain separately labeled in plan/debug identity.

`build_required_data_fetch_plan(..., spot_observation_cache=...)` uses a run-local mapping keyed by
`(symbol, source, host, port, trading_date)`. Key membership, not truthiness, distinguishes unobserved from observed
`None`. Provider request plumbing carries an explicit `spot_already_observed`/`fetch_spot_if_missing=False` state so
observed failure cannot trigger a second fetch.

The canonical exact-spec parser lives with `RequiredDataFetchSpec` planning. Internal `--fetch-specs-json` rejects
malformed values, mixed symbols, mixed physical bindings, and simultaneous legacy flat selectors. Each spec retains
one exact side-expiration relation. Deterministic aggregation requires every child status `ok`, equal symbol/binding/
spot authority, deduplicated contract rows, deterministic error priority, and one finite positive RV observation when
required.

In-process execution uses the existing injected gateway and invokes the common finalizer once. Subprocess execution
passes one-symbol exact JSON, creates one gateway, aggregates/saves/finalizes once, and sets tool-execution
`force_refresh=True` for every precomputed todo item so a prior idempotency record cannot substitute an older plan;
the finalizer remains the data-cache authority.

### 7. Run-level FX and event terminal facts

FX is observed once before account context publication. A durable run artifact binds run id, rates, source timestamp,
reason, and canonical hash. Its observation state is `ready`, `stale_fallback`, or `unavailable`; numeric rates must be
finite positive. A successfully published `unavailable` observation is still a valid terminal fact, not successful
market data. Prepared consumers only use the embedded observation. FX-dependent derived values are `None` with a typed
reason when unavailable; only accounts that actually require conversion are blocked, while trusted zero-lot or
single-currency accounts may remain ready without relabeling FX success. Manual loading uses
`(shared_state_dir or state_dir)/rate_cache.json`.

Event prefetch occurs once after the union plan and before any account starts. Its terminal schema has two axes:

- artifact status `ready|unavailable`, determined by atomic path/run/hash/schema validation;
- observation status `complete|partial|failed`, preserving exact union coverage and per-symbol typed errors.

Provider partial/failed results are published as a durable ready artifact and all consumers use the same unknown/
fail-closed business fact. Only inability to publish a terminal artifact blocks all account starts. A single strict
event loader/validator in the events owner is reused by annotator, Daily Brief, and Close Advice; no consumer performs
a weak raw read or turns corruption into clean empty.

The shared loader has two explicit modes rather than weakening one contract:

- when `expected_event_identity` is present, canonical scheduled/frozen mode requires terminal schema v2 and exact
  expected run/path/hash/union identity; legacy v1 is rejected;
- when identity is absent, manual/account-less compatibility mode may read existing schema v1, but missing/malformed
  input returns a typed unknown/unavailable read result, never a fabricated clean `{}` observation.

The parent carries v2 identity through AccountRun, the pipeline subprocess request, pipeline runtime/watchlist,
Close Advice, and Daily Brief. Manual public callers that do not supply identity remain explicitly on the legacy
branch.

On same-run terminal re-entry, valid FX/event artifacts are reused with zero provider calls. Missing, malformed, or
tampered terminal artifacts block re-entry and are not silently regenerated into a new generation.

At barrier invocation entry, before any write in that invocation, the parent computes and freezes `recovery_mode`
from pre-existing run-scoped barrier commit records (config, FX/event observation, prepared account manifest,
required-data receipt/manifest, or finalized Close Advice plan). It never recomputes the flag during that invocation.
A brand-new invocation therefore remains fresh while it publishes later records; a restarted invocation sees the
earlier records and remains recovery mode. This prevents both self-transition during a healthy first pass and a crash
after first publication being mistaken for a fresh observation.
Canonical barrier order is:

`canonical account config bytes -> recover/create FX fact -> coherent ledger groups and prepared payloads -> derive
position requirements and scoped union -> required-data fetch/seal -> finalize account plan v2 -> recover/create event
fact -> validate every required terminal authority -> account children`.

Independent provider work may be optimized later, but this slice does not parallelize or reorder commit points.

No persistent database schema changes are introduced.

## Affected files and ownership

- Required-data provider/result: `opend_market_snapshot_fetching.py`, `opend_symbol_fetching.py`,
  `required_data_fetching.py`, and `required_data_coverage.py`.
- Required-data commit authority: `opend_symbol_outputs.py`, `required_data_steps.py`,
  `multi_tick/required_data_prefetch.py`, `required_data_plan_identity.py`, and `required_data_snapshot.py`.
- Planning/execution: `required_data_prefetch_planning.py`, `required_data_planning.py`,
  `opend_symbol_fetching_cli.py`, and the existing tool-execution service used by subprocess prefetch.
- Scheduler/config: `interfaces/cli/scheduler_ops.py`, `scan_scheduler.py`, `tick_account_execution.py`,
  `account_run.py`, prepared portfolio request plumbing, and the existing atomic run-state/workspace owner.
- Ledger/positions: `ledger/repository.py`, `ledger/api.py`, `ledger/decision_snapshot.py`,
  `positions/context_builder.py`, `positions/context_cache.py`, and `pipeline_context.py`.
- Prepared/Close Advice: new `prepared_option_positions_context.py`, `close_advice_required_data.py`,
  `close_advice_runner.py`, `close_advice_report_manifest.py`, and current runtime/watchlist/account request plumbing.
- FX/events: `exchange_rate_loader.py`, `events/prefetch.py`, `events/annotator.py`, `multi_account_tick.py`,
  `tick_account_execution.py`, `account_run.py`, `infrastructure/external_services.py`, `pipeline_runtime.py`,
  `pipeline_watchlist.py`, `daily_decision_brief_service.py`, `close_advice_runner.py`, and their CLI/request plumbing.
- Documentation/tests: `docs/AGENT_WIKI.md` and the exact focused test owners listed in each slice.

## Implementation decisions

1. Preserve existing public facades; new request arguments are internal and optional outside canonical frozen mode.
2. Treat receipts and manifests as commit records, never optimistic progress markers.
3. Reuse canonical JSON/SHA and atomic state helpers; add no generic snapshot framework.
4. Read one shared SQLite generation rather than comparing sequential incomplete fingerprints.
5. Keep stable lot identity separate from run/account authority identity.
6. Missing/untrusted facts are not empty facts; zero-lot readiness requires an explicit trusted account snapshot.
7. Re-entry prefers validation/reuse or typed unavailability, never a per-account/provider retry that can mix worlds.
8. Provider outcome and artifact integrity are separate axes for FX/events.

## Implementation slices

### S1 — Required-data completion, receipt, seal, and gateway truth

- **Objective**: make final snapshot/RV/CSV coverage and exact expected contract mandatory before receipt/seal success.
- **Expected outcome**: incomplete/wrong provider data and bad identity end as typed symbol failure; valid complete and
  valid empty discoveries produce one adoptable receipt and terminal authority.
- **Allowed files/modules**:
  - `src/application/opend_market_snapshot_fetching.py`
  - `src/application/opend_symbol_fetching.py`
  - `src/application/opend_symbol_outputs.py`
  - `src/application/required_data_fetching.py`
  - `src/application/required_data_coverage.py`
  - `src/application/required_data_plan_identity.py`
  - `src/application/required_data_snapshot.py`
  - `src/application/required_data_steps.py`
  - `src/application/multi_tick/required_data_prefetch.py`
  - directly corresponding required-data tests
- **Prerequisites**: confirmed goal.
- **Exact changes**:
  - implement code-set reconciliation and typed RV completeness;
  - construct expected fetch contract before execution and bind it to plan/receipt;
  - route fresh/cache/subprocess/success-empty publication through the single finalizer modes above;
  - adopt exact existing receipts during crash re-entry; never synthesize new timestamps for cached evidence;
  - make receipt mismatch per-symbol failed and terminal seal partial/failed; only corrupt global plan prevents seal;
  - mark gateway success/failure only after typed result inspection.
- **Functions/types and call path**: extend `MarketSnapshotFetchResult`; strengthen `fetch_option_snapshots`,
  `fetch_symbol_request`, `save_outputs`/receipt publication, and seal resolution. Data flows as
  `expected contract -> typed provider payload -> finalizer -> exact durable bytes -> receipt -> terminal seal`.
- **Error handling**: safe diagnostics may remain, but no success authority. `success_empty` requires validated empty
  discovery/CSV and RV `not_applicable_no_contracts`.
- **Invariants**: no receipt before post-write readback; no production publication bypass; at most one immutable receipt
  per exact observation/contract; cached path never invokes `save_outputs`.
- **Non-goals**: exact-spec execution and spot reuse are S6; broad timestamp redesign is out of scope.
- **Tests/validation**:
  - `tests/test_market_snapshot_fetching.py`
  - `tests/test_required_data_snapshot.py`
  - `tests/test_required_data_coverage.py`
  - `tests/test_required_data_prefetch_inprocess.py`
  - `tests/test_required_data_quote_receipts.py`
  - regressions for strict subset/unexpected code, full fallback recovery, bad required RV, no-contract RV N/A,
    direct publisher bypass, raw wrong-binding/error with otherwise covering CSV, per-symbol plan mismatch partial, and
    receipt-last crash/re-entry adoption.
- **Completion signal**: focused tests pass and code search proves every production receipt caller enforces the same
  postcondition.
- **Stop condition**: current terminal manifest cannot represent per-symbol typed failure without a new public
  business-policy decision.

### S2 — Retire scheduler shadow execution

- **Objective**: make canonical tick/tick-cron the only scheduled execution authority.
- **Expected outcome**: the compatibility flag returns structured unsupported-operation without state or child
  effects; decision/mark calls remain unchanged.
- **Allowed files/modules**:
  - `src/interfaces/cli/scheduler_ops.py`
  - `src/application/scan_scheduler.py`
  - `tests/test_scan_scheduler_*.py`
  - `tests/test_cli_runtime_paths.py`, `tests/test_cli_domain_split.py`
  - `docs/AGENT_WIKI.md`
- **Prerequisites**: none.
- **Exact changes**: fail `run-if-due` at CLI and Python boundaries before runtime/config/state/adapter/subprocess use;
  remove legacy child-execution branch/imports; retain decision/mark behavior and parse compatibility.
- **Functions and call path**: `scheduler_ops` rejects the flag before adapter construction; `run_scheduler` rejects
  it before `_resolve_base`/`_resolve_state_path`; no path reaches the old `scan-pipeline` subprocess branch.
- **Invariants**: unsupported execution has zero writes and zero child processes.
- **Tests/validation**: spy runtime-root, config loader, `_resolve_base`, `_resolve_state_path`, state repository, adapter,
  and subprocess; all remain uncalled for every run-if-due flag combination. Run the four test owners above.
- **Completion signal**: tests/docs prove scheduler is decision/mark-only.
- **Stop condition**: a supported production caller is proven unable to migrate without a new user-visible decision.

### S3 — Atomic run-scoped account config

- **Objective**: publish exact config bytes/hash before any account child and remove long-lived input authority.
- **Expected outcome**: overlapping runs for one account have different immutable paths and cannot overwrite or
  consume each other's config.
- **Allowed files/modules**:
  - `src/application/tick_account_execution.py`
  - `src/application/account_run.py`
  - existing account/prepared-worker request plumbing, config serialization, and atomic run-state/workspace owner
  - `tests/test_account_run.py`, `tests/test_tick_account_execution_barrier.py`,
    `tests/test_tick_run_workspace.py`, `tests/test_pipeline_runtime_paths.py`
- **Prerequisites**: none.
- **Exact changes**: one canonical serializer; tick parent retains bytes/path/SHA and publishes before
  `prepare_portfolio_contexts`; both paths are write-once/adopt (same bytes reuse, different bytes fail before
  overwrite); pass the same authority through prepared-worker and AccountRun requests; verify both hashes; remove
  canonical long-lived writer and all downstream config reconstruction.
- **Functions and state transition**: parent account preparation plus the existing request/state writer own
  `serialize -> state write-once/adopt -> compatibility write-once/adopt -> hash verify -> prepared worker -> later
  account child`; `run_one_account` only validates/uses the prepublished authority.
- **Error handling**: any write/archive/hash failure returns typed account failure before pipeline/Close Advice.
- **Invariants**: path contains run id/account; consumed and archived SHA equal `account_config_sha256`.
- **Tests/validation**: concurrent same-account/different-run bytes/path/hash; same-run identical adopt; same-run
  mismatch preserves original bytes; injected publication/archive conflict with zero prepared-worker, pipeline, and
  Close Advice calls; historical long-lived file remains untouched.
- **Completion signal**: no canonical writer or child input references the long-lived override.
- **Stop condition**: an in-scope canonical consumer is proven to require the mutable path.

### S4 — Exact-account option-context and prepared consumer surface

- **Objective**: establish strict raw account validation, explicit empty slices, immutable prepared loading, and
  manual shared-FX fallback before activating the producer.
- **Expected outcome**: every account-scoped source is exact-account; trusted empty slices work; supplied prepared
  authority is consumed without fallback and invalid authority fails before ledger/FX access.
- **Allowed files/modules**:
  - `src/application/positions/context_builder.py`, `src/application/positions/context_cache.py`
  - `src/application/pipeline_context.py`
  - new `src/application/prepared_option_positions_context.py`
  - `src/infrastructure/external_services.py`
  - `src/application/pipeline_runtime.py`, `src/application/pipeline_watchlist.py`, `src/application/account_run.py`
  - corresponding context/prepared/runtime/account tests, including
    `tests/test_pipeline_context_exchange_rates.py`
- **Prerequisites**: S3 accepted.
- **Exact changes**:
  - validate account on raw cached/shared/direct contexts before adapters/defaults;
  - `build_shared_context(..., accounts=...)` emits an explicit empty slice for every requested trusted account;
  - atomic context publication;
  - strict ready/unavailable prepared manifest and exact-bytes loader with workspace/run/account/hash/authority checks;
  - optional internal prepared-manifest plumbing; prepared branch never falls back or calls ledger/FX/global-risk;
  - manual/non-prepared FX path uses shared state when supplied.
- **Functions/types and call path**: add `validate_option_positions_context_account`; extend `build_shared_context`;
  define prepared manifest/payload load/write/validate helpers; plumb the optional manifest through runtime,
  watchlist, and AccountRun. Prepared mode is `manifest -> exact bytes -> raw account validator -> context adapter`.
- **Invariants**: exact account even for zero rows; prepared invalid/unavailable is typed failure; account-less manual
  behavior remains compatible.
- **Tests/validation**: foreign/missing filter and row account; valid trusted empty context; tampered path/hash/account;
  prepared no-fallback/read-count zero; manual shared-FX path precedence.
- **Completion signal**: additive consumer plumbing remains dormant without explicit prepared authority and all
  account-scoped sources share one validator.
- **Stop condition**: a manifest would require persistent schema or a second position projection model.

### S5 — Coherent ledger/FX producer and Close Advice v2 authority

- **Objective**: produce trusted prepared contexts and make pipeline/Close Advice consume those exact facts.
- **Expected outcome**: each started account consumes one trusted batch generation/time/FX authority; any integrity
  mismatch is typed before notification, while unrelated ledger groups continue.
- **Allowed files/modules**:
  - `src/application/ledger/repository.py`, `src/application/ledger/api.py`,
    `src/application/ledger/decision_snapshot.py`
  - `src/application/positions/context_builder.py`
  - `src/application/prepared_option_positions_context.py`
  - `src/application/exchange_rate_loader.py`, `src/application/pipeline_context.py`
  - `src/application/tick_account_execution.py`, `src/application/multi_account_tick.py`
  - `src/application/close_advice_required_data.py`, `src/application/close_advice_runner.py`
  - `src/application/close_advice_report_manifest.py`
  - `src/application/account_run.py` and prepared plumbing from S4
  - corresponding ledger/barrier/FX/Close Advice/multi-account tests
- **Prerequisites**: S3 and S4 accepted.
- **Exact changes**:
  - coherent `read_decision_state_rows_many` transaction and snapshot construction from supplied rows;
  - shared time anchor and strict trusted snapshot postconditions;
  - stable canonical ledger/position/account authority identities including config/broker/ledger binding;
  - optional broker-scoped same-generation global-risk aggregate or explicit `not_required`;
  - one run-level immutable FX observation with typed state and conditional account dependency;
  - atomic account prepared payload/manifests and all-or-none ledger-group re-entry validation;
  - derive provisional requirements, seal required data, then publish/consume plan v2 authority without changing
    stable lot-local requirement IDs;
  - pipeline and Close Advice consume manifest-bound exact bytes only; Close Advice revalidates before output promotion
    and records prepared provenance.
- **Functions/types and data flow**: add repository `read_decision_state_rows_many` and application
  `decision_state_snapshots`; extend context builder with explicit as-of inputs; create/validate prepared payloads;
  evolve Close Advice plan/validator to v2. Flow is `one SQLite group transaction + one time anchor + one FX fact ->
  trusted account projections -> immutable prepared manifests -> provisional requirements -> required-data seal ->
  plan v2 -> pipeline/Close Advice exact-byte consumption -> promotion revalidation`.
- **Error handling**: batch/global trust failure blocks ledger group; local account contract failure isolates only that
  account; partial manifest re-entry blocks the group with zero ledger/FX reads; unrelated groups continue.
- **Invariants**: one batch generation, one time anchor, one FX generation; no prepared ledger/FX/global-risk live read;
  unavailable is never zero-lot; v1 frozen plan is rejected.
- **Tests/validation**:
  - `tests/test_ledger_decision_snapshot.py` and repository transaction tests
  - `tests/test_tick_account_execution_barrier.py`
  - `tests/test_pipeline_context_exchange_rates.py`
  - `tests/test_close_advice_required_data.py`, `tests/test_close_advice_runner.py`
  - `tests/test_multi_account_tick.py`
  - regressions for lifecycle/identity-only concurrent write, projection-untrusted/unavailable, trusted zero-lot,
    cross-midnight/deadline, partial/tampered/cross-generation manifest set, re-entry zero reads, config/broker/ledger
    mismatch, stable lot ID, prepared/global-risk no live read, FX fresh/stale/unavailable, and promotion-time tamper.
- **Completion signal**: every canonical started account has trusted prepared authority and Close Advice verifies it
  twice; spies prove one batch ledger and one FX observation.
- **Stop condition**: a same-transaction multi-account read cannot be added without changing ledger persistence
  semantics or a policy is needed for an unsupported path-risk consumer.

### S6 — Scoped planning, one spot observation, and exact spec execution

- **Objective**: make provider calls equal the approved cross-account plan.
- **Expected outcome**: requested opening scope is not expanded, explicit position extras remain traceable, each
  binding/day has one spot observation, and no undeclared side-expiration pair reaches OpenD.
- **Allowed files/modules**:
  - `src/application/required_data_prefetch_planning.py`
  - `src/application/required_data_planning.py`
  - `src/application/required_data_fetching.py`
  - `src/application/opend_symbol_fetching.py`, `src/application/opend_symbol_fetching_cli.py`
  - `src/application/multi_tick/required_data_prefetch.py`
  - focused planning/budget/in-process/subprocess/receipt/CLI tests, including
    `tests/test_opend_symbol_fetching_cli.py`
- **Prerequisites**: S1 accepted.
- **Exact changes**: filter base opening scope; keep explicit Close Advice extras traceable; run-local spot membership
  cache and explicit observed-None semantics; canonical exact-spec parser; deterministic aggregate; one gateway and
  finalizer; subprocess todo always force-refreshes execution result.
- **Functions/types and call path**: extend `build_cross_account_prefetch_config`,
  `build_required_data_fetch_plan`, `RequiredDataFetchSpec` parse/debug helpers, `fetch_symbol_request`, CLI adapter,
  and prefetch executor. Flow is `scoped union -> exact specs + shared spot observation -> one gateway execution ->
  typed aggregate -> S1 finalizer`.
- **Error handling**: malformed/mixed specs fail before gateway creation; child error/identity disagreement fails the
  aggregate and finalizer; no fallback to Cartesian selectors.
- **Invariants**: every provider relation equals a declared spec; one binding/day has at most one spot observation,
  including failure; one symbol execution has one gateway lifecycle and one final save/finalize.
- **Tests/validation**:
  - `tests/test_required_data_fetch_planning.py`
  - `tests/test_required_data_prefetch_budget.py`
  - `tests/test_required_data_prefetch_inprocess.py`
  - `tests/test_required_data_quote_receipts.py`
  - `tests/test_opend_symbol_fetching_cli.py`
  - regressions for scope, explicit extra, observed `None`, distinct binding/day, exact mixed-side calls, malformed/
    mixed specs, prior successful idempotency record, and gateway/save/finalizer counts.
- **Completion signal**: spies show exact specs and zero duplicate spot observations.
- **Stop condition**: internal exact JSON would have to become an unsupported public API.

### S7 — Parent event barrier and strict shared event consumption

- **Objective**: publish one terminal event fact and eliminate account/consumer divergence.
- **Expected outcome**: all accounts and event-risk consumers use one validated run observation; provider partial/
  failure stays visible, corruption never becomes clean empty, and terminal re-entry performs no event prefetch.
- **Allowed files/modules**:
  - `src/application/multi_account_tick.py`, `src/application/tick_account_execution.py`
  - `src/application/account_run.py`
  - `src/application/events/prefetch.py`, `src/application/events/annotator.py`, existing event store/owner
  - `src/application/daily_decision_brief_service.py`
  - `src/application/close_advice_runner.py`
  - `src/infrastructure/external_services.py`
  - `src/application/pipeline_runtime.py`, `src/application/pipeline_watchlist.py`
  - existing scan-pipeline CLI/request plumbing required to carry `expected_event_identity`
  - `tests/test_event_prefetch.py`, `tests/test_event_risk_warn.py`,
    `tests/test_daily_decision_event_risk.py`, `tests/test_daily_decision_brief_service.py`,
    `tests/test_multi_account_tick.py`, `tests/test_account_run.py`, and Close Advice/pipeline event tests
- **Prerequisites**: S3 accepted; integrate with completed S5 parent ordering.
- **Exact changes**: invoke parent event prefetch once per fresh run; resolve each union symbol through its configured
  provider chain at most once, retaining legitimate cache hits and primary-to-fallback behavior; atomic two-axis
  terminal schema; strict shared loader for all three consumers; remove account-local event lock/retry; terminal
  re-entry validate/reuse only; empty union performs zero provider calls; carry exact expected v2 identity through
  canonical scheduled paths while retaining explicit typed-unknown legacy-v1 manual mode.
- **Functions/types and state transition**: extend event prefetch/store payload and add one mode-explicit loader used
  by annotator, Daily Brief, and Close Advice. Flow is `union symbols -> one parent prefetch -> atomic terminal artifact
  -> expected identity through external service/runtime/watchlist/account request -> strict frozen-v2 consumer load`;
  manual callers use the explicit legacy-v1 typed-unknown branch. Publication failure stops before account start.
- **Error handling**: provider partial/failed remains durable shared fact and existing business unknown/fail-closed;
  publication/integrity failure blocks account start; corruption never becomes clean empty.
- **Invariants**: one run maps to one event generation; each union symbol has at most one configured-chain resolution
  shared by every account (a resolution may use legal fallback/cache behavior); empty union has zero provider calls;
  re-entry has zero provider-chain resolutions.
- **Tests/validation**: exact union coverage, primary/fallback chain preservation, empty union, per-symbol partial/
  all-failed, path/run/hash/schema tamper, v1 manual compatibility, v1 downgrade rejection in frozen mode,
  all-consumer consistency, no AccountRun retry, atomic publication failure, terminal re-entry zero call.
- **Completion signal**: code search and spies show one parent observation and one strict loader contract.
- **Stop condition**: an existing consumer cannot express typed unknown without a new scoring-policy decision.

## Slice order and Gateflow checkpoints

Execute `S1 -> S2 -> S3 -> S4 -> S5 -> S6 -> S7`.

- S1/S2/S3 are logically independent but stay sequential so each receives implementation, focused validation,
  DeepReview, accepted-finding fix/re-review, artifact, and protected commit.
- S4 establishes the consumer and config/FX fallback contracts before S5 activates prepared authority.
- S6 builds on S1's unique finalizer.
- S7 runs last so event ordering is reviewed against the completed position/FX parent barrier.
- A slice may touch only its allowed files. If a newly proven owner is needed, stop that slice and amend/re-review the
  plan rather than silently expanding it.

## Validation strategy

For each slice, run its listed tests with `./.venv/bin/python -m pytest -q`, plus `git diff --check`. Review every
slice using DeepReview and fix/re-review material findings before its commit.

Before aggregate DeepReview:

```bash
./.venv/bin/python -m compileall -q src domain
./.venv/bin/python -m pytest -q \
  tests/test_market_snapshot_fetching.py \
  tests/test_required_data_snapshot.py \
  tests/test_required_data_coverage.py \
  tests/test_required_data_fetch_planning.py \
  tests/test_required_data_prefetch_inprocess.py \
  tests/test_required_data_quote_receipts.py \
  tests/test_opend_symbol_fetching_cli.py \
  tests/test_scan_scheduler_notify_semantics.py \
  tests/test_scan_scheduler_scan_per_account.py \
  tests/test_cli_runtime_paths.py \
  tests/test_cli_domain_split.py \
  tests/test_account_run.py \
  tests/test_pipeline_runtime_paths.py \
  tests/test_pipeline_context_contract_validation.py \
  tests/test_pipeline_context_shared_context.py \
  tests/test_pipeline_context_exchange_rates.py \
  tests/test_tick_account_execution_barrier.py \
  tests/test_close_advice_required_data.py \
  tests/test_close_advice_runner.py \
  tests/test_event_prefetch.py \
  tests/test_event_risk_warn.py \
  tests/test_daily_decision_event_risk.py \
  tests/test_daily_decision_brief_service.py \
  tests/test_multi_account_tick.py
git diff --check
```

Then run the full local pytest suite if prerequisites are available. Expected result is zero failures. Any skipped or
unavailable validation is recorded with reason and residual-risk classification, never silently treated as pass.

## Docs and delivery decision

- S2 updates `docs/AGENT_WIKI.md` to describe scheduler decision/mark-only behavior and canonical tick execution.
- Internal prepared/exact-spec plumbing is not advertised as a user API; if implementation exposes a public flag or
  payload, update the corresponding CLI/tool docs and tests in that slice.
- No migration, version, changelog, release, deployment, service, or production-data action is authorized.
- Gateflow may create a draft PR only after all slice and aggregate reviews pass; it must not merge or mark ready.

## Risks and tracking

### Covered by this plan

- Final CSV/receipt divergence: S1 final readback and commit-record adoption.
- Shadow scheduled execution: S2 early fail-closed boundary.
- Cross-run config overwrite: S3 exact atomic bytes.
- Sequential ledger and mutable prepared rereads: S4/S5 batch transaction and exact-bytes authority.
- Zero-lot/foreign account ambiguity: S4/S5 explicit trusted account postcondition.
- Cartesian fetch and duplicate spot: S6 exact specs and tri-state memo.
- FX/event divergence and retry: S5/S7 terminal run observations.

### Residual risks assigned elsewhere

- Candidate required-data CSV TOCTOU -> later `snapshot-consumer-integrity` work unit.
- Manual multiplier drift outside canonical prefetch -> later config/data identity work unit.
- Receipt O(N²) discovery and failure-budget telemetry -> later performance/observability work unit.
- Path-risk dead code and future multi-binding filename collision -> later dead-code/binding hardening work unit.
- SQLite read transaction freezes the barrier view but does not coordinate external writers after the barrier;
  prepared consumers no longer reread live facts. Cross-process write coordination remains a separate future decision.

### Blocking open questions

None. All failed PlanReview findings have a bounded owner and deterministic validation path in the revised slices.

## Why this remains parsimonious

The plan adds one account prepared-context contract because exact consumer bytes cannot otherwise be proven. Batch
ledger reading is one narrow repository method using existing row readers and SQLite transaction semantics. FX and
event artifacts extend existing run-state files rather than introducing services or generic frameworks. All other
changes strengthen current owners and preserve manual/public compatibility.

## Completion report format

Final closeout records:

- mapping of all seven confirmed problems to changed owners and accepted commits;
- exact validation commands/results and any unavailable checks;
- PlanReview, per-slice DeepReview, aggregate DeepReview, and draft-PR review disposition;
- docs decision and explicit no-release/no-deploy/no-merge status;
- classified residual risks and next owner;
- draft PR URL and next entry point for user review.

## Plan gate decision

- Decision: accepted after `pass-with-risks`; all material findings are `已修复` and residual risks are classified.
- Current gate: `accepted plan commit`.
- Next entry point: create the protected accepted-plan commit, then enter S1 implementation.
