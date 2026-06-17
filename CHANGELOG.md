# Changelog

## Unreleased

### Added
- Added investigation recipes to the read-only analysis catalog so Agent planners can map task contracts and evidence
  gaps to generic analysis views, `analysis_query`, `operation_timeline`, and trace tools.
- Added Planner `selected_recipe` trace support so Agent sessions record the investigation recipe chosen from the task
  contract, including runtime inference for older planner outputs.
- Added action lifecycle evidence to preview/readback traces and `operation_timeline`, exposing preview/confirm/execute/
  verify/audit phase, required next action, and verification status without expanding write permissions.

## 1.2.299 - 2026-06-17

### Added
- Added the OM Agent intelligence upgrade plan covering phased planner contract, investigation runtime, coverage,
  follow-up, composer, answer verifier, and action lifecycle work.
- Added planner-declared `task_contract` support to AgentLoop tool plans so task domain, task mode, evidence needs, and
  answer shape can be traced and verified.

### Changed
- Upgraded TaskContract inference, coverage checks, and answer shape verification so income analysis requests require
  breakdown/driver evidence while pure metric calculations stay on key-fact evidence.
- Taught the monthly income planner guard to request detail rows for income analysis, review, and performance questions
  without changing ordinary monthly income summaries.

### Fixed
- Fixed monthly income analysis answers so guarded composition cannot pass a receipt-like summary that omits the requested
  driver or breakdown explanation.

## 1.2.298 - 2026-06-16

### Fixed
- Kept deterministic fallback renderer text out of AgentLoop synthesis evidence so natural-language analysis answers
  compose from structured tool facts instead of copying fixed OM ledger receipts.
- Updated assistant evidence coverage and docs to keep provenance deterministic while treating fallback renderer text as
  internal fallback state only.

## 1.2.297 - 2026-06-16

### Fixed
- Fixed account notification run artifacts so `symbols_notification.txt` is written after close advice is appended,
  keeping the saved notification text aligned with the text used by the delivery flow.

## 1.2.296 - 2026-06-16

### Changed
- Added explicit `composer` and `guard` audit aliases to AgentLoop synthesis traces so operator diagnostics can read
  `composer.attempted`, `guard.status`, and `guard.violation_type` without knowing the older internal field names.

## 1.2.295 - 2026-06-16

### Changed
- Upgraded the AgentLoop answer guard into an evidence-aware claim verifier so symbol-like tokens are classified against
  tool evidence vocabulary before they can trigger hard symbol violations.
- Added answer guard audit details for passed, rewritten, and fallback routes, including violation type summaries,
  contract verifier payloads, and claim classification records.

### Fixed
- Fixed candidate-filter LLM answers that mention metrics or rule vocabulary such as `IV/RV`, `OI`, `DTE`,
  `annualized_return_below_min`, or `risk_spread` so those evidence-backed terms no longer force renderer fallback.
- Fixed ISO-style date claim extraction so unsupported full dates such as `2026-06-19` are verified as dates instead of
  being truncated during answer guard checks.

## 1.2.294 - 2026-06-16

### Changed
- Upgraded AgentLoop tool plans to `om-tool-plan-v2`, removing planner-controlled `response_mode` and making
  AgentLoop responsible for final answer routing, evidence verification, and deterministic fallback.
- Moved normal diagnostic, analytical, and financial assistant answers onto LLM composition over guarded tool evidence,
  while keeping deterministic renderers as evidence formatters and fallback paths.
- Updated candidate filter explanation evidence with readable rejection reason labels and counts so one-symbol filter
  diagnostics can be summarized without exposing raw trace rule dumps.
- Renamed operation session snapshot answer markers from `response_mode` to `plan_kind` to avoid reintroducing answer
  mode terminology outside the planner boundary.

### Fixed
- Fixed candidate-filter assistant answers such as `泡泡玛特被哪个参数过滤了？` so they use the evidence composer path
  instead of the deterministic candidate renderer during normal AgentLoop responses.

## 1.2.293 - 2026-06-16

### Changed
- Moved the heavy Agent tool implementations behind `src/application/agent_tools/*_impl.py` modules while keeping
  root `agent_tool_*` files as compatibility re-export shims, reducing duplicate handler surfaces without changing the
  registered tool manifest.
- Documented the current Agent tool ownership boundary: domain modules own implementation and metadata, registry only
  collects tools, and planner-facing tool arguments continue to hide system/path fields from the LLM.

### Fixed
- Fixed `candidate_filter_explain` trace discovery so one-symbol questions can find runtime traces from resolved
  config paths, `OM_RUNTIME_ROOT`, service profile roots, latest-run pointers, recent `output_runs`, and legacy shared
  report fallbacks.
- Fixed `candidate_filter_diagnostics` to use the same runtime trace discovery path as `candidate_filter_explain`, so
  Tool OS analysis and the narrow LLM tool no longer disagree about where candidate filter traces live.

## 1.2.292 - 2026-06-15

### Changed
- Consolidated Inbound LLM planning onto the registered tool surface, removing the old LLM intent translator path and
  assistant command metadata duplication.
- Retired `assistant.mode` from runtime assistant config; active controls are now `assistant.enabled` and
  `assistant.planner.enabled`.
- Renamed Shadow Replay parameter backtest internals to candidate-impact naming and made `candidate-impact` /
  `candidate-impact-report` the only supported CLI entries.

### Removed
- Removed legacy assistant LLM intent schema/eval fixtures and the old command catalog module.
- Removed Shadow Replay `parameter-backtest` and `parameter-report` compatibility aliases.

## 1.2.291 - 2026-06-15

### Added
- Added direct assistant eval and trace-route guards for the documented P2 minimum golden-case checklist, mapping each
  case in the design document to one or more required fixtures and failing if the 6.6.2 checklist drifts from the
  checked-in fixture mappings; release test planning now includes those drift guards and JSONL fixture format checks in
  the Agent reliability gate.
- Added an online-sample contract guard for documented P2 minimum agent eval fixtures, requiring route/result assertions,
  tool evidence, required business text, forbidden leak text, and explicit gap/impact text when diagnostics or coverage
  gaps are expected.
- Added an online-sample contract guard for compact trace route fixtures, requiring trace naming, compact trace payloads,
  final-route assertions, display assertions, and explicit forbidden assertions for sensitive payload values.
- Added compact assistant trace route coverage for candidate-filter missing trace cases, ensuring the user-facing trace
  explains the evidence gap without exposing tool names, trace paths, or raw artifacts.
- Added compact assistant trace route coverage for upgrade command-log-missing cases, ensuring upgrade receipt gaps are
  visible without exposing SQL, local artifact paths, or raw command logs.
- Added compact assistant trace route coverage for failed release workflow evidence, pairing the existing published
  release sample with a failure sample while redacting raw workflow logs and GitHub URLs.
- Added compact assistant trace route coverage for successful runtime notification delivery audits, ensuring positive
  delivery evidence remains readable without exposing SQL, run IDs, message IDs, or local runtime paths.
- Added compact assistant trace route coverage for prompt-injection deny cases, ensuring untrusted tool-output
  instructions stop at the safety route without exposing the injected text or internal tool payload.
- Added compact assistant trace route coverage for planner apply attempts, ensuring confirm/apply requests stop at the
  deterministic operation boundary without exposing operation ids or apply payloads.
- Strengthened compact assistant trace coverage for manual-trade previews so receipt and confirmation-guard hooks prove
  the preview requires explicit confirmation and still hides operation ids, apply flags, and raw trade text.
- Added compact assistant trace route coverage for SQL-only period scope expansion, proving period-mismatched read
  follow-ups ask for clarification without exposing the generated SQL.
- Added a P2 closure-completion evidence guard that maps each documented 6.18 completion criterion to existing coverage,
  follow-up, answer, or eval tests.
- Added a P2 not-do boundary guard that maps each documented 6.17 boundary to existing permission, config, registry,
  trace/eval, or leak-guard tests.
- Added a release-gap evidence guard that maps each documented 6.19 P2 pre-release gap to concrete assistant eval,
  compact trace, or release-plan test evidence.
- Added a release-plan guard for the documented P2 pre-release gap categories, ensuring each 6.19 gap maps to a
  concrete Agent reliability release-gate command.
- Added a release-plan guard for the documented P2 release-readiness checks, ensuring each 6.21 release criterion maps
  to a concrete Agent reliability release-gate command.
- Added a P2 failure-handling route guard that maps each documented 6.12 failure point to existing test, Agent eval, or
  compact trace evidence.
- Added a P2 bounded follow-up deny-list guard that maps each documented 6.12 follow-up prohibition to runtime, Agent
  eval, or compact trace evidence.
- Added a P2 code-acceptance evidence guard that maps each documented 6.13 slice to model/runtime/trace-eval test or
  fixture evidence.
- Added a P2 route-priority evidence guard that maps the documented 6.14 final routes to existing tests or compact trace
  fixtures.
- Added a P2 evidence/trace ownership guard that maps the documented 6.15 final-answer, hook, session-store, compact-trace,
  and test-assertion boundaries to existing test evidence.
- Added an assistant golden eval for assigned-stock fresh quote cases on the `option_positions_read action=assigned-stock`
  path, proving current spot, stock PnL, and lifecycle PnL can be answered when quote evidence is fresh.
- Added an assistant golden eval for income breakdown follow-ups, ensuring summary-only monthly income evidence triggers
  a read-only component query before the Agent explains main drivers.
- Added an assistant golden eval for assigned-stock missing quote cases, ensuring the Agent performs the read-only
  quote refresh follow-up and still refuses to calculate current floating PnL when spot remains unavailable.
- Added assistant golden eval coverage for symbol identity resolution and moved single-symbol candidate why samples onto
  `candidate_filter_explain`, with evidence extraction for observed rejection and missing trace rows.
- Added TaskContract coverage for single-symbol candidate-filter diagnostics and symbol identity answer-guard evidence,
  so `candidate_filter_explain` answers no longer require generic breakdown drivers and `canonical_symbol` claims are
  verified from tool facts.
- Added scalar output-contract evidence annotations for `symbol_resolve`, allowing post-tool checks to pass without
  inventing table rows for scalar identity results.

### Changed
- Release test planning now treats `src/application/config_validator.py` changes as config-surface changes so assistant
  config boundary fixes automatically run config validation gates.
- Agent reliability release gates now include direct `candidate_filter_trace` tests for symbol resolution and
  single-symbol trace matching.

### Fixed
- Tightened bounded follow-up gates so recoverable gaps require an explicit `suggested_tool`, validate it against
  registry-declared pure-read tools, expose only gap-specific allowed tools to the follow-up planner, track attempted
  gap signatures so the same scoped gap is only queried once, normalize recoverable-source casing, and block release
  workflow / service repair sources before invoking the follow-up planner.
- Fixed `candidate_filter_explain` so AgentLoop-injected runtime config aliases are used before matching
  `candidate_filter_trace` rows, and LLM intent routing now carries the symbol's market sibling config into the same
  tool so single-symbol candidate diagnostics stay consistent with `symbol_resolve` and configured Chinese/name aliases.
- Fixed assigned-stock missing-quote receipts so they explicitly state which symbols lack realtime quotes and that
  current stock floating PnL and lifecycle PnL cannot be calculated.

## 1.2.290 - 2026-06-15

### Added
- Added an assistant golden eval for income comparisons where the first read only covers `lx`; the Agent must perform a
  same-scope read-only follow-up for `sy` before answering winner, amount difference, and rate difference.
- Added an assistant golden eval for candidate diagnostics with no matching artifact rows, so missing evidence cannot
  become a definitive filter root cause.

## 1.2.289 - 2026-06-15

### Added
- Added compact assistant trace route fixtures for release-status no-match, runtime notification-missing, and runtime
  freshness-gap cases, including redaction guards for SQL, local paths, raw logs, GitHub URLs, internal IDs, and
  internal tool names.
- Added an assistant golden eval for runtime freshness gaps so stale runtime snapshots cannot be turned into a
  definitive current push-failure root cause.
- Added an assistant golden eval for partial-confidence candidate diagnostics so summary-only evidence cannot become a
  definitive filter root cause.

## 1.2.288 - 2026-06-15

### Added
- Added upgrade-cancel operation readback trace coverage, including a runtime regression and compact trace redaction
  fixture for internal upgrade tool names, runtime paths, and raw logs.
- Added an assistant golden eval for release-status queries with no matching rows, ensuring the Agent does not treat
  missing release evidence as a successful publication.

### Fixed
- Fixed release-only Task Contracts so remote release publication questions require release evidence without forcing
  unrelated upgrade command/current-version/target-version gaps into the answer.

## 1.2.287 - 2026-06-15

### Added
- Added compact assistant trace route samples for confirmed and cancelled manual-trade operation readback, including redaction guards
  for internal operation tool names, raw trade text, and ledger internals.

### Fixed
- Preserved operation payload and preview metadata in cancelled operation responses so final readback updates the same
  assistant session trace as the original preview.

### Changed
- Updated the Agent reliability P0-P2 design notes to count the new operation readback route fixtures.

## 1.2.286 - 2026-06-15

### Added
- Added AgentLoop preview receipt postchecks and receipt hook results so manual-trade preview traces verify operation
  identity, permission request schema, and confirmation guard state.
- Persisted AgentLoop preview sessions into assistant trace storage so pending manual-trade previews can be audited by
  operation id without exposing raw trade-alert text.
- Added deterministic operation readback sessions so confirmed manual-trade operations update assistant trace from
  pending preview to final applied/cancelled status with postcheck hooks.

### Changed
- Updated the Agent reliability P0-P2 design notes to mark preview receipt/session trace and confirm/apply readback as
  landed in the shared trace model.

## 1.2.285 - 2026-06-15

### Added
- Added centralized assistant final-answer UX leak guards so eval cases fail if user-facing receipts expose internal tool names, SQL, internal IDs, local paths, raw logs, internal modes, or forced fact/analysis sectioning.
- Added centralized compact trace redaction guards so route samples fail if compact traces expose session IDs, internal tool names, SQL, internal IDs, local paths, raw logs, or internal modes.

### Changed
- Updated the Agent reliability P0-P2 design notes to reflect the current ToolExecutor read-path implementation and the remaining preview/receipt convergence work.

## 1.2.284 - 2026-06-15

### Added
- Added an Agent reliability release-test-plan rule so assistant, agent-tool, eval, trace, and reliability design changes automatically require the P2 fixture, eval, runtime, analysis, and plugin gates.
- Added P2 coverage guards for assistant golden eval gap groups and compact trace route samples.

### Changed
- Updated the Agent reliability P0-P2 design notes and regenerated the dependency graph for the new release gate coverage.

## 1.2.283 - 2026-06-15

### Added
- Added an `analysis_catalog` canonical renderer that summarizes available analysis views without exposing embedded SQL templates.
- Added evidence extraction coverage for `analysis_catalog` contract facts so planner support tools produce usable source-backed facts.

### Fixed
- Fixed the `analysis_catalog` evidence contract by declaring its canonical renderer, row count, and fact fields so ToolExecutor postchecks no longer report an incomplete contract.
- Regenerated the dependency graph after the new evidence-session coverage changed test imports.

## 1.2.282 - 2026-06-15

### Added
- Added Agent reliability golden eval coverage for upgrade `operation_timeline` follow-up answers, including tool-call count, plan revision, and injected audit DB assertions.
- Updated the Agent reliability P0-P2 design notes and release gate counts for the expanded follow-up eval coverage.

## 1.2.281 - 2026-06-15

### Fixed
- Fixed AgentLoop upgrade-status follow-up so command-id questions can trigger one read-only `operation_timeline` lookup with system-injected audit DB evidence.
- Fixed upgrade answer verification so operation timeline diagnostics expose operation, outcome, and receipt statuses as verifiable evidence without letting stale first-pass capability gaps dominate the final answer.
- Fixed assistant task contracts so upgrade "why" questions are not misclassified as income breakdown requests.

## 1.2.280 - 2026-06-15

### Added
- Added Agent reliability eval and compact trace coverage for runtime scheduler market-window skips.

### Fixed
- Fixed runtime tick diagnostics so scheduler skips expose scheduler reason fields and are not misclassified as notification delivery failures.
- Fixed assistant answer verification so notification-channel failure claims are rejected when evidence only proves a scheduler skip.

## 1.2.279 - 2026-06-15

### Added
- Added Agent reliability eval and compact trace coverage for runtime notification conflicts, stale quote freshness, and stale upgrade operation timelines.

### Fixed
- Fixed runtime status diagnostics so successful tick completion with failed notification delivery is treated as conflicting evidence instead of a successful push.
- Fixed quote freshness diagnostics to preserve quote status and as-of/spot timestamps in analysis evidence and answer verification.

## 1.2.278 - 2026-06-15

### Fixed
- Fixed assistant upgrade/release evidence handling so a published release status that conflicts with a failed operation outcome is treated as conflicting evidence, not a confirmed successful release.
- Added Agent reliability eval and evidence-session coverage for release/outcome status conflicts.

## 1.2.277 - 2026-06-15

### Fixed
- Fixed assistant action safety so SQL-only read payloads still enforce requested account, symbol, and month scope boundaries.
- Fixed assistant task contracts so month digits and SQL keywords are not misclassified as requested symbols.
- Expanded Agent reliability eval and compact trace fixtures for read-scope clarification paths.

## 1.2.276 - 2026-06-15

### Added
- Added assistant golden eval coverage for runtime notification audits where job success does not prove final message delivery.
- Added a compact trace fixture for successful release-workflow evidence so release answers can show verified publication without leaking raw logs or internal fields.

### Fixed
- Fixed assistant answer verification so stale, missing, conflicting, or partial diagnostics cannot support definitive root-cause or delivery-success claims.
- Preserved direct runtime skip explanations while requiring caveats for stale runtime snapshots, missing notification evidence, and partial diagnostic confidence.
- Updated Agent reliability P0-P2 design notes and release gate counts for the expanded diagnostic, eval, and trace coverage.

## 1.2.275 - 2026-06-15

### Fixed
- Fixed assistant coverage verification so release publication questions require explicit GitHub Release status evidence, not only a release tag.
- Fixed upgrade-status fallback copy to explain missing release publication evidence when the operation timeline lacks a published or failed release status.
- Updated Agent reliability P0-P2 design notes to reflect the release publication coverage verifier behavior.

## 1.2.274 - 2026-06-15

### Added
- Added release publication fields to the `upgrade_operation_status` analysis view so Agent answers can distinguish a release tag from confirmed GitHub Release publication.
- Added compact assistant trace route fixtures for ask, preview, rewrite, fallback, and denied paths.

### Fixed
- Fixed answer verification so a `release_tag` alone cannot be summarized as a successful or failed remote release without publication status evidence.
- Fixed upgrade diagnostics to surface missing release publication evidence and to allow explicit published/failed release status when supported by evidence.

## 1.2.273 - 2026-06-15

### Added
- Added Agent reliability golden eval coverage for stale quotes, runtime conflict/stale evidence, upgrade command-log gaps, old operation timelines, and read/write scope expansion boundaries.

### Fixed
- Fixed upgrade diagnostics so missing command logs, command audits, and operation logs are surfaced as explicit artifact gaps.
- Fixed answer verification so stale, missing, or conflicting diagnostics cannot be summarized as definitive success/failure/completion without disclosing the evidence gap.
- Fixed quote freshness verification so stale analysis evidence cannot be over-explained as an upstream OpenD/Futu failure without supporting evidence.

## 1.2.272 - 2026-06-15

### Added
- Added the Agent reliability P0-P2 design document and the first implementation slice for TaskContract, action policy/safety checks, coverage verification, verifier hooks, evidence bundles, and compact assistant traces.
- Added read-only upgrade operation status evidence to `analysis_query`, backed by operation timeline audit data.

### Changed
- Expanded AgentLoop evidence handling so LLM synthesis is guarded by task coverage, answer shape checks, and deterministic fallback without exposing internal SQL or tool ids.
- Updated dependency graph output for the new assistant reliability modules.

### Fixed
- Fixed account income comparisons so coverage only passes with same-period, same-currency comparable metrics for all requested accounts.
- Fixed upgrade diagnostics so conflicting operation/outcome statuses and missing audit artifacts are surfaced as explicit evidence gaps instead of being summarized as successful upgrades.

## 1.2.271 - 2026-06-14

### Fixed
- Fixed inbound WeChat ClawBot upgrade confirmations to preserve reply context through the background upgrade worker and send the final upgrade receipt through ClawBot instead of silently skipping non-Feishu channels.
- Fixed upgrade confirmation copy to describe the active notification service instead of hard-coding Feishu.
- Added idempotent WeChat ClawBot final replies with stable client ids and persisted outbound receipts for safe worker retry.

## 1.2.270 - 2026-06-14

### Changed
- Added a Claude/OpenClaw supplement preference to address the operator as `棒棒的liuxie`.

## 1.2.269 - 2026-06-14

### Added
- Added the expanded SQLite Tool OS design and implementation for semantic catalog v2, P0/P1/P2 semantic analysis views, lazy materialization, query preflight/explain metadata, evidence v2, bounded read-only follow-up planning, and P2 diagnostic interpretation.
- Added normal-answer UX golden eval coverage for account income comparison, assigned-stock PnL, candidate diagnostics, close advice, runtime diagnostics, and strategy config questions.

### Changed
- Expanded AgentLoop and EvidenceBundle handling so open-ended analytical questions can use guarded `analysis_query` evidence, follow-up decisions, formula checks, diagnostic records, and task-shaped fallback without exposing internal SQL or mode details to users.
- Updated Tool OS documentation, tool reference, and dependency graph output to match the current Agent loop and analysis workspace behavior.

### Fixed
- Fixed normal LLM-composed Agent answers so internal mode names, `analysis_query` / `analysis_catalog`, SQL, internal ids, artifact paths, and forced `事实` / `分析` headings trigger rewrite or deterministic fallback before reaching users.
- Fixed evidence unit inference for per-share cost fields such as `stock_cost_per_share`, allowing user-facing expressions like `USD 117.45/股` to be verified as currency facts.

## 1.2.268 - 2026-06-14

### Fixed
- Fixed AgentLoop analysis planning so `analysis_query` exposes whitelisted view fields and query templates to the planner, preventing LLM-generated SQL from inventing nonexistent income columns such as `net_cashflow` or `return_rate`.

## 1.2.267 - 2026-06-14

### Added
- Added Tool OS v1 read-only analysis tools: `analysis_catalog` and `analysis_query` for flexible SELECT-only comparisons, rankings, trends, breakdowns, and cross-domain OM analysis over whitelisted ledger/config views.

### Changed
- Updated AgentLoop planning, evidence extraction, answer verification, and fallback rendering so open-ended analytical questions can be composed by the LLM from query evidence while preserving task-shaped table fallback when synthesis is unavailable or unsafe.
- Documented the expanded-and-pruned Agent Tool OS design, including why narrow one-off answer tools such as account income comparison are not the primary path.

## 1.2.266 - 2026-06-13

### Fixed
- Fixed inbound upgrade confirmation receipts to recover current and target versions from payload, release tags, and nested version-check data instead of showing `-` when preview fields are incomplete.
- Fixed inbound upgrade final receipts to preserve the Feishu reply target through the running worker state and retry transient reply failures before recording `final_receipt`.

## 1.2.265 - 2026-06-13

### Added
- Added durable AgentSession snapshots, assistant trace diagnostics, evidence bundles, permission-request metadata, and an Agent completion design document for the unified assistant loop.

### Changed
- Expanded AgentLoop read planning to support bounded evidence-gap follow-up plans, answer verification, and source-backed session traces.
- Refreshed Agent architecture/control-plane docs and dependency graph output to match the current Agent loop implementation.

### Fixed
- Fixed `assistant_trace` so read-only trace queries do not create missing AgentSession tables.
- Fixed message-less local Agent sessions so repeated local requests no longer overwrite prior session traces.
- Fixed AgentLoop budget exhaustion to return an explicit `TOOL_BUDGET_EXHAUSTED` error instead of producing a successful partial answer.
- Fixed AgentLoop follow-up planning so recoverable missing-quote replans must directly close the evidence gap.

## 1.2.264 - 2026-06-13

### Changed
- Reworked AgentLoop financial answers to use a single guarded Agent Composer path: tools provide evidence, the LLM writes the user-facing response, deterministic provenance is appended, and canonical renderers remain fallback.
- Updated assigned-stock holding PnL natural-language answers to use concise Agent-composed summaries without exposing internal lot ids or forcing a facts/analysis split.

### Fixed
- Added assigned-stock answer guard coverage so unsupported LLM currency amounts, share/count claims, or percentage claims trigger rewrite/fallback instead of reaching users.

## 1.2.263 - 2026-06-13

### Changed
- Kept direct assigned-stock holding PnL queries factual-only, reserving LLM analysis blocks for explicit analysis, advice, risk, why/how, or what-to-do requests.

### Fixed
- Fixed upgrade confirmation receipts to preserve current and target version values from the upgrade preview instead of showing `-` when the background worker launch result has no version fields.

## 1.2.262 - 2026-06-13

### Changed
- Removed obsolete option-position migration docs and archived memory templates, and redirected operators to the current canonical ledger repair flow.
- Removed historical Feishu backup/bootstrap memory entries that were superseded by the local `trade_events -> position_lots` ledger boundary.

## 1.2.261 - 2026-06-13

### Changed
- Simplified assigned-stock assistant receipts by showing per-currency summaries before one-line lot details, suppressing normal `fresh` quote noise, and keeping missing quote diagnostics explicit.

## 1.2.260 - 2026-06-13

### Fixed
- Fixed assigned-stock realtime spot refresh to write OpenD snapshot limiter state under the active runtime root instead of the release code directory, preventing permission-denied `missing_quote` results after production upgrades.

## 1.2.259 - 2026-06-13

### Changed
- Simplified assigned-stock assistant receipts by hiding internal stock lot ids from default user-facing replies.

### Fixed
- Fixed assigned-stock holding PnL answers so natural-language queries use facts-first rendering with LLM analysis instead of falling back to canonical-only responses when the planner chooses canonical mode.

## 1.2.258 - 2026-06-13

### Changed
- Improved assigned-stock assistant receipts with numbered lot rows and per-currency summaries.

### Fixed
- Fixed `/assigned-stock` open assigned-stock reads to include partially sold lots that still have remaining shares, so partial stock sales stay visible in holding PnL.

## 1.2.257 - 2026-06-12

### Added
- Added `/assigned-stock` inbound read command for Sell Put assigned-stock lots, including spot, stock cost basis, realized/unrealized stock PnL, and lifecycle PnL.

### Fixed
- Fixed assistant planning and canonical rendering so "指派正股持仓盈亏" routes to `option_positions_read action=assigned-stock` with realtime quote refresh instead of ordinary option positions or monthly income.

## 1.2.256 - 2026-06-12

### Added
- Added assigned-stock lifecycle reporting for Sell Put assignments, including true stock cost basis, realized/unrealized assigned-stock PnL, lifecycle PnL, review rows, and explicit double-counting guards.
- Added `option_positions_read action=assigned-stock` with opt-in realtime spot refresh for open assigned-stock lots.
- Added manual and broker stock-sale intake for assigned-stock lots, with dry-run/confirm safety, source deal id idempotency, and ambiguous-lot review handling.

### Changed
- Excluded assignment stock settlement principal cashflow from return-summary net income while preserving it in cashflow diagnostics.
- Documented assigned-stock return accounting, quote refresh semantics, and broker stock-sale source boundaries.

## 1.2.255 - 2026-06-12

### Added
- Added local assistant user profile context so LLM replies can incorporate operator-specific preferences without relying on prompt-only state.

### Fixed
- Fixed required-data spot planning so opening scans prefer a live underlier spot and refresh cached required data when its spot no longer matches the current spot reference.
- Fixed alert symbol normalization so broker option display names no longer pollute alert output.

## 1.2.254 - 2026-06-11

### Fixed
- Fixed runtime status so systemd-injected service environment files that are intentionally unreadable by the app user no longer degrade OM status with an `ENV_FILE` warning when the required environment is already present.

## 1.2.253 - 2026-06-11

### Fixed
- Fixed Combo Yield cash protection so the sell-put leg is cash-gated before pair selection while preserving the unfiltered put universe for planning and diagnostics.
- Added Combo Yield cash-filter trace/report labeling for candidates blocked by insufficient put cash headroom.

## 1.2.252 - 2026-06-10

### Fixed
- Fixed Assistant factual answers so tool-owned fact rows are rendered before LLM analysis for diagnostics, positions, close advice, config, and runtime tools.
- Drove Assistant factual rendering policy from agent tool contracts and expanded eval coverage for facts-then-analysis responses.

## 1.2.251 - 2026-06-10

### Fixed
- Fixed WeChat ClawBot notification accounting so local receipts or successful command execution no longer mark a business-level send failure such as `ret:-2` as delivered.

## 1.2.250 - 2026-06-10

### Fixed
- Fixed early assignment intake so a zero-price option lifecycle close can match the same-account stock settlement leg before expiration when the stock side, quantity, strike price, and event time window strongly agree.
- Included lifecycle stock settlement source deal ids in trade backfill and state reconciliation so assignment stock legs are recognized as already recorded after the ledger event is written.

## 1.2.249 - 2026-06-10

### Fixed
- Fixed Sell Put and Covered Call summaries to preserve upstream scanner ordering so notification top picks respect account cash, covered-share capacity, strategy weights, and underwriting ranking instead of being re-ranked with the default candidate engine.

## 1.2.248 - 2026-06-10

### Fixed
- Fixed option position list reads to sort by expiration before applying the result limit, so all-account open position replies return near expirations first instead of SQLite insertion order.

## 1.2.247 - 2026-06-10

### Added
- Added opt-in Strategy Lab recorder service timers for remote latest-run dataset builds, mark sampling, and outcome settlement.

### Changed
- Made Strategy Lab latest-run dataset builds idempotent by default so existing replay datasets keep accumulated mark paths and outcome facts.
- Documented the remote Strategy Lab recorder deployment path, local artifact write boundaries, and upgrade-preserved service drift behavior.

## 1.2.246 - 2026-06-09

### Fixed
- Fixed assistant symbol-config queries for service requests that pass a standard runtime `config_path`, switching `config.us.json` / `config.hk.json` to the symbol's market before reading monitored-symbol config.

## 1.2.245 - 2026-06-09

### Fixed
- Fixed assistant symbol-config queries so HK aliases such as `泡泡玛特` / `9992.HK` use the HK runtime config even when the product entry has a US default market scope.

## 1.2.244 - 2026-06-09

### Fixed
- Fixed Assistant AgentLoop capability validation so successful registry-backed position reads satisfy generic tool capabilities such as `option_positions` and `read_only` instead of being reported as missing.
- Clarified planner guidance and tests for position detail phrases such as `持仓明细`, `持仓明晰`, and `持仓详情` so they route to ordinary read-only position list/detail queries.

## 1.2.243 - 2026-06-09

### Added
- Added the Strategy Lab MVP workflow for offline hypotheses, evidence readiness, proposal/update review, and Combo Yield optimization experiments.
- Added the read-only `symbol_config_read` agent tool and LLM `symbol_config_query` path for current monitored-symbol strategy config questions.

### Changed
- Split assistant natural-language handling so slash commands stay in `command_parser.py`, deterministic code only handles pending/write-preview commands, and natural-language read requests use planner tool manifests.
- Updated inbound assistant docs and tests to document slash-command read surfaces, planner-backed config reads, and explicit missing-capability responses.

### Removed
- Removed the legacy `src/application/assistant/parser.py` monolith and added architecture guards to prevent it from returning.

## 1.2.242 - 2026-06-09

### Fixed
- Separated AgentLoop fact observations from compressed LLM observations so deterministic assistant renderers and answer guards use untruncated tool data.
- Fixed canonical monthly income replies so all-account annualized basis days are rendered from the full `monthly_income_report` result even when the LLM observation view is clipped.

## 1.2.241 - 2026-06-08

### Changed
- Reframed Research / Shadow Replay around offline evidence readiness, manual strategy review, and candidate-impact comparison instead of automatic parameter optimization.
- Added `candidate-impact` and `candidate-impact-report` as the preferred Shadow Replay commands while preserving the older `parameter-backtest` and `parameter-report` compatibility entries.
- Added `review_readiness` to Shadow Replay analysis/readiness output while preserving the legacy `parameter_advice_gate` compatibility field.
- Updated README-style operator docs and tool references to document candidate-impact usage, data-readiness boundaries, and the no-production-mutation safety contract.

## 1.2.240 - 2026-06-08

### Fixed
- Made required-data prefetch reuse the spot-aware fetch plan so Combo Yield call coverage is consistent across accounts in the same tick run.
- Tightened required-data coverage checks so a cached bounded strike range must cover both requested edges instead of only containing several strikes inside the range.

## 1.2.239 - 2026-06-08

### Changed
- Clarified that repo-local Assistant inbound audit DB overrides should use the runtime-root-relative `output_shared/state/inbound_control.sqlite3` path, while `/var/lib/options-monitor/...` remains a server runtime-root path.

## 1.2.238 - 2026-06-08

### Fixed
- Restored the missing `Any` import in the agent tool registry so the published Copilot tool manifest module passes static undefined-name checks.

## 1.2.237 - 2026-06-08

### Changed
- Upgraded OM Copilot to a single AgentLoop planner path with bounded perception, deterministic understanding, registry-backed read tools, and approved preview-only write capabilities.
- Replaced active assistant product modes with `assistant.enabled` and `assistant.planner.enabled`, keeping `assistant.mode` as legacy metadata only.
- Added an inbound capability catalog and planner manifest guardrails without introducing a parallel ToolRegistry control plane.
- Clarified the conceptual AgentSession and AgentLoop architecture in docs so Copilot boundaries are fixed around perception, understanding, planning, and action.

### Fixed
- Aligned the planner-facing catalog with the real manifest so planner reads only expose registry-backed read tools and preview capabilities remain explicit.
- Hardened planner validation to reject banned system, path, config, audit, service, host, port, timeout, and environment arguments recursively inside nested payloads.

## 1.2.236 - 2026-06-08

### Fixed
- Confirmed successful WeChat ClawBot sends with the local idempotency receipt when iLink accepts a message but does not return an upstream `message_id`, preventing false `SEND_UNCONFIRMED` multi-account notification failures.

## 1.2.235 - 2026-06-07

### Changed
- Changed assistant monthly income detail replies to render deterministic ledger facts before optional LLM analysis so contracts, amounts, accounts, symbols, dates, and currencies stay grounded in `monthly_income_report`.
- Added monthly income detail rendering for realized and cashflow rows, including option strike, expiration, close type, contract count, and original-currency amounts.

### Fixed
- Preserved option strike and expiration fields in monthly income detail rows so assistant replies can identify contracts such as `0700.HK Put 440P @ 2026-06-05` without LLM inference.

## 1.2.234 - 2026-06-07

### Fixed
- Added an assistant answer guard for monthly income detail rows so LLM synthesis cannot report a multi-contract option row as one contract when `contracts` or `contracts_closed` shows a larger quantity.

## 1.2.233 - 2026-06-07

### Fixed
- Corrected WeChat ClawBot `sendmessage` payload shape so `client_id` is inside `msg` and `base_info.channel_version` is sent with iLink POST requests.
- Treated empty iLink `sendmessage` responses as accepted replies while keeping delivery confirmation false unless an upstream message id is present.

## 1.2.232 - 2026-06-07

### Changed
- Completed the Ops Copilot `AgentTool` architecture migration so all `om-agent` tools now own their metadata, execution handler, validation hook, write policy, and manifest output in domain modules under `src/application/agent_tools/`.
- Converted the agent registry into a tool-pool assembler that discovers domain `TOOLS`, deduplicates enabled tools, renders the manifest, and derives `PURE_READ_TOOLS` from registry metadata.
- Centralized Ops Copilot write gating in `agent_tools/permissions.py`, removing tool-name write special cases from the execution layer.
- Kept Research and Shadow Replay outside the Ops Copilot core tool pool while documenting them as side lanes for offline evidence and strategy-quality evaluation.

### Removed
- Removed the legacy `agent_tool_handlers.py` switchboard so new Ops Copilot tools no longer require parallel registry and handler edits.

### Fixed
- Fixed WeChat ClawBot `sendmessage` requests to include the iLink `client_id`, `base_info`, and empty `from_user_id` fields expected by the upstream API.
- Persisted WeChat ClawBot reply receipts into inbound audit responses for both successful and failed replies so operator timelines can show delivery outcome evidence.

## 1.2.231 - 2026-06-07

### Added
- Added WeChat ClawBot typing indicator support for inbound assistant replies, using iLink `getconfig` / `sendtyping` before processing and cancelling typing after replies complete.

## 1.2.229 - 2026-06-06

### Fixed
- Added `WantedBy=multi-user.target` install sections to restartable systemd services so OpenD, trade-intake, Feishu WS, and WeChat ClawBot can be enabled cleanly and survive host reboots.

## 1.2.228 - 2026-06-06

### Fixed
- Fixed WeChat ClawBot `sendmessage` payloads to wrap message bodies under `msg`, matching the iLink API contract so inbound replies are accepted instead of returning `ret=-2`.

## 1.2.227 - 2026-06-06

### Added
- Added the read-only `operation_timeline` agent tool to reconstruct Assistant operation timelines from inbound audit rows, pending operations, ledger identities, and observed reply receipts.
- Added `docs/OM_AGENT_CAPABILITY_MAP.md` as the explicit authority for OM Agent capability boundaries, risk classes, Assistant exposure, and verification paths.

### Changed
- Updated Agent integration and tool documentation to reference the capability map instead of duplicating remote-control allowlist policy.

## 1.2.226 - 2026-06-06

### Added
- Added `./om channel wechat-clawbot connect` as a guided QR login and target binding flow for first-class WeChat ClawBot notification setup.
- Added `./om channel wechat-clawbot poll-once` to process one WeChat ClawBot inbound batch through Assistant control and reply through the same ClawBot channel.
- Added `./om channel wechat-clawbot serve` plus `serve --check` for long-running WeChat ClawBot inbound control, using the same channel receive/reply path and explicit sender allowlist.
- Added `./om service render --include-wechat-clawbot` to generate systemd/launchd WeChat ClawBot inbound services with profile, lock-path, upgrade restart, and post-upgrade health-check support.
- Added a first-class message channel registry, inbound channel service dispatch, and WeChat ClawBot state store so channel capabilities and channel state are no longer embedded in notification, inbound, or binding flow code.
- Added `./om channel status` plus shared `healthcheck` / `runtime_status` channel health output for Feishu and WeChat ClawBot.

### Changed
- WeChat ClawBot service profiles now record YAML-sourced sender allowlists as configured/source metadata instead of duplicating the allowlist text in `service.profile.json`.
- Unified Feishu and WeChat inbound reply decisions so permission-denied, disabled replies, empty responses, and truncation behavior share the same channel decision path.
- Expanded channel health and service upgrade diagnostics to report WeChat cursor, bot-token readiness, allowlist configuration, service active/enabled status, drift discovery, and precise `serve --check` remediation commands.
- Improved WeChat ClawBot binding UX with QR artifact open commands, list-time `wechat:<from_user_id>` sender hints, and connect command templates when `serve --check` finds a missing bot token.

## 1.2.225 - 2026-06-06

### Fixed
- Fixed direct healthcheck calls with `env_file` so Feishu inbound audit DB paths from `OM_INBOUND_AUDIT_DB` are honored without requiring the caller to preload process environment variables.

## 1.2.224 - 2026-06-06

### Fixed
- Fixed healthcheck `starter_symbols` diagnostics so production watchlists containing example symbols such as `NVDA` no longer warn unless the watchlist is still only starter symbols.

## 1.2.223 - 2026-06-06

### Added
- Added first-class WeChat ClawBot channel support with `./om channel wechat-clawbot` QR login, status, bind, and list commands.
- Added WeChat ClawBot binding state, iLink client, and notification delivery adapter so WeChat targets can be bound and addressed directly.

### Changed
- Removed the `notifications -> OpenClaw -> openclaw-weixin` routing chain; WeChat notification configuration now uses `provider=wechat_clawbot` and `channel=wechat_clawbot`.
- Routed OpenD watchdog and recovery notices through the unified notification delivery adapter and only records alert cooldowns after a confirmed send.
- Refreshed the dependency graph after the channel, notification, ledger, and trade-intake boundary changes.

### Fixed
- Fixed report, alert, and risk-capacity handling for missing numeric values and closed short-option positions.
- Fixed OpenD prefetch/cache diagnostics and watchlist/symbol fetch paths so required-data and pipeline outputs stay consistent after failed or missing upstream reads.
- Hardened canonical trade-event void handling so invalid legacy-shaped void rows no longer hide active events in projection, review, or position reporting.
- Fixed lifecycle/manual ledger identity and lifecycle close validation so assignment, exercise, expiration, and manual-open events cannot collide or write mismatched close targets.
- Fixed trade-intake lifecycle matching, cache invalidation, backfill retries, and `trade_intake.enabled` validation so retryable unresolved deals and disabled listeners behave as configured.

## 1.2.222 - 2026-06-06

### Fixed
- Fixed service-drift reconciliation so confirmed upgrades rewrite installed systemd units whose content differs from the current release render and restart changed timers after daemon reload.

## 1.2.221 - 2026-06-06

### Changed
- Moved expired option auto-close maintenance to 09:00 Beijing time and projection verify to 09:30 Beijing time so the default `grace_days=1` cutoff has passed before scheduled maintenance runs.

### Fixed
- Surfaced expired-but-waiting lots as `grace_period_pending` in auto-close decisions and maintenance summaries so successful runs no longer look like silent noops before the grace cutoff.
- Centralized close-lot alias matching helpers so Combo Yield companion-leg detection and close-candidate summaries consistently canonicalize HK option aliases.

## 1.2.220 - 2026-06-06

### Fixed
- Fixed Futu trade intake for Combo Yield long-call legs when OpenD deals omit open/close position effect by resolving against current lots first, then safely recording unmatched buy calls as Combo Yield long calls.
- Preserved Combo Yield strategy metadata on broker-open previews, preflight results, and projected `position_lots` so paired legs share a stable account/symbol/expiration group id even when the sell-put leg arrives later.

## 1.2.219 - 2026-06-05

### Changed
- Renamed Combo Yield runtime/config/reporting surfaces from legacy `yield_enhancement` to canonical `combo_yield` while preserving safe legacy reads for old configs, artifacts, and existing positions.
- Updated Combo Yield trace, reject-summary, research, shadow-replay, required-data, alert, and documentation surfaces to emit `combo_yield` naming for new outputs.

### Fixed
- Fixed operator-facing Combo Yield examples and config validation messages so new configs point to `combo_yield` instead of the removed `yield_enhancement` authoring key.

## 1.2.218 - 2026-06-05

### Fixed
- Fixed assistant Planner capability validation so single-account income requests such as `lx 6月 收益` accept calculable `monthly_income_report.return_summary` results instead of incorrectly reporting missing `account_return` capability.

## 1.2.217 - 2026-06-05

### Fixed
- Fixed trade-intake runtime-root propagation so remote services and one-shot CLI runs can explicitly use the active runtime root instead of falling back to the release directory.
- Fixed option-position subcommands so `--runtime-root` can be passed at the parent or subcommand level for ledger-backed reads and writes.

## 1.2.216 - 2026-06-05

### Fixed
- Fixed lifecycle expiry confirmation for Futu HK option roots such as `TCH` by canonicalizing them to ledger symbols before resolving `expire_close` targets.

## 1.2.215 - 2026-06-05

### Changed
- Completed the Sell Put / Covered Call opening-config migration to `insurance_underwriting` by removing generated `short_vol` blocks and validating underwriting parameters as top-level opening fields.
- Updated Close Advice configuration resolution so Sell Put / Covered Call close thesis still accepts historical `short_vol` positions while reading current underwriting thresholds from the new top-level fields.
- Updated Shadow Replay parameter backtests and opportunity-quality analysis to use `insurance_underwriting` as the current parameter profile while mapping historical `short_vol` samples into that profile.

### Fixed
- Fixed agent config validation and health diagnostics after the strategy refactor by aligning generated defaults and validation rules with the new underwriting fields.

## 1.2.214 - 2026-06-05

### Added
- Added a guarded `option-positions lifecycle confirm-expired` command to confirm pending zero-price option lifecycle cases as expired without assignment or exercise.

### Fixed
- Fixed trade-intake state reconciliation so completed lifecycle `expire_close` cases clear unresolved zero-price option deals after manual confirmation.

## 1.2.213 - 2026-06-05

### Added
- Added the target product architecture and strategy architecture docs for the underwriting-centered strategy module split.
- Added mixed-policy candidate ranking diagnostics so `candidate_rank_explain` keeps `insurance_underwriting` and unsupported/legacy profiles in separate ranking groups.
- Added trace-only research archive market inference so archived candidate traces can build usable local evidence when final run metadata is absent.

### Changed
- Reworked Sell Put and Covered Call opening semantics from short-vol trading toward `insurance_underwriting`, including shared recall, filtering, and ranking behavior around acceptable assignment/called-away prices.
- Isolated Combo Yield as its own strategy family instead of treating it as an overlay on Sell Put or Covered Call.
- Simplified strategy defaults and generated config/docs around the refreshed underwriting parameters, including the IV/RV floor update to `1.10`.
- Refreshed the dependency graph after the strategy module split.

### Fixed
- Fixed close-advice supplementary quote refresh so RV-only refreshes do not build an implicit OpenD gateway in offline/unit-test paths.
- Fixed auto trade-intake `deal-json` dry-run replay so it does not connect to OpenD for enrichment or multiplier refresh.

## 1.2.212 - 2026-06-05

### Added
- Added combined all-account monthly income summaries so `monthly_income_report` can return `combined_return_summary` using summed CNY cashflow and summed cash-secured denominator instead of averaging account return rates.
- Added assistant Planner `required_capabilities` satisfaction checks so agent-loop replies report partial fulfillment when tool observations do not provide requested capabilities such as combined account returns.

### Changed
- Updated monthly income chat rendering to show combined account income first when available, followed by per-account breakdown.

## 1.2.211 - 2026-06-05

### Fixed
- Fixed Feishu `状态` rendering so successful `runtime_status` results without a latest status field show `OM 状态：ok` instead of `unknown`.
- Used shared `last_run.notify_summary` as fallback runtime evidence so status replies can show recent scan/notification counts when the latest run directory only contains audit or maintenance artifacts.

## 1.2.210 - 2026-06-05

### Fixed
- Fixed Feishu WebSocket `agent_loop` handling so conversation-context audit DB read failures degrade to empty context instead of dropping inbound messages before audit and reply.
- Added regression coverage for Feishu WebSocket messages continuing through planner execution when recent conversation context cannot be read.

## 1.2.209 - 2026-06-04

### Fixed
- Fixed assistant LLM-first routing so deterministic confirm/cancel operation commands such as `确认升级` take priority over agent-loop planning, preventing bare upgrade confirmations from creating a new dry-run upgrade preview.
- Added regression coverage for bare upgrade confirmation in agent-loop mode while preserving deterministic fallback for preview-write intents rejected by the LLM translator.

## 1.2.208 - 2026-06-04

### Added
- Added `om research archive` commands to mirror remote runtime evidence into local `output_shared/research/remote_archive/<remote>/`, verify archive manifests, and build Shadow Replay datasets from verified archived runs.
- Added guarded `research archive prune-remote` cleanup, which previews remote `service cleanup` and refuses confirmed deletion unless every planned `output_runs` removal is present in the local verified inventory.
- Documented the remote-evidence archive workflow for low-storage production hosts, including dry-run-first pull, local verify, dataset build, and separate remote prune steps.

## 1.2.207 - 2026-06-04

### Fixed
- Fixed trade-intake state reconciliation so pending lifecycle deals are marked processed when their lifecycle case has already been written as assignment or exercise.
- Added regression coverage to keep waiting lifecycle cases pending while allowing completed lifecycle evidence to reconcile unresolved deal state.

## 1.2.206 - 2026-06-04

### Fixed
- Fixed broker trade intake for early assignment/exercise evidence so zero-price option close legs enter the lifecycle workflow before settlement evidence arrives, instead of failing normal close-price preflight.
- Added regression coverage for retrying a failed Futu zero-price assignment close so it records a lifecycle pending case rather than repeating `LedgerPreflightError`.

## 1.2.205 - 2026-06-04

### Added
- Added approved preview-write capability planning to assistant `agent_loop`, allowing natural-language requests to create pending previews for manual trade records, monitored-symbol edits, model switches, and upgrade requests.
- Added agent-loop safeguards that reject write-like requests when the LLM incorrectly plans a read-only query, preventing Futu fill alerts from being answered as nearby position or income queries.

### Changed
- Kept legacy `llm_router` on its existing structured intent surface while moving broader natural-language capability planning to bounded `agent_loop` plans.
- Kept confirm/cancel/apply operations deterministic and outside the Planner manifest; Planner preview steps can only create pending operation previews.

## 1.2.204 - 2026-06-04

### Added
- Added assistant `agent_loop` tool semantics and coverage metadata for monthly income observations, so LLM synthesis can distinguish OM local-ledger scope from broker account history.
- Added assistant answer-guard verification that catches LLM replies contradicting tool observations and asks for one guarded rewrite before falling back to canonical rendering.

### Fixed
- Fixed assistant plan normalization for all-history, all-account, and multi-month income/cashflow requests so tool arguments do not silently collapse to the wrong account or month.
- Added income/cashflow agent-loop regressions covering cumulative cashflow, multi-month income, account comparisons, detail/composition, premium, realized PnL, and default month queries.

## 1.2.203 - 2026-06-04

### Fixed
- Fixed assistant `agent_loop` fallback responses so successful tool results are rendered through the canonical formatter when LLM synthesis is unavailable, instead of showing raw tool row-count summaries to users.
- Fixed assistant tool-plan normalization so misplaced `response_mode` fields inside tool arguments are hoisted to the plan level before validation, preventing `monthly_income_report` detail queries from failing with unsupported arguments.

## 1.2.202 - 2026-06-04

### Fixed
- Fixed release cleanup so internal directories such as `releases/_cache` are not counted as retained releases, preserving the intended rollback release count.

## 1.2.201 - 2026-06-04

### Fixed
- Fixed assistant `agent_loop` planning for no-year month phrases such as `6月` by injecting Asia/Shanghai temporal context and normalizing monthly income plan arguments before tool execution.
- Treated cashflow/net-cashflow natural-language requests as income-report intents in deterministic fallback so detail questions do not fall through to clarification when LLM planning is unavailable.

## 1.2.200 - 2026-06-03

### Added
- Added a bounded read-only assistant tool planner for Feishu/assistant `agent_loop` mode, allowing natural-language analysis requests to plan up to three safe read-only tool calls before generating a response.
- Added planner synthesis for detail/composition questions such as monthly net-cashflow breakdowns, including `monthly_income_report(include_rows=true)` observations.

### Changed
- Kept `assistant.tool_plan` as an internal pseudo-tool hidden from the public LLM capability manifest while routing execution through the existing inbound router and read-only tool policy.
- Allowed the planner to return an explicit no-plan result instead of guessing when no safe read-only tool plan exists.

## 1.2.199 - 2026-06-03

### Added
- Added optional OpenD service rendering via `om service render --include-opend`, including systemd/launchd service files, profile metadata, install commands, and upgrade restart participation.

### Changed
- Made rendered trade-intake systemd units declare `After/Wants=options-monitor-opend.service` when OpenD is included, so broker connectivity is managed before the deal listener starts.
- Documented OpenD service rendering and upgrade restart behavior for production service bundles.

## 1.2.198 - 2026-06-03

### Added
- Added periodic Futu history-deal backfill to auto trade intake so missed realtime deal pushes can still be detected and routed through the existing idempotent intake pipeline.
- Exposed push/backfill timestamps, applied counts, duplicate counts, and backfill errors in `runtime_status` trade-intake diagnostics.

### Changed
- Tagged trade-intake audit and receipt context with `push`, `backfill`, or `manual` source to make missed-push repairs distinguishable from realtime intake.
- Serialized realtime push and backfill processing with a shared lock to keep deal-state updates idempotent under concurrent OpenD callbacks and scheduled checks.

## 1.2.197 - 2026-06-03

### Added
- Added `research shadow-replay parameter-report` to generate paired JSON and Markdown parameter candidate-impact reports from existing scan evidence and explicit parameter files.

### Changed
- Renamed filter-only parameter backtest recommendations to `ready_for_live_shadow_candidate_review` so reports no longer imply live shadow has already run.

## 1.2.196 - 2026-06-03

### Changed
- Split candidate rejection summaries so unavailable spread ratios render as `报价不可评估/流动性不足` and non-positive candidate net income renders as `净收入非正` instead of being folded into generic data-missing counts.
- Tuned default option liquidity gates by adding low open-interest floors to Sell Put and Covered Call templates and relaxing the Yield Enhancement open-interest floor.

## 1.2.195 - 2026-06-03

### Added
- Added explicit Shadow Replay parameter-backtest gates for candidate-impact review versus production parameter recommendation, so filter-only evidence can show candidate-count effects without implying production config readiness.
- Added candidate-impact summaries to JSON and Markdown parameter-backtest reports, including best variants by newly accepted and total accepted candidates.

### Changed
- Allowed parameter backtests with enough complete parameter-field samples to report filter-only candidate impact even when a small portion of fields is still missing, while keeping production recommendation blocked until mark/outcome evidence is available.

## 1.2.194 - 2026-06-03

### Fixed
- Preserved short-vol parameter fields in candidate trace and reject evidence so Shadow Replay parameter backtests can evaluate DTE, Delta, IV/RV, IV-RV edge, and annualized return gates instead of producing empty candidate results.
- Reported missing parameter evidence as `parameter_fields_missing` with field coverage diagnostics before treating zero accepted variants as a parameter outcome.
- Inferred short-vol replay profile for accepted Sell Put and Covered Call candidate snapshots when explicit strategy metadata is absent but replay fields are present.

## 1.2.193 - 2026-06-02

### Added
- Added read-only `research shadow-replay parameter-backtest` for counterfactual short-vol parameter replay across existing datasets or historical `output_runs` date windows.
- Added parameter-variant whitelist validation, coverage gating for missing scan artifacts, observed-universe reporting, and Markdown report output for replay reviews.

### Changed
- Preserved bid/ask/mid/last, open interest, and volume fields in Shadow Replay candidate snapshots so parameter backtests can retain liquidity evidence.

## 1.2.192 - 2026-06-02

### Changed
- Reworked short-vol Close Advice so IV/RV edge weakness, delta drift, and event context remain underwriting observations unless profit-capture thresholds are met.
- Simplified compact monitoring receipts into status, candidate, position, and funding sections with compressed rejection summaries and clearer pending-data text.

### Fixed
- Prevented normal medium close advice from being counted or tagged as optimizer close actions unless optimizer detail evidence is present.

## 1.2.191 - 2026-06-02

### Added
- Added strategy-aware candidate-filter trace fields for option type, strategy family, and strategy profile across Sell Put, Covered Call, Yield Enhancement, and Close Advice evidence.
- Added Shadow Replay readiness diagnostics that separate sample size, instrument identity, strategy profile, trace-only evidence, mark coverage, outcome coverage, and bad-decision signal blockers.
- Added an Opportunity Quality gate for Shadow Replay parameter review so replay remains dry-run-only until evidence is sufficient.

### Changed
- Extracted Yield Enhancement overlay orchestration out of the Sell Put main flow while preserving the existing scanner and notification behavior.
- Kept Close Advice event-risk failures contextual for lifecycle review instead of fail-closing profitable or acceptable short-vol positions solely because an event source is unavailable.
- Preserved strategy metadata from scan and post-filter contexts into replay snapshots before parameter review, reducing reliance on filename or function inference.

## 1.2.190 - 2026-06-02

### Added
- Added offline short-vol insurance replay metrics for Sell Put and Covered Call, including loss ratio, underwriting margin, premium-to-capital, assignment/called-away rates, and adverse-path loss versus premium.
- Exposed insurance replay metrics by status, option mode, and DTE/Delta/IV-RV/Spread/concentration buckets for parameter review before production config tuning.

### Changed
- Treated non-profitable short-vol close scenarios as hold-by-default when assignment or called-away is acceptable, while keeping explicit risk-budget exits separate from mark-to-market losses.
- Preserved premium and capital-at-risk fields in shadow replay candidate snapshots so replay analysis can evaluate underwriting quality instead of only trade PnL.

## 1.2.189 - 2026-06-01

### Changed
- Kept Assistant natural-language routing LLM-first while reconciling same-intent read slots from the deterministic shadow parser, so explicit account/month filters such as `sy 2026-06` stay stable.
- Allowed LLM to recognize monitored-symbol `symbol_edit` requests only as preview-write operations for covered-call and sell-put settings, leaving confirm/apply/cancel paths deterministic-only.

### Fixed
- Rejected conflicting LLM preview-write interpretations when the deterministic parser identifies a different operation, preventing ambiguous text from creating unintended pending writes.
- Added offline LLM intent replay coverage for income, positions, close advice, runs, upgrade-confirm rejection, and symbol-edit previews.

## 1.2.188 - 2026-06-01

### Fixed
- Allowed LLM-first Assistant routing to fall back to deterministic confirm commands only when the LLM rejected the same known non-executable OM intent, fixing `确认升级` without broadening LLM write execution.
- Added explicit LLM rejection reasons for known non-executable intents versus unknown intents so fallback decisions stay auditable.

## 1.2.187 - 2026-06-01

### Added
- Added `om config symbol set` for audited `config.yaml` symbol strategy edits, including covered-call min-strike updates and optional runtime config rebuilds.
- Enabled Assistant/IM monitored-symbol setting previews to write YAML-backed config after confirmation while preserving sender allowlist, audit, and pending-operation confirmation controls.

### Fixed
- Prevented natural-language symbol setting phrases such as `设置 09898 covered call min strike 85` from treating command words as symbols.

## 1.2.186 - 2026-06-01

### Changed
- Simplified rejection-summary notification receipts to keep total pass/filter counts and top rejection categories while removing module breakdowns, rule-level details, and sample symbols from the main message.

## 1.2.185 - 2026-06-01

### Fixed
- Prevented soft short-vol thesis warnings from being promoted to actionable close notifications when buying back would lock in a loss.
- Rendered actionable risk exits as `风险平仓` / `风险止损` with `平仓损益`, avoiding profit-capture wording such as negative `已锁定` or negative `收益`.

## 1.2.184 - 2026-06-01

### Changed
- Made `llm_router` and `agent_loop` natural-language Assistant perception LLM-first, with deterministic parsing kept as fallback/shadow evidence while slash commands remain command-first and LLM-skipped.
- Simplified monthly income Assistant receipts around net cashflow, realized PnL, premium, annualization, and explicit long-option cash-recovery hints.

### Fixed
- Parsed Chinese month expressions such as `6月`, `六月`, and `2026年6月` for natural-language and slash-command income queries.

## 1.2.183 - 2026-06-01

### Added
- Added compact read-only `om update verify` release verification for symlink, version, runtime config freshness, event-source config, upgrade status, and service health.
- Added `om event-source probe --summary-only` for remote event-source checks without raw event payload noise.
- Added the read-only `scripts/release_test_plan.py` advisor to map changed files to a focused release validation plan.

### Changed
- Documented the faster release verification loop and refreshed the dependency graph for the new release-planning module.

## 1.2.182 - 2026-06-01

### Changed
- Made Futu/OpenD the default event-risk source, with HK scans pinned to Futu and US scans using Futu before yfinance fallback.
- Added the default Futu-first event-source policy to generated runtime configs so missing user overrides no longer fall back to yfinance.

## 1.2.181 - 2026-06-01

### Fixed
- Added explicit Yield Enhancement long-call take-profit ask guidance with bid/ask context in Close Advice notifications.
- Fixed compact close-advice alternative candidate strike rendering so Ruff lint passes.
- Kept trade-intake deal-json stdout machine-readable when the Futu SDK writes connection logs.

## 1.2.180 - 2026-06-01

### Fixed
- Rendered Yield Enhancement long-call Close Advice rows with call value ratio and unrealized gain metrics instead of empty short-option capture fields.

## 1.2.179 - 2026-06-01

### Added
- Added a multi-source event-risk resolver with Futu/OpenD primary support, yfinance fallback support, market-specific provider chains, and resolved per-run event snapshots.
- Added the read-only `om event-source probe` CLI for checking Futu/OpenD and yfinance event-source availability without writing runtime state.

### Changed
- Treated `ok_with_fallback` event-source results as usable for short-vol scanning and Close Advice while preserving per-provider failure details in `source_results`.
- Kept `futu-api` and `yfinance` as lower-bounded runtime dependencies instead of pinned constraints so validated data-source SDK upgrades can be picked up during normal releases.

## 1.2.178 - 2026-06-01

### Fixed
- Preserved position-lot strategy metadata in the option-positions context so Close Advice can evaluate repaired Yield Enhancement long-call lots.

## 1.2.177 - 2026-06-01

### Added
- Added audited `option-positions adjust-lot` strategy metadata repair fields so historical Yield Enhancement long-call lots can be marked without direct SQLite edits.

### Fixed
- Preserved adjusted strategy metadata in projected position lots so Close Advice can evaluate repaired Yield Enhancement long-call legs.

## 1.2.176 - 2026-06-01

### Fixed
- Preserved runtime event-risk snapshot paths through resolved candidate defaults so event-source failures remain visible to scanner and reject-summary diagnostics.
- Made `runtime_status` service-profile config resolution honor an explicit `config_key` when selecting the runtime `config_path`.
- Surfaced `event_source_unavailable` as an event-risk warning in candidate rejection summaries instead of burying it as generic missing data.
- Fixed account notification overview counts so rejection summaries and other explanatory text no longer inflate Covered Call or Yield Enhancement candidate totals when no detailed candidates are rendered.

## 1.2.175 - 2026-06-01

### Added
- Added service-profile and runtime-root aware Shadow Replay dataset construction so production scan evidence can be replayed without manually stitching `output_runs` and dataset paths.
- Added `--latest-scanned-run`, `--runs-root`, `--runtime-root`, and `--dataset-root` support to `research shadow-replay build`.
- Added founder operating model guidance to the agent manual, including CEO final decision authority and CTO/strategy-lead boundaries.

### Changed
- Made Shadow Replay `status`, `list`, and `run-data-plan` derive runtime dataset, required-data, and receipt roots from `service.profile.json` when provided.
- Updated Shadow Replay docs and runbooks with the profile-driven production evidence workflow and offline-only safety boundary.

## 1.2.174 - 2026-05-31

### Added
- Added Shadow Replay `status` / `list` dashboards with local dataset readiness, sampling freshness, data-maintenance plans, and a separate manual review queue.
- Added `research shadow-replay run-data-plan` as a dry-run-first local data-maintenance runner for eligible `collect_marks` / `settle` actions.

### Changed
- Split Shadow Replay maintenance actions from manual `analyze` review so `data_plan` stays executable-only and `review_queue` carries review prompts.
- Extended Close Advice redeploy evidence so `optimizer_switch` rows include explicit alternative candidate identity and source path fields.

### Fixed
- Prevented Shadow Replay data-plan dry-runs from writing receipts and rejected receipt output flags unless `--write` is explicit.
- Kept `run-data-plan` from accepting or executing `analyze`, preserving manual review as an explicit offline step.

## 1.2.173 - 2026-05-31

### Changed
- Split the unified CLI assistant, inbound, and run command implementations into focused `src.interfaces.cli.*_ops` owners while preserving the public `./om` facade.
- Updated dependency graph generation and docs to reflect the split CLI ownership model without stale high-fan-out guidance.

### Fixed
- Strengthened architecture guard coverage so CLI boundary checks continue to cover the new assistant and inbound owner files after refactors.

## 1.2.172 - 2026-05-31

### Added
- Added `./om research shadow-replay collect-marks` for repeatable Shadow Replay mark sampling from local required-data cache or explicit OpenD current quotes.
- Added a Shadow Replay runbook covering dataset construction, mark sampling cadence, settlement, analysis, and offline-only boundaries.

### Changed
- Documented Shadow Replay sampling in README, Tool Reference, Agent Wiki, and the strategy optimization first-steps guide.
- Extended Shadow Replay mark collection safety output with explicit persistent write targets.

### Fixed
- Kept OpenD collect preview mode from persisting required-data, replay marks, OpenD limiter state, or OpenD cache files by routing preview fetches through temporary paths.

## 1.2.171 - 2026-05-31

### Added
- Added offline shadow replay evidence under `src/application/shadow_replay/` with staged capture, marking, settlement, analysis, and readiness modules for accepted/rejected candidate universes.
- Added `./om research shadow-replay build|mark|settle|analyze` and `candidate_evidence.shadow_replay` readiness output for Research candidate bundles.
- Added `runtime_status.config_authority` so operators can verify runtime config identity, freshness, source hashes, stale reasons, and rebuild commands.
- Added `run_id` / `run_dir` / `account` lookup support to `candidate_rank_explain` for run-specific candidate ranking diagnosis.

### Changed
- Updated Research, CLI, agent-tool metadata, README, Tool Reference, and Agent Wiki docs for offline shadow replay evidence boundaries and candidate evidence handoff.
- Updated the agent manual guidance so LLM memory is treated as navigation context, not current-state proof.

## 1.2.170 - 2026-05-30

### Added
- Added a Close Advice action-policy scenario matrix covering Sell Put, Covered Call, Yield Enhancement short-put, and Yield Enhancement long-call exit actions.

### Changed
- Refactored Close Advice action mapping into explicit `strategy_exit_mode` policies so domain exit states, strategy-specific close actions, and shared rendering stay separated.
- Updated Close Advice architecture docs and README guidance to describe the current action-policy boundary.

## 1.2.169 - 2026-05-30

### Added
- Added candidate rejection summaries to account scan notifications, grouping existing `candidate_filter_trace.jsonl` / reject-log evidence into user-readable causes such as missing data, volatility edge, liquidity, risk budget, event risk, and cash or coverage constraints.

### Changed
- Account scan notifications now surface why a run produced no candidates or filtered candidates, without changing candidate selection, ranking, or strategy thresholds.

## 1.2.168 - 2026-05-30

### Changed
- Centralized Sell Put / Covered Call strategy semantics in `strategy_policy` so scanning, Yield Enhancement, required-data planning, and Close Advice share one mode contract.
- Updated Yield Enhancement to follow the active Sell Put strategy: return-first uses income/upside enhancement, short-vol uses vol-convexity enhancement with short-vol put-universe gates.
- Added net-credit-yield and annualized-net-credit-yield handling for Yield Enhancement reports, summaries, ranking evidence, and notifications.
- Refactored Yield Enhancement pair selection so call-leg loading, pair evaluation, funding gates, and persistence are separated inside the existing pipeline.

### Fixed
- Fixed strategy required-data planning to fail fast when template-backed symbol configs reach planning before profile expansion.
- Fixed Close Advice Yield Enhancement leg detection so short-put and long-call action semantics use one shared position-role contract instead of duplicated local checks.

## 1.2.167 - 2026-05-30

### Fixed
- Fixed Close Advice chat analysis so the default US runtime config no longer hides HK positions; symbol-specific queries now use the symbol market and symbol-less exit analysis reads recent US/HK reports.
- Fixed monitor-symbol chat writes so explicit HK/US symbols choose the matching runtime config instead of inheriting the default chat market.

## 1.2.166 - 2026-05-30

### Added
- Added a read-only `close_advice_read` agent tool and Assistant `position_exit_analysis` routing so chat can answer close/take-profit analysis requests from existing Close Advice reports without refreshing market data or writing reports.
- Added readable chat rendering for Close Advice rows, including Yield Enhancement put/call action semantics and long-call value metrics.

### Fixed
- Fixed Close Advice chat source selection so `config_key`/runtime config market filters out runs from the wrong market.
- Fixed Close Advice chat fallback ordering so runtime-root reports are preferred over repo/release-directory agent-tool artifacts.

## 1.2.165 - 2026-05-30

### Fixed
- Fixed upgrade checks and chat-triggered upgrades so an already-current release returns `没有可升级版本` and does not create a pending confirmation.
- Fixed inbound confirmation lifecycle so expired preview records are persisted as `expired`, and stale `confirmed`/`running` operations are finalized as `failed` instead of lingering without an audit result.

## 1.2.164 - 2026-05-30

### Fixed
- Fixed expired auto-close maintenance so omitted `portfolio.data_config` uses the runtime-root SQLite ledger default instead of failing on a missing `portfolio.runtime.json`.
- Fixed `runtime_status` so maintenance runs no longer trigger stale scan-notification warnings and auto-close failures are surfaced explicitly.
- Fixed Feishu status rendering so failed auto-close runs show `failed` with the failure reason instead of only `sent, closed=0`.

## 1.2.163 - 2026-05-29

### Added
- Added explicit assistant perception, reasoning, action, and observation contracts so command, deterministic, and LLM inputs flow through one auditable assistant lifecycle.
- Added Close Advice source-data readiness coverage for short-vol positions, including RV/IV/delta refresh behavior and event snapshot availability.

### Changed
- Replaced the old assistant frame/tool-plan planner path with the perception -> reasoning -> action -> observation chain, with reasoning owning tool selection, safety class, and confirmation requirements.
- Updated assistant architecture and inbound-control docs to describe the current contract names, audit fields, and capability catalog behavior.

### Fixed
- Fixed short-vol Close Advice preparation so incomplete required-data quote rows trigger realized-volatility refresh through OpenD instead of surfacing stale missing-RV gaps.
- Fixed short-vol Close Advice event-risk handling so run-level event snapshots are merged before evaluation and missing event sources fail closed with readable diagnostics.

## 1.2.162 - 2026-05-29

### Added
- Added a Close Advice contract document and regression matrix for direct Sell Put, Covered Call, Yield Enhancement short-put, and Yield Enhancement long-call exit semantics.
- Added regression coverage for yield-enhancement combo close gating, not-evaluable quote handling, and long-call convexity spread checks.

### Changed
- Updated README and tool documentation to describe the current Sell Put, Covered Call, Yield Enhancement, and Close Advice behavior without historical migration notes.
- Changed Close Advice output so Yield Enhancement short-put exits use strategy-specific semantics and optional combo-close advice only appears when paired-call economics are complete.

### Fixed
- Fixed Close Advice quote handling so domain `not_evaluable` results are preserved instead of being marked as priced.
- Fixed long-call convexity advice to reject wide bid/ask spreads as not evaluable.

## 1.2.161 - 2026-05-29

### Added
- Added regression coverage for expired auto-close runs launched from release directories with runtime-root-backed state.
- Added regression coverage for explicit `--runtime-root` propagation through the `option-positions auto-close-expired` facade.

### Fixed
- Fixed expired auto-close runtime resolution so audit logs, run outputs, shared state, and default `portfolio.runtime.json` resolution stay under the configured runtime root instead of the release directory.
- Fixed missing auto-close data config handling so scheduled jobs fail explicitly instead of reporting a successful skipped run.

## 1.2.160 - 2026-05-29

### Added
- Added option lifecycle case/evidence storage for assignment and exercise workflows.
- Added manual `option-positions assign`, `option-positions exercise`, and lifecycle inspect/list CLI commands.
- Added regression coverage for same-expiry lifecycle closes, stock-first/option-first assignment and exercise, and lifecycle auto-close blockers.

### Changed
- Separated normal close, expire-close, assignment, and exercise into distinct ledger semantics while preserving the canonical `trade_events -> projection -> position_lots` path.
- Changed expired auto-close to skip pending lifecycle cases and matching stock settlement evidence, with external/manual accounts requiring review instead of automatic close.

### Fixed
- Fixed lifecycle close publishing so assignment and exercise close types are not rewritten as buy-to-close or sell-to-close.
- Fixed stock settlement handling so ordinary stock trades keep skipping as non-option deals while late assignment evidence can still surface conflicts after expire-close.

## 1.2.159 - 2026-05-28

### Added
- Added assistant model profiles with OpenAI and DeepSeek provider catalog support.
- Added `om assistant model` commands to catalog, list, inspect, add, switch, and check assistant model profiles.
- Added `/model` chat commands for listing models and previewing model switches through the existing confirm/apply operation flow.
- Added event prefetch snapshots backed by the runtime event source path so scanner runs can reuse event-risk evidence.

### Changed
- Changed Feishu inbound processing to reload assistant configuration per message so model switches take effect without restarting the long-lived gateway.
- Changed runtime status to report event prefetch state alongside the existing run, ledger, and service summaries.
- Changed assistant routing contracts to use the semantic-frame naming consistently across command, deterministic, and LLM paths.

## 1.2.158 - 2026-05-28

### Added
- Added an explicit semantic-frame schema version to assistant intent payloads so command, deterministic, and LLM routing share one auditable contract.
- Added assistant NLU eval coverage for expected source and safety class, preventing intent-only tests from missing route or write-safety drift.
- Added an architecture guard to keep inbound transport adapters from importing assistant parser, LLM, router, and arbitration internals.

### Changed
- Centralized assistant LLM provider selection, endpoint resolution, and unsupported-provider errors behind the shared LLM provider boundary.
- Kept Feishu inbound transport on a thinner boundary by removing its dependency on assistant router typing.
- Simplified the default small-talk response so user-visible capability wording stays catalog-driven.

## 1.2.157 - 2026-05-28

### Changed
- Removed legacy JSON authoring write paths so human-authored runtime config flows through `config.yaml` and generated runtime snapshots only.
- Simplified option-position ledger handling around the canonical `trade_events -> projection -> position_lots` path, removing legacy SQLite migration and tuple result compatibility surfaces.
- Removed legacy runtime `output` symlink repair paths and old monthly report row handling, keeping runtime artifacts under `output_runs`, `output_shared`, and `output_accounts`.

## 1.2.156 - 2026-05-27

### Added
- Added structured assistant frames and tool plans so inbound messages are audited with intent, payload, safety class, planned tool, and confirmation requirement before execution.
- Added structured natural-language option-position queries with account, status, symbol, option type, side, strike, expiration, and limit filters.

### Changed
- Moved inbound tool planning out of the router into a single frame planner path, replacing the previous per-intent router mapping.
- Consolidated shared preview-save and confirm-validation lifecycle logic across manual trade, symbol, and upgrade operations.

### Fixed
- Fixed natural-language position queries such as `5月到期的持仓` so exact help examples no longer bypass semantic parsing and drop the expiration filter.

## 1.2.155 - 2026-05-27

### Added
- Added deterministic `/record-open` and `/record-close` assistant commands for manual trade write-preview intake.

### Changed
- Documented that manual trade slash commands reuse the existing preview/confirm safety path and remain outside the LLM-executable intent set.

## 1.2.154 - 2026-05-27

### Added
- Added a canonical strategy policy resolver so close advice, yield enhancement, and position workflows resolve strategy state from the same Sell Put / Covered Call configuration path.
- Added strategy snapshots for newly opened option lots and preserved them through manual trade intake, ledger preflight, publishing, projection fields, and position views.

### Changed
- Changed close advice and yield enhancement to follow the active Sell Put / Covered Call strategy profile instead of maintaining independent strategy modes.
- Regenerated dependency graph documentation after the strategy policy boundary change.

### Fixed
- Added config validation to reject independent `close_advice.strategy` and `yield_enhancement.strategy` settings, keeping strategy switching tied to the scanner strategy configuration.

## 1.2.153 - 2026-05-27

### Changed
- Unified cash footer wording to prefer total CNY cash and post-guarantee headroom, avoiding account-specific "holding" versus "cash-like" labels caused by different data sources.

## 1.2.152 - 2026-05-26

### Changed
- Changed Futu/OpenD cash aggregation to use explicit currency cash fields and fund assets, ignoring the ambiguous legacy `cash` aggregate.
- Added cash component/source diagnostics and separated broker cash buying power from cash-like asset totals.
- Clarified cash notification and Sell Put alert wording so cash-like assets and post-guarantee headroom are not described as broker available cash.

## 1.2.151 - 2026-05-26

### Added
- Added assistant intent arbitration and decision metadata so command, deterministic, LLM, and agent-loop candidates can be compared and audited.
- Added assistant NLU eval fixtures covering recent inbound inputs and Covered Call symbol configuration phrasing.

### Changed
- Moved assistant intent arbitration out of runtime into a dedicated IntentArbitrator control-plane component.
- Kept runtime focused on request orchestration, router execution, agent-loop tool traces, and response metadata.

### Fixed
- Fixed natural-language Covered Call symbol configuration so inputs such as `tigr covered call min strike=6.5` route to symbol edit instead of manual trade update.

## 1.2.150 - 2026-05-26

### Added
- Added a low-noise Ruff lint entrypoint via `make lint` and the guardrails CI workflow, limited to syntax errors and undefined names.

### Fixed
- Fixed undefined helper references in assistant manual-trade update parsing and stale standalone test runner function names that the new lint gate now catches.

## 1.2.149 - 2026-05-26

### Fixed
- Added config validation to fail fast when enabled Sell Put or Covered Call entries do not inherit a strategy template or set an explicit strategy, preventing silent fallback from `short_vol` to `return_first`.
- Updated symbol add/edit flows so Covered Call changes add `call_base` by default and inbound Covered Call edits can request the required template inheritance.

## 1.2.148 - 2026-05-26

### Added
- Added a shared short-vol assessment for Sell Put and Covered Call covering IV/RV edge, Delta band, event risk, path stress, and portfolio concentration.
- Added Covered Call gap-up right-tail opportunity cost fields, hard NAV/premium stress budgets, candidate trace rejection rules, and alert output.
- Added required-data planning for short-vol scanner inputs including realized volatility, event risk, portfolio holdings, and option-position concentration context.

### Changed
- Changed the default Covered Call profile to `short_vol` so it follows the same short-vol / short-gamma risk framing as Sell Put.
- Changed `short_vol` scanning so annualized return and net income are ranking inputs rather than first-stage hard filters.
- Expanded candidate ranking and diagnostics with volatility edge, Delta target quality, concentration, and path-risk scoring dimensions.

### Fixed
- Updated release smoke validation to assert the new `short_vol` default template contract instead of removed return-first threshold fields.

## 1.2.146 - 2026-05-26

### Fixed
- Preserved Sell Put event-risk fields through summary normalization, alert rendering, and Feishu notification output so flagged events appear in user-facing scan notifications.
- Stopped caching event-source failures as empty event lists; event fetch errors now persist source status/error metadata and legacy empty caches are refetched instead of hiding source outages.

## 1.2.145 - 2026-05-26

### Added
- Added Sell Put `short_vol` strategy screening with IV/RV edge gates, Delta target-band checks, and portfolio concentration caps.
- Added realized-volatility snapshots from OpenD/Futu historical daily K-line data, including RV20/RV60/RV120 and a weighted RV estimate in candidate outputs.
- Added short-vol ranking dimensions for volatility edge, Delta target quality, and concentration usage.

### Changed
- Changed the default Sell Put strategy to `short_vol`, making missing IV/RV/Delta/NAV/concentration evidence fail closed instead of ranking by yield alone.
- Expanded Sell Put reports, summaries, and alerts with IV/RV, Delta, and concentration diagnostics.

## 1.2.144 - 2026-05-26

### Changed
- Added `covered_call` as the preferred `config.yaml` authoring key for Covered Call settings while preserving the generated runtime/internal `sell_call` key for snapshots, traces, CSV files, and existing code paths.
- Updated YAML config migration, starter examples, config explain, and operator docs so user-facing configuration uses Covered Call terminology consistently.

## 1.2.143 - 2026-05-25

### Removed
- Removed the obsolete strategy replay analysis surface (`om strategy-replay analyze`, agent `strategy_replay_analyze`, and `scripts/tools/compare_strategy_replay.py`) after the old analysis surface was retired for redesign.

### Changed
- Renamed scan-quality evidence diagnostics from strategy evidence to candidate evidence across `healthcheck`, `doctor`, and `research`.
- Renamed the user-facing Sell Call terminology to Covered Call while preserving the stable internal `sell_call` key for runtime snapshots, traces, and historical files.
- Centralized strategy vocabulary in `domain.domain.strategy_vocab` so notifications, reports, scanner text, and agent manifests share one internal-key-to-display-name mapping.

## 1.2.138 - 2026-05-24

### Fixed
- Clarified final Feishu upgrade receipts so the pre-upgrade version is labeled separately from the active current version, and internal `applied` status no longer leaks into user-facing text.

## 1.2.137 - 2026-05-24

### Added
- Added the `research` evidence collector and agent tool as the public replacement for the old AI Cofunder naming.

## 1.2.136 - 2026-05-24

### Added
- Added an explicit trade-intake `--retry-failed` replay path for single-deal JSON repair of historical failed deal ids without allowing processed deal ids to be written again.
- Added `om run trade-intake --reconcile-state` to reconcile historical failed/unresolved intake state from ledger and audit evidence after a manual ledger repair has already corrected the position.

### Fixed
- Prevented corrected historical trade-intake failures from continuing to degrade runtime status when the ledger already contains the canonical close or skipped non-option evidence.

## 1.2.135 - 2026-05-23

### Fixed
- Clarified post-upgrade Feishu WebSocket remediation so root-only env-file deployments point operators to an explicit sudo env-file check.

## 1.2.134 - 2026-05-23

### Fixed
- Fixed post-upgrade Feishu WebSocket health checks for root-only service env directories by explicitly passing the service profile env file and using non-interactive sudo when the upgrade process cannot read that file directly.

## 1.2.133 - 2026-05-23

### Added
- Added explicit `--env-file` support for `./om healthcheck`, `./om doctor`, `./om status`, Feishu inbound commands, assistant commands, and `./om-agent run`.
- Added redacted environment-source diagnostics to `healthcheck` and `runtime_status` so production checks can confirm which env file and keys are loaded without exposing secret values.

### Fixed
- Unified Feishu Bot, Feishu holdings, assistant, and runtime-status environment loading through the same effective-env path, reducing drift between manual CLI checks, systemd services, and upgrade health checks.

## 1.2.132 - 2026-05-23

### Fixed
- Preserved systemd-loaded OM and LLM environment variables for post-upgrade Feishu WS health checks, preventing root-only env files from causing false service-health failures after release upgrades.

## 1.2.131 - 2026-05-23

### Changed
- Made `./om assistant handle` the canonical controlled message entrypoint and moved pending, audit, and upgrade-worker diagnostics under `./om assistant`.
- Narrowed `./om inbound` to channel transport adapters only, leaving `feishu` and `feishu-ws` as the public inbound subcommands.
- Removed the legacy `agent_runtime` package and old inbound backend wrappers so Assistant owns command parsing, routing, policy, audit, operation handling, and rendering directly.

### Fixed
- Updated Feishu event handling and Feishu WS to always enter Assistant control, removing the old assistant bypass flags.
- Added architecture guards and CLI smoke coverage to prevent old `inbound handle` and `agent_runtime` compatibility paths from returning.

## 1.2.130 - 2026-05-23

### Added
- Added an assistant capability catalog that exposes the project abilities visible to LLM routing while marking write, confirm, symbol-edit, and upgrade flows as known but non-executable by LLM.
- Added `om assistant capabilities` and capability summaries in `om assistant llm-check` so operators can inspect the LLM routing surface directly.

### Fixed
- Kept unknown slash commands on the deterministic command path instead of letting the LLM invent unsupported project commands.

## 1.2.129 - 2026-05-23

### Fixed
- Treated broker expiration zero-price option closes as canonical `expire_close` ledger events, allowing assigned or expired option positions to close at `0.0` without failing preflight.

## 1.2.128 - 2026-05-23

### Changed
- Consolidated the assistant control plane under `src.application.assistant`, leaving `agent_runtime` and inbound backend modules as thin compatibility wrappers.
- Made `./om assistant` the public conversational assistant inspection entry while keeping the old `./om agent` command as a hidden compatibility alias.
- Renamed assistant-facing command specs to `AssistantCommandSpec` while preserving the old `AgentCommandSpec` import alias.

### Fixed
- Added architecture guards that prevent application code from depending on the old `agent_runtime` backend and keep assistant, inbound channel adapters, and compatibility wrappers separated.

## 1.2.127 - 2026-05-23

### Changed
- Consolidated runtime artifact cleanup under `om service cleanup`, replacing the old standalone cleanup script with the canonical maintenance entry.

### Added
- Added type-aware retention controls for `output_runs` and runtime `.log` files, with dry-run planning, protected latest-run pointers, and minimum recent-run retention.

## 1.2.126 - 2026-05-22

### Added
- Added a constrained LLM general-reply fallback for harmless non-business chat, such as assistant identity questions, after deterministic and read-only intent routing cannot produce an action.

### Fixed
- Kept the general LLM reply path blocked for trade, position, income, config, upgrade, symbol, confirmation, and other write-like or business requests so it cannot bypass deterministic tools or preview/confirm flows.

## 1.2.125 - 2026-05-22

### Fixed
- Made automatic trade intake silently ignore non-option stock deals, preventing stock buys/sells from entering option-position state or sending "not recorded" receipts.

## 1.2.124 - 2026-05-22

### Fixed
- Kept default `portfolio.runtime.json` resolution scoped to the runtime root, avoiding permission failures from probing `/etc/options-monitor` during cash footer generation.

## 1.2.123 - 2026-05-22

### Added
- Added a structured `om-agent-loop-v1` trace contract for the optional assistant agent loop, including planned read-only steps, sanitized tool observations, and final response ownership.
- Added runtime status diagnostics for assistant config, LLM provider readiness, inbound audit state, and latest agent route.
- Added shared helpers for assistant read-only tool allowlists and config section resolution.

### Fixed
- Kept the assistant agent loop restricted to read-only intents that re-enter the deterministic inbound router and tool policy.
- Prevented LLM-routed write or confirmation intents from bypassing deterministic preview/confirm flows.
- Avoided reporting an LLM endpoint in `runtime_status` when LLM routing is disabled or no supported provider is configured.
- Made Feishu chat upgrade workers inherit the effective environment when launched without systemd-run, preserving deployed env-file settings.

## 1.2.122 - 2026-05-22

### Fixed
- Made Feishu chat upgrade workers inherit `OM_ENV_FILE` and `OM_RUNTIME_ROOT` through systemd-run without exposing secret values, so final upgrade receipts can use the deployed bot credentials.
- Made `config migrate-yaml` convert legacy `agent.*` settings into the canonical `assistant.*` config shape.

## 1.2.121 - 2026-05-22

### Changed
- Split `runtime_status_tool` into `agent_tool_runtime_status.py`, leaving `agent_tool_openclaw.py` focused on OpenClaw readiness.
- Updated agent tool handlers and runtime-status tests to use the neutral runtime-status module.

### Added
- Added an architecture guard to prevent `runtime_status_tool` from moving back into the OpenClaw module.

## 1.2.120 - 2026-05-22

### Changed
- Made `scripts/install.sh` resolve the latest published GitHub release by default while preserving explicit release-tag installs and avoiding floating `main`.
- Updated the quick-install documentation to use the one-line installer path, with fixed-version installs kept for production replay and rollback.

### Fixed
- Made re-running the installer for the already active release idempotent while still allowing optional server/dev dependencies to be added.

## 1.2.119 - 2026-05-22

### Fixed
- Made `runtime_status` normalize `v`-prefixed service-upgrade target versions before comparing them with the active release.
- Added service-upgrade failure details to runtime status summaries and Feishu replies, including target/current versions, reason, failed services, and remediation hints.

## 1.2.118 - 2026-05-22

### Added
- Added a bounded assistant agent loop and read-only tool policy layer for optional LLM and future LangGraph routing.
- Added assistant config diagnostics and architecture guard tests for the assistant/runtime split, read-only LLM intent surface, and Feishu WS config boundaries.

### Changed
- Split assistant control-plane settings into `config.assistant.json`, keeping `config.us.json` and `config.hk.json` focused on business runtime settings.
- Made Feishu inbound and Feishu WS load assistant behavior from `--assistant-config` while keeping business tools on `--config-path`.
- Retired live `agent.*` config in favor of `assistant.*`, with `assistant.mode` controlling deterministic, LLM router, and agent loop behavior.

### Fixed
- Rejected business runtime config files when passed as assistant config, preventing `accounts` / `symbols` / `portfolio` from entering the assistant control plane.
- Kept LLM translation restricted to read-only intents that re-enter the deterministic inbound router and renderer.
- Added signed pending-operation confirmation checks so write previews cannot be confirmed after payload or signing-key drift.

## 1.2.117 - 2026-05-22

### Fixed
- Made `runtime_status` auto-load the runtime `service.profile.json` after resolving the ledger runtime root, so Feishu status replies inspect production runtime paths instead of release-local fallback paths.
- Made service upgrade locks recover from stale PID files while preserving active upgrade locks.
- Kept failed upgrades for newer target versions classified as unrecovered runtime failures instead of historical failures.

## 1.2.116 - 2026-05-22

### Added
- Added an independent inbound upgrade worker so Feishu upgrade confirmations can survive `feishu-ws` service restarts and write final applied/failed results.
- Added final Feishu upgrade receipts from the worker after the upgrade completes.

### Changed
- Changed Feishu `确认升级` to acknowledge immediately and queue the upgrade instead of running it synchronously inside the WebSocket handler.
- Replaced raw pending-operation statuses in user-facing duplicate confirmation messages with readable progress text.

## 1.2.115 - 2026-05-22

### Added
- Added `agent.llm.provider: deepseek` support through DeepSeek's OpenAI-compatible Chat Completions API.
- Added a Chat Completions JSON-mode client for LLM intent translation, including DeepSeek endpoint diagnostics in `om agent llm-check`.

### Changed
- Documented DeepSeek LLM configuration with `DEEPSEEK_API_KEY`, `https://api.deepseek.com`, and `deepseek-v4-flash`.
- Kept OpenAI on the existing Responses API path while routing DeepSeek through `/chat/completions`.

## 1.2.114 - 2026-05-21

### Added
- Added a shared command catalog for inbound slash commands, operator help, and the optional LLM intent surface.
- Added `om agent commands` and `om agent llm-check` diagnostics for inspecting command routing and optional LLM readiness.

### Changed
- Enabled the one-shot AgentRuntime command facade by default for inbound handling, with `--no-agent-runtime` available for explicit fallback to the legacy parser path.
- Clarified that LangGraph remains deferred; the production path is the deterministic command facade plus an optional one-shot LLM translator.
- Removed legacy `om service upgrade-check`, `om service upgrade`, and `om service rollback` CLI aliases; use `om update check/apply/rollback` for release updates.
- Unified release tag parsing and upgrade target resolution behind a shared release target resolver with fetch-before-select diagnostics.

### Fixed
- Returned structured inbound configuration errors when the audit SQLite database is unwritable instead of surfacing a Python traceback.
- Kept inbound confirmation, income, and position receipts aligned with the canonical renderer behavior, including untruncated open-position output.

## 1.2.113 - 2026-05-21

### Added
- Added the optional `AgentRuntime` inbound facade with slash commands for status, health, positions, income, runs, logs, monitored symbols, pending previews, and typed confirm/cancel flows.
- Added an opt-in OpenAI Responses intent translator that can only produce bounded read-only `om-llm-intent-v1` intents before re-entering the existing inbound router, allowlist, audit, and renderer path.
- Added agent runtime config defaults, YAML passthrough, config validation, settings inspection support for `OM_LLM_API_KEY`, and coverage for Feishu WS agent runtime settings.

### Changed
- Updated inbound help and operator docs to describe the command facade, optional LLM translation, same-conversation context window, and Feishu WS runtime gating.

### Fixed
- Made one-shot `om inbound handle --agent-runtime` load the same runtime config settings as Feishu WS, keeping context-window and LLM settings consistent across local and remote inbound paths.

## 1.2.112 - 2026-05-21

### Added
- Added an inbound `立即升级` operation with preview, pending confirmation, cancellation, admin write gates, and service-upgrade execution through the existing release upgrade path.
- Added `OM_INBOUND_UPGRADE_WRITE_ENABLED` to settings inspection and doctor readiness checks for explicitly enabling inbound upgrade writes.

### Changed
- Updated inbound help, pending-operation summaries, and inbound control docs to include the `确认升级` / `取消升级` flow.

## 1.2.111 - 2026-05-21

### Added
- Added `om config init` to generate a starter `config.yaml` and build US/HK runtime config snapshots for first-run setup.
- Added YAML authoring metadata to rendered service profiles so `update apply` can rebuild runtime configs from `config.yaml`.

### Changed
- Made `om config build` and `om config explain` default to YAML authoring, with explicit `--source legacy` required for deprecated JSON overlay inputs.
- Marked `om setup init`, legacy JSON authoring, service rendering without `--config-yaml`, and runtime JSON `config set` writes with deprecation or boundary warnings.
- Updated operator and agent docs around `config.yaml` authoring, generated runtime snapshots, YAML-aware service updates, and first-run smoke checks.

### Fixed
- Rejected mixed YAML/runtime flags in `om config validate`, keeping authoring validation and generated runtime snapshot validation separate.

## 1.2.110 - 2026-05-21

### Fixed
- Made `runtime_status.latest_run` respect the requested US/HK market, preventing newer cross-market skipped runs from masking the current market runtime state.
- Stopped treating expected scheduler skips as missing-notification runtime warnings, so skip-only runs no longer produce false quality failures.
- Made AI Cofunder healthcheck snapshots load the service profile env file temporarily before checking online runtime settings.
- Included projection replay verification in AI Cofunder ledger quality, clearing the `trade_events` to `position_lots` evidence gap when replay passes.

## 1.2.109 - 2026-05-21

### Added
- Added `om support bundle` for generating a redacted JSON diagnostic bundle with setup, settings, config validation, runtime status, and optional healthcheck snapshots.

### Changed
- Refreshed quick-start, install, configuration, tool, deployment, and release docs around the current `config.yaml` authoring model, generated runtime config snapshots, global `om` / `om-agent` wrappers, and the remaining legacy auto-upgrade config rebuild boundary.

## 1.2.108 - 2026-05-21

### Changed
- Added installed global `om` / `om-agent` wrapper startup coverage to the release smoke gate, including repo-outside startup checks for `om --help`, `om setup check`, `om settings doctor`, and `om-agent spec`.

## 1.2.107 - 2026-05-21

### Changed
- Updated quick-start, install guide, and installer help examples to point at the fixed `v1.2.107` release instead of the broken `v1.2.105` global wrapper release.

## 1.2.106 - 2026-05-21

### Fixed
- Fixed installed `om` and `om-agent` entrypoints so global wrappers work from directories outside the release checkout by adding the release root to `PYTHONPATH` without changing the caller's current working directory.

## 1.2.105 - 2026-05-21

### Added
- Added installer-managed user-level `om` and `om-agent` wrappers, created in `$HOME/.local/bin` by default and pointed at the active `current` release.
- Added installer flags to skip CLI wrapper creation, choose a custom wrapper directory, or explicitly take over existing non-managed wrapper paths.

### Changed
- Updated install docs and quick-start commands to present `om` / `om-agent` as the normal installed entrypoints, while keeping `./om` / `./om-agent` as repo-local fallbacks.

## 1.2.104 - 2026-05-21

### Added
- Added a shared write contract for CLI and agent write paths, including standard `dry_run`, `write_applied`, `backup_path`, `audit_id`, and `rollback_hint` fields.
- Added Feishu app notification idempotency keys and send-attempt diagnostics for retries, ambiguous sends, and duplicate-risk reporting.

### Changed
- Unified write flag semantics: local writes use `--apply`, while high-risk trade, Feishu, and service writes require `--confirm` or non-interactive `--yes`.
- Made inbound `收益` and `持仓` default to all accounts when no account is provided, expanded income receipt details, and removed fixed truncation from income and position renderers.
- Updated rendered service commands to pass `--yes` for non-interactive trade intake and expired-position auto-close jobs.

## 1.2.103 - 2026-05-21

### Fixed
- Made agent and inbound monthly income reports use the shared exchange-rate loader and the runtime config's rate cache path, so CNY return summaries can be calculated when runtime rates are available or fetchable.

## 1.2.102 - 2026-05-21

### Added
- Added conversation-scoped inbound pending operation resolution, so bare replies such as `确认记录` / `取消记录` can safely resolve the current Feishu conversation when there is exactly one pending operation.
- Added inbound pending and audit diagnostics commands for inspecting pending previews and recent command audit rows.
- Added Feishu inbound audit and Feishu WS service profile diagnostics to `healthcheck` / `doctor`.

### Changed
- Made manual trade preview replies more readable and support in-conversation edits such as premium, contract count, expiry, strike, and close-price updates before confirmation.
- Made symbol operation confirmation follow the same conversation-scoped confirmation flow as manual trade records.

### Fixed
- Made pending operation confirmation an atomic claim before ledger/config writes, preventing duplicate confirmations from applying the same preview twice.
- Rejected decimal input for integer manual trade fields instead of silently truncating values such as contract counts.
- Avoided command-id collisions for local inbound requests without a remote message id.
- Restored option intake ledger opener compatibility and release-local ledger drift detection used by the write guard.

## 1.2.101 - 2026-05-21

### Added
- Added dependency-hash based shared virtualenv reuse for service upgrades, so unchanged release dependencies can skip repeated pip/uv installation.
- Added runtime prepare timing and intermediate `runtime_preparing` / `runtime_prepared` upgrade status writes for clearer upgrade progress diagnosis.

### Changed
- Build service upgrade virtualenvs in temporary cache paths and publish them atomically after successful dependency installation.
- Include Python, platform, installer mode, requirements, constraints, and server dependency inputs in the virtualenv cache fingerprint.

## 1.2.100 - 2026-05-20

### Added
- Added a YAML authoring surface for runtime config, backed by code-owned `DEFAULT_CONFIG` defaults and explicit US/HK market resolution.
- Added `./om config migrate-yaml` to preview or apply migration from layered JSON user config into ignored local `config.yaml`, with backup and post-write validation.

### Changed
- Documented the split between human-edited `config.yaml`, generated market runtime snapshots, env-backed write gates/secrets, and per-symbol strategy overrides.

## 1.2.99 - 2026-05-20

### Added
- Added `./om multiplier-cache seed` to dry-run or confirm runtime multiplier cache repairs without editing market config.
- Added setup diagnostics for uv availability and forced `OM_UPGRADE_INSTALLER=uv` readiness.

### Changed
- Made manual trade and broker trade multiplier resolution prefer the shared runtime cache inferred from runtime root or runtime config path before OpenD refresh.
- Made service upgrade coerce a release-entity repo root back to the active current symlink when the runtime service profile identifies it.

### Fixed
- Reconciled legacy service profiles that had an installed Feishu WS service outside the managed service list, so upgrades restart and check Feishu WS with trade-intake.
- Added sudo fallback for service drift systemd unit writes and permission-denied `systemctl` operations.
- Preserved selected runtime config hotfixes such as `inbound.feishu_ws.ack_reaction` before upgrade rebuilds overwrite generated configs.

## 1.2.98 - 2026-05-20

### Added
- Added remote runtime selection flags to `om ai-cofunder collect`, including run roots, explicit run ids, report/state roots, tail limits, and notification/freshness limits.

### Fixed
- Fixed AI Cofunder strategy evidence collection for service-profile runtime roots outside the repo checkout, so remote run candidate, reject-log, trace, and ranking evidence can be included in handoff bundles.
- Made runtime status select the latest scanned run for the requested US/HK market instead of crossing shared `output_runs` markets.

## 1.2.97 - 2026-05-20

### Changed
- Reworked multiplier resolution to use only payload fields, the shared `output_shared/state/multiplier_cache.json`, and OpenD refresh, retiring `intake.multiplier_by_symbol` and market default multiplier config fields.
- Enabled manual trade inbound drafts to refresh missing multipliers from OpenD and include clearer multiplier cache/failure diagnostics.

### Fixed
- Made Feishu WS send a visible reply when an allowlisted sender hits the inbound write-gate, while keeping unauthorized senders silent.
- Added settings doctor diagnostics for duplicate deprecated Feishu ACK env keys and manual trade write-gate readiness.

## 1.2.96 - 2026-05-20

### Added
- Added an upgrade cache boundary for service upgrades: release code is materialized from `_cache/git/options-monitor.git`, with `--cache-root` and `OM_UPGRADE_CACHE_ROOT` overrides.
- Added stable uv and pip download cache directories for release runtime preparation.

### Changed
- Changed confirmed upgrades to mirror/fetch once and archive target release tags instead of cloning a fresh working tree for every release.
- Kept release directories free of `.git` while allowing later upgrade checks and upgrades to resolve release tags and remote URLs from the upgrade git cache.
- Updated uv runtime preparation to use the host `python3` interpreter and to avoid installing uv during upgrades.

## 1.2.95 - 2026-05-20

### Changed
- Removed legacy `./om init runtime ...` and top-level `./om setup --market ...` compatibility entrypoints in favor of the current `./om setup init ...` command.
- Updated generated runtime config rebuild commands and onboarding docs to reference only the current setup/init flow.

## 1.2.94 - 2026-05-20

### Fixed
- Fixed service drift reconciliation to preserve profile-provided runtime roots and to skip empty service profiles instead of forcing default maintenance units into intentionally empty profiles.

## 1.2.93 - 2026-05-20

### Added
- Added active ledger store write guards for `trade-events`, `option-positions`, and manual option intake write paths, including `--runtime-root` support and structured ledger-store diagnostics.
- Added service drift diagnostics and reconciliation for rendered systemd/launchd profiles, with runtime status visibility for missing required maintenance units.

### Changed
- Hardened broker trade intake so Futu millisecond timestamps parse as Beijing time and broker trade events no longer write `trade_time_ms=0`.
- Verified post-write close projections before reporting intake success, added ledger/projection details to trade-intake receipts, and invalidated stale option-position context caches after applied closes.
- Included ledger store details in repair/replay/inspect/verify outputs so production operators can see which SQLite store a command actually used.
- Reconciled missing service units during confirmed upgrades before restarting long-running services.

## 1.2.92 - 2026-05-20

### Added
- Added a shared Linux/macOS platform profile for install/setup defaults, including service target, recommended runtime root, env-file path, prerequisite hints, and service notes.

### Changed
- Improved `./om setup check` onboarding output with platform profile diagnostics, optional Feishu long-connection server dependency visibility, recommended runtime/env paths, and platform-specific service render next steps.
- Improved `scripts/install.sh` with Linux/macOS prerequisite hints, Python/venv preflight checks, and platform-specific env-file guidance without writing secrets or enabling services.
- Expanded install/getting-started/deployment docs with separate Linux and macOS paths, Feishu `--with-server` guidance, and safer env-file initialization examples.

## 1.2.91 - 2026-05-20

### Changed
- Split Feishu inbound manual-trade recognition from trade draft normalization so the parser only identifies manual open/close commands while a dedicated draft builder handles Futu fill parsing, symbol canonicalization, multiplier resolution, and close-side conversion.
- Added auditable manual-trade draft diagnostics to inbound preview payloads, including raw/canonical symbol, multiplier source and attempts, fill parser source, fill time, side conversion, and missing fields.

## 1.2.90 - 2026-05-20

### Added
- Added `scripts/install.sh`, a pinned-release installer that creates a release directory, prepares `.venv`, installs dependencies, and updates the `current` symlink without writing config, secrets, services, timers, or runtime state.
- Added `./om setup check`, a read-only first-run diagnostic for install layout, dependencies, settings, runtime config, runtime root, option-position SQLite path, and service/timer presence.

### Changed
- Split installation, ordinary getting started, and Agent getting started docs into separate `docs/INSTALL.md`, `docs/GETTING_STARTED.md`, and `docs/AGENT_GETTING_STARTED.md` paths.

## 1.2.89 - 2026-05-20

### Added
- Added `./om settings inspect`, `./om settings explain`, and `./om settings doctor` to show redacted effective env-file settings, sources, deprecated env usage, Feishu Bot readiness, and write-gate state.
- Added automatic local env-file bootstrap for `./om` and `./om-agent`, with service rendering support for systemd `EnvironmentFile` and launchd `OM_ENV_FILE`.

### Changed
- Moved Feishu long-connection reaction, reply, and queue behavior into runtime config under `inbound.feishu_ws` instead of secret env vars.
- Hardened config validation against inline secret material, retired Feishu callback settings, and retired option-position Feishu sync/bootstrap settings.
- Clarified setup, deployment, inbound, and agent docs around env-file secrets, fixed option-position store paths, and Feishu long-connection configuration.

## 1.2.88 - 2026-05-20

### Added
- Added `./om service cleanup`, a dry-run-by-default release cleanup command that reports active, kept, and deletable releases, optional cache cleanup, and estimated freed space.
- Added `--cleanup-after-upgrade` for service upgrades so old releases can be cleaned only after a successful symlink switch and runtime config rebuild/validation.

### Changed
- Made confirmed service upgrades fail fast when `--repo-root` is not the current symlink path, preventing clones into the wrong release layout.
- Improved monthly income diagnostics so existing original-currency cash-secured values are not reported as missing when only CNY conversion rates are absent.
- Changed inbound income replies to show original-currency premium, cash-secured, and return-rate summaries when CNY conversion is unavailable.

## 1.2.87 - 2026-05-20

### Changed
- Expanded Feishu inbound read-only replies for status, healthcheck, config validation, position, recent-run, and log queries so bot responses show actionable summaries instead of generic completion messages.

## 1.2.86 - 2026-05-20

### Added
- Added project-level memory governance docs that define `memory/` as the LLM wiki, including authority order, ingest triggers, lint expectations, and audit logging.
- Added `memory/index.md` to organize existing decisions, patterns, and failures by module for future agent work.
- Added templates for durable memory decisions, patterns, and failures.

### Changed
- Documented the Memory / LLM Wiki workflow in `docs/AGENT_WIKI.md`, including manual ingest prompts and the rule that ordinary debug/session summaries must not be promoted automatically.

## 1.2.85 - 2026-05-20

### Added
- Added inbound manual trade recording with preview, sender-gated confirmation, pending-operation audit records, and readable Feishu responses for manual open/close ledger writes.
- Added canonical monitored-symbol calibration for config writes so inputs such as `700`, `HK.00700`, `腾讯`, `POP`, and lowercase US symbols resolve to stable `symbols[]` entries.
- Added the `./om symbols` CLI for monitored-symbol list/add/edit/remove operations with preview-by-default writes.
- Added inbound monitored-symbol operations with preview/confirm/cancel flow and the dedicated `OM_INBOUND_SYMBOL_WRITE_ENABLED` safety gate.

### Changed
- Routed `manage_symbols` writes through the same canonical symbol calibration contract.

### Removed
- Removed the old `./om watchlist` user entrypoint and watchlist mutation compatibility module; user-facing monitored-symbol operations now use `symbols`.

## 1.2.84 - 2026-05-20

### Fixed
- Distinguish remediated or historical service-upgrade failures in `runtime_status` so stale `upgrade_status.json` failures no longer force a current `runtime_failed` result.
- Downgrade remediated service-upgrade failures to AI Cofunder warnings while preserving unrecovered upgrade failures as runtime failures.

## 1.2.83 - 2026-05-20

### Fixed
- Restart all profile-managed long-running systemd services after service upgrades, including `options-monitor-trade-intake.service` and `options-monitor-feishu-ws.service`, using the configured restart command strategy.
- Record service-restart failures after a successful symlink/config switch as `upgraded_restart_failed` with `restart_failed_services` and manual remediation instead of failing the upgrade unit outright.

## 1.2.82 - 2026-05-20

### Changed
- Added structured monthly-income diagnostics for inbound `收益` queries so empty or incomplete return summaries explain matched events, lots, closed lots, premium rows, cash-secured availability, and missing fields.
- Changed inbound monthly-income rendering to show an explicit "暂无可计算收益" explanation instead of successful-looking rows with all return fields as `-`.

## 1.2.81 - 2026-05-19

### Changed
- Replaced the Feishu HTTPS callback inbound gateway with `./om inbound feishu-ws`, a Feishu long-connection client that reuses the existing allowlist, audit, idempotency, and read-only tool routing.
- Added optional Feishu message reaction acknowledgements for `feishu-ws` through `OM_FEISHU_ACK_REACTION`.
- Switched rendered services from `--include-feishu-gateway` to `--include-feishu-ws` and removed callback-only Feishu encrypt/token/host/port/path environment settings.

## 1.2.80 - 2026-05-19

### Added
- Added account-level `return_summary` to monthly income reports, including current cash-secured basis, CNY income totals, monthly and annualized return rates, and CLI/inbound summary rendering.

## 1.2.79 - 2026-05-19

### Changed
- Consolidated Feishu bot inbound/reply/send configuration on fixed `OM_FEISHU_BOT_*` environment variables and removed gateway CLI secret override flags.
- Moved Feishu notification route resolution into the application layer while keeping infrastructure Feishu bot code as an HTTP client only.
- Retired default ledger legacy bootstrap paths; legacy SQLite `trade_events`, `position_lots`, and `option_positions` migration now requires the explicit `option-positions store migrate-legacy` command.

### Fixed
- Applied Feishu event signature verification consistently to URL verification callbacks when signature checks are enabled.
- Added architecture guard tests for infrastructure layering, Feishu gateway secret flags, and Feishu bot custom env-name compatibility regressions.

## 1.2.78 - 2026-05-19

### Fixed
- Hardened service upgrade user overlay recovery by falling back from runtime config metadata to runtime overlays and older complete releases before rebuilding and validating runtime configs.
- Added post-switch runtime config rebuild/validation so upgrade success is tied to the current symlink freshness path used by tick services.

## 1.2.77 - 2026-05-19

### Added
- Added controlled inbound remote command handling with deterministic read-only routing, sender allowlist enforcement, message-id idempotency, and SQLite audit records.
- Added Feishu App event callback support through `./om inbound feishu-gateway`, including signature/token checks, encrypted payload handling, and Feishu App reply API responses.
- Added `./om service render --include-feishu-gateway` to generate a long-running Feishu gateway service and documented the Linux deployment/env configuration.

### Fixed
- Rebuild runtime configs during service upgrades after migrating `configs/user*.json` from the previous release, failing before symlink switch when required market user overlays are missing.

## 1.2.76 - 2026-05-19

### Fixed
- Restart trade-intake during systemd service upgrades through profile-driven `sudo -n systemctl` when the upgrade unit runs as a non-root deploy user, and record restart remediation on permission failures.

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
- Documented the replay input contract and evidence model.

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
