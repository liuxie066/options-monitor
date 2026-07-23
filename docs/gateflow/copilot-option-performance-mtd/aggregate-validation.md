# Gateflow Aggregate Validation

- Work unit: `copilot-option-performance-mtd`
- Gate: `aggregate-validation`
- Date: 2026-07-23
- Base: `origin/main@0db40d50`
- Head before validation artifacts: `b748bbea`
- Status: pass; pending aggregate DeepReview

## Full repository quality gates

```text
ruff check .
All checks passed.
```

```text
python3.12 -m compileall -q domain src scripts
passed.
```

```text
python3.12 -m pytest -q -p no:cacheprovider
3065 passed, 10 skipped, 6 warnings in 47.88s.
```

The six warnings are existing deprecation warnings for legacy notification renderers.

## Environment diagnosis

The first isolated-worktree full-suite run reported 19 `FileNotFoundError` failures because the
worktree did not contain `.venv/bin/python`; 3,046 tests passed in that run. A temporary symlink
to the original checkout's existing Python 3.12 virtual environment was added solely for CLI
subprocess tests and removed after validation.

With the CLI environment available, one remaining gate identified a stale generated dependency
graph. The repository generator refreshed `docs/DEPENDENCY_GRAPH.md`; it reports:

```text
production_modules=481
production_cycles=0
boundary_guard=PASS
```

The final full-suite run was clean.

## Focused financial and Copilot evidence

- S1: 175 focused tests passed.
- S2: 182 focused tests passed.
- S3: 101 focused tests passed.
- Exact MTD first-call payload normalizes to one period family.
- Explicit invalid/null inputs remain fail closed.
- Combined realized PnL reconciles to option plus assigned-stock components.
- Cash, premium, fees, assignment principal, sale proceeds, and evidence gaps render separately.
- Exact Feishu question/correction regressions require canonical MTD/all-account behavior.

## Additional checks

- `git diff --check`: passed.
- Generated dependency graph check: passed inside the final full suite.
- No production config/data, Feishu send, ledger write, release, or deployment occurred.
