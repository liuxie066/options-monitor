# Gateflow Implementation Artifact — option-performance-refactor S7

## Gate

- Work unit: `option-performance-refactor`
- Slice: `S7 — New Agent Tool, CLI, Evidence Import/Capture and Legacy Adapter`
- Gate: implementation
- Status: implementation complete; pending code review
- Artifact: `docs/gateflow/option-performance-refactor-s7-implementation-20260717-233601.md`

## Scope

Implemented the approved S7 public-surface cutover:

- registered primary read-only Agent tool `option_performance_report` with exact period/scope/default fields;
- added shared request normalization and report application path used by Agent and CLI;
- added `./om option-performance report` for MTD/YTD/month/year/range;
- added explicit v1 evidence import and current capture, dry-run by default with mutually exclusive `--dry-run/--apply`;
- exposed a legal ledger application boundary for the evidence repository sharing the ledger SQLite file;
- converted `monthly_income_report` into a deprecated compatibility adapter over the new report;
- added explicit close-advice gross/fee/net fields while preserving `realized_if_close` as the deprecated net alias;
- added row canonical ordering, 1000-row cap, aggregate-account semantics, and truncation diagnostics.

## Changed Files

Production:

- `domain/domain/close_advice.py`
- `src/application/close_advice_runner.py`
- `src/application/agent_tools/materialization_impl.py`
- `src/application/agent_tools/positions.py`
- `src/application/ledger/read_model.py`
- `src/application/ledger/queries.py`
- `src/application/ledger/api.py`
- `src/interfaces/cli/main.py`
- `src/interfaces/cli/option_performance.py` (new)

Tests:

- `tests/test_option_performance_cli.py` (new)
- `tests/test_option_performance_agent_tool.py` (new)
- `tests/test_agent_plugin_contract.py`
- `tests/test_agent_plugin_smoke.py`
- `tests/test_close_advice_domain.py`
- `tests/test_close_advice_runner.py`

## Decisions

1. `account=None` and `broker=None` remain true aggregate filters; no portfolio-default or first-account fallback is applied.
2. Period validation runs before runtime config, ledger, or evidence loading.
3. Historical/full-past reports still pass the user refresh preference to the service, but the service owns and enforces `skipped_historical` without invoking live adapters.
4. Public report schema renames the core `assigned_stock` section to `assignment_lifecycle` and leaves the internal core contract unchanged.
5. Evidence writes remain CLI-only in v1; report generation never imports captured evidence.
6. The compatibility adapter maps legacy gross option cash/activity/realized fields from the new monthly breakdown and sets unsupported rate/capital fields to `null` with explicit deprecation and semantic warnings.
7. `realized_if_close` remains the compatibility alias for net close PnL after the runner applies estimated close fees.

## Validation

Passed:

```text
python3 -m pytest \
  tests/test_option_performance_cli.py \
  tests/test_option_performance_agent_tool.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  tests/test_option_positions_cli.py \
  tests/test_close_advice_contract.py \
  tests/test_close_advice_domain.py \
  tests/test_close_advice_reallocation_shadow.py \
  tests/test_close_advice_runner.py \
  tests/test_notification_compact.py -q

233 passed
```

Passed focused Ruff checks on every S7 production/test file and `git diff --check`.

## Docs Decision

- Public migration/runbook documentation is intentionally deferred to approved Slice S8/S10.
- Agent manifest output contract is updated in code and contract tests in S7.

## Residual Risks / Uncovered Areas

- S8 must migrate analysis/assistant/Copilot consumers from legacy field names and remove direct semantic dependence on `monthly_income_report` (`covered by later approved slice`).
- S9 must migrate portfolio bridge consumers and split PnL/cash equations (`covered by later approved slice`).
- S10 must add full reconciliation, rollback/deprecation documentation, full-suite validation, and the repository-wide legacy-consumer allowlist check (`covered by later approved slice`).
- Evidence capture is covered through CLI/shared-boundary tests and the pre-existing collector/repository suites; S10 full validation remains responsible for cross-slice regression coverage (`covered by later approved slice`).

## Completion Status

Implementation gate complete. Next Gateflow entry point: S7 code review using `deepreview`.
