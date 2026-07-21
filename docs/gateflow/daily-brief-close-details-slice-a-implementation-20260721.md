# Gateflow Implementation: Daily Brief Close-Position Details

## Gate

- Work unit: `daily-brief-close-details`
- Slice: `slice-a`
- Gate: implementation
- Base commit: `70fc0485` (`gateflow: accept plan for daily-brief-close-details`)
- Date: 2026-07-21

## Scope Implemented

1. `daily_decision_brief_service._position_view()` now projects an allowlisted `metrics` mapping with `close_mid`, `realized_if_close`, and `remaining_annualized_return`.
2. `daily_decision_brief_renderer` adds one nested decision-detail line for priced close actions only.
3. The detail line uses market currency, explicit `(mid)` wording, sign-sensitive P&L labels, and finite-number filtering.
4. Hold and unavailable positions suppress close metrics even if stale values are present.
5. `docs/AGENT_WIKI.md` documents the advisory close-detail contract.

## Changed Files

- `src/application/daily_decision_brief_service.py`
- `src/application/daily_decision_brief_renderer.py`
- `tests/test_daily_decision_brief_service.py`
- `tests/test_daily_decision_brief_renderer.py`
- `docs/AGENT_WIKI.md`

## Validation

### Passed

```text
PYTHONPYCACHEPREFIX=/tmp/om-close-details-pycache python3 -m compileall -q \
  src/application/daily_decision_brief_service.py \
  src/application/daily_decision_brief_renderer.py
```

Result: pass.

```text
python3 -m pytest \
  tests/test_daily_decision_brief_service.py \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_domain.py \
  tests/test_daily_decision_brief_notification_flow.py
```

Result: `68 passed`.

After adding explicit US-currency coverage:

```text
python3 -m pytest \
  tests/test_daily_decision_brief_renderer.py \
  tests/test_daily_decision_brief_service.py
```

Result: `37 passed`.

Broader daily-brief suite excluding one time-expired baseline test:

```text
python3 -m pytest tests/test_daily_decision_brief_*.py \
  -k 'not test_agent_tool_is_pure_read_and_returns_structured_contract'
```

Result: `107 passed, 1 deselected`.

`git diff --check`: pass.

### Existing baseline failure

The complete `tests/test_daily_decision_brief_*.py` run produced `107 passed, 1 failed` because `tests/test_daily_decision_brief_agent_tool.py::test_agent_tool_is_pure_read_and_returns_structured_contract` hard-codes `valid_until="2026-07-20T20:00:00+00:00"` but the current date is July 21, 2026. The tool correctly returns `planning_only` for that expired fixture while the test expects `live_actionable`.

This work unit does not modify the agent tool, freshness calculation, or that test. The failure is classified as an existing time-sensitive test defect, assigned to a separate maintenance work unit rather than widened into this notification fix.

## Docs Decision

Updated `docs/AGENT_WIKI.md` because the public human projection changed. No CLI, config, or schema documentation change was required.

## Residual Risks and Uncovered Areas

- Mid-price fillability: fixed in current slice through explicit advisory `(mid)` wording.
- Signed close P&L: fixed in current slice through sign-sensitive wording and negative-value regression coverage.
- Historic briefs without metrics: fixed in current slice through one-line compatibility fallback.
- Full daily-brief suite baseline test expiry: assigned to a separate maintenance work unit; not caused by this slice.
- Production notification canary: assigned to later operator-authorized release/deployment work.

## Completion Status

- Implementation complete.
- No config, runtime artifact, notification, broker, or remote state was mutated.
- Next gate: code review.
