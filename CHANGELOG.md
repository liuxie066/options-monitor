# Changelog

## Unreleased

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
