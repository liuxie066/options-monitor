# Gateflow Scope Amendment — Dependency Graph

- Work unit: `sell-put-top1-w1a`
- Trigger: implementation validation
- Accepted plan commit: `ea03818d`
- Decision: allow exactly `docs/DEPENDENCY_GRAPH.md` and `docs/dependency_graph.mmd` as generated W1A artifacts

## Evidence

- `scripts/generate_dependency_graph.py --check` passes on the clean accepted-plan commit.
- Overlaying only the W1A production and test files makes both generated graph files stale.
- The graph was generated in a temporary tree containing the accepted-plan commit plus only W1A files.
- Existing unrelated fee/assignment tests add two separate test-to-domain edges; those edges are deliberately excluded from the W1A generated artifacts and remain owned by their existing dirty work unit.

## Boundary

This amendment does not add production behavior, dependencies, modules, or scope. It only permits the repository-owned deterministic dependency graph outputs already required by the accepted validation plan. No other file is authorized.
