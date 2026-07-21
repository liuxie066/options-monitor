# Gateflow Slice 3 Implementation — Repository v2 与迁移

- Gate: implementation
- Work unit: option notification experience
- Slice: 3 / Repository v2 and migration
- Date: 2026-07-21
- Status: accepted after code review and re-review

## Objective

把成功扫描快照持久化与通知 delivery 状态拆开，并为后续统一通知决策提供一个可验证、可精确重试、可显式迁移的 v2 repository boundary。

## Scope

Changed files:

- `src/application/daily_decision_brief_repository.py`
- `src/interfaces/cli/daily_brief_ops.py`
- `tests/test_daily_decision_brief_repository_v2.py`
- `tests/test_daily_decision_brief_cli.py`

Unrelated untracked plan/review files were not modified or staged.

## Implemented decisions

1. Added `persist_daily_decision_brief_success()` as the success-only current/revision writer.
   - only `ready|degraded` and non-blocked briefs advance current;
   - validates current -> immutable revision before allocation;
   - returns previous/current/new candidate identity sets;
   - blocked or malformed input cannot replace the last successful current.
2. Kept the existing v1 facade temporarily for Slice 4 compatibility; v2 uses the same delivery file and account+market lock but a distinct schema.
3. Added strict `daily_decision_brief_delivery.v2` normalization.
   - account/market/date isolation;
   - pending/alerted/fixed/candidate state validation;
   - successful source revision and digest validation;
   - exact rendered-message SHA-256 validation;
   - stable fixed/candidate delivery-key validation;
   - malformed or mixed v1/v2 state fails closed.
4. Added candidate pending-state replacement from each successful current snapshot:
   - `pending = current identities - alerted identities`;
   - first-seen metadata is retained while an identity remains pending.
5. Added durable exact-envelope preparation and run-scoped audit plan.
   - durable state is written before the run-scoped plan;
   - ambiguous envelopes are frozen;
   - pending fixed failure may upgrade to fixed report;
   - pending candidate envelope may rotate only before ambiguity/confirmation;
   - successful candidate identities must be present in the referenced immutable revision;
   - failure artifacts are hash-verified at prepare time, while retry reads only the durable envelope.
6. Added read-only retry selection:
   - ambiguous first;
   - earliest fixed backlog second;
   - candidate envelope third;
   - no writes, no revision allocation, no run plan creation.
7. Added explicit v1 inspect/migrate APIs and CLI:
   - `./om daily-brief delivery-inspect --account <account> --market <market>`;
   - `./om daily-brief delivery-migrate ...` is dry-run by default;
   - `--confirm` acquires the account+market lock, writes an exact v1 backup, recomputes digest only from the immutable revision, writes v2, rereads/validates, and restores v1 on post-write validation failure;
   - v1 full confirmations migrate provable current candidate identities;
   - v1 delta confirmations migrate only `candidate_added` identities proven by the persisted diff; missing/malformed diff migrates no alerted identities;
   - no current/revision/tick/send mutation.

## State and API invariants

- v1 is never implicitly migrated by normal runtime.
- `delivery.json` contains exactly one supported schema at a time.
- delivery envelope message/key/source/identity set cannot change after ambiguous outcome.
- fixed key is independent of revision/content and keyed by scheduled market target.
- candidate key is the SHA-256 of canonical sorted identities.
- a candidate cannot be both pending and alerted on the same market date.
- historical confirmation digest is recomputed from immutable revision; legacy pointer digest is audit-only and never copied as authority.

## Validation

Executed:

```text
python3.12 -m pytest -q tests/test_daily_decision_brief_repository_v2.py
10 passed

python3.12 -m pytest -q tests/test_daily_decision_brief_repository.py tests/test_daily_decision_brief_cli.py
23 passed

python3.12 -m pytest -q tests/test_daily_decision_brief*.py
151 passed

python3.12 -m ruff check src/application/daily_decision_brief_repository.py src/interfaces/cli/daily_brief_ops.py tests/test_daily_decision_brief_repository_v2.py tests/test_daily_decision_brief_cli.py
pass

python3.12 -m compileall -q src/application/daily_decision_brief_repository.py src/interfaces/cli/daily_brief_ops.py
pass

git diff --check
pass
```

Covered behaviors include success-only current, current/revision digest validation, fixed/candidate stable keys, exact hash, durable retry after failure-artifact cleanup, ambiguous freeze, account/market isolation, dry-run no-write, exact backup, recomputed digest, conservative delta migration, missing revision fail-closed, and CLI error rendering.

## Docs decision

No user-facing runtime behavior is switched in this slice. CLI migration commands are introduced here and will be included in the Slice 5 user/operator documentation update together with the final notification/query behavior.

## Residual risks / uncovered areas

- `covered by later approved slice`: v2 attempt/ambiguous/confirmed transitions and candidate/fixed confirmation effects are owned by Slice 4 notification orchestration.
- `covered by later approved slice`: old v1 prepare/confirm facade remains temporarily callable until Slice 4 switches the normal runtime to v2.
- `covered by later approved slice`: prior-day expiration/retention policy is applied by Slice 4 scheduling/orchestration; this slice only validates persisted states.
- `covered by later approved slice`: end-user Markdown projections and query aggregation are Slice 5.
- `requiring explicit user decision`: production pointer migration and any real notification canary remain separately approval-gated.

## Completion signal

Slice 3 implementation and review/fix/re-review loop are complete; review conclusion: pass.
