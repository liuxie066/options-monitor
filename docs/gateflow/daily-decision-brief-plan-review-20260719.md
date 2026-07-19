# Plan Review — Daily Decision Brief

- **Gate**: plan review
- **Work unit**: `daily-decision-brief`
- **Target**: `docs/gateflow/daily-decision-brief-plan-20260719.md`
- **Date**: 2026-07-19
- **Reviewer mode**: adversarial `planreview`
- **Status**: findings accepted; plan requires fix and re-review
- **Artifact path**: `docs/gateflow/daily-decision-brief-plan-review-20260719.md`

## Findings

### PR-1 — High — Account current/delivery paths collide across US and HK, and the integration does not define multi-market delivery

- **Plan location**: Persistence paths and `TickNotificationRequest` integration.
- **Trigger**: The same account (`lx` or `sy`) runs HK and US scheduled ticks on the same host, or a manual `--market-config all` run produces both markets.
- **Expected**: Identity and state remain isolated by `market + trading_date + account`; one market cannot overwrite another market's current brief or delivery pointer.
- **Actual plan**: `daily_decision_brief.current.json` and `daily_decision_brief.delivery.json` are account-only paths. The plan passes `markets_to_run` but describes one assemble/persist lifecycle and one message per account, without defining partitioning or the `all` case.
- **Direct evidence**: Goal contract explicitly defines market-level identity; `multi_account_tick.py` passes a list of `markets_to_run`; `messages_by_account` only has one key per account.
- **Impact**: US run can overwrite HK current/delivery state, revision lookup can use the wrong market, and a successful send can advance the wrong delivery pointer.
- **Required fix**: Make all current/revision/delivery files market-qualified; key shared index by `market/account`; explicitly define single-market scheduled behavior and multi-market manual behavior. For `all`, assemble one lifecycle per market and combine due market sections into the account's one outbound message, then advance each included lifecycle only after that account delivery is confirmed.
- **Fix risk**: medium; requires delivery metadata to map one account send back to one-or-more market lifecycle items.
- **Severity**: High.
- **Decision**: accepted.

### PR-2 — High — Delta idempotency key includes current revision and cannot absorb the documented post-send/pre-pointer crash

- **Plan location**: stable provider idempotency key.
- **Trigger**: Provider accepts delta revision N, process crashes before local delivery pointer update, next run persists N+1 with the same material action state.
- **Expected**: Retry uses the same provider idempotency key so a provider that supports idempotency absorbs the duplicate.
- **Actual plan**: key contains `to:<current revision>`, so the next run necessarily produces a new key even if the material diff is unchanged.
- **Direct evidence**: Existing `build_notification_idempotency_key()` includes run ID; the plan correctly adds an override but its proposed delta key remains revision-volatile.
- **Impact**: Feishu can receive duplicate delta messages in the exact crash window the plan claims to reduce.
- **Required fix**: Define delta key from `market/date/account + last-delivered revision digest/identity + canonical material diff digest`, excluding current revision. Persist current revision in audit metadata, not the provider key.
- **Fix risk**: low; digest canonicalization must exclude non-material price/rank changes.
- **Severity**: High.
- **Decision**: accepted.

### PR-3 — Medium — Per-account locks do not protect the shared current index from lost updates

- **Plan location**: repository locking and shared current index.
- **Trigger**: HK and US market cron processes, protected by different tick locks, update `daily_decision_briefs.current.json` concurrently.
- **Expected**: shared index preserves every market/account entry.
- **Actual plan**: locks are account-local, while both writers perform read-modify-write on one shared file.
- **Direct evidence**: `tick_cron.py` uses separate market lock files; state JSON writes are atomic replacements but atomic replacement does not serialize read-modify-write.
- **Impact**: one market/account current pointer can disappear from the shared read model even though account files remain correct.
- **Required fix**: add one repository-level shared-index lock around read-modify-write, or use independent shared current files. Prefer the minimal shared lock and test interleaved updates.
- **Fix risk**: low.
- **Severity**: Medium.
- **Decision**: accepted.

### PR-4 — Medium — The plan can wrongly block an entire account for partial symbol-level data gaps

- **Plan location**: actionability policy and assembler error handling.
- **Trigger**: Required-data prefetch reports one symbol error while other symbols, positions, Close Advice and candidates are valid.
- **Expected**: Produce a partial but explicit brief with `data_gaps`; only a failure that prevents a trustworthy account-level decision model should set `blocked`.
- **Actual plan**: “明确 critical prefetch failure” and “关键 artifact 不可读” are not operationally defined. Existing prefetch summaries aggregate per-item errors, so an implementation agent could map any non-zero error count to account-wide blocked.
- **Direct evidence**: `account_run.py` records prefetch summaries and continues pipeline behavior; candidate trace explicitly represents per-symbol rejections and gaps.
- **Impact**: noisy blocked briefs suppress valid position-management and candidate actions.
- **Required fix**: Define account-wide blocked narrowly: account pipeline failed/no scan result, no structured decision sources are readable, or a canonical account-level cash/position source required for every action is unavailable. Partial symbol/strategy failures remain `data_gaps` and may suppress only affected actions.
- **Fix risk**: medium; tests must prove partial-data recovery and valid-action preservation.
- **Severity**: Medium.
- **Decision**: accepted.

### PR-5 — Medium — Candidate ordering policy is underspecified for Combo Yield and mixed schemas

- **Plan location**: Structured assembler.
- **Trigger**: Combo Yield candidate artifacts or underwriting/legacy candidate files have different ranking fields.
- **Expected**: Daily Brief never invents a parallel rank or calls a mode-incompatible ranker.
- **Actual plan**: It says all CSV candidates use `rank_candidate_rows()`, but that API is put/call oriented and Combo Yield is already emitted as paired/group evidence by its own pipeline.
- **Direct evidence**: `portfolio_capacity_shadow.py` explicitly applies canonical put ranking only to Sell Put and keeps calls in existing order; Combo Yield artifacts have pair/group semantics.
- **Impact**: paired strategy ordering can be incorrect or fail on missing fields; group/leg identity can be flattened.
- **Required fix**: Use `rank_candidate_rows(mode="put")` for Sell Put and `mode="call"` for Covered Call. Preserve canonical emitted row order for Combo Yield and deduplicate by `strategy_group_id + contract identities`; do not rerank it in this work unit.
- **Fix risk**: low.
- **Severity**: Medium.
- **Decision**: accepted.

## Open Questions

无 blocking open question；上述均可在既定 scope 内修复。

## Residual Risk

- Provider 不支持幂等时的 post-send/pre-pointer crash duplicate 仍存在；分类为后续 production observation，不要求新数据库。
- `--market-config all` 的合并消息可能较长；分类为 current work unit renderer limit + scenario test。

## Gate decision

- **Decision**: fail pending fix.
- **Accepted findings**: PR-1, PR-2, PR-3, PR-4, PR-5.
- **Current gate**: fix.
- **Next entry point**: 更新 plan，写 fix artifact，再执行 plan re-review。
