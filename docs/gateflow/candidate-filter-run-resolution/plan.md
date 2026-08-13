# Plan — candidate-filter-run-resolution

- Work unit: `candidate-filter-run-resolution`
- Branch: `feat/candidate-filter-run-resolution`
- Base: `main@421591dd`
- Status: `plan fixed after planreview; ready for re-review`
- Plan review: `docs/reviews/plan-review-20260813-141152.md` (fail -> F1-F5 fixed below)

## Goal / motivation / success signal

Goal: when a Copilot user asks "why was symbol X filtered" right after a monitoring notification arrived, `candidate_filter_explain` can resolve the intended scan run from notification delivery evidence instead of requiring an explicit `run_id` or silently falling back to the latest terminal run.

Motivation (direct code evidence):

- `candidate_filter_explain` today only accepts an optional `run_id`; when omitted it resolves the latest terminal manifest-bound run via `load_latest_candidate_snapshot_bundle` (`src/application/agent_tools/candidate_filter_impl.py:21-70`, `src/application/candidate_snapshot_manifest.py:605-678`).
- In the 0700.HK incident the local `output_runs` was empty and the user could not map "the notification that just arrived" to a run; the tool had no way to express that intent.
- Notification delivery evidence already records `run_id`, `accounts`, and `delivery.action` per event in `audit_events.jsonl` (`src/application/multi_tick/assistant_perception_event.py:16-110`, read path `src/application/notification_perception_read.py:17-202`).
- The Copilot tool layer already supports `copilot_input_normalizer` for natural input normalization, with precedent `_normalize_option_performance_copilot_input` (`src/application/agent_tools/positions.py:265-283`).

Success signal:

- `candidate_filter_explain` accepts a new optional `run_selector` input with values `latest_notification` / `latest`, and an optional `notification_date` (ISO date, default today in the runtime local timezone), and resolves the run fail-closed from notification delivery evidence.
- When no matching delivered notification run exists, the tool raises a structured `AgentToolError` (no silent fallback, no guessing).
- Copilot can call the tool with just `{symbol, account}` after a notification and get the filter explanation for the run that produced the notification.

## Non-goals / scope boundary

- No changes to filter/rank logic, no re-filtering, no re-ranking; tool stays `pure_read`.
- No new persistent entities, tables, indexes, or snapshot formats.
- No changes to notification delivery, scheduler, or audit event writing.
- No natural-language date engine beyond a single ISO `notification_date`; no cross-account aggregation.
- `candidate_rank_explain` is out of scope (same mechanism could be adopted later; not required by the incident).
- The three unrelated untracked docs under `docs/plans/` and `docs/reviews/` are not touched.

## Design alignment

No external design doc; alignment is with the conversation design decision ("只需要再为这个能力增加一个处理日期的能力") plus the established Copilot normalizer precedent.

## First-principles judgment

The missing capability is run resolution, not date parsing per se. The authoritative link between "the notification the user just saw" and "the candidate snapshot that explains filtering" is the notification perception event's `run_id` + `accounts` + `delivery.action` in `audit_events.jsonl`. Resolving through that evidence keeps the tool fail-closed and audit-grounded instead of guessing from timestamps. The date parameter exists only because multiple notifications can arrive in one day and the user may ask about a previous day's notification.

## Affected files/modules

| File | Change |
|---|---|
| `src/application/agent_tools/candidate_filter_impl.py` | Add `run_selector` / `notification_date` handling; resolve run via notification events before falling back to explicit `run_id` / latest; emit resolution provenance in output `source` |
| `src/application/agent_tools/candidate.py` | Extend `CANDIDATE_FILTER_EXPLAIN_TOOL` input schema, description, examples, `copilot_input_fields`; register `copilot_input_normalizer` |
| `src/application/notification_perception_read.py` | Add `iter_notification_perception_events` (shared internals, internal-use limit up to 5000); public tool behavior unchanged |
| `tests/test_candidate_filter_trace.py` (or new focused test file) | Resolution tests: latest_notification happy path, date-filtered path, no-match fail-closed, explicit run_id precedence, latest default unchanged, input validation |
| `docs/TOOL_REFERENCE.md` | Document new inputs if the file enumerates this tool's schema |

## Contract / interface changes

Tool input additions (all optional, backward compatible):

- `run_selector`: `"latest_notification" | "latest"`; default behavior unchanged (current latest terminal manifest-bound run semantics).
- `notification_date`: ISO `YYYY-MM-DD`; only meaningful with `run_selector=latest_notification`; defaults to the runtime local date of "today".

Resolution precedence (fail-closed, no silent fallback):

1. explicit `run_id` (unchanged, highest precedence);
2. `run_selector=latest_notification`: resolve from delivered notification events for the account on `notification_date`;
3. default: latest terminal manifest-bound run (unchanged).

Resolution rule for `latest_notification` (frozen by planreview F1-F5):

- Event source: add a narrow read-only helper `iter_notification_perception_events(*, repo_root, event_kind, limit)` in `src/application/notification_perception_read.py` that shares `_audit_paths`/`_read_jsonl`/`_matches_event`/`_public_event` with `read_notification_perception_events` but allows `limit` up to 5000 for internal resolution. The public tool keeps its 50-row cap. (F1)
- Sent predicate (frozen): an event is a user-visible delivered notification iff
  - `event_kind == "notification_delivery_completed"`, AND
  - `no_send` is not `True` (heartbeat/dry-run completed events are excluded), AND
  - the account is visible: if `send_summary.sent_accounts` is a non-empty list, the account must be in it; otherwise the account must be in the top-level `accounts` list (heartbeat/legacy compatibility). Events with empty `sent_accounts` and `failure_count > 0` are not evidence for a failed account. (F2, F3)
  - `delivery.action` is NOT used as sent evidence (it records the decision action such as `fixed_report`, not transport outcome).
- Date window: convert each candidate event's `created_at_utc` (UTC ISO) to the system local timezone of the runtime host and compare against `notification_date`. The timezone used is recorded in output; no market-timezone inference. (F4)
- Pick the most recent matching event; its `run_id` must load a valid terminal manifest-bound snapshot via `load_candidate_snapshot_bundle`; if the snapshot is unavailable, raise `DEPENDENCY_MISSING` with the resolved `run_id` in details.
- Zero matches -> `AgentToolError(code="DEPENDENCY_MISSING", message="no delivered notification run found for account on notification_date", details={"reason": "no_notification_run", "account": account, "notification_date": ...})`. Rationale: `COPILOT_SAFE_ERROR_CODES` does not include a custom `RUN_NOT_FOUND` and `safe_error_code` would fold it to `TOOL_ERROR`, hiding the reason from Copilot users; `DEPENDENCY_MISSING` is the established fail-closed code for this tool. (F5)
- Runtime root: the audit lookup and the snapshot lookup share the same resolved base (`payload.runtime_root or repo_base()`), so a notification event's `run_id` is always resolved against the same runtime that wrote it.

Output additions: `source.run_resolution` = `{selector, notification_date, timezone, resolved_run_id, matched_event_created_at_utc}` so the answer is auditable. For `selector=latest`/explicit `run_id`, `run_resolution` records `{selector, resolved_run_id}` only.

## Implementation decisions

- Keep the delivered-run resolver as a small pure function in `candidate_filter_impl.py`; event scanning lives in the new `iter_notification_perception_events` helper.
- Reuse `_read_jsonl`/`_matches_event`/`_public_event` via the new helper; do not weaken sensitive-field stripping.
- Timezone: system local timezone of the runtime host, applied to UTC event timestamps; recorded in `run_resolution.timezone`. No market-timezone inference.
- `copilot_input_normalizer`: validate ISO date format early and reject `notification_date` without `run_selector=latest_notification` to avoid silent no-ops.

## Slices

Single slice (one cohesive input-resolution feature):

- S1: schema + resolver + normalizer + tests + docs touch-up.

## Tests / validation

New focused tests (pytest, no live OpenD/Feishu):

- delivered event exists for account+date -> resolves that run_id and explanation matches that run;
- multiple events same day -> most recent delivered event wins;
- target event sits beyond the first 50 perception rows in the shared audit file -> still resolved (F1 regression);
- `no_send=true` completed event -> not selected (F2);
- event with `sent_accounts=[sy]`, failure for lx -> lx query does not resolve it; lx falls through to RUN-not-found semantics (F3);
- event exists but accounts do not include requested account -> DEPENDENCY_MISSING with `reason=no_notification_run`;
- no events for date -> DEPENDENCY_MISSING with account/date in details;
- explicit run_id still wins over run_selector;
- omitted run_selector keeps latest-run behavior (regression);
- invalid notification_date format -> INPUT_ERROR;
- notification_date without latest_notification -> INPUT_ERROR;
- cross-UTC-midnight event maps to the correct local date (F4);
- Copilot-facing error stays in `COPILOT_SAFE_ERROR_CODES` vocabulary (F5).

Validation commands:

- `./.venv/bin/python -m pytest tests/test_candidate_filter_trace.py tests/test_candidate_snapshot_manifest.py -q`
- `./.venv/bin/python -m pytest tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py -q`
- `./om-agent run --tool candidate_filter_explain --input-json '{"account":"lx","symbol":"NVDA"}'` (regression: latest behavior)

## Docs decision

Update `docs/TOOL_REFERENCE.md` entry for `candidate_filter_explain` if it enumerates inputs; otherwise docstrings/schema serve as the contract. No AGENT_WIKI change needed (no new module ownership).

## Risks / open questions

- Shared audit file scans are O(file size) per resolution; acceptable at current event volume; if it becomes hot, a later work unit can add an indexed read (tracked, not blocking).
- Historical audit rotation/cleanup makes old dates fail-closed; covered by the `no_notification_run` semantics.

## Over-design statement

No new service, index, table, or persistent mapping is introduced; resolution reuses the existing audit read path and snapshot manifest loader. The date handling is a single ISO date, not a natural-language engine. `candidate_rank_explain` is deliberately untouched.

## Completion report format

Final closeout summary with what changed, validation results, finding status, residual risks, draft PR URL, and next entry point.
