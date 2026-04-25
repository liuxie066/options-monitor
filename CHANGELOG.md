# Changelog

## Unreleased

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
