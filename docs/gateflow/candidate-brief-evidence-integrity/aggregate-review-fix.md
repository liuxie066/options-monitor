# Gateflow Fix Artifact — Aggregate DeepReview

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `fix`
- Review artifact: `docs/reviews/code-review-20260812-090935.md`
- Re-review artifact: `docs/reviews/code-review-20260812-091424.md`
- Status: fix accepted by aggregate re-review
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/aggregate-review-fix.md`

## Finding decision and fix

### CR-AGG-01 — accepted — fixed

The first Slice 2 implementation inferred raw-candidate existence from the already budget-truncated user-view rows.
Because candidate rendering uses a shared total budget across families, a later family could have canonical candidates
but no selected rows. Its unavailable AI copy would then falsely claim there was no displayable raw ranking while the
same section reported omitted candidates.

The fix keeps the public Brief schema unchanged and preserves the authority boundary:

- `build_daily_brief_user_view()` derives a private per-family boolean map from the canonical mapping-shaped
  `brief["candidates"]` before applying renderer budgets;
- the private map is carried only in the transient allowlisted user view;
- `_ai_advice_lines_for_family()` uses this canonical presence fact when available and retains mapping/flat-view
  fallback logic for internal compatibility;
- candidate selection, ranking, capacity, actions, limits, and persisted payloads are unchanged.

Final status: `已修复`.

## Regression coverage

A delta-render regression creates 41 material Sell Put rows, exhausting the shared render budget before Covered Call.
The canonical Covered Call candidate is omitted and the section reports one omitted row, but unavailable AI advice now
correctly retains the candidate-present raw-ranking fallback. The existing empty-family regression still proves the
no-ranking copy.

## Validation

- Core aggregate suite after fix: `156 passed`.
- Related AI Advice, Daily Brief, CC+LP, and Combo Yield suite: `387 passed`.
- Brief-focused subset during the fix loop: `117 passed`.
- `python -m compileall -q domain src tests`: passed.
- Ruff on all changed source/tests: `All checks passed!`.
- `git diff --check`: passed.

## Docs decision

No public docs change. The new field is private transient renderer context, not a persisted/public Brief schema field.

## Residual risks

- Runtime replay remains `assigned to separately authorized release/upgrade verification`.
- Manual symbol-subset propagation and future definitive reason taxonomy remain `assigned to later work unit`.

## Completion status

CR-AGG-01 is fixed. The aggregate re-review concluded `pass`. Current gate / next entry point:
`accepted aggregate review commit`.
