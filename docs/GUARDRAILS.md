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
- Runtime config tracking check: forbid committing root runtime configs such as `config.us.json` / `config.hk.json`; commit only templates under `configs/examples/`
- Minimal regression: run `tests/run_smoke.py`

Trigger: `push` and `pull_request` to `main`

## C) Symbol Canonicalization Rule

- Any entrypoint that accepts user-entered symbol, broker raw payload, or OpenD/Futu underlying identifier must canonicalize to the shared symbol format before business logic.
- Canonical market symbols are values like `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not be persisted as runtime/watchlist/position symbols.
- Shared alias handling lives in `src/application/opend_utils.py:resolve_underlier_alias`; new entrypoints should reuse it instead of adding ad hoc `upper()` or market-specific parsing branches.
