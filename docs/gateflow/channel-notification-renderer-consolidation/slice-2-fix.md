# Gateflow Slice 2 Fix

- Gate: `fix`
- Work unit: `channel-notification-renderer-consolidation`
- Slice: `2`
- Review artifact: `docs/reviews/code-review-20260721-210132.md`
- Status: `fix complete; pending re-review`
- Artifact path: `docs/gateflow/channel-notification-renderer-consolidation/slice-2-fix.md`

## Finding decisions and fixes

### DR-S2-01 — accepted — fixed

`preview_notification` now distinguishes an absent/`None` renderer from an explicitly invalid value. Only omission defaults to Compact. The public input schema now declares the exact enum `compact|legacy`, so explicit empty string and unknown renderer values fail with `INPUT_ERROR` before renderer execution.

Validation added:

- default omission returns Compact;
- Legacy remains available with deprecation warning;
- unknown and empty-string values fail closed;
- manifest exposes the renderer enum.

### DR-S2-02 — accepted — fixed

Updated the owning runtime-status public output contract in `src/application/agent_tools/diagnostics.py` to advertise:

- `notification_authority.ordinary_scheduled_renderer`;
- compatibility artifact authority/delivery-evidence facts;
- shared canonical compatibility artifact metadata;
- canonical account-summary compatibility count.

`notification_authority` and `shared.compatibility_notification` are now explicit model-preview priorities. Contract tests assert these canonical facts.

## Additional changed files approved by accepted finding

- `src/application/agent_tools/diagnostics.py`

This file owns the `runtime_status` public output contract and is the minimum root-cause boundary for DR-S2-02.

## Validation

```text
ruff check <fix files>  # All checks passed
PYTHONPYCACHEPREFIX=/tmp/om-pycache python3.12 -m pytest -q -p no:cacheprovider \
  tests/test_agent_plugin_contract.py tests/test_agent_plugin_smoke.py

102 passed
```

## Residual risks

- No finding remains deferred.
- Phase C removal remains hard-paused.
- The baseline stale close-advice bridge assertion remains outside Slice 2 ownership.
