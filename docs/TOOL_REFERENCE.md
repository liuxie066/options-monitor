# Tool Reference

## Support Matrix

| Tool | Default Availability | Needs `portfolio.data_config` | Needs OpenD | Writes |
| --- | --- | --- | --- | --- |
| `healthcheck` | yes | yes | yes | no |
| `scan_opportunities` | yes | optional but recommended for SQLite position-lot context | only for Futu-backed symbols | no |
| `query_cash_headroom` | yes | yes for SQLite position-lot context | yes | no |
| `get_portfolio_context` | yes | only for holdings-backed fallback | yes | no |
| `prepare_close_advice_inputs` | yes | yes for SQLite position-lot context | yes in minimal public setup | no |
| `close_advice` | yes | no | only when quote_source falls back to OpenD | no |
| `get_close_advice` | yes | yes for SQLite position-lot context | yes in minimal public setup | no |
| `manage_symbols` | yes | no | no | dry-run by default |
| `preview_notification` | yes | no | no | no |

Notes:
- Public minimal setup is: OpenD for行情/持仓/现金 + SQLite for `trade_events + position_lots`.
- `portfolio.data_config` in the minimal setup only needs to point at a small JSON file with `option_positions.sqlite_path`.
- Feishu is optional and only needed for holdings fallback or first-run bootstrap of legacy option records.
- If you add an `external_holdings` account, `portfolio.data_config` also needs `feishu.app_id` / `feishu.app_secret` / `feishu.tables.holdings`.
- Use `configs/examples/portfolio.external_holdings.example.json` as the public starting point for that case.

## `healthcheck`

Purpose:
- validate local runtime config
- summarize account resolution
- require a real `trade_intake.account_mapping.futu`
- require OpenD and the SQLite `portfolio.data_config`
- show `account_primary_paths` and `account_fallback_paths` separately so Futu primary vs Feishu fallback is visible

Example:

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

Notes:
- `data.account_paths` is the stable machine-readable summary for UI / scripts.
- `checks[]` remains the detailed diagnostic stream for humans.

## `scan_opportunities`

Purpose:
- run the symbols scan pipeline
- return normalized summary rows

Example:

```bash
./om-agent run --tool scan_opportunities --input-json '{"config_key":"us","symbols":["NVDA"],"top_n":3}'
```

## `query_cash_headroom`

Purpose:
- calculate sell-put cash usage and free cash

Example:

```bash
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"user1"}'
```

Notes:
- public input should use `broker` when you need to override the default broker filter
- `market` is still accepted as a legacy alias for compatibility with older callers

## `get_portfolio_context`

Purpose:
- fetch holdings / Futu-backed account context with current source resolution

Example:

```bash
./om-agent run --tool get_portfolio_context --input-json '{"config_key":"us","account":"user1"}'
```

Notes:
- public input should use `broker` when you need to override the default broker filter
- `market` is still accepted as a legacy alias for compatibility with older callers
- when `portfolio.source=futu`, this tool can work without Feishu config

## `manage_symbols`

Purpose:
- list or mutate `symbols[]`

Notes:
- `list` is always allowed
- non-dry-run writes require `OM_AGENT_ENABLE_WRITE_TOOLS=true` and `confirm=true`

Examples:

```bash
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"list"}'
./om-agent run --tool manage_symbols --input-json '{"config_key":"us","action":"add","symbol":"TSLA","sell_put_enabled":true,"dry_run":true}'
```

## `close_advice`

Purpose:
- build close-position suggestions from cached `option_positions_context.json`
- reuse local `required_data` quotes and only best-effort fetch missing quotes from OpenD

Example:

```bash
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

Notes:
- default install path expects:
  - `output/agent_plugin/state/option_positions_context.json`
  - `output/agent_plugin/required_data/`
- you can override them with `context_path` and `required_data_root`
- output files are written to `output/agent_plugin/reports/close_advice.csv` and `close_advice.txt`

## `get_close_advice`

Purpose:
- one-shot public entrypoint for close advice
- prepare local cached inputs first, then build close-advice output

Example:

```bash
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
```

Notes:
- this is the recommended public tool for Agent callers
- it internally does:
  - `prepare_close_advice_inputs`
  - `close_advice`
- keep the two lower-level tools for debugging and staged workflows

## `prepare_close_advice_inputs`

Purpose:
- refresh `option_positions_context.json`
- fetch local `required_data` for symbols that currently exist in open option positions

Example:

```bash
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

Notes:
- default output paths are:
  - `output/agent_plugin/state/option_positions_context.json`
  - `output/agent_plugin/required_data/`
- public install flow should call this before `close_advice`
- symbol fetch source follows `symbols[].fetch.source`; if a held symbol is not explicitly configured, the minimal product assumption is OpenD

## `preview_notification`

Purpose:
- build final notification text without sending it

Example:

```bash
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output/reports/symbols_alerts.txt","changes_path":"output/reports/symbols_changes.txt","account_label":"user1"}'
```
