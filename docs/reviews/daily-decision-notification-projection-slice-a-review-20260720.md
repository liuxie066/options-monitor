# Deepreview — Daily Decision Notification Projection Slice A

- **Gate**: code review / fix / re-review
- **Reviewed target**: Slice A workspace changes
- **Status**: pass after fix

## Finding 1 — Candidate transitions could emit duplicate business changes

- **Severity**: P1
- **Location**: `domain/domain/daily_decision_brief.py::diff_daily_decision_briefs`
- **Trigger**: the same opening candidate changes both priority and eligibility/state in one revision, for example P1 active with capacity 2 to P0 blocked with capacity 1.
- **Original behavior**: the first implementation could emit priority-upgrade, invalidation, and potentially obscure the single user-facing transition.
- **Expected behavior**: one candidate should produce one primary lifecycle transition; eligibility/state or priority transition must suppress capacity noise for the same diff.
- **Impact**: the later renderer could report inflated change counts such as “新增/升级/失效” for one candidate.
- **Fix**: candidate transitions now use an explicit mutually exclusive branch: enter high-priority active, leave high-priority active, remain high-priority with priority change, otherwise candidate-scoped capacity change.
- **Fix status**: 已修复
- **Re-review evidence**: `test_candidate_transition_emits_one_semantic_change_before_capacity` asserts exactly one `candidate_invalidated` change for the combined transition.

## Consumer inventory

Repo-wide search found runtime consumption of diff change labels only in the Daily Brief renderer. Other references are tests and plan/review documentation. Renderer support is intentionally deferred to approved Slice B; no external closed-enum consumer was found in this repository.

## Architecture review

- Domain remains independent from `src/` and owns lifecycle classification.
- Service adds explicit structured facts rather than renderer-side parsing.
- Persisted brief schema version, action identity fields, delivery repository, and confirmation pointer remain unchanged.
- Top-level capacity remains in the audit brief for compatibility but no longer acts as a false account-wide material trigger.

## Open questions

None blocking Slice A.

## Residual risks

- Renderer currently lacks the new candidate labels. Classification: covered by Slice B.
- Old code reading a new diff artifact may display raw unknown labels, but rollback still sees stable opening action IDs and does not fabricate action add/remove transitions. Classification: accepted mixed-version presentation risk covered by same-release Slice B and rollout verification.

## Re-review conclusion

**pass** — accepted finding is fixed, focused tests and Ruff pass, and no unclassified Slice A risk remains.
