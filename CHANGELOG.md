# Changelog

## Unreleased

## 1.2.33 - 2026-05-13

### Added
- Added run-level required_data prefetch metrics and `required_data_prefetch_summary.json` status exposure through OpenClaw runtime status.
- Added OpenD option-expiration caching by underlier and trading date to reduce repeated `get_option_expiration_date` calls.
- Added same-run required_data prefetch dedupe that merges matching OpenD endpoints while preserving strategy DTE and strike bounds.

### Changed
- Narrowed required_data prefetches by enabled strategy bounds and pushed single-side option-chain requests down to OpenD when only put or call data is needed.
- Kept prefetch completion-first without adding complete/best-effort mode switches, expiration cache switches, or dedupe switches.
- Removed repeated OpenD snapshot and expiration endpoint defaults from `configs/system.json`; code defaults still protect those endpoints and explicit config overrides remain compatible.

### Fixed
- Recorded OpenD rate-limit cooldowns for legacy option-type fallback calls during option-chain fetches.
- Required cached required_data coverage to satisfy requested max DTE before skipping a fetch.
- Avoided marking shared force-prefetch state done when a prefetch run fails, so later accounts can retry.

## 1.2.32 - 2026-05-13

### Added
- Added offline strategy replay analysis for joined candidate outcome rows, including DTE effectiveness, Delta win-rate buckets, symbol risk/return summaries, filter-value diagnostics, and shadow-only dry-run parameter suggestions.
- Exposed the replay analyzer through `./om-agent run --tool strategy_replay_analyze` and `./om strategy-replay analyze`.
- Documented the replay input contract and evidence model in `docs/STRATEGY_REPLAY.md`.

## 1.2.31 - 2026-05-12

### Changed
- Moved the OpenD option-chain rate-limit configuration surface to `runtime.opend_rate_limits.option_chain`, while keeping `runtime.option_chain_fetch` compatible for older local configs.
- Removed the legacy `runtime.option_chain_fetch` default from `configs/system.json`; the built-in `10 calls / 30 seconds` option-chain limit now comes from code defaults unless explicitly configured.

### Fixed
- Serialized file-backed OpenD rate-limit acquisition across independent in-process/subprocess workers to prevent bursts from exceeding shared OpenD windows.
- Recorded server-side OpenD rate-limit responses as a shared cooldown so retries wait for the configured window instead of immediately hammering the endpoint again.

## 1.2.30 - 2026-05-12

### Changed
- Enabled default Sell Put and Sell Call candidate ranking weights for liquidity and risk distance through the system templates.
- Wired configured candidate `score_weights` through the sell-put/sell-call scan pipeline so ranking can use the risk-adjusted score instead of remaining return-only by default.

## 1.2.29 - 2026-05-12

### Changed
- Relaxed default Sell Put yield-enhancement optimizer thresholds for US/HK symbol defaults so volatile names can surface candidates while keeping funding mode and combo-spread limits unchanged.

## 1.2.28 - 2026-05-12

### Added
- Added trade intent normalization for manual intake and Futu normalized deals, making trade side, position effect, and target position side explicit.
- Added `om trade-events` review, replay, void, and repair commands for manual intervention on the trade event ledger.

### Changed
- Allow manual close flows to auto-match a strict unique open lot when `record_id` is omitted, while listing candidates and refusing ambiguous matches.
- Made manual close parsing skip multiplier resolution and rely on contract selectors for safe matching.

### Fixed
- Guarded manual trade-event repair against repeated repair of an already voided event.
- Blocked open-event repair when downstream close or adjust events depend on the original lot identity.
- Included projection previews in trade-event void and repair dry runs before applying ledger changes.

## 1.2.27 - 2026-05-12

### Changed
- Simplified Sell Call strike-floor configuration by replacing `min_if_exercised_total_return` with `min_strike_cost_multiplier`.
- Raised the system Sell Call template floor to `avg_cost * 1.02` while preserving the configured `min_strike` floor.

## 1.2.26 - 2026-05-12

### Changed
- Made multi-account tick default to sequential account execution unless `runtime.multi_account_max_workers` or `runtime.account_max_workers` explicitly opts into account-level parallelism.
- Replaced per-account scheduler CLI subprocess calls with in-process scheduler decisions while keeping the run-level scheduler CLI audit surface.

### Fixed
- Batched scheduler state updates for scanned/notified accounts to reduce OpenClaw cron overhead.
- Reduced nested OpenD/Futu pressure from account-level and symbol-level worker pools that could push cron runs into the 120s timeout.

## 1.2.24 - 2026-05-12

### Changed
- Validated `--default-account` against the active account set for the current tick run.

### Fixed
- Made multi-account tick scan scheduling account-scoped so one account's scheduler state no longer suppresses or drives another account's pipeline run.
- Marked scheduler scans only for accounts whose pipeline actually ran.
- Kept `--no-send` shared last-run metadata observable without marking dry runs as sent.

## 1.2.23 - 2026-05-12

### Changed
- Refined compact notification wording and Markdown layout for per-account reports.

## 1.2.22 - 2026-05-12

### Changed
- Extracted reusable release workflow to DRY `release.yml` and `release-from-version.yml`.
- Opted into Node.js 24 for GitHub Actions to resolve Node 20 deprecation warnings.

## 1.2.21 - 2026-05-12

### Added
- Added per-account notification delivery audits for send start, confirmation, failure reason, message id, retry attempts, and run-level attempted/confirmed counters.
- Added no-candidate heartbeat backfill for scanned accounts that have no candidates when another account in the same run does have candidate messages.
- Added an operator failure-summary notification when one or more per-account notification sends fail.

### Changed
- Changed notification routing to use `notifications.provider` for the delivery adapter and `notifications.channel` for the OpenClaw transport channel, while keeping the legacy `wechat_clawbot` alias compatible.
- Changed OpenClaw notification sending to require a confirmed `message_id` before marking an account notified.
- Updated WebUI, docs, examples, healthcheck, and validation surfaces to default to `provider: openclaw` with `channel: openclaw-weixin`.

### Fixed
- Prevented one account's notification send timeout or failure from silently stopping later account sends.
- Marked scheduler `sent_accounts` only for confirmed per-account deliveries.

## 1.2.17 - 2026-05-11

### Added
- Added a stricter Sell Put yield-enhancement optimizer score that compares Sell Put alone against Sell Put + Long Call before recommending the long Call.

### Changed
- Yield-enhancement ranking now prioritizes optimizer score, scenario-score lift, downside breakeven deterioration, and combo spread before falling back to the existing scenario score ordering.

## 1.2.16 - 2026-05-11

### Added
- Added `candidate_rank_explain` as a read-only Agent diagnostic tool for explaining existing candidate CSV ranking scores, score components, inputs, warnings, and optional baseline rank changes.
- Added `explain_candidate_rank()` to the candidate engine so ranking explanations reuse the canonical score calculation instead of introducing another ranking path.

## 1.2.15 - 2026-05-11

### Changed
- Rewrote the README into a product-oriented guide covering user onboarding, common workflows, strategy models, configuration, notifications, Agent safety defaults, scheduling, troubleshooting, and documentation navigation.
- Extracted candidate ranking score calculation into the canonical candidate engine with explicit score weights and explainable score components.
- Made the legacy DataFrame candidate strategy wrapper delegate sorting to `candidate_engine.rank_candidate_rows()`, leaving it as an adapter for DataFrame/reject-log/layered selection behavior instead of a separate ranking implementation.

## 1.2.14 - 2026-05-11

### Changed
- Split OpenD symbol required-data ownership so option-chain fetching, market-snapshot fetching, and required-data output writing live in separate application modules.
- Updated required-data, close-advice, CLI, agent-tool, and prefetch callers to use the new output/planning owners instead of treating `opend_symbol_fetching.py` as the owner for every OpenD concern.

### Fixed
- Kept snapshot fallback, expiration rate limiting, output preservation on fetch errors, and owner-boundary coverage intact after the OpenD hot-path split.

## 1.2.13 - 2026-05-11

### Changed
- Moved the operational healthcheck owner from `scripts/healthcheck.py` into `src.application.healthcheck_runner`, with structured results and the legacy human report formatter kept behind the application service.
- Extracted OpenD required-data prefetch lifecycle pieces into `src.infrastructure.futu_gateway_pool` and `src.application.multi_tick.prefetch_coordinator`, separating gateway reuse and prefetch scheduling from the hot-path fetch entrypoint.

### Fixed
- Removed the healthcheck notify wrapper's subprocess dependency on `scripts/healthcheck.py`.
- Kept OpenD prefetch endpoint reuse keyed by host/port/cache settings while moving the lifecycle policy out of `required_data_prefetch.py`.

## 1.2.12 - 2026-05-11

### Changed
- Moved OpenD watchdog, Futu doctor, and cash footer runtime logic out of `scripts/` into application/infrastructure modules, leaving scripts as operational CLI wrappers.
- Consolidated DataFrame candidate filtering around `candidate_engine` return and risk gates so `candidate_strategy` only adapts, ranks, and formats reject logs.

### Fixed
- Removed application-layer subprocess/JSON-stdout coupling for watchdog, doctor, and cash footer flows.

## 1.2.11 - 2026-05-11

### Changed
- Restored Covered Call/Sell Call assigned-return hard filtering with `min_if_exercised_total_return`, using account `avg_cost` as the cost basis.
- Documented the default `0.0` assigned-return floor in system config and strategy docs.

## 1.2.10 - 2026-05-11

### Changed
- Removed legacy `scripts.option_candidate_strategy` and `scripts.pm_bridge` compatibility owners after callers moved to domain/application modules.
- Added boundary coverage so tests fail if removed business-script owners are reintroduced.

## 1.2.9 - 2026-05-11

### Changed
- Redesigned monthly option income reporting around cashflow, realized PnL, and open-basis attribution views.
- Updated CLI and agent monthly income output to expose cashflow, realized, open-basis, and yield-enhancement detail rows while keeping `premium_received_gross` and `realized_gross` as compatibility fields.

### Fixed
- Counted buy-to-close cash outflows and long call open/close cashflows in monthly income reports.
- Calculated long option realized PnL as close proceeds minus open cost instead of using the short-option premium formula.

## 1.2.7 - 2026-05-11

### Added
- Added shared risk-capacity helpers for Sell Put cash headroom and Covered Call share coverage decisions.

### Changed
- Hardened Sell Put and Covered Call gating so missing multiplier, currency, cash-secured basis, or cash requirement data fails closed instead of using guessed defaults.
- Propagated cash-secured unavailable diagnostics through candidate filtering, cash-headroom queries, and cash footers so unknown cash usage is visible instead of silently reported as available.

### Fixed
- Stopped defaulting short-call locked shares to multiplier 100 when the real contract multiplier is missing.
- Stopped defaulting short-put secured cash currency or candidate cash requirement currency to USD when the real currency is missing.
- Stopped summary generation from inventing `cash_required_usd` with `strike * 100`.

## 1.2.3 - 2026-05-10

### Added
- Added `./om config explain` to show the final layered value, source layer, and override trace for a config key.

### Changed
- Consolidated portfolio data-config examples around a single `portfolio.sqlite.json` shape that can also hold optional Feishu holdings and option-position mirror table refs.
- Made `option_positions.sync_to_feishu.enabled` available as a runtime config override, so `configs/user.common.json` can enable or disable Feishu option-position mirror writes across US/HK.
- Allowed `symbol_defaults` in user/common config to override system defaults before they are applied to each `symbols[]` item.

## 1.2.2 - 2026-05-10

### Added
- Added an optional `configs/user.common.json` authoring layer for shared US/HK user overrides, with CLI controls, example config, and documentation.

### Changed
- Changed the multi-tick OpenD watchdog fallback so `retry_enabled` defaults to enabled when `watchdog.retry_enabled` is omitted, matching the shipped system default.

## 1.2.1 - 2026-05-10

### Changed
- Added bounded account-level and watchlist-symbol parallelism for unified tick scans while preserving deterministic account and symbol output ordering.
- Reused shared required-data prefetch state across concurrent account workers to avoid duplicate fetch work in one tick run.

### Fixed
- Serialized option-position maintenance across concurrent account workers so auto-close projection writes do not race on the shared option positions store.
- Avoided concurrent legacy `output` symlink refreshes during multi-account runs by keeping that compatibility update to single-account execution.

## 1.2.0 - 2026-05-10

### Added
- Added `option_positions.sync_to_feishu.enabled` as an explicit data-config switch for Feishu `option_positions` mirror writes, defaulting to off.

### Changed
- Guarded post-write option-position auto sync and `./om option-positions sync-feishu --apply` writes behind the new switch, reporting disabled writes as skipped instead of creating remote rows.
- Updated portfolio data-config examples, configuration docs, and repair guidance to show the default-off Feishu mirror switch.

### Fixed
- Rejected `./om option-positions sync-feishu --apply --dry-run` as an invalid mixed mode to prevent accidental remote writes.

## 1.1.7 - 2026-05-09

### Changed
- Completed release metadata alignment for `v1.1.7`.
- Added automatic GitHub Release publishing from `main` when the top-level `VERSION` changes, so `1.1.7` no longer waits on a separate manual tag push.

## 1.1.6 - 2026-05-08

### Added
- Added OpenClaw profile support for agent runtime and readiness tools, including path, account, cron job, and freshness defaults.
- Added OpenClaw readiness diagnostics for runtime freshness, per-account output summaries, notification route checks, optional cron inspection, and machine-readable next actions.

### Changed
- Hardened agent write-capable surfaces so VERSION updates and account config mutations require explicit write-tool enablement and confirmation, with account commands supporting dry-run previews.

## 1.1.5 - 2026-05-08

### Fixed
- Mapped the config-level `wechat_clawbot` notification channel to the actual OpenClaw transport channel `openclaw-weixin` so unified tick, WebUI test sends, healthcheck notifications, and OpenD alerts no longer call OpenClaw with an unknown channel.

## 1.1.4 - 2026-05-07

### Added
- Added `wechat_clawbot` as a supported notification channel, routing it through OpenClaw message sending while preserving the Feishu App sender for `feishu`.
- Exposed 微信 Clawbot as a WebUI notification channel option and documented its target/secrets semantics.

## 1.1.3 - 2026-05-07

### Changed
- Tightened shipped starter defaults so onboarding configs no longer silently rely on market-level multiplier fallbacks and now surface starter placeholder warnings more clearly across healthcheck and WebUI.

### Fixed
- Removed remaining default-config/runtime drift in the WebUI notification model so saved config fields now match actual send semantics.

## 1.1.2 - 2026-05-07

### Changed
- Aligned shipped starter configs with current runtime defaults so US/HK DTE windows and close-advice spread defaults no longer drift from code behavior.
- Removed market-level multiplier starter defaults from onboarding configs so new installs prefer payload/cache/per-symbol multiplier sources over silent money-math fallbacks.

### Fixed
- Split pure config validation from runtime notification readiness checks and surfaced placeholder starter values through healthcheck/init warnings instead of hiding them.
- Removed the ineffective `notifications.enabled` WebUI toggle so saved config fields now match actual notification send logic.

## 1.1.1 - 2026-05-07

### Fixed
- Changed unified tick idempotency from start-time success writes to in-progress claims with stale recovery and final completion writes.
- Required the WebUI token before running local-write tools and rejected WebUI tool path inputs outside the repository/runtime-config roots.
- Reused shared symbol and account normalization for WebUI/watchlist mutations so aliases and account labels persist canonically.

### Changed
- Reused the RunLogger run id for run directories, audit events, and current-run pointers.
- Added install constraints for reproducible dependency resolution.

## 1.1.0 - 2026-05-06

### Added
- Added Sell Put 收益增厚 recommendations that pair qualifying Sell Put candidates with the best same-expiration buy-Call strike, including separate/inline outputs and notification rendering.
- Added expected-move scenario scoring for the paired Put/Call plan using option-chain IV, DTE, spot, liquidity, spread, and funding coverage.
- Added automatic Call-chain required-data planning for 收益增厚, so `sell_call.enabled=false` symbols can still fetch the Call data needed for recommendations.

### Changed
- Simplified 收益增厚 configuration to a single top-level `yield_enhancement.enabled=true` switch on each symbol, with optional tuning fields only when stricter Call bounds, liquidity, funding, or scenario thresholds are needed.

## 1.0.12 - 2026-05-06

### Added
- Added the agent-facing `version_update` tool for dry-run-first local `VERSION` updates with explicit apply mode.

### Changed
- Documented scheduled and long-running task entry points for tick monitoring, scheduler checks, trade intake, Feishu mirroring, and version checks.
- Tightened manual `/om` option-intake command parsing around account/action flags, apply/dry-run aliases, and record-id shorthand.

### Fixed
- Restored close-message parsing for common close-price aliases and buy-to-close wording.

## 1.0.11 - 2026-05-06

### Changed
- Moved the agent tool manifest, response contract, and handler ownership into `src/application` while keeping `scripts/agent_plugin/*` as compatibility facades.
- Moved unified tick and WebUI implementation ownership behind `src/application/multi_account_tick.py` and `src/interfaces/webui/server.py`, leaving script paths as thin compatibility entry points.

### Fixed
- Restored direct multi-account tick help via the unified `./om run tick --help` entrypoint.

### Documentation
- Clarified that `query_cash_headroom` is the agent-facing wrapper for `query_sell_put_cash(...)` and documented `lx` / `sy` account examples.
- Documented that single-account tick execution is now a one-account invocation of the unified tick chain rather than a separate business path.

## 1.0.10 - 2026-05-05

### Changed
- Calculated covered-call net premium annualized return against current spot opportunity cost while keeping exercised total return on the holding cost basis.
- Promoted monthly option income statistics to the agent-facing `monthly_income_report` tool.
- Added agent-facing read tools for version checks, config validation, scheduler decisions, and option-position ledger diagnostics.

## 1.0.9 - 2026-05-04

### Fixed
- Recorded structured failed intake state and audit diagnostics when trade normalization or resolver persistence raises, preventing received Futu fills from disappearing without a terminal state.
- Isolated per-fill OpenD push callback failures so one bad deal cannot interrupt later rows in the same push batch.
- Canonicalized option-position trade event symbols and close projection matching on both sides, allowing legacy HK aliases such as `00700.HK` to close the canonical `0700.HK` lot.
- Returned structured unresolved diagnostics for invalid open-fill numeric fields such as zero contracts instead of letting validation exceptions bypass intake state recording.
- Moved deal IDs between intake state buckets on status changes so retryable unresolved entries are removed after a later applied or failed outcome.

## 1.0.8 - 2026-05-04

### Fixed
- Restored spaced broker trade-side aliases such as `sell short`, `short sell`, and `buy to close` so valid option fills continue to normalize to open/close effects after the shared contract identity refactor.

## 1.0.7 - 2026-05-04

### Changed
- Centralized symbol identity normalization across intake, multiplier fallback, OpenD lookup, cash-secured usage, portfolio context, and watchlist paths so HK display names and Futu codes resolve through the same canonical contract.
- Consolidated trade contract identity normalization for side, position effect, expiration, option type, strike keys, and quote keys across auto-intake, ledger projection, close-advice, and agent scan summaries.
- Reused shared account and currency normalization in position-event persistence, portfolio context, close-advice, cash-secured aggregation, fee calculation, and agent summaries to keep HK/CNY/USD aliases and account labels consistent.

## 1.0.6 - 2026-05-04

### Fixed
- Normalized Futu HK option display names such as `泡泡玛特 260528 135.00 沽` to their canonical underlier before multiplier resolution.
- Resolved the remaining auto-trade intake multiplier fallback gap when the active listener config lacks HK `intake` defaults but receives valid HK Futu option fills.

## 1.0.5 - 2026-05-04

### Fixed
- Preserved broker fill timestamps from Futu trade messages during option intake so persisted events no longer fall back to local execution time.
- Persisted valid Futu option open fills that omit multiplier by resolving multiplier from payload data, contract metadata, configured symbol overrides, or market defaults.
- Canonicalized Futu option symbols before intake persistence and close matching, preventing non-canonical broker payload text from drifting ledger and timeline state.
- Stored retryable unresolved intake records with structured diagnostics when required normalization fields are still missing.

## 1.0.4 - 2026-05-02

### Fixed
- Refreshed local option-position projections before expired-position auto-close runs so stale `position_lots` cannot create duplicate close attempts after trade events have already closed a lot.
- Treated already-closed or zero-open expired lots as skipped auto-close decisions instead of errors, preventing stale local candidates from producing false `contracts_open <= 0` alerts.
- Included skipped auto-close counts in summaries only when there is an actual close or error, while keeping skipped-only maintenance runs silent.

## 1.0.3 - 2026-05-02

### Fixed
- Used a compact auto-close notification template when scan gating skips the options monitor, preventing skipped-scan auto-close alerts from including regular candidate counts and cash footers.

## 1.0.2 - 2026-05-02

### Fixed
- Moved expired option-position auto-close into per-account maintenance so it can run, report, and notify even when scan gating skips the pipeline.
- Preserved scheduler state selection when trading-day guards block scans, preventing blocked-market runs from falling back to the shared scheduler state file.
- Hardened auto-close configuration validation and summary formatting so invalid grace/max-close values fail explicitly instead of silently changing close timing.

## 1.0.1 - 2026-05-01

### Fixed
- Normalized option expiration timestamp display and DTE calculations to Asia/Shanghai business dates, so midnight Beijing records no longer render one UTC calendar day early in close-advice and position contexts.

## 1.0.0 - 2026-05-01

### Changed
- Promoted the agent-facing tool surface to the first stable release after adding local-runtime diagnostics and OpenClaw readiness checks for safer Codex, Claude Code, and OpenClaw usage.
- Documented the release/update-check contract around Git tags, `VERSION`, and agent tool references so remote version checks have a stable source of truth.

## 0.4.8 - 2026-05-01

### Changed
- Made scheduled config validation cache writes happen only after validation succeeds, preventing failed scheduled configs from being treated as already validated.
- Removed `sys.argv` mutation from the multi-account tick application entrypoint and passed CLI arguments explicitly into the reusable multi-tick main function.
- Moved multi-account notification preparation details into application helpers, keeping the operational multi-tick script focused on orchestration.

## 0.4.7 - 2026-05-01

### Changed
- Made multi-account notifications explicitly per-account by introducing account delivery batch naming in the application layer while preserving the existing delivery contract for compatibility.
- Removed the unused merged notification formatter and updated multi-account CLI/docs/tests to state that each account sends one message to the configured target with isolated failures.
- Simplified multi-tick scheduler result state by removing an always-empty `markets_to_run` field.

## 0.4.6 - 2026-05-01

### Changed
- Unified OpenD spot, option-expiration, option-chain, and market-snapshot calls behind shared endpoint-specific rate-limit configuration and diagnostics, so required-data and close-advice refreshes use the same throttling contract.
- Ensured close-advice held-position coverage can fetch missing option quotes via the converged OpenD path while marking last-price-only or unusable quotes as not evaluable instead of emitting close suggestions.
- Moved reusable OpenD symbol-fetch orchestration into the application layer, leaving the script as a CLI adapter, and made multiplier-cache writes lock-protected and atomic.
- Tightened runtime config validation for OpenD rate-limit endpoint names and close-advice item limits to fail fast on ignored typos or decimal values.

## 0.4.5 - 2026-05-01

### Changed
- Inferred manual option-position currency from normalized symbols when no explicit currency is provided, so HK symbols such as `0700.HK` record as `HKD` while US symbols default to `USD`
- Reused the same symbol-based currency inference in chat-style trade intake and manual position writes to keep dry-run previews, persisted trade events, and position lots aligned

## 0.4.4 - 2026-05-01

### Changed
- Routed OpenD option-chain requests through a shared coordinator with cross-process file limiting and per-expiration cache shards, reducing `get_option_chain` rate-limit failures during required-data refreshes
- Preserved existing parsed required-data CSVs when OpenD returns structured empty errors, while surfacing rate-limit diagnostics as `OpenD 限频` in close-advice output
- Allowed holdings-only Feishu data configs in agent healthcheck so external holdings accounts do not require an unrelated `feishu.tables.option_positions` bootstrap table

## 0.4.2 - 2026-04-30

### Changed
- Refactored option-position projection around stable local lot `record_id` targets so runtime close/adjust replay no longer depends on mutable projected `source_event_id` state
- Added projection diagnostics and a read-only `option_positions inspect` flow to explain unmatched or conflicting close/adjust events and export reproducible local incident state
- Restricted direct `position_lots` field updates to Feishu sync metadata only, preventing business-state drift outside canonical `trade_events -> position_lots` replay while keeping closed lots out of downstream context and notify paths

## 0.4.1 - 2026-04-30

### Changed
- Unified sell-put cash gating around upstream candidate filtering while preserving defensive consistency in standalone alert/detail renderers, so `base CNY`, `total CNY`, and `USD` fallback paths no longer disagree about whether a candidate can still be added
- Carried `cash_available_total_cny` and `cash_free_total_cny` through candidate enrichment, processor summaries, canonical normalization, and notification rendering so merged cash footers, alert text, and per-contract detail views share the same cash semantics
- Hardened standalone `alert_engine` / `render_sell_put_alerts` replay flows against unfiltered input CSVs by downgrading or explaining cash-insufficient sell-put rows instead of emitting contradictory high-priority or positive judgment text

## 0.4.0 - 2026-04-30

### Changed
- Hardened option-position close projection so bootstrap seed lots and historical `manual-close-*` events rebuild correctly from canonical `trade_events -> position_lots`
- Made manual close events carry explicit lot targets via `close_target_source_event_id` while preserving legacy `record_id` replay compatibility for existing repair history
- Prevented explicit-target close events from partially applying during reprojection when event quantity exceeds the targeted lot's remaining open contracts

## 0.3.7 - 2026-04-30

### Changed
- Redesigned required-data fetch planning so `sell_put` and `sell_call` derive independent near/far strike bounds before merging compatible OpenD requests, ensuring covered-call target strikes are fetched instead of being filtered only at scan time
- Removed legacy `target_otm_pct_*` planning semantics, standardized fetch/debug terminology on side-specific near/far bounds, and kept fetch-plan diagnostics backward compatible by emitting both `coverage` and `bounds_coverage`

## 0.3.6 - 2026-04-29

### Changed
- Refined SQLite and Feishu sync flows by fixing incremental sync and remote-prune edge cases, refreshing Feishu tenant tokens once on auth failures, and simplifying bootstrap, transaction, payload, and context-building paths without adding extra fallback layers

## 0.3.5 - 2026-04-29

### Changed
- Tightened Claude Code / OpenClaw repository guidance so agents prefer read-first analysis, `./om-agent` / `./om` entry points, and low-risk validation steps before direct runtime Python scripts or live operational commands

## 0.3.4 - 2026-04-29

### Changed
- Suppressed the close-advice fallback `行情质量不足` summary in notifications when `spread_too_wide` is the sole quote-quality issue and no strong/medium close suggestions were generated, reducing expiry-day noise without changing evaluation logic

## 0.3.3 - 2026-04-29

### Changed
- Stopped writing canonical option contract fields (`expiration`, `strike`, `multiplier`, `premium`) into `note` for new or adjusted position lots, leaving them in structured fields only
- Preserved backward-compatible readers for historical `note` tokens while making adjustment flows actively scrub legacy `exp=` / `strike=` / `multiplier=` / `premium_per_share=` tokens when those fields are updated
- Kept close advice, reporting, context building, trade-intake matching, and manual close flows aligned on the structured lot fields so old note payloads are no longer required for steady-state behavior

## 0.3.2 - 2026-04-29

### Changed
- Improved close-advice quote evaluation to accept reliable bid/ask-derived mids, reducing false `missing_quote` / `missing_mid` skips when required-data rows lack a precomputed mid
- Split close-advice account summaries into system issues versus market-quality issues so wide spreads and thin liquidity no longer read like runtime failures
- Hardened Feishu/bootstrap and repository write paths against incomplete option lots, and fixed legacy auto-close quantity fallback so records without `contracts_open` no longer report applied closes on zero contracts

## 0.3.1 - 2026-04-29

### Changed
- Added first-class SQLite contract columns for `position_lots` (`expiration`, `strike`, `multiplier`), backfilled legacy rows on startup, and exposed local expiry-aware listing so near-expiration queries no longer need Feishu as a read-time fallback
- Propagated contract metadata through `option_positions_context`, close-advice preparation, reporting, manual close events, and trade-intake close matching so downstream consumers consistently read canonical lot fields instead of ad hoc note parsing
- Hardened trade-open workflow construction against optional contract fields by preserving nulls instead of serializing `"None"` into generated commands and notes

## 0.3.0 - 2026-04-29

### Changed
- Stabilized local option-position repair workflows around the canonical `trade_events -> position_lots` model by adding operator-safe rebuild, lot history inspection, event voiding, and controlled lot adjustment paths
- Preserved Feishu mirror sync metadata across local reprojection, added optional remote orphan cleanup during repairs, and documented the repair playbook so invalid records no longer pollute downstream monthly income and premium reporting
- Unified `position_id` generation on canonical `symbol` values instead of alias names so SQLite and Feishu stop drifting on underlier naming for new records

## 0.2.0-beta.9 - 2026-04-29

### Changed
- Hardened local option-position repair workflows around the canonical `trade_events -> position_lots` model by adding CLI repair primitives for rebuild, lot history inspection, event voiding, and controlled lot adjustment
- Preserved Feishu sync metadata across local reprojection, added optional remote orphan cleanup for mirror rows, and documented the operator repair playbook so repaired records no longer leak into downstream monthly income and premium reporting

## 0.2.0-beta.8 - 2026-04-28

### Changed
- Unified expiration normalization for OpenD explicit-expiration fetch paths so held-option requests consistently convert `YYYY-MM-DD`, Unix seconds, and Unix milliseconds into the `YYYY-MM-DD` format required by `get_option_chain`
- Hardened close-advice preparation and required-data fetch entrypoints against timestamp expirations, preventing `wrong time or time format` regressions when open positions carry numeric expiration values

## 0.2.0-beta.7 - 2026-04-28

### Changed
- Hardened close-advice held-expiration pricing by forcing exact-contract coverage refreshes to bypass stale same-day option-chain cache when coverage is missing
- Fixed OpenD explicit-expiration cache semantics so cache coverage is proven by returned chain rows rather than declared expiration lists, preventing false full-coverage hits for partially fetched chains

## 0.2.0-beta.6 - 2026-04-28

### Changed
- Refactored close advice around exact-contract pricing so each open position is priced by its concrete symbol, option type, expiration, and strike before any suggestion tier is computed
- Made close advice self-heal required-data coverage for held expirations, merge refreshed rows back into required_data, and classify unpriced positions as not evaluable instead of mixing them into normal advice tiers

## 0.2.0-beta.5 - 2026-04-28

### Changed
- Redesigned close-advice required-data preparation to fetch option chains by open position contract coverage, passing explicit held expirations, option types, and strike bounds instead of relying on symbol-level recent-expiration scans
- Added required-data coverage diagnostics so close advice can distinguish missing expiration/contract coverage from quote usability issues, keeping OpenD fallback limited to last-mile quote repair when the contract is already present in required_data

## 0.2.0-beta.4 - 2026-04-28

### Changed
- Unified shared symbol canonicalization across close advice, watchlist writes, option-position writes, multiplier refresh, Futu portfolio context, trade detail enrichment, and trade event normalization so aliases like `POP` consistently resolve to canonical symbols such as `9992.HK`
- Added system-level symbol normalization contract coverage plus repository guardrails documenting that user-entered symbols, broker raw payloads, and OpenD/Futu underliers must canonicalize before entering business logic

## 0.2.0-beta.3 - 2026-04-28

### Changed
- Added a final Futu option-code root fallback for trade intake so payloads like `HK.POP260528P150000` can resolve `symbol=9992.HK` even when no underlying fields are present in the raw push or lookup response

## 0.2.0-beta.2 - 2026-04-28

### Changed
- Unified Futu underlying symbol normalization during trade enrichment and deal normalization so raw fields like `owner_stock_code=HK.09992` resolve into canonical symbols such as `9992.HK` for automatic option bookkeeping

## 0.2.0-beta.1 - 2026-04-28

### Changed
- Completed Futu auto trade-intake semantic parsing for raw deal payloads by deriving option fields from option codes, mapping raw `trd_side` values into open/close semantics, and allowing these trades to proceed into automatic option bookkeeping

## 0.1.0-beta.14 - 2026-04-28

### Changed
- Completed Futu auto trade-intake semantic parsing for raw deal payloads by mapping `trd_side` values like `SELL_SHORT` and `BUY_BACK`, and inferring option currency from the option code when standard fields are absent

## 0.1.0-beta.13 - 2026-04-28

### Changed
- Made trade-intake normalization accept Futu option-code payloads by backfilling lookup row fields and deriving symbol, option type, strike, and expiration from enriched OpenD trade data

## 0.1.0-beta.12 - 2026-04-28

### Changed
- Hardened auto trade intake account enrichment by retrying OpenD order/deal lookups without `acc_id` when push payloads omit the futu account id
- Added explicit trade-intake diagnostics for missing account mapping, including visible account fields, attempted lookup paths, and enrichment audit events

## 0.1.0-beta.11 - 2026-04-28

### Changed
- Made close advice fee-aware so post-fee non-positive buybacks no longer emit close recommendations
- Grouped standalone close-advice markdown by account, aligned notify row counts with rendered output, and surfaced spread-blocked quote issues in fallback summaries

## 0.1.0-beta.10 - 2026-04-27

### Changed
- Prevented cross-account option position sync collisions by requiring account-aware business-lot matching for shared `position_id` values
- Preserved schema-aware numeric payload coercion and explicit conflict reporting in the beta10 sync behavior shipped from `origin/main`

## 0.1.0-beta.9 - 2026-04-27

### Changed
- Hardened option position Feishu sync payload typing with schema-aware numeric coercion before create/update writes
- Added explicit duplicate-business-key conflict reporting for rows blocked by repeated remote option position identifiers

## 0.1.0-beta.8 - 2026-04-27

### Changed
- Preserved bootstrapped option positions by migrating snapshot lots into synthetic trade events before projection rebuilds
- Kept best-effort Feishu sync wiring available on manual option position writes without changing local-write success behavior

## 0.1.0-beta.7 - 2026-04-27

### Changed
- Simplified cash footer account config so notifications default to the top-level `accounts` list
- Made WebUI show effective cash footer accounts and avoid persisting redundant `cash_footer_accounts` overrides

## 0.1.0-beta.6 - 2026-04-27

### Changed
- Clarified cash footer wording so base-CNY and total-CNY cash figures are labeled by actual data scope
- Narrowed close-advice quote lookup to the current market run and surfaced quote-failure samples in notifications
- Improved auto trade intake account resolution by enriching push payloads via `order_id`/`deal_id` lookups when account ids are absent
- Cleaned legacy schedule fields from the US example config and preserved explicit non-Futu fetch sources

## 0.1.0-beta.5 - 2026-04-27

### Changed
- Removed account-level primary/backup source fallback semantics while preserving `external_holdings` as a distinct primary source identity
- Simplified healthcheck and WebUI account surfaces to expose a single primary source path
- Cleaned stale fallback wording in tests, docs, and historical notes to match the single-source model

## 0.1.0-beta.4 - 2026-04-27

### Added
- Version update check via `./om version` against remote `origin` git tags
- Shared version-check service for CLI and WebUI consumption

### Changed
- WebUI surfaces a non-blocking header status for release update checks
- Release documentation now records the git-tag based update-check contract

## 0.1.0-beta.3 - 2026-04-26

### Added
- 6-module WebUI configuration center with modular frontend structure
- Per-account OpenD holdings runtime support for Futu-backed accounts
- Feishu app notification secrets example and stronger local notification wiring

### Changed
- Rewrote README and key docs into product-facing install/init/use guidance
- Reorganized WebUI code into API, actions, model, shared, state, and panel layers
- Repositioned `scripts/send_if_needed_multi.py` as a compatibility/developer launcher while preferring unified CLI docs

### Fixed
- Futu/OpenD doctor and healthcheck false-negative handling under noisy SDK output
- Futu SDK compatibility for `get_option_chain` when `is_force_refresh` is unsupported
- Pipeline/runtime compatibility issues around `append_cash_summary`, holdings context wiring, and multi-account launcher argument flow
- Option intake parsing by inferring currency from symbol when explicit currency is absent

## 0.1.0-beta.2 - 2026-04-24

### Added
- Local plugin initialization flow for standalone setup
- Web UI phase 1/2 productization, including server and frontend updates
- Expanded public docs and example configs for agent/plugin and portfolio setup

### Changed
- Productized standalone install flow and reduced legacy pm fallback coupling
- Updated public tool surface, config discovery, and release-facing smoke coverage

### Fixed
- Lazy-load agent tool handlers on the `spec` path
- Correct futu mapped account id typing for cash queries
- Sanitize futu account ids in release-facing tests

## 0.1.0-beta.1 - 2026-04-23

### Added
- Public local agent launcher: `./om-agent`
- Public JSON tool manifest via `./om-agent spec`
- Public agent tool surface:
  - `healthcheck`
  - `scan_opportunities`
  - `query_cash_headroom`
  - `get_portfolio_context`
  - `manage_symbols`
  - `preview_notification`
- Public config discovery with `OM_CONFIG_DIR`, `OM_CONFIG_US`, `OM_CONFIG_HK`, `OM_DATA_CONFIG`
- Write-tool gate with `OM_AGENT_ENABLE_WRITE_TOOLS`
- Install script: `scripts/install_agent_plugin.sh`
- Public docs for agent integration, getting started, and tool reference
- Repository `LICENSE` and `SECURITY.md`
- Public release metadata: `VERSION`, release validation, and generated release notes
