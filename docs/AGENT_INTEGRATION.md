# Agent Integration

The public launcher is `./om-agent`.

It exposes a stable JSON contract intended for local agent usage:

- `./om-agent init --market us|hk --futu-acc-id <digits>`
- `./om-agent add-account --market us|hk --account-label <label> --account-type futu|external_holdings`
- `./om-agent spec`
- `./om-agent run --tool <name> --input-json '<json>'`

## Contract

All tool responses return:

```json
{
  "schema_version": "1.0",
  "tool_name": "healthcheck",
  "ok": true,
  "data": {},
  "warnings": [],
  "error": null,
  "meta": {}
}
```

Errors are normalized to stable codes such as:

- `CONFIG_ERROR`
- `INPUT_ERROR`
- `DEPENDENCY_MISSING`
- `PERMISSION_DENIED`
- `CONFIRMATION_REQUIRED`
- `INTERNAL_ERROR`

## Claude Code

Use the launcher as a local command tool. Typical pattern:

```bash
./om-agent init --market us --futu-acc-id <REAL_ACC_ID> --symbol NVDA
./om-agent spec
./om-agent run --tool query_cash_headroom --input-json '{"config_key":"us","account":"user1"}'
./om-agent run --tool get_close_advice --input-json '{"config_key":"us"}'
./om-agent run --tool prepare_close_advice_inputs --input-json '{"config_key":"us"}'
./om-agent run --tool close_advice --input-json '{"config_key":"us"}'
```

## Kimi Code

Use the same launcher contract. Kimi Code only needs a local command invocation and JSON parsing.

## OpenClaw

Treat `./om-agent` as a local tool host command.

Recommended environment:

- keep repo-local `config.us.json` / `config.hk.json` as the default runtime configs
- initialize them once with `./om-agent init`
- use explicit `config_path` input only when you intentionally want to override the default repo-local config
- set `OM_PM_ROOT` or `OM_PM_RATE_CACHE` only if you intentionally want legacy external fallback after the built-in Yahoo providers
- keep `OM_AGENT_ENABLE_WRITE_TOOLS` unset unless you explicitly want config writes

OpenClaw integration in this public repo is local-tool oriented. Private cron/deploy workflows are not part
of the public plugin contract.
