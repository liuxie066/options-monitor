# Gateflow Slice 4 Implementation — Option Notification Experience

- Gate: implementation
- Work unit: option-notification-experience
- Slice: 4 — unified notification decision and confirmation
- Status: implemented, validated, ready for review
- Artifact: `docs/gateflow/option-notification-experience-s4-orchestration-implementation-20260721.md`

## Scope

Implemented the approved single-pipeline delivery state machine:

- pure fixed/candidate/failure/retry decision matrix;
- reliable-success-only current revision advancement;
- run-scoped scan-failure artifacts without current overwrite;
- v2 exact-envelope attempt, ambiguous and confirmation transitions;
- fixed confirmation alerts all candidate identities in the full report;
- durable prepare -> exact scan-target commit -> provider send ordering;
- no-send snapshot/pending update without publishing a new retry envelope;
- quiet-hours durable envelope preservation;
- no-scan delivery-only retry through the same notification flow;
- delivery-only avoids pipeline workspace, assembler, broker and revision writes;
- fixed backlog and exact pending candidate envelope preservation.

## Changed files

- `domain/domain/daily_decision_brief.py`
- `src/application/daily_decision_brief_repository.py`
- `src/application/tick_account_execution.py`
- `src/application/tick_notification_flow.py`
- `src/application/multi_account_tick.py`
- focused tests in `tests/`

## State and ordering invariants

1. A scheduled scan target is committed only after a durable successful revision or failure artifact/envelope exists.
2. Provider send is unreachable when target commit fails.
3. Delivery-only reads one existing exact envelope and does not assemble or persist a brief.
4. Definite provider failure remains pending; ambiguous outcomes freeze the exact envelope.
5. Confirmation validates logical key, transport key, source digest and message hash.
6. `--no-send` may update successful current and pending candidates, but does not publish a new delivery envelope.
7. Fixed report wins over simultaneous new candidates; confirmation marks the full candidate identity set alerted.

## Validation

- Focused Slice 4 suite: `70 passed`
- Daily Brief + scheduler integration suite: `157 passed`
- Broad multi-tick suite: `76 passed`
- Ruff: passed
- Compileall: passed
- `git diff --check`: passed

## Docs decision

No user-facing docs changed in Slice 4. Renderer, query projection and public documentation remain assigned to approved Slice 5.

## Residual risks

- Final user-facing fixed/candidate/failure wording is covered by approved Slice 5.
- Production migration, real provider canary, service changes and remote rollout remain separately approval-gated and are covered by Slice 6/rollout gates.
- Multi-market `all` remains fail-closed for dispatch by design.
