# Gateflow Slice 2 Implementation — Compact compatibility, public read authority, and Legacy deprecation

- Gate: `implementation`
- Work unit: `channel-notification-renderer-consolidation`
- Slice: `2`
- Baseline: `6cf03add gateflow: accept channel-notification-renderer-consolidation slice-1`
- Worktree: `/private/tmp/options-monitor-channel-notification-renderer-consolidation`
- Status: `implementation complete; pending code review`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/slice-2-implementation.md`

## Scope and outcome

Implemented the approved Phase A/B compatibility boundary without changing scheduled delivery, provider transport, production config, or runtime services:

1. `build_notification()` now defaults to Compact and strictly accepts only `compact|legacy`.
2. Legacy body/full-message entry points emit `DeprecationWarning`; `build_account_message_compact()` is explicitly compatibility-only.
3. `preview_notification` remains read-only, declares `render_style`, and returns actual renderer, compatibility authority, and `delivery_evidence=false`; Legacy preview adds an explicit returned warning.
4. `runtime_status` exposes canonical `compatibility_notification` payloads for shared/account/latest-run/latest-scanned-run reads with mixed-bundle metadata, while retaining deprecated Phase A/B `notification` aliases with `deprecated_field=true`.
5. Top-level `notification_authority` identifies Daily Brief as the sole scheduled ordinary renderer and publishes machine-readable Phase C alias migration metadata.
6. Account summaries and `analysis.runtime_tick_status` use compatibility-specific canonical names while retaining bounded aliases.
7. Compatibility artifact absence no longer contributes a canonical runtime warning or analysis delivery-failure diagnosis.
8. Operator documentation now states the compatibility artifact and preview authority boundaries.

## Changed files

- `src/application/notify_symbols.py`
- `src/application/multi_tick/notify_format.py`
- `src/application/agent_tools/notifications.py`
- `src/application/agent_tools/notifications_impl.py`
- `src/application/agent_tools/runtime_status_impl.py`
- `src/application/agent_tools/diagnostics.py`
- `src/application/agent_tools/analysis.py`
- `tests/test_notify_symbols_markdown.py`
- `tests/test_notification_compact.py`
- `tests/test_multi_tick_notify_format.py`
- `tests/test_agent_plugin_contract.py`
- `tests/test_agent_plugin_smoke.py`
- `tests/test_analysis_tools.py`
- `docs/AGENT_WIKI.md`

## Contract decisions

- No renderer registry, wrapper renderer, generic message DTO, runtime renderer flag, or second delivery path was added.
- `symbols_notification.txt` remains at its existing path and remains Compact-primary; public reads now describe it as `artifact_kind=compatibility_notification_bundle`, `primary_renderer=compact`, `may_include=[candidate_reject_summary,close_advice]`, `authority=compatibility_only`, `delivery_evidence=false`.
- Canonical compatibility payloads never contain `deprecated_field`; only the legacy `notification` aliases do.
- Internal summary/materialization reads prefer `compatibility_notification` and fallback to `notification` only for pre-P1/mixed-version payloads.
- Actual delivery health remains owned by `notification_diagnosis`, scheduler decisions, and send counters.

## Validation

Passed:

```text
PYTHONPYCACHEPREFIX=/tmp/om-pycache python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_notify_symbols_markdown.py \
  tests/test_notification_compact.py \
  tests/test_multi_tick_notify_format.py \
  tests/test_agent_plugin_contract.py \
  tests/test_agent_plugin_smoke.py \
  tests/test_analysis_tools.py

198 passed, 10 skipped
```

Additional regression matrix:

```text
PYTHONPYCACHEPREFIX=/tmp/om-pycache python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_pipeline_runtime_paths.py \
  tests/test_multi_tick_contract_batch2.py \
  tests/test_feishu_bot.py

All selected tests passed as part of the 93-test command below.
```

The broader command also included `tests/test_close_advice_runner.py` and produced `92 passed, 1 failed`. The single failure is a pre-existing baseline inconsistency: at accepted baseline `6cf03add`, `build_account_message()` already renders `# OM · 决策简报 · lx`, while `test_close_advice_text_can_drive_account_message_without_opening_candidates` still expects the removed `账户提醒（lx）` / `Put 0 / Covered Call 0` copy. Slice 2 does not modify that Phase C bridge test.

Static checks:

```text
ruff check <changed Python files>  # All checks passed
python3.12 -m py_compile <changed source files>  # pass
git diff --check  # pass
```

## Docs decision

Updated `docs/AGENT_WIKI.md` because public read fields, preview semantics, and operator interpretation of `symbols_notification.txt` changed.

## Residual risks

- Legacy physical deletion and removal of all `notification*` aliases remain assigned to hard-paused Slice 6 after compatibility-release evidence and explicit CEO approval.
- The existing stale close-advice bridge assertion is a baseline test debt. It is not caused by Slice 2 and is intentionally not repaired outside its planned Phase C ownership.
- Real Feishu/WeChat visual canaries, release, deployment, and production config migration remain outside this work unit execution authorization.
- System Notice and Receipt shell consolidation are covered by later approved Slices 3 and 4.
