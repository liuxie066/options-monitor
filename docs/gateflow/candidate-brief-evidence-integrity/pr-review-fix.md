# Gateflow Fix Artifact — PR DeepReview

- Work unit: `candidate-brief-evidence-integrity`
- Gate: `PR review -> fix`
- Draft PR: `https://github.com/liuxie066/options-monitor/pull/149`
- Initial review: `docs/reviews/code-review-20260812-093929.md`
- Latest integrated base: `github/main@b85607be`
- Artifact path: `docs/gateflow/candidate-brief-evidence-integrity/pr-review-fix.md`
- Status: fixes implemented; pending PR re-review

## Latest-main integration

The Draft PR was opened from the accepted aggregate head `5b33bb27`. GitHub then reported a merge conflict because
`main` had advanced through the compact fixed-report commit `c031bf7b` and release commit `b85607be` (`1.13.13`).
Those upstream commits are incorporated unchanged as the new base context; this work unit did not create a release,
tag, deployment, or runtime mutation.

The sole textual conflict was a duplicate `fetched` success regression. The resolution retains exactly one fetched
test plus all four branch-owned CC+LP configuration regressions. Auto-merged compact renderer/service behavior is
then reviewed together with this work unit rather than taking either side wholesale.

## Finding decisions and fixes

### CR-PR-01 — accepted — fixed

`_fixed_report_error_reminders()` now distinguishes generic `partial_data` from recognized sealed hard-evidence
causes. A strategy gap whose stable `reason_code` is `term_matched_rv_unavailable` renders the existing specific
Chinese RV explanation. Unknown/generic partial gaps remain suppressed, preserving the compact-report noise policy.

The new fixed-card regression includes both kinds of gaps and proves that exactly the RV reminder is shown.

Final status: `已修复`.

### CR-PR-02 — accepted — fixed

Fixed reports retain the Sell Put and Covered Call modules when Advice is unavailable. The one-line global notice is
driven by the canonical pre-budget per-family presence map. Per-family unavailable copy is compacted as follows:

- canonical candidates present: no repeated boilerplate; the actual strategy ranking follows;
- no canonical candidates: `本轮无可展示的策略排序。`;
- legal zero-candidate Advice: existing compact zero-candidate wording retains priority.

This preserves latest-main compact rendering while keeping per-family candidate facts truthful, including when the
shared display budget omits a family's rows.

Final status: `已修复`.

### CR-PR-03 — accepted — fixed

The calculation-decision projection now applies the definitive economic-reason allowlist only after an explicit
`opening_contract_status == "ready"`. Empty status maps to canonical `input_invalid`; non-empty non-ready status keeps
the Candidate Engine reason. A regression proves an empty-status `net_premium_non_positive` remains unresolved and
cannot seal as a clean policy rejection.

Final status: `已修复`.

## Documentation decision

The latest-main compact-card contract in `docs/AI_DECISION_ADVICE_DESIGN.md` and
`docs/OPTION_NOTIFICATION_EXPERIENCE_PRD.md` is refined to state both boundaries:

- generic partial-data noise stays hidden, but recognized sealed hard-evidence gaps such as term-matched RV remain
  visible and specific;
- the single global AI unavailable notice does not replace each strategy's independent raw-candidate presence fact.

No public schema, CLI, config key, strategy threshold, ranking rule, or persistence contract changed.

## Validation before re-review

- Core candidate/Brief/renderer suite after all fixes: `160 passed`.
- Earlier related AI Advice, Daily Brief, CC+LP, and Combo Yield suite on the merged tree: `400 passed`.
- Ruff, compileall, and `git diff --check` will be rerun on the final fix tree during re-review.

## Residual risks

- Production/runtime replay: assigned to separately authorized release/upgrade verification.
- Manual symbol-subset CC+LP config propagation: assigned to later work unit.
- Future definitive calculation reason taxonomy: assigned to later work unit.

## Completion status

All accepted PR-review findings are fixed. Current gate / next entry point: `PR re-review`.
