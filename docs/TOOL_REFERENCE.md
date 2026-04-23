# Tool Reference

## `healthcheck`

Purpose:
- validate local runtime config
- summarize account resolution
- confirm whether `portfolio.pm_config` is configured

Example:

```bash
./om-agent run --tool healthcheck --input-json '{"config_key":"us"}'
```

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
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"lx"}'
```

## `get_portfolio_context`

Purpose:
- fetch holdings / Futu-backed account context with current source resolution

Example:

```bash
./om-agent run --tool get_portfolio_context --input-json '{"config_key":"us","account":"lx"}'
```

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

## `preview_notification`

Purpose:
- build final notification text without sending it

Example:

```bash
./om-agent run --tool preview_notification --input-json '{"alerts_path":"output/reports/symbols_alerts.txt","changes_path":"output/reports/symbols_changes.txt","account_label":"lx"}'
```
