# Gateflow Aggregate Fix Artifact

- Work unit: `ai-decision-advice-drift-remediation`
- Initial aggregate review: `docs/reviews/code-review-20260810-015804.md`
- Status: fixes complete; aggregate re-review passed

## Finding decisions and fixes

### DR-AGG-01 — accepted — fixed

`ai_decision_advice.contexts` now imports
`validate_combo_group_membership` through `src.application.ledger.api`, the
existing public ledger facade. The non-ledger import guard and all context tests
pass, and the dependency graph reports no production cycle or layer violation.

### DR-AGG-02 — accepted — fixed

The privacy regression now removes the allowed random anonymous `account_ref`
field from the captured structured payload and then checks for the complete
JSON account value `"lx"`. It no longer treats an incidental `lx` substring in
cryptographic randomness as a real account leak, while retaining checks against
NAV/totals and position quantities.

### DR-AGG-03 — accepted — fixed

The official dependency graph generator refreshed `docs/DEPENDENCY_GRAPH.md`
and `docs/dependency_graph.mmd` after the public-API import repair. The generated
state reports `production_modules=596`, `cycles=0`, and the check mode passes.

## Validation after fixes

- AI Decision Advice aggregate suite: `211 passed`.
- PM/prepared option/Tick barrier/Daily Brief/service aggregate suite:
  `462 passed`.
- Full sandbox-compatible suite: `4915 passed, 10 skipped, 1 deselected`.
- The one deselected loopback HTTP test passed separately in a narrowly
  permitted local-bind environment: `1 passed`.
- Complete effective result: `4916 passed, 10 skipped`.
- US and HK example YAML config validation: passed.
- Dependency graph check and generator tests: passed.
- Non-ledger ledger-API boundary guard: passed.
- Ruff, Python compilation and `git diff --check`: passed.

No live model, search, PM, OpenD, notification, release or deployment action
was performed.
