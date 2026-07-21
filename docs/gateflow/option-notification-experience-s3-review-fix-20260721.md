# Gateflow Slice 3 Review Fix

- Gate: fix
- Work unit: option notification experience
- Slice: 3 / Repository v2 and migration
- Review artifact: `docs/reviews/code-review-20260721-191508.md`
- Date: 2026-07-21
- Status: fixes complete, pending re-review

## Finding adjudication

| Finding | Decision | Fix status |
|---|---|---|
| CR-S3-001 historical v1 full key semantic drift | accepted | 已修复 |
| CR-S3-002 cross-day candidate baseline | accepted | 已修复 |
| CR-S3-003 stale pending candidate retry | accepted | 已修复 |
| CR-S3-004 same candidate key payload mutation | accepted | 已修复 |

## Fixes

### CR-S3-001

Legacy full/delta delivery keys are now validated by their immutable structural contract instead of recomputing a version-sensitive semantic digest:

- full: exact account/market/date prefix + one SHA-256 component;
- delta: exact account/market/date prefix + two SHA-256 components.

The original key is preserved. The v2 `brief_digest` is still recomputed only from the immutable revision. The regression test mutates a valid v1 full pointer to a historical-looking digest that differs from the current algorithm and proves migration still succeeds.

### CR-S3-002

`previous_successful_brief` remains available for audit, but `previous_candidate_identities` and `newly_detected_candidate_identities` use an empty baseline when the previous current belongs to another market trading date.

### CR-S3-003

- candidate envelope preparation now requires every identity to be in the day's pending set;
- pending candidate retry returns no envelope when a later successful snapshot removed any envelope identity from pending;
- ambiguous envelopes remain frozen and retain first priority.

### CR-S3-004

- candidate envelope replacement is allowed only when the pending identity set changes and therefore produces a different stable key;
- same-key content/revision/message mutation fails closed;
- a rotated candidate key starts with its own preparation timestamp and no inherited attempt time;
- fixed failure -> fixed report remains the explicit same-key upgrade exception and preserves scheduled-target audit timestamps.

## Validation

```text
python3.12 -m pytest -q tests/test_daily_decision_brief_repository_v2.py
12 passed

python3.12 -m pytest -q tests/test_daily_decision_brief_repository_v2.py tests/test_daily_decision_brief_repository.py tests/test_daily_decision_brief_cli.py
35 passed

python3.12 -m pytest -q tests/test_daily_decision_brief*.py tests/test_notify_symbols_markdown.py tests/test_multi_tick_notify_format.py
181 passed

ruff / compileall / git diff --check
pass
```

## Residual risks

- `covered by later approved slice`: attempt/ambiguous/confirmed transitions and scheduled-target commit/send sequencing remain Slice 4.
- `requiring explicit user decision`: production migration and real notification canary remain separately approval-gated.
