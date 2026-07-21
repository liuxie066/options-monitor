# Gateflow Slice 5 Implementation — 用户投影与查询

- Gate: implementation
- Work unit: option-notification-experience
- Slice: 5 — user projection and query
- Date: 2026-07-21
- Status: accepted after code review

## Scope

Implemented the approved user-facing projection and read-only query slice without adding another renderer, scanner, sender, database, or broker fetch:

- explicit fixed-report, candidate-alert, fixed-failure, and query render entry points in the existing renderer;
- fixed reports always include current candidates, positions, funds, candidate capacities, and the shared-cash reminder;
- candidate alerts render only the newly pending candidate identities, expand at most three, include account funds, and omit ordinary material-delta banners;
- fixed scan failures render a short explicit failure message and never show the last successful current as this round's candidates;
- funds show cash total, option-opening available funds, and per-candidate capacity; unknown values remain explicit and total assets/NAV/securities market value are not projected;
- latest query supports optional account/market filters and aggregates canonical enabled scopes from runtime configs;
- day/revision queries remain explicit operator reads requiring account and market;
- query reads successful current state, reports current/stale status, hides revision in Markdown, and does not mutate delivery state;
- CLI and Agent Tool accept empty latest-query scope;
- Copilot binding exposes the agreed natural-language query examples;
- README and Agent Wiki now describe the fixed/half-hour/query behavior and production approval boundary.

## Changed files

- `src/application/daily_decision_brief_renderer.py`
- `src/application/tick_notification_flow.py`
- `src/application/agent_tools/daily_brief.py`
- `src/interfaces/cli/daily_brief_ops.py`
- `src/application/assistant/tool_bindings.py`
- focused tests in `tests/`
- `README.md`
- `docs/AGENT_WIKI.md`

## User-visible contracts

1. Fixed report title is `<account> · <market>期权监控`; the batch target and actual data time remain distinct.
2. Candidate alert title is `新增候选`, uses the exact half-hour scan target, and does not show unrelated positions or ordinary change summaries.
3. Fixed failure says the scan did not form a reliable result and explicitly avoids the no-candidate interpretation.
4. Query defaults to all enabled account/market scopes, renders each scope independently, and clearly marks partial/unavailable sections.
5. Markdown never exposes revision or internal delivery/source identity fields.
6. Existing pending/ambiguous exact envelopes remain unchanged because the repository persists rendered Markdown and delivery-only replays it byte-for-byte.

## Validation

Executed:

```text
python3.12 -m pytest \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_agent_tool.py \
  tests/test_daily_decision_brief_cli.py \
  tests/test_daily_decision_brief_notification_flow.py \
  tests/test_copilot_phase1.py -q
109 passed

python3.12 -m ruff check <Slice 5 Python files and focused tests>
pass

git diff --check
pass
```

Covered behaviors include fixed/candidate/failure/query wording, funds and unknown handling, candidate-alert top-three projection, half-hour target display, aggregate scope isolation, partial unavailable sections, revision hiding, pure-read delivery-state preservation, CLI optional latest scope, and Copilot query examples. Agent plugin contract and smoke validation also passed: `103 passed`.

## Code review

- Artifact: `docs/reviews/code-review-20260721-201523.md`
- Conclusion: `pass`
- Material findings: none

## Residual risks

- Production config enablement, v1 pointer migration, real provider canary, release, and remote upgrade remain separately approval-gated.
- Aggregate latest depends on valid canonical `config.us.json` / `config.hk.json`; invalid runtime config fails closed instead of guessing scope.
- Existing persisted pending/ambiguous envelopes intentionally keep their old rendered text until exact delivery is confirmed or operationally resolved.
