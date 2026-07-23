# Gateflow Slice 1 Deepreview Fix

- Work unit: `copilot-option-performance-mtd`
- Slice: `S1`
- Review: `docs/reviews/code-review-20260723-165433.md`
- Status: finding accepted and fixed; re-review passed

## S1-DR-01 — accepted — fixed

The initial implementation flattened model arguments and fixed scene scope before the
option-performance normalizer. An explicit model `period=mtd` could therefore delete a month
that the UI had supplied as authoritative scope.

The payload boundary now preserves source ordering:

1. static/model arguments are normalized;
2. safe defaults are applied;
3. fixed scene fields are applied last.

Consequences:

- model-added irrelevant period fields can still be pruned for the online MTD failure;
- fixed `config_key`, `symbol`, and `month` cannot be removed or overridden;
- fixed month plus model MTD remains an explicit conflict for canonical fail-closed validation;
- fixed month plus model month succeeds with the fixed value.

Regression coverage includes both direct payload construction and the real
`run_contract -> run_engine -> build_tool_payload` call path.

## Re-review

`docs/reviews/code-review-20260723-165944.md` found no remaining material issue.
