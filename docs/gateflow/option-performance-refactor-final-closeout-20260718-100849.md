# Gateflow Final Closeout — Option Performance Refactor

## Gate and Status

- **Gate**: final closeout
- **Work unit**: `option-performance-refactor`
- **Closeout time**: 2026-07-18 10:08:49 CST
- **Branch**: `excited-rhino`
- **Accepted PR review head**: `1dd57e3f17a6e4f2ef97345e34157786860e1daa`
- **Draft PR**: https://github.com/liuxie066/options-monitor/pull/71
- **PR state at closeout**: open, draft, `MERGEABLE`, merge state `CLEAN`
- **Completion status**: final closeout pass

## What Changed

### Reporting period contract

Option Performance v1 now owns explicit operator-date periods under the `Asia/Shanghai` reporting boundary:

- MTD;
- YTD;
- specified natural month (`YYYY-MM`);
- specified natural year (`YYYY`);
- internal explicit date ranges where required by service/bridge boundaries.

The same period semantics flow through domain models, service/materialization, Agent tool, CLI, Assistant/Feishu command handling, tests, and documentation.

### Accounting semantics

The new model separates concepts that the legacy monthly report mixed together:

- activity: premium collected/paid and contract quantities;
- cash: option trade cash, assignment-related stock cash, and complete net cash movement;
- PnL: realized option PnL, assigned-stock realized/unrealized PnL, opening/end valuation, and period total;
- capital: explicit capital metrics without reusing cash or PnL as a proxy;
- evidence/quality: observed, partial, not observed, missing fee/FX/valuation/source evidence, and proven-zero semantics.

Assignment/exercise is supported through the canonical trade-event projection and assigned-stock lifecycle. Missing settlement evidence, invalid sales, stale marks, and FX gaps fail closed instead of becoming silent zero or observed profit.

### Public and integration surfaces

- Added the primary `option_performance_report` Agent/CLI/Assistant path.
- Added portfolio PnL and cash bridges using the correctly named option metrics.
- Required exact account ownership, exact `Asia/Shanghai` cutoff alignment, observed report-level quality, CNY evidence, actual fee coverage, and compatible period facts before bridge amounts are usable.
- Kept the old monthly report and capital bridge only as documented deprecated rollback boundaries.
- Added old/new reconciliation, deterministic replay/coverage checks, and an exact legacy-reference allowlist.
- Integrated current main v1.2.409 and updated the Feishu end-to-end assertion to the new option-performance tool/payload while preserving the official mixed-case `Typing` Reaction behavior.

## Verification

### Local repository validation

Resolved current-main tree:

```text
2590 passed, 10 skipped
```

The only locally ignored files are four known environment-dependent entrypoint suites that require a worktree `.venv`; no functional test was deselected after the PR integration fix.

Quality gates:

```text
python3 -m ruff check .
All checks passed!

python3 scripts/generate_dependency_graph.py --check
[OK] dependency graph current; production_modules=465 cycles=0

git diff --check
passed

git diff --cached --check
passed
```

Legacy-reference inventory:

```text
status=pass
unowned=[]
stale_allowlist=[]
matches=25
```

### GitHub validation

All final checks on accepted PR-review head `1dd57e3f` passed:

- Analyze (actions): pass;
- Analyze (python): pass;
- CodeQL: pass;
- agent-plugin: pass;
- guardrails: pass.

PR mergeability is `MERGEABLE/CLEAN` against `main@66929010`.

## Docs Updated

- authoritative design: `docs/OPTION_PERFORMANCE_DESIGN.md`;
- migration/rollback/removal guide: `docs/migrations/OPTION_PERFORMANCE_V1_MIGRATION.md`;
- public README, Agent Wiki, integration guide, capability map, and assigned-stock design;
- regenerated dependency graph artifacts;
- Gateflow plan, slice implementation/fix, code review, aggregate deepreview, PR review/fix/re-review, and this closeout artifact.

No release version or changelog entry was authored by this work unit.

## Finding Status

Aggregate deepreview:

- ADR-01 exact per-account bridge ownership: fixed;
- ADR-02 exact reporting timezone: fixed;
- ADR-03 assigned-stock review debt affects top-level quality: fixed;
- ADR-04 report-level partial quality blocks bridge use: fixed.

PR deepreview:

- PR-01 current-main merge conflict: fixed;
- PR-02 stale Feishu end-to-end legacy income assertion: fixed.

Final totals:

```text
6 fixed
0 unresolved
0 partially fixed
0 stale evidence
0 unclassified residual risk
```

## Remaining Classified Risks and Owners

| Risk | Owner / destination |
|---|---|
| External portfolio-management runtime endpoints may not yet conform to the strict facts contract | portfolio-management service owner; OM fails closed and exposes explicit reasons |
| Historical assigned-stock lifecycle rows may require repair when stricter quality warnings surface them | operations/data-repair follow-up |
| Deprecated legacy adapters remain during the migration window | later versioned removal work unit after the documented removal gate |
| Separate FX-attribution decomposition is not represented as its own PnL component | accepted v1 design limitation; future work only if a concrete reporting requirement appears |
| GitHub raw diff endpoint rejects this PR above 20,000 lines | review evidence uses local git range identity plus committed artifacts; not a runtime risk |

## Workspace Preservation

Unrelated local release/Feishu changes were backed up before the main integration and restored byte-for-byte using a SHA-256 manifest. The remaining visible dirty paths are intentionally not staged or included in this work unit:

```text
CHANGELOG.md
VERSION
src/application/inbound/feishu_ws.py
tests/test_inbound_feishu_ws.py
```

Other previously dirty/untracked Feishu paths became identical to files now inherited from current main; their bytes were also verified against the pre-merge manifest.

## Issue Link Status

Not applicable: this work unit was initiated as a refactor request and has no linked GitHub issue. No issue closeout comment is required.

## Next Entry Point

The technical work unit has reached `final closeout pass`. The next human-controlled action is to review draft PR #71 and, when desired, separately authorize marking it ready and/or merging it. Gateflow did not approve, mark ready, merge, request reviewers, comment externally, or delete the branch.
