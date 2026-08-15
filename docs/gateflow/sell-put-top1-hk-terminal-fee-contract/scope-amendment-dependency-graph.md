# Gateflow Scope Amendment — Dependency Graph Outputs

- Gate: `implementation scope amendment`
- Work unit: `sell-put-top1-hk-terminal-fee-contract`
- Accepted plan commit: `8b879390`
- Artifact path: `docs/gateflow/sell-put-top1-hk-terminal-fee-contract/scope-amendment-dependency-graph.md`

## Evidence

- `scripts/generate_dependency_graph.py --check` passes on a clean detached `origin/main@8528de6b`: `production_modules=577`, `cycles=0`.
- The same check reports `docs/DEPENDENCY_GRAPH.md` stale with this work unit's code/tests applied.
- The implementation adds no new production module or forbidden dependency; the generated edge inventory still must reflect the changed import/test graph.

## Added allowed files

- `docs/DEPENDENCY_GRAPH.md`
- `docs/dependency_graph.mmd`

Both files must be produced only by the repository generator. No hand editing or other scope expansion is authorized.

## Exit signal

`scripts/generate_dependency_graph.py --check` passes and reports zero production cycles/boundary violations after regeneration.
