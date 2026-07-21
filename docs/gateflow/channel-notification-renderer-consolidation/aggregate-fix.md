# Gateflow Aggregate DeepReview Fix — Channel Notification Renderer Consolidation

## Gate

- Work unit: `channel-notification-renderer-consolidation`
- Gate: `fix`
- Initial review: `docs/reviews/code-review-20260721-213128.md`
- Status: `fix complete; pending re-review`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/aggregate-fix.md`

## Finding Decisions and Fixes

### ADR-01 — accepted — fixed

Regenerated the existing architecture artifacts with `python3.12 scripts/generate_dependency_graph.py`:

- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`

The generated evidence now reflects `478` production modules, the current import counts, and `0` production cycles. No generator, dependency policy, module boundary, or production behavior was changed.

Final status: `已修复`.

### ADR-02 — accepted — fixed

Updated only `tests/test_domain_engine_batch4.py` so the existing source-level architecture guard matches the accepted scheduled notification authority:

- positively requires `assemble_daily_decision_briefs`;
- positively requires `prepare_daily_decision_brief`;
- positively requires `render_daily_brief_lifecycle`;
- positively requires `build_per_account_delivery_batch` and `decide_notification_delivery`;
- negatively rejects the removed Compact/Legacy engine entrypoint, candidate filter/rank injection, full/compact account message builders, and `prepare_multi_account_notification` from `tick_notification_flow.py`.

The scheduler-context and OpenD guard owner assertions remain unchanged. No production code was altered for this finding.

Final status: `已修复`.

## Validation

- Direct three-test reproduction after fix: `3 passed`.
- Dependency graph + complete Batch-4 architecture tests: `8 passed`.
- Dependency graph `--check`: passed; `478` production modules, `0` cycles.
- Four plan aggregate matrices: `154 passed`; `56 passed`; `148 passed` with 5 expected deprecation warnings; `96 passed` with 4 expected deprecation warnings.
- Full repository with the known baseline Legacy assertion deselected: `2931 passed, 10 skipped, 1 deselected`, 5 expected deprecation warnings.
- US/HK example config validation: passed.
- US/HK example config build `--dry-run`: passed; `write_applied=false`.
- Ruff on all changed Python files relative to the implementation base: passed.
- `python3.12 -m compileall -q domain src tests`: passed.
- `git diff --check`: passed.

A first Ruff invocation used a newline-delimited zsh scalar as one filename and failed with `E902 File name too long`. Re-running the identical file set via NUL-delimited `xargs -0` passed; this was command construction, not a code failure.

## Docs Decision

The generated dependency documents were refreshed. No further public/operator documentation changed because ADR-02 corrects test evidence only and the accepted Daily Brief/System/Receipt contracts are already documented in `docs/AGENT_WIKI.md`.

## Residual Risks / Classification

| Risk | Classification / owner |
|---|---|
| Known baseline close-advice Legacy assertion | covered by later approved but hard-paused Phase C/Slice 6 |
| Real Feishu/WeChat provider/client rendering | assigned to separately authorized compatibility release/canary gate |
| Phase C Legacy physical deletion and strict old-key rejection | requires explicit CEO decision after compatibility-release evidence |

The independent-worktree `.venv` issue is closed: a temporary untracked symlink to the main workspace environment enabled the complete run and will be removed before commit. No residual risk is unclassified.

## Completion Status / Next Entry Point

- Fix gate: complete.
- Next entry point: `re-review`.
