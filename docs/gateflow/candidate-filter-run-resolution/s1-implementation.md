# Gateflow Artifact — S1 Implementation

- Gate: `implementation`
- Work unit: `candidate-filter-run-resolution`
- Slice: `S1` (schema + resolver + normalizer + tests + docs)
- Status: `implementation complete; pending code review`

## What changed

| File | Change |
|---|---|
| `src/application/notification_perception_read.py` | Added `iter_notification_perception_events` (internal, bounded at 5000 rows) sharing `_audit_paths`/`_read_jsonl`/`_matches_event`/`_public_event`; public tool cap unchanged |
| `src/application/agent_tools/candidate_filter_impl.py` | Added `run_selector` / `notification_date` handling; delivered-notification run resolver with frozen sent predicate (`notification_delivery_completed` + `no_send is not True` + account-visible via `send_summary.sent_accounts` preferred over `accounts`); local-tz date mapping; `run_resolution` provenance in output `source`; fail-closed `DEPENDENCY_MISSING` with `details.reason=no_notification_run` |
| `src/application/agent_tools/candidate.py` | Extended `CANDIDATE_FILTER_EXPLAIN_TOOL` schema/description/examples/`copilot_input_fields`; added `_normalize_candidate_filter_copilot_input` normalizer |
| `tests/test_candidate_filter_run_resolution.py` | 13 focused tests (new file) |
| `docs/TOOL_REFERENCE.md` | Documented `run_selector=latest_notification` semantics and fail-closed behavior |

## Validation

- `./.venv/bin/python -m pytest tests/test_candidate_filter_run_resolution.py -q` -> 13 passed
- `./.venv/bin/python -m pytest tests/test_candidate_filter_trace.py tests/test_candidate_snapshot_manifest.py tests/test_agent_plugin_contract.py -q` -> 32 passed
- `./.venv/bin/python -m pytest tests/test_agent_plugin_smoke.py -q` -> 101 passed (1 pre-existing deprecation warning)
- `./om-agent run --tool candidate_filter_explain --input-json '{"account":"lx","symbol":"NVDA"}'` -> unchanged fail-closed `DEPENDENCY_MISSING` on this machine (no local runs), confirming default latest path is untouched

## Residual risks

- O(file) scan of the shared audit JSONL per resolution: accepted for current volume; later indexed read if hot (tracked in plan).
- Old dates fail closed when audit history is rotated: covered by `no_notification_run` semantics (in-scope design).

## Completion signal

All approved plan test cases implemented and green; regression suites green; docs updated.
