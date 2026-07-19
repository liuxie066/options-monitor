# Fix — trade-intake-process-isolated-auth-preflight / slice 1

- Gate: code review fix / re-review
- Decision: pass

## CR-1 — generated dependency graph stale

- Status: 已修复
- Fix: regenerated `docs/DEPENDENCY_GRAPH.md` after adding `process_supervisor.py`.
- Validation: `python3.12 scripts/generate_dependency_graph.py --check` reports 469 production modules and zero cycles.

## Additional correction

Corrected the plan validation command from the nonexistent `scripts/check_dependency_graph.py` to the repository-owned `scripts/generate_dependency_graph.py --check`.

## Residual risks

No new residual risk. Production Futu proof remains assigned to the approved release canary.
