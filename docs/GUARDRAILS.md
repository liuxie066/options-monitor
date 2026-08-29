# Guardrails

## A) Local Commit Gate

Enable hooks once:

```bash
cd <repo-root>
bash scripts/setup_git_hooks.sh
```

Enabled checks:

- Reject commits if repo path/name matches `options-monitor-prod`
- Scan staged index content for high-confidence credentials, private data fingerprints, known personal email addresses, personal paths, and tracked runtime configs; findings are redacted
- Reject a repository-effective Git author email already classified as private; use a GitHub `noreply` identity for public commits
- Reject a missing living-doc authority/indexed target and deterministic repository paths whose owner is absent from the staged Git index; explicit historical, removed, retired, proposed, and example paths are exempt
- Require the first commit-message line to match `<type>(<scope>): <subject>`
- No trailer or co-author line is required by the hooks

## B) Remote Merge Gate (CI)

Workflow: `.github/workflows/guardrails.yml`

- Docs check: forbid treating `config.json` / `config.scheduled` / `config.market_*` as OM runtime entry, require the living-doc authority graph to remain present, and reject deterministic repository paths in indexed living docs when their owner is missing
- Runtime config tracking check: forbid committing root runtime configs such as `config.us.json` / `config.hk.json`; commit only templates under `configs/examples/`
- Sensitive artifact check: reject high-confidence provider credentials, private keys, credentialed URLs, known private runtime/financial/email fingerprints, and literal personal home or mounted-volume paths without printing the blocked value
- Lint: run `python -m ruff check .`
- Standalone smoke: run `tests/run_smoke.py`
- Launcher spec smoke: render `./om-agent spec` through the public wrapper
- Full regression: run automatic pytest discovery for pull requests and VERSION-changing pushes; ordinary pushes to `main` reuse the required pull-request result

Trigger: `push` and `pull_request` to `main`. The active `main` ruleset requires pull requests, an up-to-date `guardrails` status, and blocks deletion and force pushes.

## C) Symbol Canonicalization Rule

- Any entrypoint that accepts user-entered symbol, broker raw payload, or OpenD/Futu underlying identifier must canonicalize to the shared symbol format before business logic.
- Canonical market symbols are values like `NVDA`, `0700.HK`, `9992.HK`; aliases such as `POP` must not be persisted as runtime symbol config or position symbols.
- Shared alias handling lives in `src/application/opend_utils.py::resolve_underlier_alias`; new entrypoints should reuse it instead of adding ad hoc `upper()` or market-specific parsing branches.
