# Gateflow Aggregate Deepreview Fix: Daily Brief Close-Position Details

## Gate

- Work unit: `daily-brief-close-details`
- Gate: aggregate deepreview fix
- Source finding: `DR-001`
- Controller decision: `accepted`
- Completion status: complete; pending re-review

## Scope

The fix is limited to the generated dependency graph documentation. No application logic, tests, configuration, runtime state, notification behavior, or production surface changed in this gate.

## Fix Applied

- Regenerated `docs/DEPENDENCY_GRAPH.md` with `python3 scripts/generate_dependency_graph.py`.
- The branch adds two test-to-application import edges, so the generated totals move from `4192` to `4194`, and `tests -> application` moves from `1572` to `1574`.
- Corrected the pre-fix aggregate review artifact so its evidence records the direction of the generated delta accurately.

## Validation

- `python3 scripts/generate_dependency_graph.py --check`: pass (`production_modules=476`, `cycles=0`).
- `git diff --check`: pass.
- `./.venv/bin/python -m pytest tests/test_daily_decision_brief_renderer.py tests/test_daily_decision_brief_service.py tests/test_dependency_graph_generator.py -q`: `39 passed`.
- Full suite after regeneration: `1 failed, 2858 passed, 10 skipped in 42.58s`.
  - The only failure is `tests/test_daily_decision_brief_agent_tool.py::test_agent_tool_is_pure_read_and_returns_structured_contract`.
  - Its fixture expires at `2026-07-20T20:00:00+00:00`; on July 21, 2026 the runtime correctly returns `planning_only`, while the assertion expects `live_actionable`.
  - This date-sensitive baseline failure is outside this work unit and is assigned to a separate maintenance work unit.

## Finding Status

- `DR-001`: `已修复` — generated documentation is current and its repository check passes.

## Docs Decision

- `docs/DEPENDENCY_GRAPH.md` is updated because repository policy checks the generated graph.
- No additional user documentation change is required beyond the already committed `docs/AGENT_WIKI.md` contract note.

## Residual Risks

- Expired date fixture: assigned to a separate maintenance work unit.
- Production notification canary: assigned to a later operator-authorized release/deployment work unit.
- No unclassified residual risk remains in this aggregate review loop.

## Artifact

`docs/gateflow/daily-brief-close-details-deepreview-fix-20260721.md`
