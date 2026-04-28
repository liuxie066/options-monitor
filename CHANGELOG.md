# Changelog

## Unreleased

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
