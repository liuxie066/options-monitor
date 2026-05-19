# Changelog

## Unreleased

## 1.2.75 - 2026-05-19

### Fixed
- Resolved manual option intake ledger stores from the runtime config path so `/var/lib/options-monitor/config.*.json` writes to the runtime active SQLite store without requiring `OM_RUNTIME_ROOT`.
- Added manual intake ledger target output and fail-closed protection when populated active/default stores indicate possible ledger drift.
- Standardized human-readable trade time output on Beijing time across manual intake summaries, trade intake receipts, trade-event review output, and option-position history/inspection payloads.

## 1.2.74 - 2026-05-19

### Added
- Added top-level `./om status` as a terminal-friendly, read-only wrapper over `runtime_status`, with `--json` for the raw agent-tool envelope.
- Added top-level `./om runs` to list and inspect local runtime run snapshots from `output_runs`.
- Added top-level `./om logs` to tail run audit files and service logs from the terminal.
- Added read-only `runtime_runs` and `runtime_logs` agent tools for Clawbot/agent access to the same runtime evidence as `./om runs` and `./om logs`.
- Added `runtime_runs` and `runtime_logs` snapshots to AI Cofunder bundles so handoffs use the same terminal evidence surfaces.

## 1.2.73 - 2026-05-19

### Changed
- Preserved symlink repo roots in service rendering and defaulted auto-upgrade config paths to runtime-root configs.
- Prepared release `.venv` runtime dependencies during confirmed service upgrades before switching the `current` symlink.
- Reused the current Python executable for tick child processes instead of assuming every release directory already has `.venv/bin/python`.

## 1.2.72 - 2026-05-19

### Added
- Added top-level `./om doctor`, `./om setup`, `./om update check/apply/rollback`, and safe `./om config get/set` operator entrypoints.

### Changed
- Render opt-in auto-upgrade services through `./om update apply` while keeping legacy `./om service upgrade*` commands compatible.

## 1.2.71 - 2026-05-19

### Fixed
- Use parsed Futu fill timestamps for manual BTC close preview and write paths when available, while preserving execution-time fallback.

## 1.2.70 - 2026-05-19

### Changed
- Render US and HK systemd tick timers with market-timezone calendar-aligned 10-minute boundaries while leaving scheduler run-point decisions unchanged.

## 1.2.69 - 2026-05-19

### Added
- Added opt-in service release upgrade commands and timers: `service upgrade-check`, dry-run/confirmed `service upgrade`, and dry-run/confirmed `service rollback`.
- Surfaced the latest service upgrade status in runtime status.

## 1.2.68 - 2026-05-19

### Added
- Added checkpointed `./om option-positions verify-projection` to validate `position_lots` by replaying canonical `trade_events`, with latest report and checkpoint artifacts under option-position state.
- Surfaced the latest projection verification status in option-position inspection and runtime status.
- Added a rendered daily projection verification service/timer that runs at 06:00 Beijing time.
- Moved rendered expired auto-close service/timer execution to 05:30 Beijing time.

### Removed
- Removed the external option-position snapshot reconciliation command and loader so reconciliation is internal event-vs-position projection verification only.

## 1.2.67 - 2026-05-19

### Added
- Added Linux service preflight checks for env-file shape, runtime directory permissions, output symlink state, and generated runtime config metadata.
- Added `./om service repair-output` to migrate a real runtime `output` directory into `output_accounts/<default-account>` and replace it with the required symlink.
- Added OpenD Telnet readiness reporting to healthcheck and Futu doctor outputs.

### Changed
- `./om service render` now always writes `OM_RUNTIME_ROOT` into systemd units and supports optional deploy identity via `--deploy-user` / `--deploy-home` or `OM_DEPLOY_USER` / `DEPLOY_USER`.
- Runtime config JSON parse errors now include precise file, line, and column diagnostics.
- Standardized user-facing call-side strategy naming on Sell Call to match Sell Put terminology.

## 1.2.66 - 2026-05-19

### Changed
- Retired the repo-local dev-to-prod checkout deployment path from Makefile, guardrails, and operator docs; service deployment guidance now points to `./om service render` for Linux systemd and Mac launchd.
- Narrowed guardrails checks to current documentation wording and runtime config tracking after removing the obsolete deploy argument policy.

### Removed
- Removed old deploy helper entrypoints and deploy observability remnants from the active architecture contract.
- Removed obsolete OpenD, Futu, healthcheck, watchdog-loop, required-data schema, report-retention, and SSH deploy-key self-check scripts that duplicated maintained CLI/application paths.

### Tests
- Added structural regressions to keep retired deployment, WebUI, OpenD doctor, healthcheck wrapper, report-retention, and deploy-key helper scripts from returning.
- Re-ran focused structure/runtime/service/OpenD CLI tests, guardrails, release metadata validation, and diff checks.

## 1.2.65 - 2026-05-19

### Added
- Added `./om service render` / `./om service status` support for Linux systemd and Mac launchd deployments, including runtime-root aware service profiles and split runtime/dev/server dependency files.
- Added runtime path and secret resolution helpers so deployed services can read server-local environment variables without depending on repo-local secret JSON files.
- Added AI Cofunder ranking evidence for strategy handoff bundles, including per-report top candidates, score inputs, configured score weights, cash headroom, reject samples, and handoff Markdown summaries.

### Changed
- Added `--env-file` to `./om service render` for systemd deployments so generated services reference the server-local environment file for Feishu credentials.
- Routed scheduler, sell-put cash, pipeline runtime, multiplier cache, external service, and agent health/status paths through the configured runtime root.
- Enforced sell-put `min_otm_pct` in the candidate engine and scan pipeline so configured OTM distance is part of the hard strategy gate.

### Removed
- Retired option-position Feishu Bitable mirror sync, including the `sync-feishu` CLI, sync metadata writes, sync receipts, runtime-status sync readouts, config defaults, docs, and sync-specific tests.
- Removed repo-local `secrets/*.json` from the formal runtime path; Feishu holdings and Feishu app notifications now resolve credentials from environment variables, and option-position SQLite defaults to runtime-root storage without `portfolio.data_config`.
- Removed retired one-off scripts and obsolete optimization notes that duplicated maintained CLI, runtime status, close-advice, notification, and deployment paths.

### Tests
- Re-ran full pytest, changed Python compile checks, focused AI Cofunder/plugin tests, config build dry-runs for US/HK, changed-path type checks, diff checks, and release metadata validation.

## 1.2.64 - 2026-05-18

### Fixed
- Enforced canonical option trade write rules for symbol, type, side, strike, expiration, contracts, multiplier, locked shares, premium, and cash-secured amount.
- Required positive `premium_per_share` on manual and broker open writes, preserved up to three decimal places, and stopped defaulting missing open prices to `0.0`.
- Required positive manual/broker close prices while keeping expire auto-close as the only zero-price close path.
- Marked parsed trade messages without premium as not write-ready instead of only listing `premium_per_share` in missing fields.

### Changed
- Treat `underlying_share_locked` as a short-call-only derived risk field that must equal `contracts * multiplier` when explicitly supplied.
- Treat `cash_secured_amount` as a short-put-only derived risk field from `strike * multiplier * contracts`.

### Tests
- Added domain regressions for required write fields, price precision, locked-share validation, and cash-secured derivation.
- Re-ran changed-path type checking, compile checks, focused option-position/trade-intake tests, full pytest, diff checks, and release metadata validation.

## 1.2.63 - 2026-05-18

### Fixed
- Preserved scheduler `last_run_id` / trigger timing in AI Cofunder evidence so stale runtime output can be judged against the actual online job run.
- Downgraded stale runtime output from a hard failure to a warning when scheduler evidence confirms the latest runtime run completed successfully.
- Split candidate CSVs from `*_reject_log.csv` files in AI Cofunder strategy evidence so rejected rows no longer inflate candidate counts or create bogus empty candidate samples.
- Added Feishu option-position sync failure/conflict details to AI Cofunder ledger evidence and deterministic findings.
- Added account-level candidate, reject-log, and filter-trace summaries to the AI Cofunder account-strategy matrix.

### Tests
- Added AI Cofunder regressions for scheduler run-id evidence, confirmed stale runtime handling, Feishu sync `partial_failed` details, reject-log separation, and account-level strategy evidence.
- Re-ran focused AI Cofunder tests, agent plugin contract/smoke tests, changed-path type checking, compile checks, diff checks, and release metadata validation.

## 1.2.62 - 2026-05-18

### Changed
- Repointed runtime position/trade imports to the canonical `domain.domain.ledger.position_fields` owner instead of the legacy `domain.domain.option_position_lots` re-export.
- Removed retired post-write v2 projection status payloads from option-position workflow and CLI outputs.
- Retired the old local WebUI surface, including `src/interfaces/webui`, `src/application/webui_*`, `scripts/webui`, `run_webui.sh`, and WebUI-specific tests/static assets.
- Updated onboarding docs and install guidance to use `./om init runtime` and CLI/agent entrypoints instead of the retired WebUI.
- Updated project memory and architecture guidance so future work no longer treats WebUI as an active interface.

### Tests
- Added structural coverage to keep runtime code off the legacy `option_position_lots` re-export.
- Added structural coverage to keep retired WebUI code and script entrypoints from returning.
- Verified with focused ledger/WebUI-retirement tests, changed-file type checking, compile checks, full pytest, diff checks, and release metadata validation.

## 1.2.61 - 2026-05-18

### Changed
- Rewrote `AGENTS.md` as the short agent-facing operating manual for safety boundaries, entrypoint selection, module ownership, and focused quality gates.
- Rebuilt `docs/AGENT_WIKI.md` into a task-driven agent handbook covering tool selection, AI Cofunder handoff, runtime evidence paths, investigation playbooks, module boundaries, and verification guidance.
- Updated README, Getting Started, Agent Integration, Docs Index, and Tool Reference navigation so agents can find the handbook and the `ai_cofunder` workflow from the public docs.

### Tests
- Verified doc whitespace with `git diff --check`, confirmed the `ai_cofunder` manifest through `./om-agent spec`, and checked `./om ai-cofunder collect --help`.

## 1.2.60 - 2026-05-18

### Fixed
- Fixed legacy SQLite bootstrap so `option_positions.bootstrap_from_legacy_sqlite.enabled=true` reads the deprecated `option_positions.sqlite_path` database instead of the active runtime database.
- Prefer migrating legacy `trade_events` as the source of truth, with explicit fallbacks for legacy `position_lots` and old `option_positions` snapshots.
- Added explicit bootstrap statuses for missing, empty, disabled, and unreadable legacy SQLite stores instead of silently skipping migration.

### Tests
- Added regression coverage for active-empty / legacy-populated dual-store bootstrap, legacy `trade_events` precedence, disabled legacy migration, and missing legacy database diagnostics.
- Re-ran focused ledger/option-position/trade CLI tests, changed-file type checking, compile checks, full pytest, diff checks, and release metadata validation.

## 1.2.59 - 2026-05-18

### Added
- Added `./om ai-cofunder collect` and the `ai_cofunder` agent tool as the dedicated MacBook Codex handoff path for redacted runtime, scheduler, ledger, account-strategy, and strategy evidence.
- Added optional `--include-healthcheck` / `include_healthcheck=true` support so AI Cofunder bundles can carry a redacted `healthcheck_snapshot` without duplicating healthcheck readiness logic.

### Changed
- Removed the old top-level `doctor` CLI/tool/module instead of keeping it as a compatibility alias for the AI partner workflow.
- Moved AI Cofunder evidence collection, deterministic checks, and redaction into `src/application/ai_cofunder/`.
- Renamed healthcheck OpenD output checks from `opend_doctor*` to `opend_readiness*` to keep readiness probes distinct from the removed doctor lane.

### Tests
- Replaced doctor contract/behavior coverage with AI Cofunder tests for scheduler evidence, strategy evidence, redaction, output-write gating, and local runtime artifacts.
- Re-ran focused AI Cofunder/agent tests, changed-file type checking, compile checks, full pytest, CLI smoke checks, diff checks, and release metadata validation.

## 1.2.58 - 2026-05-18

### Added
- Added `./om option-positions store inspect` to diagnose active, legacy-configured, and repository-default SQLite stores, including `trade_events` / `position_lots` row counts and multi-store drift warnings.
- Added ledger-store visibility to agent healthcheck, runtime status, option-position inspection/rebuild output, trade-event replay output, and expired-position maintenance results.

### Changed
- Fixed the option-position ledger store to `<runtime_root>/output_shared/state/option_positions.sqlite3`; deprecated `option_positions.sqlite_path` is ignored as an active path and retained only for diagnostics.
- Retired Feishu `option_positions` bootstrap reads so option positions are sourced from local SQLite `trade_events -> projection -> position_lots`; Feishu `option_positions` remains mirror/sync-only.
- Kept general Feishu holdings / `external_holdings` reads intact while limiting Feishu `option_positions` schema checks to explicitly enabled mirror sync.
- Updated migration, architecture, getting-started, and ledger redesign docs to document the SQLite-only source of truth and Feishu mirror-only boundary.

### Tests
- Added regression coverage for ignored legacy SQLite paths, store inspection drift diagnostics, retired Feishu bootstrap config, healthcheck mirror-schema gating, and ledger-store payload exposure.
- Re-ran full pytest, focused ledger/position/trade/healthcheck type checks, compile checks, `git diff --check`, release metadata validation, and store-inspect CLI verification.

## 1.2.57 - 2026-05-18

### Added
- Added a canonical trade/position ledger package around `trade_events -> projection -> position_lots`, with explicit lot identity, projection replay, read views, close-target resolution, preflight, writer, maintenance, intervention, reconciliation, and storage boundaries.
- Added dedicated `positions` and `trades` application namespaces for position workflows, auto-close maintenance, Feishu mirror sync, trade intake, trade normalization, receipts, and trade-event review.
- Added explicit result contracts for ledger preflight/write/projection refresh, manual open/close/adjust, broker trade operations, expired-close decisions, and manual void/repair interventions.

### Changed
- Retired the v2 snapshot/compatibility position model and removed legacy option-position facade/service modules from default runtime paths.
- Unified manual close, broker close, and auto-close target resolution through a single `CloseTargetResolution` contract with fail-closed guards for same-expiry, same-strike, multi-lot, and cross-expiry cases.
- Moved position lot fields, patch handling, sync metadata, projection writes, and close target validation behind explicit contracts instead of free-form core dictionaries.
- Kept Feishu, reports, receipts, CLI JSON, SQLite codec, migration, and reconciliation as boundary adapters rather than canonical position sources.

### Tests
- Added structural regression guards preventing retired v2/facade imports, legacy fallback reads, non-public ledger imports, and free-form result contracts from returning to core position/trade paths.
- Added ledger, position, trade, close-target, auto-close, migration, projection, publisher, reporting, Feishu sync, and trade-intake regression coverage for the rebuilt core model.
- Re-ran full pytest, focused ledger/position/trade type checking, release metadata validation, diff checks, and a dry-run trade-event replay.

## 1.2.56 - 2026-05-17

### Added
- Added the `doctor` agent tool and `./om doctor` CLI for production-quality triage from runtime status, scheduler evidence, audit tails, and deployment metadata.
- Added optional OpenAI-compatible AI triage with custom `base_url`, `model`, `api_key_env`, strict JSON prompting, and redacted evidence handoff output.
- Added strategy evidence collection from candidate CSVs, `candidate_filter_trace.jsonl`, and strategy replay artifacts so doctor can support evidence-backed optimization suggestions.

### Changed
- Made doctor report writes opt-in through `write_outputs=true`, write-tool permission, and `confirm=true`, while keeping the default path as no local writes.
- Restricted doctor output directories to the repository tree and kept API keys, webhooks, bearer tokens, and long account identifiers out of handoff evidence.
- Preserved deterministic runtime status in handoffs when AI triage is unavailable, and kept runtime summary warnings visible alongside scheduler findings.

### Tests
- Added doctor coverage for scheduler evidence boundaries, AI config/redaction, strategy evidence, output-write gating, path restrictions, and agent/CLI contracts.
- Re-ran focused doctor, agent plugin contract/smoke, type checking, compile, config dry-runs, diff, and release metadata checks.

## 1.2.55 - 2026-05-16

### Added
- Added `./om option-positions auto-close-expired` as the dedicated expired-position auto-close entrypoint with runtime config, account, dry-run/apply, `--no-send`, and persisted run-state support.

### Changed
- Removed expired auto-close execution from option-monitor tick/account/pipeline orchestration so scans no longer perform maintenance writes as a side effect.
- Removed the obsolete `option_positions.auto_close.run_on_tick` config knob and related validation surface.
- Updated README, RUNBOOK, CONFIGS, and configuration guidance to document auto-close as an independent scheduled/manual workflow.

### Tests
- Added dedicated auto-close command coverage and removed tick notification/account-run tests that depended on auto-close side effects.
- Re-ran focused tick, position-maintenance, auto-close, config dry-run, compile, diff, and targeted type checks.

## 1.2.54 - 2026-05-15

### Added
- Added task-level receipt delivery for `option-positions sync-feishu` with confirmed duplicate suppression and unconfirmed receipt retry support.
- Added persisted `option_positions_feishu_sync` last-run and receipt state for Feishu mirror synchronization diagnostics.
- Added `runtime_status.option_positions_feishu_sync` so operators can inspect the latest sync result and receipt status without reading cron logs.
- Added `--no-send` to `option-positions sync-feishu` for silent manual or scheduled runs.

### Changed
- Documented Feishu mirror sync receipt behavior, daily cron handoff, `receipt_key` dedupe, and troubleshooting surfaces.
- Extended `option_positions.sync_to_feishu.receipt` defaults and config validation.

### Tests
- Added regression coverage for Feishu sync receipt decisions, message rendering, duplicate suppression, retry behavior, persisted receipt state, runtime-status summaries, and config validation.
- Re-ran full pytest, focused type checking, compile checks, config dry-runs, diff checks, and release metadata validation.

## 1.2.53 - 2026-05-15

### Added
- Added idempotent auto-close receipt state keyed by account, broker, business date, and closed position records so daily maintenance cron retries do not resend already confirmed receipts.
- Added `retry_unconfirmed` receipt policy for retrying prior unconfirmed auto-close receipt deliveries.
- Added `runtime_status.latest_run.accounts.<account>.auto_close_receipt` summary fields for receipt diagnosis.

### Changed
- Emitted explicit auto-close receipt audit events with status, attempt count, and receipt key metadata.
- Documented daily maintenance cron handoff and auto-close receipt dedupe behavior.

### Tests
- Added regression coverage for confirmed duplicate skips, unconfirmed receipt retries, receipt state persistence, receipt audit events, and runtime-status receipt summaries.
- Re-ran focused receipt/account-run/runtime-status tests, changed-file type checking, compile checks, config dry-runs, and release metadata validation.

## 1.2.52 - 2026-05-15

### Added
- Added `runtime_status` support for inspecting a specific `output_runs` directory by `run_id` or `run_dir`.
- Added `latest_scanned_run` and scanned-run prefetch summaries so a later skipped tick no longer hides the most recent real scan from runtime diagnostics.

### Changed
- Expanded required-data prefetch observability with sparse/shared summary fields such as `cached_unique_symbols`, `skipped`, `force_refresh`, reported OpenD call counts, and shared force-prefetch markers.

### Tests
- Added runtime-status regression coverage for skipped latest runs, explicit run selection, and shared force-prefetch summaries.
- Re-ran focused agent plugin smoke/contract tests, changed-file type checking, compile checks, and release metadata validation.

## 1.2.51 - 2026-05-15

### Fixed
- Isolated yield enhancement from account cash prefilters so account-specific sell-put cash caps no longer shrink the market put universe used for YE pair selection.
- Kept ordinary sell-put cash hard filtering on the account-scoped sell-put path while leaving the YE put universe market-scoped.

### Changed
- Updated Agent Wiki architecture references to current candidate engine, option-position ledger, close-advice, and tick entrypoint symbols.

### Tests
- Added regression coverage for account-prefiltered YE orchestration, YE put-universe cash-filter isolation, and current Agent Wiki symbol references.
- Re-ran focused domain-boundary, sell-put liquidity, symbol-monitoring, YE helper/planning, pipeline wrapper, type, compile, and release metadata checks.

## 1.2.50 - 2026-05-15

### Added
- Added generated runtime config freshness metadata for system, common user, and market user config sources.
- Added stale runtime config checks to `config validate --market`, `run tick`, and `run tick-cron`, with clear rebuild commands.
- Added an emergency `--allow-stale-config` override for tick entrypoints.

### Fixed
- Prevented cron/tick runs from silently using stale runtime configs after `configs/system.json`, `configs/user.common.json`, or market user configs change.
- Preserved `init runtime` compatibility by recording inline generation metadata for starter runtime configs.
- Returned schedule contract validation failures as structured JSON from `config validate --market`.

### Tests
- Added regression coverage for stale market-user config detection, newly appearing common-user config detection, tick-cron preflight failures, init-runtime metadata, and structured validation errors.
- Re-ran focused pytest, smoke tests, changed-file type checking, compile checks, config dry-runs, and release metadata validation.

## 1.2.49 - 2026-05-15

### Added
- Added `./om run tick-cron` as the cron-safe tick entrypoint with market-specific default config, lock file, timeout, dry-run command output, and trigger diagnostics.
- Added runtime trigger context capture so tick runs and `runtime_status` can report outer runner source, job id, delivery mode, and timeout metadata.
- Added `runtime_status.notification_diagnosis` to distinguish scheduler skips, `delivery.mode=none`, `--no-send`, missing notification routes, confirmed sends, partial sends, and unconfirmed delivery attempts.

### Changed
- Documented the recommended HK/US cron handoff through `tick-cron`, keeping cron as a 10-minute wakeup while code owns business-window and run-point decisions.
- Clarified cron wrapper return semantics so lock skips, process failures, and timeouts are observable as distinct outcomes.

### Tests
- Added tick-cron, trigger-context, CLI, and runtime-status diagnosis coverage.
- Re-ran full pytest, changed-file type checking, compile checks, config dry-runs, tick-cron dry-runs, and release metadata validation.

## 1.2.48 - 2026-05-15

### Fixed
- Added a runtime schedule market guard so HK ticks fail fast when the loaded config carries a US-market schedule timezone instead of silently skipping during HK day-session cron runs.
- Added HK 11:00 Beijing-time scheduler regression coverage to keep the HK run window on `09:30-16:00`.

### Tests
- Re-ran the full pytest suite, HK 11:00 scheduler verification, config dry-runs, and release metadata validation.

## 1.2.47 - 2026-05-15

### Changed
- Reworked scan scheduling around explicit `timezone`, `run_window`, `run_points`, `gates`, and `cron_interval_min` settings.
- Simplified scan/notification timing so scheduled points are open-plus-10 minutes, hourly, and close-minus-10 minutes instead of separate scan and notify intervals.
- Applied the US Beijing-before-02:00 gate to auto market selection so US tick work is skipped after the cutoff across daylight saving and standard time.
- Updated WebUI, generated static assets, config validation, migration helpers, and configuration guidance to use the new schedule fields.

### Fixed
- Preserved per-account scheduler behavior when reading upgraded state by falling back to legacy `last_scan_utc_by_account` only for the matching account.
- Prevented stale WebUI bundles from shipping old schedule field names.

### Tests
- Added regression coverage for Beijing cutoff auto-market selection, legacy per-account scheduler state, and committed WebUI schedule bundle contents.
- Re-ran the full pytest suite, config dry-runs, WebUI bundle checks, and release metadata validation.

## 1.2.46 - 2026-05-15

### Added
- Added default-on receipt delivery for expired auto-close maintenance after local `option_positions` events/projection are updated.
- Added `option_positions.auto_close.receipt` controls for applied, failed, noop, and dry-run receipt behavior, with `--no-send` suppressing receipt delivery.
- Added `runtime_status` visibility for the latest run's `expired_position_maintenance` state and receipt result.

### Changed
- Kept receipt delivery outside the option-position persistence service so the canonical `trade_events -> projection -> position_lots` chain remains replayable.
- Documented auto-close receipt side effects and troubleshooting surfaces in README, RUNBOOK, CONFIGS, and configuration guidance.

### Tests
- Added auto-close receipt decision, message, delivery, no-send, account-run state/audit, runtime status, and config validation coverage.
- Re-ran focused auto-close, account-run, runtime-status, tick orchestration, option-position service, import ownership, compile, config dry-run, type, and release metadata checks.

## 1.2.45 - 2026-05-15

### Added
- Added auto trade intake receipt delivery for applied, unresolved, and failed deals, using the configured notification route and default-on `trade_intake.receipt` settings.
- Added listener status output with heartbeat, restart/error state, last deal result, and last receipt result so long-running intake jobs are observable from cron.
- Added `runtime_status.trade_intake` summaries for intake state, listener status, audit file presence, and receipt confirmation counts.

### Changed
- Kept receipt delivery outside the option-position resolver path, sending only after intake resolution/persistence has produced a terminal result.
- Documented auto trade intake receipt side effects and troubleshooting surfaces in README, RUNBOOK, and configuration guidance.

### Tests
- Added receipt decision, delivery normalization, state/audit persistence, duplicate-retry, runtime status, and receipt-config validation coverage.
- Re-ran focused intake/runtime suites, changed-file type checking, compile checks, config dry-runs, and release metadata validation.

## 1.2.44 - 2026-05-14

### Changed
- Rewrote the README into a product/operator manual with a safer quick start, clearer entry-point guidance, and a workflow-first structure for WebUI, `./om`, and `./om-agent`.
- Promoted candidate filter trace troubleshooting, side-effect boundaries, scheduled-task guidance, and agent safety rules so common online issues can be collected and analyzed locally with less guesswork.

### Tests
- Re-ran the agent plugin contract/smoke suite, `./om-agent spec` JSON validation, and `git diff --check` while verifying the README command surface against the current CLI.

## 1.2.43 - 2026-05-14

### Added
- Added candidate filter trace rows for Sell Put, Sell Call, close advice, yield enhancement, cash reserve, and share coverage decisions.
- Added the read-only `candidate_filter_explain` agent tool to explain why a symbol was rejected, post-filtered, accepted, notified, or not observed from existing trace files.

### Changed
- Tightened candidate scan typing and trace-path handling so changed-file `basedpyright` can validate the trace/explain implementation without being blocked by older weakly typed code.

### Tests
- Added regression coverage for candidate filter trace writing, missing required_data visibility, cash-reserve filtering traces, and the explain tool.
- Re-ran focused candidate, close-advice, agent-plugin, compile, type, and release metadata validation.

## 1.2.42 - 2026-05-14

### Fixed
- 修复收益增强通知建议挂单字段，Put 建议价固定使用 Put 卖出报价，避免误用组合净价。
- 修复收益增强 `max_debit` 模式下默认成本比例约束的处理，仅在显式配置时限制 Call 成本/Put 权利金比例。
- 统一收益增强 Call 侧 DTE 规划与 Sell Put 窗口，避免预取窗口和候选过滤窗口不一致。

### Tests
- 补充收益增强通知字段、资金过滤模式和 required_data 规划回归测试。

## 1.2.41 - 2026-05-14

### Changed
- Reworked Sell Put yield enhancement from a second-pass Sell Put optimizer into a premium-funded long-call combination strategy.
- Moved yield-enhancement defaults into application configuration constants and locked `configs/system.json` against those defaults.
- Broadened yield-enhancement Put universe generation so it inherits symbol, strike, DTE, cash, risk, and liquidity boundaries without inheriting Sell Put return thresholds.
- Replaced old optimizer output fields with funding coverage and upside elasticity fields across candidates, summaries, canonical rows, alerts, and README guidance.

### Fixed
- Kept yield enhancement running when normal Sell Put minimum-income currency conversion is unavailable, while normal Sell Put output still fails closed.
- Rejected removed yield-enhancement optimizer and legacy call OTM fields during config validation instead of allowing stale settings to apply silently.

### Tests
- Added regression coverage for system default consistency, premium-funded call acceptance/rejection, config tombstones, required-data call planning, and Sell Put return-floor isolation.
- Re-ran the full pytest suite plus release metadata, config dry-run, compile, type, and diff validation.

## 1.2.40 - 2026-05-14

### Added
- Added required_data prefetch option-chain budget waves so OpenD `get_option_chain` calls stay under the configured shared window during global prefetch.
- Added run summary fields for OpenD rate-limit classes, rate-limit items, prefetch budget plans, cooldowns, and stale option-chain cache hits.

### Changed
- Reduced effective prefetch option-chain budget below the raw OpenD limit to leave headroom for retries and concurrent callers.
- Reused stale option-chain cache only as a bounded RATE_LIMIT fallback, with force-refresh runs and cache entries older than the retention horizon excluded.

### Fixed
- Recorded single-expiration OpenD option-chain RATE_LIMIT details in required_data prefetch summaries instead of leaving `opend_rate_limit_classes` empty.

### Tests
- Added focused US/HK required_data prefetch, OpenD coordinator, option-chain cache fallback, and budget planning regression coverage.
- Re-ran focused OpenD limiter/config, required_data prefetch, explicit-expiration fetch, runtime status, compile, type, and diff validation.

## 1.2.39 - 2026-05-14

### Changed
- Tightened close-advice remaining annualized thresholds: `strong` now requires remaining annualized return at or below 4.5%, and `medium` now requires at or below 7%.
- Kept close-advice system defaults, no-config domain fallbacks, and operator documentation aligned on the new thresholds.

### Tests
- Re-ran focused close-advice, web UI presenter, layered config, and config dry-run validation.

## 1.2.38 - 2026-05-13

### Changed
- Simplified recently split tick helper modules without changing runtime behavior.
- Inlined low-value single-use helper code while preserving compatibility exports and tick orchestration boundaries.

### Tests
- Re-ran focused tick helper, import-boundary, watchdog, and unified tick regression suites.

## 1.2.37 - 2026-05-13

### Added
- Added `docs/ARCHITECTURE.md` to document module layers, public entry points, tick orchestration, scan/candidate ownership, option positions, close advice, and runtime state boundaries.
- Added narrow tick helper modules for idempotency context, guard admission, run workspace setup, scheduler context, account execution, and notification delivery.

### Changed
- Reduced `multi_account_tick` to a public orchestration spine while preserving the `./om run tick` chain and compatibility exports.
- Updated architecture guard tests to assert against the new owner modules instead of relying on implementation details inside the main tick entry point.

### Tests
- Added coverage for tick idempotency context and tick run workspace preparation.

## 1.2.36 - 2026-05-13

### Changed
- Consolidated candidate reject-rule mapping so scanner and pandas adapter reject logs share the same engine reason vocabulary.
- Removed unused event-risk gate hooks from the candidate scanner wiring because current production behavior remains post-scan annotation.

### Fixed
- Logged Stage 1 hard-constraint rejects plus open-interest, volume, and spread-quality rejects in candidate reject CSVs.
- Treated unavailable or invalid bid/ask spread quality as a `max_spread_ratio` rejection when spread filtering is enabled.

## 1.2.35 - 2026-05-13

### Changed
- Reused parsed required_data CSV reads during prefetch cache coverage checks instead of reading the same CSV twice per symbol.
- Preserved option-chain DataFrames through OpenD symbol fetch processing and used tuple iteration for final row assembly to reduce pandas round trips.

### Fixed
- Removed duplicate option type and strike filtering during OpenD row construction after the existing pre-snapshot pruning already applied those bounds.

## 1.2.34 - 2026-05-13

### Changed
- Ordered alert rows within each priority section by strategy and then by candidate strength so same-strategy candidates stay consistently ranked.
- Updated notification candidate selection to preserve cross-strategy coverage across high, medium, and low sections while keeping the existing global 5-item budget.

### Fixed
- Prevented high-priority Sell Put rows from crowding out medium-priority Sell Call notifications when capacity-limited strategy coverage is needed.
- Kept compact and legacy notification renderers aligned on the same capped cross-strategy selection behavior.

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
- Restored Sell Call assigned-return hard filtering with `min_if_exercised_total_return`, using account `avg_cost` as the cost basis.
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
- Added shared risk-capacity helpers for Sell Put cash headroom and Sell Call share coverage decisions.

### Changed
- Hardened Sell Put and Sell Call gating so missing multiplier, currency, cash-secured basis, or cash requirement data fails closed instead of using guessed defaults.
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
- Calculated sell-call net premium annualized return against current spot opportunity cost while keeping exercised total return on the holding cost basis.
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
- Redesigned required-data fetch planning so `sell_put` and `sell_call` derive independent near/far strike bounds before merging compatible OpenD requests, ensuring sell-call target strikes are fetched instead of being filtered only at scan time
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
