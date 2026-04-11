# Guardrails

## A) Local Commit Gate

Enable hooks once:

```bash
cd /home/node/.openclaw/workspace/options-monitor
bash scripts/setup_git_hooks.sh
```

Enabled checks:

- Reject commits if repo path/name matches `options-monitor-prod`
- Enforce commit message format: `<type>(<scope>): <subject>`

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
- Verifies `options-monitor-prod/config.us.json` and `options-monitor-prod/config.hk.json` SHA-256 before/after; any change fails with non-zero exit
