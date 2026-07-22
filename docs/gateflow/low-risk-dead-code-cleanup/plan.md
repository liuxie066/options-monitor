# Gateflow Plan — Low-Risk Dead Code Cleanup

## Gate

- Work unit: `low-risk-dead-code-cleanup`
- Current gate: `plan`
- Next entry point: `plan review`
- Base: `origin/main@64ccd3e0` (`v1.4.9`)
- Branch: `refactor/low-risk-dead-code-cleanup`

## Goal / Motivation / Success Signal

Remove only production functions that are statically proven to have no calls, callable references, imports, string registrations, test references, public package exports, documented compatibility role, or framework callback role.

Success requires:

- the approved definitions no longer exist;
- runtime behavior, schemas, CLI commands, agent tools, storage behavior, and external protocols remain unchanged;
- Ruff, focused regressions, smoke, dependency-graph validation, and the full pytest suite pass;
- the original checkout and its unrelated untracked files remain untouched.

## First-Principles Judgment and Direct Evidence

The repository contains 480 production Python files and 266 test files. A Python 3.12 AST scan over production and tests counted definitions, direct calls, callable references, original import names, import aliases, and identifier strings. Each function in scope has exactly one definition and zero calls/references/imports/registrations. Exact whole-repository text search found no non-definition occurrences.

The cleanup removes definitions only. It does not redirect call paths, replace behavior, add abstractions, or change ownership boundaries.

## Non-Goals / Scope Boundary

- Do not remove public or compatibility-sensitive candidates from the medium-risk list.
- Do not remove `_scheduled_run_targets`; it is an explicitly documented compatibility helper.
- Do not remove `logging.Handler.emit`; it is a framework callback.
- Do not remove unused class methods in this work unit; method/interface conformance is harder to prove statically.
- Do not change runtime behavior, schemas, configuration, notifications, storage, Feishu operations, or CLI/Agent contracts.
- Do not modify the original checkout or its unrelated untracked plans/reviews.

## Contract / Schema / State-Machine / Public-Interface Changes

None. No function in scope is exported through package `__all__`, the human CLI, the Agent Tool registry, documented entry points, or compatibility declarations.

## Implementation Decisions

- Delete complete unused function definitions and their now-unused imports only.
- Do not rewrite adjacent code or reformat unrelated sections.
- Re-run the same AST proof after deletion to verify the target definitions are absent and no unexpected candidate classification changed.
- If deletion makes an import unused, remove only that import and prove it with an explicit `F401` Ruff selection rather than relying on the repository's narrower default lint selection.

## Slice 1 — Private Helpers

Objective: remove 16 private top-level helpers with zero repository references.

Allowed functions/files:

- `src/application/config_validator.py`: `_validate_optional_non_negative_number_list`
- `src/application/daily_decision_brief_renderer.py`: `_changed_position_symbols`
- `src/application/candidate_reject_summary.py`: `_format_rule_counts`, `_format_function_counts`, `_format_samples`
- `src/application/yield_enhancement_config.py`: `_normalize_sell_put_strategy`
- `src/application/service_deploy.py`: `_json_arg`
- `src/application/service_upgrade.py`: `_restart_command_prefix`
- `src/application/close_advice_runner.py`: `_strike_key`
- `src/application/assistant/renderer.py`: `_analysis_cell`
- `src/application/ledger/preflight.py`: `_list_position_lots`
- `src/application/agent_tools/analysis.py`: `_nested_value`
- `src/application/agent_tools/candidate_filter_impl.py`: `_trace_paths`
- `src/application/positions/workflows.py`: `_build_manual_assigned_stock_sale_event`
- `src/application/multi_tick/notify_format.py`: `_compact_reject_lines`
- `src/application/channels/status.py`: `_service_present`

Expected outcome: definitions disappear without changing any caller because none exists.

## Slice 2 — Redundant Internal Facades

Objective: remove 12 public-named but non-exported internal functions with zero repository references.

Allowed functions/files:

- `domain/storage/repositories/report_repo.py`: `write_state_json`, `write_state_text`
- `src/application/symbol_mutations.py`: `canonical_symbol_for_config_write`, `calibrate_symbol_for_config`
- `src/application/opend_fetch_config.py`: `resolve_option_chain_fetch_config`
- `src/application/assistant/capability_catalog.py`: `capability_specs`
- `src/application/assistant/tool_bindings.py`: `primary_intent_name_for_tool`, `tool_name_for_intent`, `symbol_market_config_tool_names`
- `src/application/agent_tools/symbols_impl.py`: `require_float`
- `src/application/multi_tick/misc.py`: `append_json_list`
- `src/application/multi_tick/notify_format.py`: `is_high_priority_notification`

Expected outcome: redundant facades disappear while the existing owning implementations remain unchanged.

## Slice 3 — Unused Infrastructure Utilities

Objective: remove three non-exported utilities with zero repository references.

Allowed functions/files:

- `src/infrastructure/io_utils.py`: `bj_now`, `parse_last_json`, `copy_if_exists`

Expected outcome: dead utilities disappear; remaining I/O helpers and imports continue to pass Ruff and tests.

## Tests / Validation

- AST target scan: every approved function is absent after implementation.
- `python3.12 -m ruff check --select E9,F821,F401` on all touched Python files; expected result: zero syntax, undefined-name, or unused-import findings.
- `python3.12 scripts/generate_dependency_graph.py --check`.
- `python3.12 -m pytest -q -p no:cacheprovider tests/test_daily_decision_brief_renderer.py tests/test_candidate_reject_summary.py tests/test_service_deploy.py tests/test_close_advice_runner.py tests/test_candidate_filter_trace.py tests/test_positions_workflows_manual_close.py tests/test_multi_tick_notify_format.py tests/test_symbol_mutations.py`; expected result: all focused regressions pass without changed assertions.
- `python3.12 -m pytest -q -p no:cacheprovider tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py`; expected result: CLI/Agent Tool exposure remains unchanged.
- `python3.12 tests/run_smoke.py`.
- `python3.12 -m pytest -q -p no:cacheprovider`.
- `git diff --check`.

## Docs Decision

No product documentation changes are needed because no public behavior changes. Gateflow plan/review/implementation/closeout artifacts document the cleanup evidence and decisions.

## Risks / Open Questions

- Static analysis cannot observe unknown out-of-repository imports. This repository's documented public entry points are `./om` and `./om-agent`, and none of the scoped functions is exposed there. Residual external-import risk is classified as low.
- No blocking open questions.

## Why This Is Not Over-Designed

The change deletes code only. It adds no replacement layer, compatibility shim, configuration, registry, abstraction, or new runtime path. The three slices exist solely to keep review and validation evidence attributable by ownership boundary.

## Completion Report Format

- removed functions and touched files;
- validation commands and results;
- review finding status;
- residual risk classification;
- branch/commit/PR state and next entry point.
