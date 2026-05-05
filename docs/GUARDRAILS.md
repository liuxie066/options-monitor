# Guardrails

## A) Local Commit Gate

Enable hooks once:

```bash
cd /home/node/.openclaw/workspace/options-monitor
bash scripts/setup_git_hooks.sh
```

Enabled checks:

- Reject commits if repo path/name matches `options-monitor-prod`
- Enforce the repository Lore commit protocol:
  - first line states intent / reason, not just touched files
  - trailers such as `Constraint:`, `Rejected:`, `Confidence:`, `Scope-risk:`, `Directive:`, `Tested:`, `Not-tested:`
  - `Co-authored-by: OmX <omx@oh-my-codex.dev>` trailer when commits are made through OMX/Codex automation

## B) Remote Merge Gate (CI)

Workflow: `.github/workflows/guardrails.yml`

- Docs wording check: forbid treating `config.json` / `config.scheduled` / `config.market_*` as OM runtime entry
- Deploy args check: forbid `deploy_to_prod.py --include-runtime-config` as default path
- Minimal regression: run `tests/run_smoke.py`

Trigger: `push` and `pull_request` to `main`

## C) Deploy Execution Gate

Use safe deploy path:

```bash
cd /home/node/.openclaw/workspace/options-monitor
make deploy-safe
```

`deploy-safe` behavior:

- Always runs `scripts/deploy_to_prod.py --dry-run` then `--apply`
- Never adds `--include-runtime-config`
- If `OM_CANONICAL_CONFIG_US` and `OM_CANONICAL_CONFIG_HK` are set, verifies those canonical runtime configs SHA-256 before/after; any change fails with non-zero exit
- If the two env vars are not set, code deploy still runs and runtime-config hash guard is skipped with an explicit `SKIP` log

## D) Symbol Canonicalization Rule

- Any entrypoint that accepts user-entered symbol, broker raw payload, or OpenD/Futu underlying identifier must canonicalize to the shared symbol format before business logic.
- Canonical market symbols are values like `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not be persisted as runtime/watchlist/position symbols.
- Shared alias handling lives in `scripts/opend_utils.py:resolve_underlier_alias`; new entrypoints should reuse it instead of adding ad hoc `upper()` or market-specific parsing branches.
