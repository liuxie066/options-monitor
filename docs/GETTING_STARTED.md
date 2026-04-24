# Getting Started

This page is the shortest path to running the public local agent tools.

## 1. Install

```bash
git clone <repo-url> options-monitor
cd options-monitor
bash scripts/install_agent_plugin.sh
./run_webui.sh
```

After OpenD is running and logged in, complete first-time initialization in the local WebUI.
The WebUI writes the repo-local files needed by the minimal public setup:

- `config.us.json` or `config.hk.json`
- `secrets/portfolio.sqlite.json`

To append another account later:

```bash
./om-agent add-account --market us --account-label user2 --account-type futu --futu-acc-id <REAL_ACC_ID>
./om-agent add-account --market us --account-label lx --account-type futu --futu-acc-id <REAL_ACC_ID> --holdings-account "lx"
./om-agent add-account --market us --account-label ext1 --account-type external_holdings --holdings-account "Feishu EXT"
./om-agent edit-account --market us --account-label lx --futu-acc-id <NEW_REAL_ACC_ID> --holdings-account "lx"
./om-agent remove-account --market us --account-label ext1
```

If you add an `external_holdings` account, copy `configs/examples/portfolio.external_holdings.example.json`
to a local `secrets/` path and fill `feishu.app_id` / `feishu.app_secret` / `feishu.tables.holdings`.
If you add `--holdings-account` to a `futu` account, it stays on Futu as the primary source and only uses Feishu holdings as a fallback.

To install a tagged pre-release instead of `main`:

```bash
git clone --branch v0.1.0-beta.1 --depth 1 <repo-url> options-monitor
cd options-monitor
bash scripts/install_agent_plugin.sh
./run_webui.sh
```

## 2. Prepare runtime config

By default, the public install flow uses repo-local runtime configs:

- `config.us.json`
- `config.hk.json`

Use explicit overrides only when you intentionally want a different path.

`portfolio.data_config` is used for the SQLite `trade_events + position_lots` storage config.
If it is a relative path, it will be resolved relative to the runtime config file.

For the runtime config `portfolio` block, use `broker` as the public field name.
`market` is still accepted as a legacy compatibility alias.

## 3. Inspect public tool manifest

```bash
./om-agent spec
```

## 4. Run a basic healthcheck

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

Public `healthcheck` now treats the following as blocking errors:

- `secrets/portfolio.sqlite.json` missing
- placeholder `trade_intake.account_mapping.futu.REAL_12345678`
- OpenD not reachable from the configured `symbols[].fetch.host/port`

## 5. Run one read-only tool

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
```

## 6. Start the local WebUI

```bash
./run_webui.sh
```

Default URL: `http://127.0.0.1:8000`

Default runtime config paths:

- `config.us.json`
- `config.hk.json`

Use `OM_WEBUI_CONFIG_DIR`, `OM_WEBUI_CONFIG_US`, or `OM_WEBUI_CONFIG_HK` only when you intentionally want to override the repo-local defaults.

## 7. Optional environment variables

- `OM_OUTPUT_DIR`: override plugin output/cache directory
- `OM_AGENT_ENABLE_WRITE_TOOLS=true`: allow non-dry-run `manage_symbols` writes
