# Gateflow Implementation Artifact — S2 Recorder Wiring and Operator Contract

- Gate: implementation slice
- Work unit: `close-evidence-collection`
- Slice: S2 — Existing recorder wiring and operator contract
- Plan: `docs/gateflow/close-evidence-collection-plan-20260723.md`
- Artifact path: `docs/gateflow/close-evidence-collection/s2-implementation.md`
- Completion status: implementation and code review complete; ready for accepted slice commit

## Scope implemented

- Added `--include-close-decisions` to the existing Strategy Lab build action for both systemd and launchd bundles.
- Added `strategy_lab_recorder.include_close_decisions=true` to the generated service profile as an observable deployment fact.
- Kept the existing three-service/timer lifecycle and all cadences unchanged.
- Updated operator docs for independent candidate/Close run selection, same-run idempotency, strict failure isolation, 6h sampling coverage, 2h marks and daily settlement.
- Preserved the safety boundary: local research/replay writes only; no production strategy, notification, runtime config, trade, Feishu or broker-facing mutation.

## Changed files

- `src/application/service_deploy.py`
- `tests/test_service_deploy.py`
- `docs/TOOL_REFERENCE.md`
- `docs/SHADOW_REPLAY_RUNBOOK.md`
- `docs/DEPLOY_LINUX_MAC.md`

## Contract evidence

- systemd build command contains `--build-dataset --include-close-decisions --write`.
- launchd build `ProgramArguments` contains `--include-close-decisions`.
- service profile records `include_close_decisions: true` only when the existing recorder opt-in is enabled.
- Default render without recorder remains unchanged and emits no Strategy Lab units.
- Service drift reconciliation continues to use the same profile and unit set.

## Validation

```text
PYTHONDONTWRITEBYTECODE=1 python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_service_deploy.py tests/test_strategy_lab.py tests/test_research.py \
  tests/test_close_advice_shadow_capture.py
175 passed in 2.81s

python3.12 -m ruff check src/application/service_deploy.py tests/test_service_deploy.py
All checks passed!

git diff --check
passed
```

## Docs decision

Updated only existing public/operator sections that describe Strategy Lab update and recorder lifecycle. No additional design document or duplicate runbook was added.

## Residual risks and uncovered areas

- 6h sampling is not event-complete: `assigned to later work unit` S5 readiness coverage evaluation; documented here.
- Existing candidate-only/incomplete datasets are not repaired: `assigned to later work unit` only with production evidence.
- Service installation, remote upgrade and canary: `covered by later approved rollout` after merge/release authorization.
- Production strategy promotion: `requiring explicit user decision`; not authorized by this work unit.

## Stop condition

S2 code/docs, focused validation and deepreview are complete; no accepted unresolved finding remains.
