# Gateflow Closeout — Close Evidence Collection

- Work unit: `close-evidence-collection`
- Branch: `codex/close-evidence-collection`
- Draft PR: https://github.com/liuxie066/options-monitor/pull/114
- Artifact path: `docs/gateflow/close-evidence-collection/final-closeout.md`
- Status: local and PR gates complete; waiting explicit merge authorization

## Delivered outcome

- Strategy Lab update can independently select and strictly build the latest non-empty Close Advice run.
- Existing candidate collection is preserved across Close capture failures.
- Same-run idempotency prevents duplicate datasets; existing/incomplete targets are never overwritten and are explicitly classified.
- Existing systemd/launchd Strategy Lab recorder build action automatically enables Close evidence collection.
- Service profile and operator docs expose the 6h build, 2h mark and daily settle lifecycle.
- P0 remains the sole production authority; P1/P2/P3 remain shadow-only.

## Gate results

- Planreview: pass-with-risks after three accepted findings were fixed.
- S1 deepreview: pass-with-risks after two accepted findings were fixed.
- S2 deepreview: pass-with-risks, no findings.
- Aggregate deepreview: pass-with-risks, no findings.
- PR #114 deepreview: pass-with-risks, no findings.
- Local full suite: 3049 passed, 10 skipped, 6 pre-existing warnings.
- Dependency graph: 481 production modules, 0 cycles, guards pass.
- GitHub checks after PR review artifact push: Analyze (actions), Analyze (python), CodeQL, agent-plugin and guardrails all pass.

## Safety boundary verified

- No Close Advice policy threshold or production recommendation authority change.
- No notification content/routing/send change.
- No operator-authored runtime config, ledger/trade, Feishu or broker-facing write path change.
- New writes are limited to existing local research/replay artifact paths.

## Residual risks and ownership

- 6h sampling is not event-complete: `assigned to later work unit` S5 readiness coverage evaluation.
- Active-run partial-write/collision recovery: `requiring new issue or explicit user decision` only if production canary/runtime evidence proves material recurrence.
- Existing incomplete/candidate-only dataset repair: `assigned to later work unit` only if production evidence justifies migration.
- Release, service reconcile and one production research canary: `covered by later approved rollout` after merge authorization.
- Policy promotion: `requiring explicit user decision` after S5 evidence readiness.

## Stop condition

Do not mark the PR ready, merge, release, reconcile production services or run the production write canary without explicit CEO authorization. The current safe handoff is Draft PR #114 with all implementation/review gates complete.
